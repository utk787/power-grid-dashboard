# Administration guide

Running, publishing, backing up and fixing the Power Status dashboard on the
Raspberry Pi. For how it works internally, see [ARCHITECTURE.md](ARCHITECTURE.md).

## At a glance

| What | Where |
|---|---|
| App directory | `/home/utkarsh/powersensor/` (`app.py`, `index.html`, `static/`) |
| Config file | `/home/utkarsh/powersensor/config.ini` (read once at startup) |
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
scp -r app.py index.html static config.ini powersensor.service utkarsh@raspberrypi:~/powersensor/
```

On the Pi, check `~/powersensor/config.ini`, especially the sensor `url` and
`site_name` (see §2). Then try it in the foreground:

```bash
cd ~/powersensor && python3 app.py
# expect: "loaded config from /home/utkarsh/powersensor/config.ini", then "polling http://192.168.1.249/ every 5.0s, serving on http://0.0.0.0:8080"
# open http://raspberrypi:8080 and check it says "Power is ON", then press Ctrl+C
```

Install it as a service, so it starts on boot and restarts if it crashes:

```bash
sudo cp ~/powersensor/powersensor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now powersensor
```

If your username or path differs from `utkarsh` / `/home/utkarsh/powersensor`, edit
`User=`, `WorkingDirectory=` and `ExecStart=` (including the `--config` path) in the
unit file first.

## 2. Configuration

All settings live in **`config.ini`**, an INI file next to `app.py`. The app reads it
**once at startup**, so restart after editing:

```bash
nano ~/powersensor/config.ini
sudo systemctl restart powersensor
journalctl -u powersensor -n 5      # should say "loaded config from …"
```

The file is commented line by line. Every setting is optional: a missing or
commented-out line uses the default below.

| Section | Key | Default | Meaning |
|---|---|---|---|
| `[sensor]` | `url` | `http://192.168.1.249/` | Sensor address (must start with `http://` or `https://`) |
| | `match` | `Mains On` | Text in the reply that means power is on (case-insensitive, matched anywhere) |
| | `poll_interval` | `5` | Seconds between checks (decimals allowed) |
| | `request_timeout` | `3` | Seconds before a check counts as failed |
| | `fail_threshold` | `3` | Consecutive failed checks before an outage is declared (3 × 5 s ≈ 15 s). Raise it if Wi-Fi is flaky |
| `[monitor]` | `heartbeat_interval` | `60` | Seconds between "monitor alive" timestamps |
| | `gap_threshold` | `180` | If the app was down longer than this many seconds, the gap is logged as a likely outage |
| `[storage]` | `db_path` | `power.db` | SQLite file. A relative path is relative to the config file's folder; `~` is expanded |
| `[server]` | `host` | `0.0.0.0` | Listen address. `127.0.0.1` allows only a tunnel on the same Pi (see §4) |
| | `port` | `8080` | Listen port (1–65535) |
| | `site_name` | `Power Status` in code; `Power Grid Status` in the shipped file | Page title and header |

Example: change the name and tolerate flakier Wi-Fi:

```ini
[sensor]
fail_threshold = 4

[server]
site_name = Green Park Power
```

**Rules**

- Put comments on their own line. A `#` after a value becomes part of the value.
- Key names are case-insensitive and spaces around `=` are ignored. No quotes are
  needed, even if the value contains spaces or `%`.
- A **mistake stops the app** with a message naming the line: an unknown section or
  key (e.g. a typo like `poll_intervall`), a value of the wrong type, or a value out
  of range. Nothing is silently ignored. The process exits with code 2, and systemd
  doesn't keep restarting it (`RestartPreventExitStatus=2`). Fix the file, then run
  `sudo systemctl start powersensor`. The error is in
  `journalctl -u powersensor -n 5`.
- **Another location:** `python3 app.py --config /path/to/file.ini`. The service unit
  passes `--config` explicitly. If no `--config` is given and there's no `config.ini`
  next to `app.py`, the app logs a warning and runs on built-in defaults.
- Environment variables are **not** read. Older setups that used
  `Environment=SITE_NAME=…` lines or a `systemctl edit` override must move those
  values into `config.ini`. Remove the old override with
  `sudo systemctl revert powersensor`.

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
| **Oracle Cloud VM + reverse SSH tunnel** (Option C) | Free (Always Free tier) + a free DuckDNS name or your domain | `https://yourname.duckdns.org` | ~1 h | Full control. When home is offline it shows a "status unavailable" page. You maintain a second server. Ready-made files in `deploy/` |
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

### Option C: Oracle Cloud VM (reverse SSH tunnel)

**The app stays on the Pi:** the sensor is on the home network, which a cloud server
can't reach, and running it there would also count home internet drops as outages.
The VM is only the public front door:

```
visitors ─HTTPS─▶ Oracle VM: Caddy ─▶ 127.0.0.1:18080 ═ reverse SSH tunnel ═▶ Pi :8080
```

- **Tunnel:** the Pi opens the SSH connection *outward* and keeps it open (as the
  systemd service `powersensor-tunnel`), so CGNAT doesn't matter.
- **HTTPS:** Caddy on the VM gets and renews the certificate itself.
- **Home offline:** if the Pi, or the power or internet at home, is down, visitors
  see a short "Status unavailable" page, and API calls get a JSON `503`.
- **Files:** `deploy/oracle/` (setup script, Caddyfile, offline page) and
  `deploy/pi/powersensor-tunnel.service`.

**1. Create the VM** (Oracle Cloud console → Compute → Instances → Create):
- **Shape:** an *Always Free* shape. `VM.Standard.E2.1.Micro` (AMD) is plenty for
  this. Ampere A1 works too, but is often "out of capacity" in busy regions.
- **Image:** Ubuntu 22.04 or 24.04.
- **SSH key:** add *your computer's* public key, so you can log in as `ubuntu`.
- **Note** the instance's **public IP**.

**2. Open ports 80 and 443** in Oracle's cloud firewall: Networking → Virtual cloud
networks → your VCN → Security Lists → Default → **Add Ingress Rules**. Add source
`0.0.0.0/0`, protocol TCP, destination port `80`, then the same again for `443`. (The
VM's own firewall is opened by the setup script in step 5.)

**3. Give it a name.** HTTPS certificates need a domain name.
- **Free:** sign in at [duckdns.org](https://www.duckdns.org), add a subdomain (e.g.
  `power-greenpark`), and set its IP to the VM's public IP.
- **Your own domain:** add an `A` record pointing to the VM's IP.

**4. On the Pi, create the tunnel key** (no passphrase, used only for this tunnel):

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/powersensor_tunnel -C powersensor-tunnel
cat ~/.ssh/powersensor_tunnel.pub        # copy this line for step 5
```

**5. Set up the VM.** From your computer, in the repo folder:

```bash
scp -r deploy/oracle ubuntu@<VM_IP>:~/powersensor-oracle
ssh ubuntu@<VM_IP>
cd ~/powersensor-oracle
sudo ./setup-vm.sh power-greenpark.duckdns.org "ssh-ed25519 AAAA...  powersensor-tunnel"   # the line from step 4
```

The script is safe to run again. It:
- installs Caddy and opens 80/443 in the VM's firewall;
- creates a `tunnel` user for the Pi's key. sshd lets it do **only** one thing:
  publish the Pi's dashboard on `127.0.0.1:18080` (reverse forwarding). Outbound
  forwarding is refused, so a stolen key can't use the VM to reach other hosts, and
  there's no shell or terminal. The script checks sshd's effective settings, and stops
  if the restrictions didn't apply or leaked to other users;
- makes sshd drop dead tunnel sessions within about 90 s;
- installs the site and checks the config.

**6. Start the tunnel on the Pi** (after `git pull` in the app folder):

```bash
cd /usr/local/smart-home/power-grid-dashboard
sed "s/VM_HOST/<VM_IP>/" deploy/pi/powersensor-tunnel.service | sudo tee /etc/systemd/system/powersensor-tunnel.service
sudo systemctl daemon-reload && sudo systemctl enable --now powersensor-tunnel
journalctl -u powersensor-tunnel -n 20     # no errors = connected
```

If your Pi username isn't `utkarsh`, edit `User=` and the key path in the unit first.

**7. Check**
- **Through the tunnel:** on the VM, `curl -s 127.0.0.1:18080/healthz` prints `ok`.
- **Publicly:** `https://power-greenpark.duckdns.org` shows the dashboard. Phones get
  the app-style layout and the API works the same (`/api/status`, …).
- **Offline page:** `sudo systemctl stop powersensor-tunnel` on the Pi should show
  "Status unavailable" within a few seconds. Start it again afterwards.

**Notes**
- **Recovery after outages:** after a power or internet cut at home, the tunnel
  reconnects by itself once the Pi is back. The Pi retries every 10 s, and the VM
  frees the port from the dead session within about 90 s.
- **Logging:** Caddy keeps no access log (no visitor IPs are recorded), the same as
  the app.
- **Idle reclamation:** Oracle's terms allow it to reclaim Always Free instances it
  considers idle; this VM does very little work. Check the current policy. Upgrading
  the account to Pay-As-You-Go (still free within the Always Free limits) is the
  usual way to avoid this.
- **Updates:** keep the VM patched: `sudo apt update && sudo apt upgrade` now and
  then, or enable `unattended-upgrades`.

### Hardening once a tunnel is in place

- If nobody needs the LAN URL, set `host = 127.0.0.1` under `[server]` in
  `config.ini` (see §2). The app is then only reachable through the tunnel.
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

The database and your `config.ini` are the only state. Everything else can be
re-copied from the repo. Keep a copy of `config.ini` alongside the database backups:
`cp ~/powersensor/config.ini ~/backups/`.

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
# from the repo machine (note: config.ini is NOT copied, so your settings are kept)
scp -r app.py index.html static utkarsh@raspberrypi:~/powersensor/
ssh utkarsh@raspberrypi 'sudo systemctl restart powersensor'
```

- **Config:** don't overwrite the Pi's `config.ini` when upgrading. If a new version
  adds settings, they work with their defaults straight away. To adopt them, compare
  with the repo's `config.ini` (`diff`) and copy over the lines you want.
- **Service unit changes:** if `powersensor.service` changed, copy it to
  `/etc/systemd/system/` again and run `sudo systemctl daemon-reload` before
  restarting.

- **Schema changes:** new tables or indexes are created automatically on start
  (`CREATE … IF NOT EXISTS`). Existing data is never rewritten.
- **Browser caches:** a new `static/echarts.min.js` changes the asset version in the
  page, so browsers fetch the new bundle automatically.
- **Changing chart types:** this needs a bundle rebuild on any machine with Node:
  `cd tools/echarts && npm install && npm run build`. Then commit and copy
  `static/echarts.min.js`.

## 8. Clock and time zone

**Time zone.** Nothing in the app is tied to a particular zone. Stored times are
unix seconds, and the dashboard shows each visitor their own local time. The only
place the machine's zone appears is the `timezone`, `utc_offset` and `*_local`
fields of `/api/status`. The app detects the zone at startup, in this order:

1. The `TZ` environment variable, if set.
2. `/etc/timezone`.
3. The `/etc/localtime` link.
4. The zone abbreviation, e.g. `IST`.

On a new machine, set its zone with `sudo timedatectl set-timezone Area/City` and
check it with `timedatectl`. Restart the service after changing it, because the zone
is read once at startup.

**Clock.** The Pi has no battery-backed clock. After a power cut it boots with the last saved
time and corrects it once NTP syncs, which needs internet. Timestamps written before
sync can be off, mainly the end of a "likely outage" recorded at startup.

- Check sync: `timedatectl` should show `System clock synchronized: yes`.
- If the internet often comes back much later than power, consider a cheap RTC module
  (e.g. DS3231, about $2–5), or correct affected rows as in §6.
- Making the service wait for time sync (`After=time-sync.target` together with
  `systemd-time-wait-sync`) is possible, but **not recommended**. If the internet is
  down, the dashboard wouldn't start at all.

## 9. Importing history from the old logger (backfill)

Before this app, a separate logger script wrote `power-status*.log` files, with lines
like `2025-03-29 01:16:03.639,Mains On`. `tools/backfill_logs.py` imports that
history into `power.db`, so the dashboard shows it alongside the app's own data. It
needs only Python 3.10 and runs on the Pi.

**What it imports**

- **readings:** every On/Off change, with the log's **millisecond** timestamp. Short
  blips are included, so no detail is lost.
- **outages:** every stretch of `Mains Off` lasting at least 15 s (`--min-outage`).
  It starts at the first Off line and ends at the next On line, the same rule the app
  uses. Long stretches are imported like any other; delete any you know were sensor
  problems (§6).
- **The time cutoff:** only data from **before the app first started** is imported.
  That moment is saved in the database as `app_started`, and the app's own records
  are never touched. An outage still running at that moment is cut off there.
- **Messy logs:** duplicate files and two loggers running at once are merged by
  timestamp. Blank padding left by unclean shutdowns is ignored. Gaps where the
  logger wasn't running are not filled in.
- **History start:** afterwards the dashboard's "history since" moves back to the
  first log line.

**Safe while the app runs.** The data is first loaded into a temporary staging table,
which doesn't lock `power.db`. It's then moved in small transactions with a 0.25 s
pause between them, so the app never waits long for the write lock. In tests, the
longest lock held was 0.02 s, and the app kept writing every second throughout.
Re-running is harmless: rows that already exist are skipped.

**Steps** (on the Pi; adjust the paths to where your logs and the app live):

```bash
cd /usr/local/smart-home/power-grid-dashboard
sqlite3 power.db ".backup '/home/utkarsh/power-before-backfill.db'"   # 1. backup (§5)

# 2. dry run: reads the logs and reports what would be imported; writes nothing
python3 tools/backfill_logs.py /path/to/logs/power-status*.log --db power.db --dry-run
```

Then choose one way to apply it:

```bash
# 3a. insert directly (each write transaction is kept under 0.5 s, --max-lock)
python3 tools/backfill_logs.py /path/to/logs/power-status*.log --db power.db

# 3b. or generate an SQL file (about 1 MB) and apply it with the sqlite3 tool
python3 tools/backfill_logs.py /path/to/logs/power-status*.log --sql backfill.sql
sqlite3 power.db < backfill.sql        # prints "backfill done: … outages, … readings"
```

```bash
sudo systemctl restart powersensor     # 4. so every open page picks up the new history
```

Then check the dashboard: choose **All**. The footer should say "history since"
followed by the first log date.

**Notes**

- **Time zone:** log times are read in the time zone of the machine running the
  script. If you run it on a machine set to a different zone, add
  `--utc-offset +05:30` (the zone the logs were written in).
- **Run time:** reading 2.4 GB / 79 million lines took about 35 s on a laptop. On a
  Pi 2 expect several minutes. Only the On/Off changes are kept in memory.
- **The SQL file** batches 500 rows per transaction (`--batch`). If your `sqlite3`
  build rejects the `.shell sleep` lines (the pauses), delete them; the batches
  are still small.
- **Undo:** restore the backup from step 1 (§5). Or delete just the imported rows:
  `DELETE FROM outages WHERE start < (SELECT v FROM meta WHERE k='app_started');`,
  then the same for `readings` using `ts`, then set `tracking_since` back to
  `app_started`.

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Page shows "Status unavailable · Monitor last checked … ago" | Poller stuck or app overloaded | `sudo systemctl restart powersensor`; check the logs |
| Log says `could not record a check; will retry` | Another process held the database write lock for over 10 s (e.g. an open `sqlite3` session with a transaction) | Finish or close that session. The app keeps polling and retries the write on the next check |
| Page shows "Can't reach the status server" | Pi, app or tunnel down, or the visitor is offline | `systemctl status powersensor`; check the tunnel (`tailscale funnel status` / Cloudflare dashboard) |
| Public site (Oracle) shows "Status unavailable" while the Pi is fine | Tunnel down | On the Pi: `systemctl status powersensor-tunnel` and `journalctl -u powersensor-tunnel -n 30`. `remote port forwarding failed` means the VM still holds the old session; it clears within about 90 s and the tunnel retries by itself. `Permission denied (publickey)` means the key in step 5 doesn't match `~/.ssh/powersensor_tunnel.pub` |
| Public site (Oracle) doesn't load at all, or there's no HTTPS certificate | DNS not pointing at the VM, or ports 80/443 closed | Check the DuckDNS/`A` record IP, the VCN security list rules (step 2), and on the VM `sudo journalctl -u caddy -n 50` |
| Service won't start; log says `config.ini: unknown key …` or `… is invalid; expected …` | Mistake in `config.ini` | Fix the named line (§2), then `sudo systemctl start powersensor` |
| Settings change has no effect | Config is only read at startup, or the service uses another file via `--config` | `sudo systemctl restart powersensor`; check the "loaded config from" log line for the path |
| Permanent "Power is OUT" while power is on | Sensor IP changed or sensor reply text changed | `curl -i http://192.168.1.249/` from the Pi; it should print `Mains On`. Fix `url` / `match` under `[sensor]` in `config.ini` |
| Outage recorded when power was fine | Wi-Fi/router hiccup longer than ~15 s, or a maintenance stop over 3 min | Delete the row (§6). Raise `fail_threshold` in `config.ini` if Wi-Fi is flaky |
| Charts stay on "Loading chart…" | `static/` folder missing on the Pi | Copy `static/` again and restart |
| `Address already in use` in the logs | Another process is on port 8080 | `sudo ss -ltnp | grep 8080`; change `port` in `config.ini` or stop the other process |
| `sqlite3.OperationalError: database is locked` | A manual `sqlite3` session holds a write transaction open | Finish or exit that session |
| Wrong times on the page | Visitor's phone time zone, or the Pi clock (see §8) | Times show in each visitor's own time zone by design |

## 11. Uninstall

```bash
sudo systemctl disable --now powersensor
sudo rm /etc/systemd/system/powersensor.service && sudo systemctl daemon-reload
# keep or archive ~/powersensor/power.db before deleting the directory
```
