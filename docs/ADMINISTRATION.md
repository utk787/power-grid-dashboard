# Administration guide

Running, publishing, backing up and fixing the Power Status dashboard on the
Raspberry Pi. For how it works internally, see [ARCHITECTURE.md](ARCHITECTURE.md).

## At a glance

| What | Where |
|---|---|
| App directory | `/home/utkarsh/powersensor/` (`app.py`, `index.html`, `static/`) |
| Database | `/home/utkarsh/powersensor/power.db` (plus `power.db-wal` and `power.db-shm` while running) |
| Service | `powersensor` (systemd), runs as user `utkarsh` |
| Local URL | `http://raspberrypi:8080` or `http://<pi-ip>:8080` |
| Logs | `journalctl -u powersensor` (state changes only, no per-request logs) |
| Memory | ~25 MB normally; systemd caps it at 80 MB |

## 1. First-time install

Requirements: Raspberry Pi OS with Python 3.10 or newer (`python3 --version`). No
pip packages are needed. The `sqlite3` CLI is optional but handy for backups and
data fixes: `sudo apt install sqlite3`.

From the machine that has the repo:

```bash
ssh utkarsh@raspberrypi 'mkdir -p ~/powersensor'
scp -r app.py index.html static powersensor.service utkarsh@raspberrypi:~/powersensor/
```

On the Pi, try it in the foreground first:

```bash
cd ~/powersensor && python3 app.py
# expect: "polling http://192.168.1.249/ every 5.0s, serving on http://0.0.0.0:8080"
# open http://raspberrypi:8080 and check it says "Power is ON", then press Ctrl+C
```

Install it as a service, so it starts on boot and restarts if it crashes:

```bash
sudo cp ~/powersensor/powersensor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now powersensor
```

If your username or path differs from `utkarsh` / `/home/utkarsh/powersensor`, edit
`User=`, `WorkingDirectory=` and `ExecStart=` in the unit file first.

## 2. Configuration

All settings are environment variables. Defaults are shown below.

| Variable | Default | Meaning |
|---|---|---|
| `SENSOR_URL` | `http://192.168.1.249/` | Sensor endpoint |
| `SENSOR_MATCH` | `Mains On` | Text in the reply that means power is on (case-insensitive) |
| `POLL_INTERVAL` | `5` | Seconds between checks |
| `REQUEST_TIMEOUT` | `3` | Seconds before a check counts as failed |
| `FAIL_THRESHOLD` | `3` | Consecutive failed checks before an outage is declared (3 × 5 s ≈ 15 s) |
| `HEARTBEAT_INTERVAL` | `60` | Seconds between "monitor alive" timestamps |
| `GAP_THRESHOLD` | `180` | If the app was down longer than this, the gap is logged as a likely outage |
| `DB_PATH` | `./power.db` (next to `app.py`) | SQLite file |
| `HOST` | `0.0.0.0` | Listen address. Use `127.0.0.1` to allow only a tunnel on the same Pi (see §4) |
| `PORT` | `8080` | Listen port |
| `SITE_NAME` | `Power Status` | Page title and header |

To change them, use a systemd override rather than editing the unit file, so your
changes survive upgrades:

```bash
sudo systemctl edit powersensor
```

```ini
[Service]
Environment=SITE_NAME=Green Park Power
Environment=FAIL_THRESHOLD=4
```

```bash
sudo systemctl restart powersensor
```

> **Sensor IP:** give the sensor a DHCP reservation in your router so
> `192.168.1.249` never changes. If it does change, every check fails and the
> dashboard reports a permanent outage.

## 3. Day-to-day operation

```bash
systemctl status powersensor           # running? since when?
journalctl -u powersensor -n 50        # recent events
journalctl -u powersensor -f           # follow live
sudo systemctl restart powersensor     # restart (safe at any time)
```

Typical log lines:

```
INFO polling http://192.168.1.249/ every 5.0s, serving on http://0.0.0.0:8080
INFO power outage detected
INFO power restored
INFO monitor was offline for 412s, recording likely outage
```

### Health check

`/healthz` only proves the web server is up. To check that polling works, look at
how old the last sensor check is:

```bash
curl -s localhost:8080/api/status | python3 -c \
 'import sys,json,time; s=json.load(sys.stdin); print(s["state"], "- last check", int(time.time())-(s["checked"] or 0), "s ago")'
# healthy: "on - last check 3 s ago" (should stay under ~10 s)
```

### Maintenance windows

If the app is stopped for **more than 3 minutes**, the next start records that gap as
a likely outage (shown as "Monitor was offline · start time approximate"). Quick
restarts are fine. After longer planned maintenance while power was actually on,
delete that row (see §6).

## 4. Publishing on the internet (no public IP / CGNAT)

Your ISP doesn't give you a public IP, so port forwarding is impossible. All the
options below use an **outbound tunnel**: the Pi connects out to a service, and
visitors reach the Pi through it. No router changes are needed.

| Option | Cost | URL people get | Effort | Notes |
|---|---|---|---|---|
| **Tailscale Funnel** | Free | `https://raspberrypi.<your-tailnet>.ts.net` | ~10 min | No domain needed. HTTPS included. Best zero-cost choice |
| **Cloudflare Tunnel + own domain** | Free tunnel + domain (often ~$1–3 first year for cheap TLDs, ~$10/yr for `.com` at renewal) | `https://power.yourdomain.com` | ~20 min | Nicest URL, CDN caching, DDoS protection, analytics. **Recommended if you'll pay for a domain** |
| Cloudflare quick tunnel | Free | Random `https://xyz.trycloudflare.com` that **changes on every restart** | 1 min | Demos only, no uptime guarantee |
| Small VPS + reverse SSH / WireGuard | ~$0 (Oracle Cloud Always Free) to ~$4–6/month | VPS IP or your domain | 1–2 h | Full control, but you maintain a second server. Only if the tunnels above don't suit you |
| ngrok free tier | Free | One static `*.ngrok-free.app` domain | 10 min | Shows a warning page before the site on first visit, which confuses visitors. Not recommended |

All of these provide HTTPS; the app itself only speaks HTTP on the LAN. The page uses
relative URLs, so it works behind any of them without changes.

### Option A: Tailscale Funnel (free, no domain)

```bash
curl -fsSL https://tailscale.com/install.sh | sh      # supports armv7
sudo tailscale up                                     # opens a login link; sign in
sudo tailscale funnel --bg 8080                       # publish port 8080 publicly
```

- The first `funnel` command prints a link to enable HTTPS and Funnel for your
  tailnet in the Tailscale admin console. Approve it and run the command again.
- `tailscale funnel status` shows the public URL. The setting persists across reboots.
- To rename the URL, change the machine name in the Tailscale admin console (for
  example `power`).
- Turn it off with `sudo tailscale funnel --https=443 off`.

### Option B: Cloudflare Tunnel with your own domain

1. Buy a domain (any registrar; Cloudflare Registrar sells at cost) and add it to a
   free Cloudflare account.
2. In the Cloudflare dashboard, go to **Zero Trust → Networks → Tunnels → Create a
   tunnel** and choose **Cloudflared**. Name it `power`.
3. Choose **Debian / 32-bit ARM (armhf)**. The dashboard shows an install command and
   a `cloudflared service install <TOKEN>` command; run both on the Pi.
4. Add a **public hostname**: `power.yourdomain.com` → service `HTTP`, URL
   `localhost:8080`. Save.

Cloudflare will cache `/static/echarts.min.js`, which is marked immutable for a year,
at its edge, so the Pi barely serves the heavy file. Status and API responses are
`no-store`, so they're always live.

### Hardening once a tunnel is in place

- If nobody needs the LAN URL, add `Environment=HOST=127.0.0.1` (see §2). The app is
  then only reachable through the tunnel.
- The app is read-only with no login and exposes no write endpoints, so nothing
  further is required.
- With Cloudflare you can add a rate-limiting rule as cheap insurance against
  scrapers.

### Keeping the site up during outages

Visitors need the site most during an outage. If the Pi, router or fibre/ONT box lose
power, the site goes offline exactly then, wherever it's hosted. To avoid that:

- Put the **router, ONT and Pi** on an inverter or a small DC mini-UPS for routers.
  The Pi draws only a few watts.
- Even with backup power, the dashboard can't tell "mains off" from "router off" (see
  ARCHITECTURE.md). A UPS on the router also keeps your outage data accurate.

## 5. Backups and restore

The database is the only state. Everything else can be re-copied from the repo.

**Manual backup.** This is safe while the app runs and gives a consistent snapshot:

```bash
sqlite3 ~/powersensor/power.db ".backup '/home/utkarsh/power-$(date +%F).db'"
```

Without the `sqlite3` CLI:

```bash
python3 -c "import sqlite3; s=sqlite3.connect('/home/utkarsh/powersensor/power.db'); d=sqlite3.connect('/home/utkarsh/power-backup.db'); s.backup(d)"
```

> Don't just `cp power.db` while the service runs: recent changes may still be in
> `power.db-wal`.

**Nightly backup**, keeping 30 days (`crontab -e`):

```cron
0 3 * * * mkdir -p ~/backups && sqlite3 ~/powersensor/power.db ".backup '$HOME/backups/power-$(date +\%F).db'" && find ~/backups -name 'power-*.db' -mtime +30 -delete
```

This deletes old *backup copies* only, never data in the live database. Also copy
backups off the SD card now and then (`scp` to another machine, or `rclone` to cloud
storage); SD cards do fail.

**Restore**

```bash
sudo systemctl stop powersensor
cd ~/powersensor
mkdir -p old && mv power.db* old/          # keep the current files, just in case
cp ~/backups/power-2026-10-20.db power.db
sudo systemctl start powersensor
```

## 6. Correcting data

History is stored losslessly in two tables (details in ARCHITECTURE.md):

- **`readings`:** the raw sensor log. **Don't edit it.** It is the ground truth.
- **`outages`:** the interpreted outages the dashboard displays. It is safe to
  correct. For example, delete a period when only the router was down, or a
  maintenance gap recorded as a likely outage.

Take a backup first (§5), then:

```bash
sqlite3 ~/powersensor/power.db
```

```sql
-- list the latest outages in local time
SELECT id, datetime(start,'unixepoch','localtime') AS start,
       datetime(end,'unixepoch','localtime') AS end,
       (end-start)/60 AS minutes, inferred
FROM outages ORDER BY start DESC LIMIT 20;

-- remove a false outage
DELETE FROM outages WHERE id = 123;

-- see the raw sensor log for one day (useful for checking a disputed outage)
SELECT datetime(ts,'unixepoch','localtime'), ok FROM readings
WHERE ts BETWEEN strftime('%s','2026-10-17','utc') AND strftime('%s','2026-10-18','utc');
```

Then restart the app (`sudo systemctl restart powersensor`) so its cache and every
open browser pick up the change straight away. Without a restart, browsers catch up
within about 5 minutes. Don't delete the open row (`end IS NULL`) during an ongoing
outage.

## 7. Upgrading

```bash
# from the repo machine
scp -r app.py index.html static utkarsh@raspberrypi:~/powersensor/
ssh utkarsh@raspberrypi 'sudo systemctl restart powersensor'
```

- **Schema changes:** new tables or indexes are created automatically on start
  (`CREATE … IF NOT EXISTS`). Existing data is never rewritten.
- **Browser caches:** a new `static/echarts.min.js` changes the asset version in the
  page, so browsers fetch the new bundle automatically.
- **Changing chart types:** this needs a bundle rebuild on any machine with Node:
  `cd tools/echarts && npm install && npm run build`. Then commit and copy
  `static/echarts.min.js`.

## 8. Clock

The Pi has no battery-backed clock. After a power cut it boots with the last saved
time and corrects it once NTP syncs, which needs internet. Timestamps written before
sync can be off, mainly the end of a "likely outage" recorded at startup.

- Check sync: `timedatectl` should show `System clock synchronized: yes`.
- If the internet often comes back much later than power, consider a cheap RTC module
  (e.g. DS3231, about $2–5), or correct affected rows as in §6.
- Making the service wait for time sync (`After=time-sync.target` together with
  `systemd-time-wait-sync`) is possible, but **not recommended**. If the internet is
  down, the dashboard wouldn't start at all.

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Page shows "Status unavailable · Monitor last checked … ago" | Poller stuck or app overloaded | `sudo systemctl restart powersensor`; check the logs |
| Page shows "Can't reach the status server" | Pi, app or tunnel down, or the visitor is offline | `systemctl status powersensor`; check the tunnel (`tailscale funnel status` / Cloudflare dashboard) |
| Permanent "Power is OUT" while power is on | Sensor IP changed or sensor reply text changed | `curl -i http://192.168.1.249/` from the Pi; it should print `Mains On`. Fix `SENSOR_URL` / `SENSOR_MATCH` |
| Outage recorded when power was fine | Wi-Fi/router hiccup longer than ~15 s, or a maintenance stop over 3 min | Delete the row (§6). Raise `FAIL_THRESHOLD` if Wi-Fi is flaky |
| Charts stay on "Loading chart…" | `static/` folder missing on the Pi | Copy `static/` again and restart |
| `Address already in use` in the logs | Another process is on port 8080 | `sudo ss -ltnp | grep 8080`; change `PORT` or stop the other process |
| `sqlite3.OperationalError: database is locked` | A manual `sqlite3` session holds a write transaction open | Finish or exit that session |
| Wrong times on the page | Visitor's phone time zone, or the Pi clock (see §8) | Times show in each visitor's own time zone by design |

## 10. Uninstall

```bash
sudo systemctl disable --now powersensor
sudo rm /etc/systemd/system/powersensor.service && sudo systemctl daemon-reload
# keep or archive ~/powersensor/power.db before deleting the directory
```
