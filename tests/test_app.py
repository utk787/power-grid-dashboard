"""Backend unit tests for app.py (stdlib unittest, no dependencies).

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import gzip
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402

T0 = 1_800_000_000  # fixed "now" for state-machine tests (2027-01-15)


class TempDB(unittest.TestCase):
    """Gives each test a fresh database file and default thresholds."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "power.db")
        self.store = app.Store(self.db_path)
        patches = {"FAIL_THRESHOLD": 3, "HEARTBEAT_INTERVAL": 60, "GAP_THRESHOLD": 180, "POLL_INTERVAL": 5}
        for name, value in patches.items():
            p = mock.patch.object(app, name, value)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def outages(self):
        return self.store.db.execute("SELECT start, end, inferred FROM outages ORDER BY start").fetchall()

    def readings(self):
        return self.store.db.execute("SELECT ts, ok FROM readings ORDER BY ts, rowid").fetchall()


# --------------------------------------------------------------------------- Store

class StoreTests(TempDB):
    def test_schema_and_wal_mode(self):
        tables = {r[0] for r in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"outages", "readings", "meta"} <= tables)
        self.assertEqual(self.store.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_outage_roundtrip_keeps_exact_seconds(self):
        self.store.start_outage(T0 + 7)
        self.assertEqual(self.store.open_outage()[1], T0 + 7)
        self.store.end_outage(T0 + 4321)
        self.assertIsNone(self.store.open_outage())
        self.assertEqual(self.outages(), [(T0 + 7, T0 + 4321, 0)])
        self.assertEqual(self.store.last_outage_end(), T0 + 4321)

    def test_outages_since_filters_and_orders_newest_first(self):
        for s, e in [(100, 200), (300, 400), (500, None)]:
            self.store.start_outage(s)
            if e:
                self.store.end_outage(e)
        self.assertEqual(self.store.outages_since(0), [[500, None, 0], [300, 400, 0], [100, 200, 0]])
        # An outage that ended before `since` is excluded; an open one is always included.
        self.assertEqual(self.store.outages_since(250), [[500, None, 0], [300, 400, 0]])
        self.assertEqual(self.store.outages_since(10_000), [[500, None, 0]])

    def test_version_bumps_only_on_outage_changes(self):
        v = self.store.version
        self.store.set_meta("k", 1)
        self.store.add_reading(T0, True)
        self.assertEqual(self.store.version, v)
        self.store.start_outage(T0)
        self.store.end_outage(T0 + 1)
        self.assertEqual(self.store.version, v + 2)

    def test_meta_roundtrip(self):
        self.assertIsNone(self.store.get_meta("missing"))
        self.store.set_meta("alive", 123)
        self.store.set_meta("alive", 456)
        self.assertEqual(self.store.get_meta("alive"), "456")

    def test_readings_between_inclusive_and_ordered(self):
        for ts, ok in [(30, 1), (10, 1), (20, 0)]:
            self.store.add_reading(ts, ok)
        self.assertEqual(self.store.readings_between(10, 20), [[10, 1], [20, 0]])
        self.assertEqual(self.store.last_reading(), True)  # latest by ts is (30, 1)
        self.assertEqual(self.store.readings_between(31, 99), [])

    def test_history_survives_reopen_without_loss(self):
        rows = [(T0 + i * 97, T0 + i * 97 + (i % 13) + 1) for i in range(500)]
        for s, e in rows:
            self.store.start_outage(s)
            self.store.end_outage(e)
            self.store.add_reading(s, False)
            self.store.add_reading(e, True)
        self.store.db.close()
        self.store = app.Store(self.db_path)
        self.assertEqual([tuple(r[:2]) for r in reversed(self.store.outages_since(0))], rows)
        self.assertEqual(len(self.store.readings_between(0, 2**40)), 1000)


# --------------------------------------------------------------------------- Monitor

class MonitorTests(TempDB):
    def feed(self, monitor, results, start=T0, step=5):
        """Feed check results at `step`-second intervals; returns the next timestamp."""
        ts = start
        for ok in results:
            monitor.record(ok, ts)
            ts += step
        return ts

    def test_initial_state_and_tracking_since_persists(self):
        m = app.Monitor(self.store)
        self.assertEqual(m.state, "unknown")
        first = m.tracking_since
        self.assertEqual(int(self.store.get_meta("tracking_since")), first)
        with mock.patch.object(app.time, "time", return_value=first + 10_000):
            self.assertEqual(app.Monitor(self.store).tracking_since, first)

    def test_first_success_turns_on_since_tracking_start(self):
        m = app.Monitor(self.store)
        m.record(True, T0)
        self.assertEqual((m.state, m.since), ("on", m.tracking_since))

    def test_failures_below_threshold_do_not_create_outage(self):
        m = app.Monitor(self.store)
        self.feed(m, [True, False, False, True])
        self.assertEqual(m.state, "on")
        self.assertEqual(self.outages(), [])

    def test_outage_starts_at_first_failed_check_after_threshold(self):
        m = app.Monitor(self.store)
        self.feed(m, [True, False, False])          # T0 ok, T0+5 and T0+10 fail
        self.assertEqual(self.outages(), [])
        m.record(False, T0 + 15)                     # third consecutive failure
        self.assertEqual(self.outages(), [(T0 + 5, None, 0)])
        self.assertEqual((m.state, m.since), ("off", T0 + 5))

    def test_single_success_restores_power(self):
        m = app.Monitor(self.store)
        self.feed(m, [True, False, False, False, False])
        m.record(True, T0 + 25)
        self.assertEqual(self.outages(), [(T0 + 5, T0 + 25, 0)])
        self.assertEqual((m.state, m.since), ("on", T0 + 25))

    def test_success_resets_consecutive_failure_count(self):
        m = app.Monitor(self.store)
        self.feed(m, [True, False, False, True, False, False, True])
        self.assertEqual(self.outages(), [])

    def test_threshold_is_configurable(self):
        with mock.patch.object(app, "FAIL_THRESHOLD", 1):
            m = app.Monitor(self.store)
            self.feed(m, [True, False])
        self.assertEqual(self.outages(), [(T0 + 5, None, 0)])

    def test_readings_record_every_flip_including_blips(self):
        m = app.Monitor(self.store)
        self.feed(m, [True, True, False, True, True, False, False, False, False, True])
        # A 1-check blip at T0+10 is kept in readings even though it never became an outage.
        self.assertEqual(self.readings(), [(T0, 1), (T0 + 10, 0), (T0 + 15, 1), (T0 + 25, 0), (T0 + 45, 1)])
        self.assertEqual(self.outages(), [(T0 + 25, T0 + 45, 0)])

    def test_readings_continue_from_previous_run(self):
        self.store.add_reading(T0 - 100, True)
        m = app.Monitor(self.store)
        m.record(True, T0)   # same as last stored result: no new row
        m.record(False, T0 + 5)
        self.assertEqual(self.readings(), [(T0 - 100, 1), (T0 + 5, 0)])

    def test_heartbeat_written_once_per_interval(self):
        m = app.Monitor(self.store)
        with mock.patch.object(self.store, "set_meta", wraps=self.store.set_meta) as spy:
            self.feed(m, [True] * 30, step=5)  # 150 s of checks
        alive_writes = [c.args[1] for c in spy.call_args_list if c.args[0] == "alive"]
        self.assertEqual(alive_writes, [T0, T0 + 60, T0 + 120])

    def test_restart_after_long_gap_records_inferred_outage(self):
        now = int(time.time())
        self.store.set_meta("alive", now - 1000)
        m = app.Monitor(self.store)
        self.assertEqual(self.outages(), [(now - 1000, None, 1)])
        m.record(True, now + 5)
        self.assertEqual(self.outages(), [(now - 1000, now + 5, 1)])
        self.assertEqual(m.state, "on")

    def test_restart_after_short_gap_records_nothing(self):
        self.store.set_meta("alive", int(time.time()) - 30)
        app.Monitor(self.store)
        self.assertEqual(self.outages(), [])

    def test_restart_during_outage_continues_it(self):
        self.store.start_outage(T0 - 500)
        m = app.Monitor(self.store)
        m.record(False, T0)  # one failure is enough: the outage is already open
        self.assertEqual((m.state, m.since), ("off", T0 - 500))
        m.record(True, T0 + 5)
        self.assertEqual(self.outages(), [(T0 - 500, T0 + 5, 0)])

    def test_snapshot_fields(self):
        m = app.Monitor(self.store)
        m.record(True, T0)
        snap = m.snapshot()
        self.assertEqual(set(snap), {"state", "since", "checked", "now", "tracking_since", "poll", "v"})
        self.assertEqual((snap["state"], snap["checked"], snap["poll"]), ("on", T0, 5))

    def test_run_loop_polls_and_stops_promptly(self):
        m = app.Monitor(self.store)
        stop = threading.Event()
        calls = []

        def fake_check():
            calls.append(1)
            if len(calls) >= 3:
                stop.set()
            return True

        with mock.patch.object(app, "check_sensor", fake_check), mock.patch.object(app, "POLL_INTERVAL", 0.01):
            t = threading.Thread(target=m.run, args=(stop,))
            t.start()
            t.join(timeout=2)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(calls), 3)
        self.assertEqual(m.state, "on")


# --------------------------------------------------------------------------- check_sensor

class FakeSensor(BaseHTTPRequestHandler):
    reply = (200, b"Mains On", 0.0)

    def log_message(self, *a):
        pass

    def do_GET(self):
        code, body, delay = FakeSensor.reply
        time.sleep(delay)
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body)


class CheckSensorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeSensor)
        threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        cls.url = "http://127.0.0.1:%d/" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def check(self, reply, timeout=1.0):
        FakeSensor.reply = reply
        with mock.patch.object(app, "SENSOR_URL", self.url), mock.patch.object(app, "REQUEST_TIMEOUT", timeout):
            return app.check_sensor()

    def test_mains_on(self):
        self.assertTrue(self.check((200, b"Mains On", 0)))

    def test_match_is_case_insensitive_and_substring(self):
        self.assertTrue(self.check((200, b"status: MAINS ON\n", 0)))

    def test_other_text_means_no_power(self):
        self.assertFalse(self.check((200, b"Mains Off", 0)))

    def test_http_error_means_no_power(self):
        self.assertFalse(self.check((500, b"Mains On", 0)))

    def test_timeout_means_no_power(self):
        self.assertFalse(self.check((200, b"Mains On", 0.5), timeout=0.1))

    def test_unreachable_means_no_power(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing listens here now
        with mock.patch.object(app, "SENSOR_URL", "http://127.0.0.1:%d/" % port):
            self.assertFalse(app.check_sensor())


# --------------------------------------------------------------------------- HTTP

class HttpTests(TempDB):
    def setUp(self):
        super().setUp()
        self.monitor = app.Monitor(self.store)
        self.monitor.record(True, T0)
        self.app = app.App(self.store, self.monitor)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(self.app))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, path, gzip_ok=False, method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        if gzip_ok:
            req.add_header("Accept-Encoding", "gzip")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def get_json(self, path):
        status, headers, body = self.get(path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        return json.loads(body)

    def test_page_has_all_placeholders_filled_and_boot_data(self):
        self.store.start_outage(T0 - 100)
        self.store.end_outage(T0 - 50)
        status, headers, body = self.get("/")
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertNotIn("{{", html)
        self.assertIn("root.PS = factory()", html)  # core.js inlined
        boot = json.loads(html.split('<script id="boot" type="application/json">')[1].split("</script>")[0])
        self.assertEqual(boot["status"]["state"], "on")
        self.assertEqual(boot["outages"], [[T0 - 100, T0 - 50, 0]])
        self.assertEqual(boot["echarts"], "static/echarts.min.js?v=" + self.app.asset_version)

    def test_site_name_is_html_escaped(self):
        with mock.patch.object(app, "SITE_NAME", "<b>A&B</b>"):
            page = app.App(self.store, self.monitor).page()
        self.assertIn("&lt;b&gt;A&amp;B&lt;/b&gt;", page)
        self.assertNotIn("<b>A&B</b>", page)

    def test_page_is_gzipped_only_when_accepted(self):
        _, headers, body = self.get("/", gzip_ok=True)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertIn(b"<!doctype html>", gzip.decompress(body))
        _, headers, body = self.get("/")
        self.assertNotIn("Content-Encoding", headers)
        self.assertTrue(body.startswith(b"<!doctype html>"))

    def test_status_endpoint(self):
        s = self.get_json("/api/status")
        self.assertEqual((s["state"], s["checked"]), ("on", T0))

    def test_outages_endpoint_days_param(self):
        now = int(time.time())
        self.store.start_outage(now - 10 * 86400)
        self.store.end_outage(now - 10 * 86400 + 60)
        self.store.start_outage(now - 3600)
        self.store.end_outage(now - 3000)
        self.assertEqual(len(self.get_json("/api/outages?days=1")["outages"]), 1)
        self.assertEqual(len(self.get_json("/api/outages?days=0")["outages"]), 2)
        self.assertEqual(self.get_json("/api/outages?days=abc")["days"], 30)    # bad value: default
        self.assertEqual(self.get_json("/api/outages?days=-5")["days"], 0)      # clamped
        self.assertEqual(self.get_json("/api/outages?days=99999")["days"], 3650)

    def test_outages_cache_invalidated_by_new_outage(self):
        self.assertEqual(self.get_json("/api/outages?days=0")["outages"], [])
        self.store.start_outage(int(time.time()))
        self.assertEqual(len(self.get_json("/api/outages?days=0")["outages"]), 1)

    def test_readings_endpoint(self):
        for ts, ok in [(100, 1), (200, 0), (300, 1)]:
            self.store.add_reading(ts, ok)
        r = self.get_json("/api/readings?from=150&to=300")
        self.assertEqual(r["readings"], [[200, 0], [300, 1]])
        status, _, _ = self.get("/api/readings?from=x")
        self.assertEqual(status, 400)

    def test_static_served_precompressed_with_long_cache(self):
        raw = (app.HERE / "static" / "echarts.min.js").read_bytes()
        status, headers, body = self.get("/static/echarts.min.js?v=" + self.app.asset_version, gzip_ok=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "public, max-age=31536000, immutable")
        self.assertEqual(headers["Content-Type"], "application/javascript")
        self.assertEqual(gzip.decompress(body), raw)

    def test_unknown_and_traversal_paths_are_404(self):
        for path in ("/nope", "/static/missing.js", "/static/../app.py", "/static/..%2Fapp.py"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_head_has_no_body(self):
        status, headers, body = self.get("/api/status", method="HEAD")
        self.assertEqual(status, 200)
        self.assertGreater(int(headers["Content-Length"]), 0)
        self.assertEqual(body, b"")

    def test_healthz(self):
        self.assertEqual(self.get("/healthz")[2], b"ok")


if __name__ == "__main__":
    unittest.main()
