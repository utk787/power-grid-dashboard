"""Keeps the API docs in step with the code (stdlib only; no YAML parser needed).

Run from the repo root:  python3 -m unittest tests.test_docs -v
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "app.py").read_text(encoding="utf-8")
SPEC = (ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8")
GUIDE = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")


def spec_paths():
    """Path keys under `paths:` (two-space indented lines like '  /api/status:')."""
    section = SPEC.split("\npaths:\n", 1)[1].split("\ncomponents:\n", 1)[0]
    return set(re.findall(r"^  (/[^:\s]*):\s*$", section, re.M))


def app_routes():
    """Paths the request handler answers, read from app.py's do_GET."""
    handler = APP.split("def do_GET(self):", 1)[1].split("do_HEAD = do_GET", 1)[0]
    routes = set(re.findall(r'url\.path == "([^"]+)"', handler))
    routes |= {p for p in re.findall(r'url\.path in \(([^)]*)\)', handler) for p in re.findall(r'"([^"]+)"', p)}
    if 'url.path.startswith("/static/")' in handler:
        routes.add("/static/{file}")
    return routes - {"/index.html"}  # an alias of "/"


class ApiDocsTest(unittest.TestCase):
    def test_every_route_is_in_the_openapi_spec_and_nothing_extra(self):
        self.assertEqual(spec_paths(), app_routes())

    def test_every_route_is_in_the_api_guide(self):
        for path in app_routes():
            with self.subTest(path=path):
                self.assertIn("`GET %s`" % path, GUIDE)

    def test_spec_basics(self):
        self.assertRegex(SPEC, r"^openapi: 3\.1\.\d+\n")
        self.assertEqual(len(re.findall(r"operationId: ", SPEC)), len(spec_paths()))

    def test_static_files_listed_in_spec_exist(self):
        listed = re.search(r"enum: \[([^\]]*echarts[^\]]*)\]", SPEC).group(1)
        files = {f.strip() for f in listed.split(",")}
        self.assertEqual(files, {p.name for p in (ROOT / "static").iterdir() if p.is_file()})

    def test_range_limit_matches_code(self):
        limit = re.search(r"MAX_QUERY_RANGE = (\d+) \* (\d+)", APP)
        seconds = int(limit.group(1)) * int(limit.group(2))
        self.assertIn("const: %d" % seconds, SPEC)
        self.assertIn("max_range_s: %d" % seconds, SPEC)


if __name__ == "__main__":
    unittest.main()
