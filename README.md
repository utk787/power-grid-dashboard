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

## Quick start

```bash
python3 app.py                 # then open http://localhost:8080
```

On the Pi, install it as a systemd service and optionally publish it on the internet
(this works without a public IP). See the admin guide.

## Docs

- [docs/ADMINISTRATION.md](docs/ADMINISTRATION.md): install, configuration,
  publishing on the internet without a public IP, backups, data fixes, upgrades,
  troubleshooting
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, state machine, data
  model, HTTP API, frontend design, failure modes, design decisions

## Repository layout

```
app.py                 server: poller, SQLite store, HTTP server
index.html             the whole frontend (CSS + JS inline)
static/echarts.min.js  trimmed ECharts bundle (built from tools/echarts)
tools/echarts/         bundle build (Node; only needed to change chart types)
powersensor.service    systemd unit
docs/                  architecture and administration guides
```
