"""Tests for tools/backfill_logs.py (stdlib unittest; the sqlite3 CLI test is skipped if it's missing).

Run from the repo root:  python3 -m unittest tests.test_backfill -v
"""
import io
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import app  # noqa: E402
import backfill_logs as bf  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def ts(text):
    """'2025-03-29 01:16:03.639' in IST -> unix seconds."""
    whole, _, frac = text.partition(".")
    t = datetime.strptime(whole, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST).timestamp()
    return round(t + (int(frac) / 10 ** len(frac) if frac else 0), 6)


def lines(start, states, step=0.6):
    """Log lines from `start` (IST text) every `step` seconds; states is a string like '++--+'."""
    t0 = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    out = []
    for i, s in enumerate(states):
        t = t0 + timedelta(seconds=i * step)
        out.append("%s.%03d,Mains %s" % (t.strftime("%Y-%m-%d %H:%M:%S"), t.microsecond // 1000, "On" if s == "+" else "Off"))
    return out


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def log(self, name, rows, tail=b""):
        p = self.dir / name
        p.write_bytes(("\n".join(rows) + "\n").encode() + tail)
        return str(p)

    def run_scan(self, paths):
        report = bf.Report()
        flips = bf.scan(paths, IST, report)
        return flips, report

    def make_db(self, app_start, name="power.db"):
        path = str(self.dir / name)
        store = app.Store(path)
        store.set_meta("tracking_since", int(app_start))
        store.add_reading(int(app_start), True)                 # the app's own data, after it started
        store.start_outage(int(app_start) + 100)
        store.end_outage(int(app_start) + 200)
        store.db.close()
        return path

    def rows(self, path):
        db = sqlite3.connect(path)
        try:
            return (db.execute("SELECT start, end, inferred FROM outages ORDER BY start").fetchall(),
                    db.execute("SELECT ts, ok FROM readings ORDER BY ts").fetchall(),
                    dict(db.execute("SELECT k, v FROM meta").fetchall()))
        finally:
            db.close()

    # ---- parsing ----

    def test_parse_keeps_milliseconds_and_microseconds(self):
        self.assertEqual(bf.parse_ts(b"2025-03-29 01:16:03.639,Mains On", IST), ts("2025-03-29 01:16:03.639"))
        self.assertEqual(bf.parse_ts(b"2025-08-08 14:16:15.941110,Mains On", IST), ts("2025-08-08 14:16:15.941110"))
        self.assertEqual(bf.parse_ts(b"2025-08-08 14:16:15,Mains On", IST), ts("2025-08-08 14:16:15"))

    def test_utc_offset_option(self):
        self.assertEqual(bf.parse_offset("+05:30"), IST)
        self.assertEqual(bf.parse_offset("-04:00"), timezone(timedelta(hours=-4)))

    def test_nul_padding_blank_and_garbage_lines(self):
        p = self.log("power-status-a.log", lines("2025-03-29 01:00:00", "++--") + ["", "garbage", "2025-13-99 xx,Mains On"],
                     tail=b"\0" * 300)
        flips, report = self.run_scan([p])
        self.assertEqual(report.lines, 5)          # 4 good + the malformed-date line that ends with "Mains On"
        self.assertEqual(report.bad, 2)            # "garbage" and the unparseable date (it would be a flip)
        self.assertEqual([ok for _, ok in flips], [True, False])

    # ---- merging ----

    def test_flips_only_with_exact_times(self):
        p = self.log("power-status-a.log", lines("2025-03-29 01:00:00", "+++--+"))
        flips, _ = self.run_scan([p])
        self.assertEqual(flips, [(ts("2025-03-29 01:00:00.000"), True), (ts("2025-03-29 01:00:01.800"), False),
                                 (ts("2025-03-29 01:00:03.000"), True)])

    def test_files_taken_in_time_order_and_duplicates_skipped(self):
        a = self.log("power-status-2.log", lines("2025-03-29 02:00:00", "++--++"))
        b = self.log("power-status-1.log", lines("2025-03-29 01:00:00", "+-+"))
        dup = self.log("power-status.log", lines("2025-03-29 02:00:00", "++--++"))   # an exact copy of a
        flips, report = self.run_scan([a, dup, b])
        self.assertEqual(report.dups, 6)
        self.assertEqual([round(t - flips[0][0], 1) for t, _ in flips], [0.0, 0.6, 1.2, 3601.2, 3602.4])
        self.assertEqual([ok for _, ok in flips], [True, False, True, False, True])

    def test_overlapping_loggers_merge_without_repeats(self):
        a = self.log("power-status-a.log", lines("2025-03-29 01:00:00", "+" * 10 + "-" * 10))
        b = self.log("power-status-b.log", lines("2025-03-29 01:00:06", "-" * 10 + "+" * 5))   # starts inside a
        flips, report = self.run_scan([a, b])
        self.assertEqual([ok for _, ok in flips], [True, False, True])
        self.assertEqual(flips[2][0], ts("2025-03-29 01:00:12.000"))

    # ---- outages ----

    def test_outages_threshold_and_blips(self):
        flips = [(0, True), (10, False), (14.9, True), (100, False), (115, True), (200, False)]
        self.assertEqual(bf.outages_from(flips, 300, 15), [(100, 115), (200, 300)])   # last one: logs end while off

    def test_outage_continues_across_files_and_gaps(self):
        a = self.log("power-status-a.log", lines("2025-03-29 01:00:00", "++----"))
        b = self.log("power-status-b.log", lines("2025-03-29 03:00:00", "---++"))       # 2 h with no logger
        flips, report = self.run_scan([a, b])
        self.assertEqual(bf.outages_from(flips, report.last, 15),
                         [(ts("2025-03-29 01:00:01.200"), ts("2025-03-29 03:00:01.800"))])

    # ---- applying ----

    def backfill_db(self, db, paths, *extra):
        with redirect_stdout(io.StringIO()) as out:
            bf.main(paths + ["--db", db, "--utc-offset", "+05:30", "--pause", "0", *extra])
        return out.getvalue()

    def sample_logs(self):
        return [self.log("power-status-a.log",
                         lines("2025-03-29 01:00:00", "+" * 5 + "-" * 50 + "+" * 5 + "-" + "+" * 5, step=0.6) +
                         lines("2025-03-29 05:00:00", "+" * 3 + "-" * 100, step=0.6))]

    def test_db_import_respects_app_start_and_keeps_app_rows(self):
        app_start = ts("2025-03-29 05:00:30")           # the second outage runs past the app's first start
        db = self.make_db(app_start)
        self.backfill_db(db, self.sample_logs())
        outages, readings, meta = self.rows(db)
        backfilled = [o for o in outages if o[0] < int(app_start)]
        self.assertEqual(backfilled, [(ts("2025-03-29 01:00:03.000"), ts("2025-03-29 01:00:33.000"), 0),
                                      (ts("2025-03-29 05:00:01.800"), app_start, 0)])   # clipped at app start
        self.assertIn((int(app_start) + 100, int(app_start) + 200, 0), outages)          # app's own outage untouched
        self.assertEqual([r for r in readings if r[0] < int(app_start)], [
            (ts("2025-03-29 01:00:00.000"), 1), (ts("2025-03-29 01:00:03.000"), 0), (ts("2025-03-29 01:00:33.000"), 1),
            (ts("2025-03-29 01:00:36.000"), 0), (ts("2025-03-29 01:00:36.600"), 1),     # a 0.6 s blip, kept
            (ts("2025-03-29 05:00:01.800"), 0)])
        self.assertEqual(meta["tracking_since"], str(int(ts("2025-03-29 01:00:00"))))  # history now starts at the logs
        self.assertEqual(meta["app_started"], str(int(app_start)))

    def test_db_import_is_idempotent(self):
        db = self.make_db(ts("2025-03-30 00:00:00"))
        self.backfill_db(db, self.sample_logs())
        first = self.rows(db)
        self.backfill_db(db, self.sample_logs())
        self.assertEqual(self.rows(db), first)

    def test_dry_run_writes_nothing(self):
        db = self.make_db(ts("2025-03-30 00:00:00"))
        before = self.rows(db)
        self.assertIn("dry run", self.backfill_db(db, self.sample_logs(), "--dry-run"))
        self.assertEqual(self.rows(db), before)

    def test_db_without_app_start_is_refused(self):
        path = str(self.dir / "empty.db")
        app.Store(path).db.close()
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            bf.main(self.sample_logs() + ["--db", path, "--utc-offset", "+05:30", "--pause", "0"])

    def test_write_transactions_stay_short(self):
        p = self.log("power-status-big.log", lines("2025-04-01 00:00:00", "+-" * 24000, step=3))  # 48k flips over 40 h
        db = self.make_db(ts("2025-05-01 00:00:00"))
        report = bf.Report()
        flips = bf.scan([p], IST, report)
        readings = [(t, int(ok)) for t, ok in flips]
        stats, _ = bf.apply_db(db, readings, bf.outages_from(flips, report.last, 15), int(report.first), 0.5, 0)
        self.assertGreater(len(readings), 40000)
        self.assertLess(stats["longest_lock"], 0.5)

    @unittest.skipUnless(shutil.which("sqlite3"), "sqlite3 command-line tool not installed")
    def test_sql_file_matches_direct_import(self):
        logs = self.sample_logs()
        app_start = ts("2025-03-29 05:00:30")
        direct = self.make_db(app_start, "direct.db")
        self.backfill_db(direct, logs)
        via_sql = self.make_db(app_start, "via_sql.db")         # same starting point
        sql = str(self.dir / "backfill.sql")
        with redirect_stdout(io.StringIO()):
            bf.main(logs + ["--sql", sql, "--utc-offset", "+05:30", "--pause", "0"])
        for _ in range(2):                                      # applying twice changes nothing
            with open(sql, "rb") as fh:
                subprocess.run(["sqlite3", via_sql], stdin=fh, check=True, capture_output=True)
        self.assertEqual(self.rows(via_sql)[:2], self.rows(direct)[:2])
        self.assertEqual(self.rows(via_sql)[2]["tracking_since"], self.rows(direct)[2]["tracking_since"])


if __name__ == "__main__":
    unittest.main()
