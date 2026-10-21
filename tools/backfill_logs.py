#!/usr/bin/env python3
"""Backfill power history from the old logger's power-status*.log files.

Each log line is "YYYY-MM-DD HH:MM:SS.fff,Mains On" (or "Mains Off") in the logger machine's
local time. The import keeps full detail:

  * readings: every On/Off change, with the log's millisecond timestamp (blips included)
  * outages:  every run of "Mains Off" lasting at least --min-outage seconds; it starts at the
              first Off line and ends at the next On line (the same rule the app applies live)

Only data from before the app first started is imported (the database's 'tracking_since',
saved once as 'app_started'); from then on the app's own records stay as they are. Every
insert skips rows that already exist, so running it again is harmless. Afterwards the
dashboard's "history since" moves back to the first log line.

Two ways to apply it, both safe while the app is running:

  --sql FILE   write an SQL script to run later:  sqlite3 power.db < FILE
  --db  PATH   insert directly into the database

Either way the data is first loaded into a temporary staging table (which does not lock the
app's database), then moved in small transactions with a pause between them, so the app never
waits long for the write lock. In --db mode each transaction is kept under --max-lock seconds.

Examples (on the Pi, with Python 3.10+ and no extra packages):
  python3 tools/backfill_logs.py ~/logs/power-status*.log --db power.db --dry-run
  python3 tools/backfill_logs.py ~/logs/power-status*.log --db power.db
  python3 tools/backfill_logs.py ~/logs/power-status*.log --sql backfill.sql
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

ON, OFF = b",Mains On", b",Mains Off"

# Guarded moves from the staging tables into the app's tables. They only insert rows from before
# the app first started (meta 'app_started') and skip rows that are already there.
SETUP = [
    "CREATE TEMP TABLE IF NOT EXISTS bf_readings (ts REAL NOT NULL, ok INTEGER NOT NULL)",
    "CREATE TEMP TABLE IF NOT EXISTS bf_outages (start REAL NOT NULL, end REAL NOT NULL)",
    "DELETE FROM temp.bf_readings",
    "DELETE FROM temp.bf_outages",
]
REMEMBER_APP_START = ("INSERT OR IGNORE INTO main.meta (k, v) "
                      "SELECT 'app_started', v FROM main.meta WHERE k = 'tracking_since'")
APP_START = "(SELECT CAST(v AS REAL) FROM main.meta WHERE k = 'app_started')"
MOVE_READINGS = (
    "INSERT INTO main.readings (ts, ok) SELECT b.ts, b.ok FROM temp.bf_readings b "
    "WHERE b.rowid BETWEEN {lo} AND {hi} AND b.ts < " + APP_START + " "
    "AND NOT EXISTS (SELECT 1 FROM main.readings r WHERE r.ts = b.ts)")
MOVE_OUTAGES = (
    # An outage still running when the app started is cut off there; the app has its own record.
    "INSERT INTO main.outages (start, end, inferred) SELECT b.start, MIN(b.end, " + APP_START + "), 0 "
    "FROM temp.bf_outages b WHERE b.rowid BETWEEN {lo} AND {hi} AND b.start < " + APP_START + " "
    "AND NOT EXISTS (SELECT 1 FROM main.outages o WHERE o.start = b.start)")
EXTEND_HISTORY = ("UPDATE main.meta SET v = '{start}' WHERE k = 'tracking_since' "
                  "AND CAST(v AS REAL) > {start}")


class Report:
    def __init__(self):
        self.files = self.lines = self.bad = self.dups = 0
        self.first = self.last = None


def parse_ts(line, tz):
    """b'2025-03-29 01:16:03.639,...' -> unix seconds, keeping the fraction (ms or µs)."""
    text = line[:line.index(b",")].decode("ascii")
    whole, _, frac = text.partition(".")
    dt = datetime.strptime(whole, "%Y-%m-%d %H:%M:%S")
    if tz is not None:
        dt = dt.replace(tzinfo=tz)
    return round(dt.timestamp() + (int(frac) / 10 ** len(frac) if frac else 0), 6)


def clean(line):
    if b"\0" in line:  # unclean shutdowns leave NUL padding
        line = line.replace(b"\0", b"")
    return line.rstrip()


def first_timestamp(path, tz):
    with open(path, "rb") as fh:
        for line in fh:
            line = clean(line)
            if line.endswith(ON) or line.endswith(OFF):
                try:
                    return parse_ts(line, tz)
                except ValueError:
                    continue
    return None


def scan(paths, tz, report):
    """Merge the logs into one timeline and return its On/Off changes as [(ts, ok)].

    Only lines where the state changes need their timestamp parsed, which keeps this fast on a
    Pi. Files are taken in time order; lines a later file repeats (a duplicate copy, or two
    loggers running at once) are skipped by timestamp.
    """
    starts = []
    for p in paths:
        t = first_timestamp(p, tz)
        if t is None:
            print("  skipping %s: no readable lines" % p, file=sys.stderr)
        else:
            starts.append((t, p))
    flips, state, last_t = [], None, None
    for _, path in sorted(starts):
        report.files += 1
        skipping = last_t is not None
        last_line = None
        with open(path, "rb") as fh:
            for line in fh:
                line = clean(line)
                if line.endswith(ON):
                    ok = True
                elif line.endswith(OFF):
                    ok = False
                else:
                    report.bad += bool(line)
                    continue
                report.lines += 1
                if skipping or ok != state:
                    try:
                        t = parse_ts(line, tz)
                    except ValueError:
                        report.bad += 1
                        continue
                    if skipping:
                        if t <= last_t:
                            report.dups += 1
                            continue
                        skipping = False
                    if ok != state:
                        flips.append((t, ok))
                        state = ok
                last_line = line
        if last_line is not None:
            last_t = max(last_t or 0, parse_ts(last_line, tz))
    report.first = flips[0][0] if flips else None
    report.last = last_t
    return flips


def outages_from(flips, last_t, min_outage):
    """Runs of Off from the first Off line to the next On line, at least min_outage long."""
    out, start = [], None
    for t, ok in flips:
        if not ok and start is None:
            start = t
        elif ok and start is not None:
            if t - start >= min_outage:
                out.append((start, t))
            start = None
    if start is not None and last_t - start >= min_outage:  # logs end during an outage
        out.append((start, last_t))
    return out


def batches(n, size):
    for lo in range(1, n + 1, size):
        yield lo, min(n, lo + size - 1)


def values_sql(rows, width):
    fmt = "(%r,%d)" if width == "reading" else "(%r,%r)"
    return ",".join(fmt % row for row in rows)


def write_sql(path, readings, outages, history_start, batch, pause, report, min_outage):
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as fh:
        w = fh.write
        w("-- Power history backfill, generated by tools/backfill_logs.py at %s\n" % stamp)
        w("-- %d log files, %d lines; %d readings, %d outages (>= %ss) before the app's first start.\n"
          % (report.files, report.lines, len(readings), len(outages), min_outage))
        w("-- Apply (the app may keep running):  sqlite3 /path/to/power.db < %s\n" % os.path.basename(path))
        w("-- Safe to re-run: rows that already exist, and anything from after the app started, are skipped.\n")
        w("-- Each transaction moves at most %d rows, then pauses %.2f s so the app can write.\n" % (batch, pause))
        w("-- If your sqlite3 build rejects '.shell', delete those lines (the pauses).\n")
        w(".bail on\n.timeout 10000\n")
        for stmt in SETUP:
            w(stmt + ";\n")
        # Staging tables live in the temp database: filling them doesn't lock power.db.
        for i in range(0, len(readings), 500):
            w("INSERT INTO temp.bf_readings (ts, ok) VALUES %s;\n" % values_sql(readings[i:i + 500], "reading"))
        for i in range(0, len(outages), 500):
            w("INSERT INTO temp.bf_outages (start, end) VALUES %s;\n" % values_sql(outages[i:i + 500], "outage"))
        w(REMEMBER_APP_START + ";\n")
        for move, n in ((MOVE_OUTAGES, len(outages)), (MOVE_READINGS, len(readings))):
            for lo, hi in batches(n, batch):
                w("BEGIN IMMEDIATE;\n%s;\nCOMMIT;\n.shell sleep %g\n" % (move.format(lo=lo, hi=hi), pause))
        if history_start is not None:
            w(EXTEND_HISTORY.format(start=history_start) + ";\n")
        w("SELECT 'backfill done: ' || (SELECT COUNT(*) FROM main.outages) || ' outages, ' || "
          "(SELECT COUNT(*) FROM main.readings) || ' readings in the database';\n")


def apply_db(path, readings, outages, history_start, max_lock, pause):
    """Insert directly, keeping every write transaction under max_lock seconds."""
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'readings'").fetchone() is None:
            raise SystemExit("%s has no app tables; start the app once so it creates them" % path)
        for stmt in SETUP:
            db.execute(stmt)
        db.executemany("INSERT INTO temp.bf_readings (ts, ok) VALUES (?, ?)", readings)
        db.executemany("INSERT INTO temp.bf_outages (start, end) VALUES (?, ?)", outages)
        db.execute(REMEMBER_APP_START)
        if db.execute("SELECT 1 FROM main.meta WHERE k = 'app_started'").fetchone() is None:
            raise SystemExit("%s has no 'tracking_since' yet; start the app once first" % path)
        stats = {"transactions": 0, "longest_lock": 0.0}
        size = 200  # rows per transaction; adapted so each stays well under max_lock
        for move, n in ((MOVE_OUTAGES, len(outages)), (MOVE_READINGS, len(readings))):
            lo = 1
            while lo <= n:
                hi = min(n, lo + size - 1)
                t0 = time.monotonic()
                db.execute("BEGIN IMMEDIATE")  # waits (up to 30 s) while the app holds the lock
                held = time.monotonic()
                db.execute(move.format(lo=lo, hi=hi))
                db.execute("COMMIT")
                took = time.monotonic() - held
                stats["transactions"] += 1
                stats["longest_lock"] = max(stats["longest_lock"], took)
                if took > max_lock / 2:
                    size = max(10, size // 2)
                elif took < max_lock / 8:
                    size = min(5000, size * 2)
                lo = hi + 1
                time.sleep(pause)
        if history_start is not None:
            db.execute(EXTEND_HISTORY.format(start=history_start))
        counts = db.execute("SELECT (SELECT COUNT(*) FROM main.outages), (SELECT COUNT(*) FROM main.readings)").fetchone()
        return stats, counts
    finally:
        db.close()


def app_start_of(path):
    """When the app first started (read-only), or None if unknown."""
    try:
        db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    except sqlite3.Error:
        return None
    try:
        row = db.execute("SELECT v FROM meta WHERE k = 'app_started'").fetchone() or \
              db.execute("SELECT v FROM meta WHERE k = 'tracking_since'").fetchone()
        return float(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        db.close()


def parse_offset(text):
    sign = -1 if text.startswith("-") else 1
    h, _, m = text.lstrip("+-").partition(":")
    return timezone(sign * timedelta(hours=int(h), minutes=int(m or 0)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("logs", nargs="+", help="power-status*.log files (shell glob)")
    out = ap.add_mutually_exclusive_group(required=True)
    out.add_argument("--sql", metavar="FILE", help="write an SQL script instead of touching a database")
    out.add_argument("--db", metavar="PATH", help="insert directly into this power.db")
    ap.add_argument("--dry-run", action="store_true", help="with --db: report what would be imported, write nothing")
    ap.add_argument("--min-outage", type=float, default=15, help="shortest Off run counted as an outage, seconds (default 15)")
    ap.add_argument("--utc-offset", help="time zone the logs were written in, e.g. +05:30 (default: this machine's)")
    ap.add_argument("--max-lock", type=float, default=0.5, help="--db: longest write transaction, seconds (default 0.5, must be < 1)")
    ap.add_argument("--batch", type=int, default=500, help="--sql: rows moved per transaction (default 500)")
    ap.add_argument("--pause", type=float, default=0.25, help="pause between transactions so the app can write, seconds (default 0.25)")
    args = ap.parse_args(argv)
    if not 0 < args.max_lock < 1:
        ap.error("--max-lock must be between 0 and 1 second")
    tz = parse_offset(args.utc_offset) if args.utc_offset else None
    zone = args.utc_offset or "%s (this machine)" % (time.strftime("%Z") or "local")

    report = Report()
    t0 = time.monotonic()
    flips = scan(args.logs, tz, report)
    if not flips:
        raise SystemExit("no readable log lines found")
    outages = outages_from(flips, report.last, args.min_outage)
    readings = [(t, int(ok)) for t, ok in flips]
    fmt = lambda t: datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d %H:%M:%S") if t else "-"
    print("read %d files, %d lines in %.0f s (log times taken as %s)" % (report.files, report.lines, time.monotonic() - t0, zone))
    print("  span %s -> %s; %d duplicate lines skipped, %d unreadable" % (fmt(report.first), fmt(report.last), report.dups, report.bad))

    cutoff = app_start_of(args.db) if args.db else None
    if cutoff is not None:
        readings_in = [r for r in readings if r[0] < cutoff]
        outages_in = [(s, min(e, cutoff)) for s, e in outages if s < cutoff]
        print("  app first started %s: only data before that is imported" % fmt(cutoff))
    else:
        readings_in, outages_in = readings, outages
        if args.sql:
            print("  rows from after the app's first start are skipped when the SQL is applied")
    hours = sum(e - s for s, e in outages_in) / 3600
    print("  %d readings (On/Off changes), %d outages >= %gs totalling %.1f h, %d shorter blips (readings only)"
          % (len(readings_in), len(outages_in), args.min_outage, hours, sum(1 for t, ok in flips if not ok) - len(outages)))
    for s, e in sorted(outages_in, key=lambda o: o[0] - o[1])[:5]:
        print("    longest: %s -> %s  %.1f h" % (fmt(s), fmt(e), (e - s) / 3600))
    history_start = int(report.first)

    if args.sql:
        write_sql(args.sql, readings, outages, history_start, args.batch, args.pause, report, args.min_outage)
        print("wrote %s (%.1f MB). Apply with:  sqlite3 /path/to/power.db < %s"
              % (args.sql, os.path.getsize(args.sql) / 1e6, args.sql))
    elif args.dry_run:
        print("dry run: nothing written")
    else:
        stats, counts = apply_db(args.db, readings, outages, history_start, args.max_lock, args.pause)
        print("done: %d transactions, longest held the write lock %.3f s; database now has %d outages, %d readings"
              % (stats["transactions"], stats["longest_lock"], counts[0], counts[1]))
        print("restart the app (sudo systemctl restart powersensor) so every open page picks up the history now")


if __name__ == "__main__":
    main()
