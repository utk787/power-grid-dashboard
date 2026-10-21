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


class DeployFilesTest(unittest.TestCase):
    """The Oracle + Headscale scripts and their templates must agree (they only run on real servers)."""

    DEPLOY = ROOT / "deploy"
    SETUP = (DEPLOY / "oracle" / "setup-vm.sh").read_text(encoding="utf-8")
    JOIN = (DEPLOY / "pi" / "join-tailnet.sh").read_text(encoding="utf-8")

    def test_every_template_placeholder_is_filled_by_the_script(self):
        for template in ("Caddyfile", "headscale/config.yaml"):
            text = (self.DEPLOY / "oracle" / template).read_text(encoding="utf-8")
            for placeholder in set(re.findall(r"\{\{[A-Z_]+\}\}", text)):
                with self.subTest(template=template, placeholder=placeholder):
                    self.assertIn("s/%s/" % placeholder, self.SETUP)

    def test_files_the_script_needs_exist(self):
        for name in re.search(r"for f in ([^;]+); do", self.SETUP).group(1).split():
            with self.subTest(file=name):
                self.assertTrue((self.DEPLOY / "oracle" / name).is_file())

    def test_pi_command_printed_by_the_vm_script_exists(self):
        path = re.search(r"sudo (deploy/pi/[\w.-]+) https://", self.SETUP).group(1)
        self.assertTrue((ROOT / path).is_file())
        self.assertTrue((ROOT / path).stat().st_mode & 0o111, "join script must be executable")

    def test_pi_node_name_matches_between_scripts(self):
        self.assertEqual(re.search(r"^PI_NAME=(\S+)", self.SETUP, re.M).group(1),
                         re.search(r"^NAME=(\S+)", self.JOIN, re.M).group(1))

    def test_policy_allows_only_vm_to_pi_dashboard(self):
        policy = (self.DEPLOY / "oracle" / "headscale" / "policy.hujson").read_text(encoding="utf-8")
        rules = re.findall(r'\{"action": "accept".*\}', policy)
        self.assertEqual(rules, ['{"action": "accept", "src": ["proxy@"], "dst": ["home@:8080"]}'])
        for user in ("proxy", "home"):
            self.assertIn("for u in proxy home", self.SETUP)
            self.assertIn("user_id %s" % user, self.SETUP)
        self.assertIn(":8080", re.search(r'write_caddyfile "\$PI_IP:(\d+)"', self.SETUP).group(0))

    def test_headscale_listens_where_caddy_proxies(self):
        config = (self.DEPLOY / "oracle" / "headscale" / "config.yaml").read_text(encoding="utf-8")
        caddy = (self.DEPLOY / "oracle" / "Caddyfile").read_text(encoding="utf-8")
        listen = re.search(r"^listen_addr: (\S+)", config, re.M).group(1)
        self.assertIn("reverse_proxy %s" % listen, caddy)
        self.assertIn("http://%s/health" % listen, self.SETUP)


if __name__ == "__main__":
    unittest.main()
