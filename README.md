# Power Status

A dashboard for a community to see whether mains power is on right now, and the
history and patterns of past outages.

It runs on a Raspberry Pi as a single Python 3.10+ process using only the standard
library, with SQLite storage and about 25 MB of RAM. It polls a sensor on the LAN
(`http://192.168.1.249/`, which replies `Mains On` while there is power and goes
offline when there isn't) and records every change to the second. The history is
never compressed or pruned.

The page:

- **Status card:** power on or off, since when, and when the last outage ended
- **Range slider:** 24h / 7d / 30d / 90d / All presets. It drives everything below
  it, including the outage list at the bottom
- **Reliability stats**
- **Eight interactive charts:** on/off step chart, outages per day, outage timeline,
  uptime trend, calendar, weekday × hour pattern, start-time histogram, duration
  breakdown

The status card appears from the first response; charts (a trimmed, self-hosted
ECharts bundle) load afterwards.

**On phones** the page becomes app-style, with a bottom tab bar: **Now** (status, key
stats, latest outages) · **Trends** · **Patterns** · **History**. The status stays
pinned at the top of every tab, and each tab has its own link
(e.g. `http://…:8080/#history`).

## Quick start

```bash
python3 app.py                 # reads config.ini, then open http://localhost:8080
```

All settings (sensor address, thresholds, port, site name, database path) are in
[`config.ini`](config.ini), which is commented line by line. The app reads it once
at startup; use `--config PATH` to point at another file.

On the Pi, install it as a systemd service and optionally publish it on the internet
(this works without a public IP). See the admin guide.

## Tests

```bash
tests/run.sh                  # everything
python3 -m unittest tests.test_app -v   # backend only
node --test tests/frontend/   # frontend logic only
```

There are three suites, and none need packages installed:

- **Backend:** 53 Python `unittest` tests, covering config loading
- **Frontend logic:** 24 Node `node:test` tests, run in UTC+5:30 and DST time zones
- **Browser smoke:** 6 headless-Chrome tests (desktop and phone layouts) that check every chart renders without
  errors. They skip if Chrome isn't installed

See [docs/TESTING.md](docs/TESTING.md) for requirements, running single tests, and
what's covered.

## Docs

- [docs/ADMINISTRATION.md](docs/ADMINISTRATION.md): install, `config.ini` settings,
  publishing on the internet without a public IP, backups, data fixes, upgrades,
  troubleshooting
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, state machine, data
  model, HTTP API, frontend design, failure modes, design decisions
- [docs/TESTING.md](docs/TESTING.md): running the tests locally, what each suite
  covers, adding tests

## Repository layout

```
app.py                 server: config loading, poller, SQLite store, HTTP server
config.ini             all settings (read once at startup)
index.html             frontend markup, CSS and rendering JS
static/core.js         frontend data logic (pure functions, inlined into the page)
static/echarts.min.js  trimmed ECharts bundle (built from tools/echarts)
tests/                 backend, frontend logic and browser smoke tests
tools/echarts/         bundle build (Node; only needed to change chart types)
powersensor.service    systemd unit
docs/                  architecture, administration and testing guides
```
