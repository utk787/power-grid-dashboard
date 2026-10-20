#!/usr/bin/env python3
"""Power status dashboard.

Polls a mains sensor over HTTP, records outages in SQLite and serves a small
web page showing live status and outage history. Standard library only
(Python 3.10+), so it runs on a bare Raspberry Pi OS install.
"""
import gzip
import hashlib
import html
import json
import logging
import os
import signal
import sqlite3
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent


def env(name, default, cast=str):
    return cast(os.environ.get(name, default))


SENSOR_URL = env("SENSOR_URL", "http://192.168.1.249/")
SENSOR_MATCH = env("SENSOR_MATCH", "Mains On")
POLL_INTERVAL = env("POLL_INTERVAL", 5, float)        # seconds between checks
REQUEST_TIMEOUT = env("REQUEST_TIMEOUT", 3, float)    # seconds per check
FAIL_THRESHOLD = env("FAIL_THRESHOLD", 3, int)        # failed checks before declaring an outage
HEARTBEAT_INTERVAL = env("HEARTBEAT_INTERVAL", 60, int)
GAP_THRESHOLD = env("GAP_THRESHOLD", 180, int)        # monitor downtime treated as a likely outage
DB_PATH = env("DB_PATH", str(HERE / "power.db"))
HOST = env("HOST", "0.0.0.0")
PORT = env("PORT", 8080, int)
SITE_NAME = env("SITE_NAME", "Power Status")

log = logging.getLogger("powersensor")

SCHEMA = """
CREATE TABLE IF NOT EXISTS outages (
    id       INTEGER PRIMARY KEY,
    start    INTEGER NOT NULL,           -- unix seconds
    end      INTEGER,                    -- NULL while ongoing
    inferred INTEGER NOT NULL DEFAULT 0  -- 1 = monitor itself was offline, start is approximate
);
CREATE INDEX IF NOT EXISTS outages_end ON outages(end);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
-- Raw sensor log: one row each time the check result flips, including blips too short
-- to count as an outage. Never aggregated or pruned; outages are derived from this.
CREATE TABLE IF NOT EXISTS readings (
    ts INTEGER NOT NULL,                 -- unix seconds of the first check with this result
    ok INTEGER NOT NULL                  -- 1 = sensor reported mains on, 0 = no/bad reply
);
CREATE INDEX IF NOT EXISTS readings_ts ON readings(ts);
"""


class Store:
    """Single shared SQLite connection; writes are rare so one lock is plenty."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)
        self.version = 0  # bumped on every outage change, used for cache invalidation

    def _one(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def _write(self, sql, args=()):
        with self.lock:
            self.db.execute(sql, args)
            self.version += 1

    def get_meta(self, k):
        row = self._one("SELECT v FROM meta WHERE k = ?", (k,))
        return row[0] if row else None

    def set_meta(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, str(v)))

    def open_outage(self):
        return self._one("SELECT id, start FROM outages WHERE end IS NULL ORDER BY start DESC LIMIT 1")

    def last_outage_end(self):
        row = self._one("SELECT MAX(end) FROM outages")
        return row[0] if row else None

    def add_reading(self, ts, ok):
        with self.lock:
            self.db.execute("INSERT INTO readings (ts, ok) VALUES (?, ?)", (ts, int(ok)))

    def last_reading(self):
        row = self._one("SELECT ok FROM readings ORDER BY ts DESC, rowid DESC LIMIT 1")
        return None if row is None else bool(row[0])

    def readings_between(self, start, end, limit=100000):
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, ok FROM readings WHERE ts >= ? AND ts <= ? ORDER BY ts LIMIT ?",
                (start, end, limit),
            ).fetchall()
        return [list(r) for r in rows]

    def start_outage(self, ts, inferred=False):
        self._write("INSERT INTO outages (start, inferred) VALUES (?, ?)", (ts, int(inferred)))

    def end_outage(self, ts):
        self._write("UPDATE outages SET end = ? WHERE end IS NULL", (ts,))

    def outages_since(self, ts, limit=20000):
        """Outages that overlap [ts, now], newest first, as compact [start, end, inferred] rows."""
        with self.lock:
            rows = self.db.execute(
                "SELECT start, end, inferred FROM outages WHERE end IS NULL OR end >= ? "
                "ORDER BY start DESC LIMIT ?",
                (ts, limit),
            ).fetchall()
        return [list(r) for r in rows]


def check_sensor():
    try:
        with urllib.request.urlopen(SENSOR_URL, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read(512).decode("utf-8", "replace")
        return SENSOR_MATCH.lower() in body.lower()
    except Exception:
        return False


class Monitor:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.fails = 0
        self.first_fail = None
        self.checked = None
        self.state = "unknown"
        now = int(time.time())
        self.tracking_since = int(store.get_meta("tracking_since") or now)
        store.set_meta("tracking_since", self.tracking_since)

        alive = store.get_meta("alive")
        if store.open_outage() is None and alive and now - int(alive) > GAP_THRESHOLD:
            # The monitor was down for a while (often because the power was). Open an
            # approximate outage; the first successful check will close it.
            log.info("monitor was offline for %ss, recording likely outage", now - int(alive))
            store.start_outage(int(alive), inferred=True)
        self.since = now
        self.last_heartbeat = 0
        self.last_ok = store.last_reading()

    def record(self, ok, ts):
        with self.lock:
            self.checked = ts
            if ok != self.last_ok:
                self.store.add_reading(ts, ok)
                self.last_ok = ok
            if ts - self.last_heartbeat >= HEARTBEAT_INTERVAL:
                self.store.set_meta("alive", ts)
                self.last_heartbeat = ts

            if ok:
                self.fails = 0
                if self.state != "on":
                    if self.store.open_outage():
                        self.store.end_outage(ts)
                        log.info("power restored")
                    self.state = "on"
                    self.since = self.store.last_outage_end() or self.tracking_since
                return

            if self.fails == 0:
                self.first_fail = ts
            self.fails += 1
            if self.state == "off":
                return
            open_ = self.store.open_outage()
            if open_:
                self.state, self.since = "off", open_[1]
            elif self.fails >= FAIL_THRESHOLD:
                self.store.start_outage(self.first_fail)
                self.state, self.since = "off", self.first_fail
                log.info("power outage detected")

    def snapshot(self):
        with self.lock:
            return {
                "state": self.state,
                "since": self.since,
                "checked": self.checked,
                "now": int(time.time()),
                "tracking_since": self.tracking_since,
                "poll": POLL_INTERVAL,
                "v": self.store.version,
            }

    def run(self, stop):
        while not stop.is_set():
            t0 = time.monotonic()
            self.record(check_sensor(), int(time.time()))
            stop.wait(max(0.0, POLL_INTERVAL - (time.monotonic() - t0)))


class App:
    def __init__(self, store, monitor):
        self.store = store
        self.monitor = monitor
        template = (HERE / "index.html").read_text(encoding="utf-8")
        self.template = template.replace("{{SITE_NAME}}", html.escape(SITE_NAME))
        # Static files are small and never change while running: compress once, cache for a year.
        self.static = {}
        for f in (HERE / "static").glob("*"):
            raw = f.read_bytes()
            ctype = {".js": "application/javascript", ".css": "text/css"}.get(f.suffix, "application/octet-stream")
            self.static[f.name] = (raw, gzip.compress(raw, 9), ctype)
        self.asset_version = hashlib.sha1(b"".join(v[0] for v in self.static.values())).hexdigest()[:10]
        self._cache = {}
        self._cache_lock = threading.Lock()

    def outages(self, days):
        """Outage rows for the last `days` days (0 = all), cached until data changes."""
        key = (days, self.store.version, int(time.time()) // 60)
        with self._cache_lock:
            hit = self._cache.get(days)
            if hit and hit[0] == key:
                return hit[1]
        since = int(time.time()) - days * 86400 if days else 0
        rows = self.store.outages_since(since)
        with self._cache_lock:
            self._cache[days] = (key, rows)
        return rows

    def page(self):
        boot = {
            "status": self.monitor.snapshot(),
            "outages": self.outages(0),
            "echarts": "static/echarts.min.js?v=" + self.asset_version,
        }
        data = json.dumps(boot, separators=(",", ":")).replace("</", "<\\/")
        return self.template.replace("{{BOOT}}", data)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "powersensor"

        def log_message(self, *args):  # keep journald (and the SD card) quiet
            pass

        def send(self, code, body, ctype, cache="no-store", gzipped=None):
            raw = body.encode("utf-8") if isinstance(body, str) else body
            gz = len(raw) > 512 and "gzip" in self.headers.get("Accept-Encoding", "")
            if gz:
                raw = gzipped or gzip.compress(raw, 6)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", cache)
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def send_json(self, obj):
            self.send(200, json.dumps(obj, separators=(",", ":")), "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self.send(200, app.page(), "text/html; charset=utf-8")
            elif url.path == "/api/status":
                self.send_json(app.monitor.snapshot())
            elif url.path == "/api/outages":
                try:
                    days = max(0, min(3650, int(parse_qs(url.query).get("days", ["30"])[0])))
                except ValueError:
                    days = 30
                self.send_json({"days": days, "outages": app.outages(days)})
            elif url.path.startswith("/static/") and url.path[8:] in app.static:
                raw, gz, ctype = app.static[url.path[8:]]
                self.send(200, raw, ctype, "public, max-age=31536000, immutable", gzipped=gz)
            elif url.path == "/api/readings":
                # Raw flip log for a time range (unix seconds); defaults to the last 24h.
                q = parse_qs(url.query)
                try:
                    end = int(q.get("to", [time.time()])[0])
                    start = int(q.get("from", [end - 86400])[0])
                except ValueError:
                    self.send(400, "from/to must be unix seconds", "text/plain")
                    return
                self.send_json({"from": start, "to": end, "readings": app.store.readings_between(start, end)})
            elif url.path == "/healthz":
                self.send(200, "ok", "text/plain")
            else:
                self.send(404, "not found", "text/plain")

        do_HEAD = do_GET

    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = Store(DB_PATH)
    monitor = Monitor(store)
    app = App(store, monitor)

    stop = threading.Event()
    poller = threading.Thread(target=monitor.run, args=(stop,), name="poller", daemon=True)
    poller.start()

    server = ThreadingHTTPServer((HOST, PORT), make_handler(app))
    server.daemon_threads = True

    def shutdown(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.info("polling %s every %ss, serving on http://%s:%s", SENSOR_URL, POLL_INTERVAL, HOST, PORT)
    server.serve_forever()
    poller.join(timeout=REQUEST_TIMEOUT + 1)


if __name__ == "__main__":
    main()
