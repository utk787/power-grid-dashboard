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
# Which phone tab each chart lives on (see data-tab in index.html).
PHONE_TABS = {"now": ["step"], "trends": ["daily", "timeline", "uptime"], "patterns": ["cal", "heat", "hour", "dur"]}


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
        # Recent enough to fall inside the default 3-hour view.
        self.store.start_outage(now - 2 * 3600, inferred=True)
        self.store.end_outage(now - 2 * 3600 + 1800)
        if ongoing:
            self.store.start_outage(now - 1200)

    def serve(self):
        monitor = app.Monitor(self.store)
        monitor.record(not self.store.open_outage(), self.now)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(app.App(self.store, monitor)))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return "http://127.0.0.1:%d/" % self.server.server_port

    def render(self, url, width=1000):
        """Returns (rendered DOM, browser console lines).

        width=1000 gives the desktop layout. Headless Chrome won't go below 500 px wide, which is
        still under the 640 px phone breakpoint, so width=500 gives the phone layout.
        """
        args = [self.chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
                "--virtual-time-budget=8000", "--window-size=%d,7000" % width,  # tall: every lazy chart is in view
                "--enable-logging=stderr", "--v=1", "--dump-dom", url]
        if sys.platform.startswith("linux"):
            args.insert(1, "--no-sandbox")  # needed in most containers / CI runners
        with tempfile.TemporaryDirectory() as profile:
            args.insert(1, "--user-data-dir=" + profile)
            out = subprocess.run(args, capture_output=True, text=True, timeout=90)
        console = [line for line in out.stderr.splitlines() if "CONSOLE" in line]
        return out.stdout, console

    def chart_html(self, dom, el_id):
        m = re.search(r'<div[^>]*\bid="%s"[^>]*>(.*?)(?=<div[^>]*\bid="c-|</section>|<div id="win")' % el_id, dom, re.S)
        self.assertIsNotNone(m, "%s element missing" % el_id)
        return m

    def step_subtitle(self, dom):
        m = re.search(r'id="step-sub">([^<]*)<', dom)
        self.assertIsNotNone(m, "step chart subtitle missing")
        return m.group(1)

    def assert_no_console_errors(self, console):
        errors = [line for line in console if re.search(r"Uncaught|Error", line)]
        self.assertEqual(errors, [], "browser console errors")

    def assert_charts_rendered(self, dom, console, charts=CHARTS, slider=True):
        self.assert_no_console_errors(console)
        for name in list(charts) + (["range"] if slider else []):
            el_id = "range" if name == "range" else "c-" + name
            m = self.chart_html(dom, el_id)
            opening = dom[m.start(): dom.index(">", m.start())]
            self.assertNotIn("data-error", opening, "%s chart threw while drawing" % name)
            self.assertIn("<canvas", m.group(1), "%s chart has no canvas" % name)
        if set(charts) == set(CHARTS):
            self.assertNotIn("Loading chart…", dom)

    def test_power_on_page_renders_everything(self):
        self.seed()
        dom, console = self.render(self.serve())
        self.assertIn("Power is ON", dom)
        self.assertIn("Last outage ended", dom)
        self.assert_charts_rendered(dom, console)
        self.assertEqual(len(re.findall(r'class="card stat( more)?"', dom)), 8)
        self.assertIn("Monitor was offline", dom)  # inferred outage is labelled in the list
        self.assertIn("Drag across the chart to zoom", self.step_subtitle(dom))  # desktop drag-to-zoom

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


    # ---- phone layout (bottom tabs) ----

    def test_phone_now_tab(self):
        self.seed()
        dom, console = self.render(self.serve(), width=500)
        self.assertRegex(dom, r'<body[^>]*data-tab="now"')
        self.assertRegex(dom, r'data-go="now"[^>]*aria-current="page"|aria-current="page"[^>]*data-go="now"')
        self.assertIn("Latest outages", dom)
        recent = dom[dom.index('id="recent"'):dom.index('id="list"')]
        self.assertEqual(recent.count('class="row'), 3)
        self.assertIn("See all outages", recent)
        self.assertNotIn("Drag across", self.step_subtitle(dom))  # no drag-zoom on phones (it would fight scrolling)
        self.assert_charts_rendered(dom, console, PHONE_TABS["now"], slider=False)
        for name in PHONE_TABS["trends"] + PHONE_TABS["patterns"]:  # other tabs stay undrawn until opened
            self.assertNotIn("<canvas", self.chart_html(dom, "c-" + name).group(1), name)

    def test_phone_tab_from_link(self):
        self.seed()
        for tab, charts in (("trends", PHONE_TABS["trends"]), ("patterns", PHONE_TABS["patterns"])):
            with self.subTest(tab=tab):
                dom, console = self.render(self.serve() + "#" + tab, width=500)
                self.server.shutdown()
                self.server.server_close()
                self.assertRegex(dom, r'<body[^>]*data-tab="%s"' % tab)
                self.assert_charts_rendered(dom, console, charts, slider=False)
                self.assertNotIn("<canvas", self.chart_html(dom, "c-step").group(1), "Now-tab chart drawn while hidden")
        self.serve()  # tearDown expects a running server

    def test_phone_history_tab(self):
        self.seed()
        dom, console = self.render(self.serve() + "#history", width=500)
        self.assert_no_console_errors(console)
        self.assertRegex(dom, r'<body[^>]*data-tab="history"')
        self.assertIn("All outages in this period", dom)


if __name__ == "__main__":
    unittest.main()
