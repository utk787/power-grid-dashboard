"""Browser smoke test: loads the real page in headless Chrome and checks every chart renders.

Skipped automatically when Chrome/Chromium isn't installed. Set CHROME=/path/to/chrome to
point at a specific binary. Run from the repo root:  python3 -m unittest tests.test_smoke -v
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402

CHARTS = ["step", "daily", "timeline", "uptime", "cal", "heat", "hour", "dur"]


def find_chrome():
    candidates = [os.environ.get("CHROME"), "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    for c in filter(None, candidates):
        path = shutil.which(c) or (c if os.path.isfile(c) else None)
        if path:
            return path
    return None


class BrowserSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chrome = find_chrome()
        if not cls.chrome:
            raise unittest.SkipTest("Chrome/Chromium not found (set CHROME=/path/to/chrome)")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = app.Store(str(Path(self.tmp.name) / "power.db"))
        self.now = int(time.time())

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.db.close()
        self.tmp.cleanup()

    def seed(self, ongoing=False):
        now = self.now
        self.store.set_meta("tracking_since", now - 60 * 86400)
        # A spread of outages over 45 days: short blips, long cuts, one crossing midnight, one inferred.
        for day in range(1, 45):
            for k, length in enumerate([120, 1800, 7200][: day % 4]):
                start = now - day * 86400 + k * 5 * 3600
                self.store.start_outage(start)
                self.store.end_outage(start + length)
        self.store.start_outage(now - 3 * 86400, inferred=True)
        self.store.end_outage(now - 3 * 86400 + 5400)
        if ongoing:
            self.store.start_outage(now - 1200)

    def serve(self):
        monitor = app.Monitor(self.store)
        monitor.record(not self.store.open_outage(), self.now)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(app.App(self.store, monitor)))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return "http://127.0.0.1:%d/" % self.server.server_port

    def render(self, url):
        """Returns (rendered DOM, browser console lines)."""
        args = [self.chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
                "--virtual-time-budget=8000", "--window-size=1000,7000",  # tall: every lazy chart is in view
                "--enable-logging=stderr", "--v=1", "--dump-dom", url]
        if sys.platform.startswith("linux"):
            args.insert(1, "--no-sandbox")  # needed in most containers / CI runners
        with tempfile.TemporaryDirectory() as profile:
            args.insert(1, "--user-data-dir=" + profile)
            out = subprocess.run(args, capture_output=True, text=True, timeout=90)
        console = [line for line in out.stderr.splitlines() if "CONSOLE" in line]
        return out.stdout, console

    def assert_charts_rendered(self, dom, console):
        errors = [line for line in console if re.search(r"Uncaught|Error", line)]
        self.assertEqual(errors, [], "browser console errors")
        for name in CHARTS + ["range"]:
            el_id = "range" if name == "range" else "c-" + name
            m = re.search(r'<div[^>]*\bid="%s"[^>]*>(.*?)(?=<div[^>]*\bid="c-|</section>|<div id="win")' % el_id, dom, re.S)
            self.assertIsNotNone(m, "%s element missing" % el_id)
            opening = dom[m.start(): dom.index(">", m.start())]
            self.assertNotIn("data-error", opening, "%s chart threw while drawing" % name)
            self.assertIn("<canvas", m.group(1), "%s chart has no canvas" % name)
        self.assertNotIn("Loading chart…", dom)

    def test_power_on_page_renders_everything(self):
        self.seed()
        dom, console = self.render(self.serve())
        self.assertIn("Power is ON", dom)
        self.assertIn("Last outage ended", dom)
        self.assert_charts_rendered(dom, console)
        self.assertEqual(len(re.findall(r'class="card stat"', dom)), 8)
        self.assertIn("Monitor was offline", dom)  # inferred outage is labelled in the list

    def test_ongoing_outage_page(self):
        self.seed(ongoing=True)
        dom, console = self.render(self.serve())
        self.assertIn("Power is OUT", dom)
        self.assertIn("so far", dom)
        self.assertIn("ongoing", dom)
        self.assert_charts_rendered(dom, console)

    def test_empty_history_page(self):
        dom, console = self.render(self.serve())
        self.assertIn("Power is ON", dom)
        self.assertIn("No outages recorded", dom)
        self.assert_charts_rendered(dom, console)


if __name__ == "__main__":
    unittest.main()
