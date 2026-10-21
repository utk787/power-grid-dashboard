# Testing

The project has four test suites. None of them need any packages installed: they use
Python's built-in `unittest`, Node's built-in `node:test`, and an optional local
Chrome. No test talks to the real sensor or the internet. Everything runs against
throwaway databases and servers on `127.0.0.1`.

| Suite | Tests | What it covers | Tool | File(s) |
|---|---|---|---|---|
| **Backend** | 58 | Config file loading, SQLite store, outage state machine, sensor check, HTTP API | `python3 -m unittest` | `tests/test_app.py` |
| **Frontend logic** | 26 | Date bucketing, stats and chart data in `static/core.js`, including time zone and DST cases | `node --test` | `tests/frontend/core.test.js` |
| **Backfill tool** | 14 | `tools/backfill_logs.py`: log parsing, merging, outage rules, safe and repeatable import, short write locks, SQL file = direct import | `python3 -m unittest` | `tests/test_backfill.py` |
| **Browser smoke** | 6 | The real page in headless Chrome, desktop and phone layouts: every chart draws, no JS errors, correct status text, phone tabs | `python3 -m unittest` + Chrome | `tests/test_smoke.py` |

## Requirements

| Needed for | Version | Check |
|---|---|---|
| Backend tests | Python 3.10+ | `python3 --version` |
| Frontend logic tests | Node.js 18+ (has `node:test`) | `node --version` |
| Browser smoke tests | Google Chrome or Chromium (optional; the tests skip if it's missing) | `google-chrome --version` / `chromium --version` |

## Running the tests locally

From the repository root:

```bash
tests/run.sh
```

This runs all four suites and exits non-zero if anything fails. If Node isn't
installed, the frontend logic tests are skipped with a message. If Chrome isn't
found, the smoke tests are reported as skipped.

### Running one suite

```bash
python3 -m unittest tests.test_app -v        # backend only (fast, ~2 s)
python3 -m unittest tests.test_app.ConfigTests -v   # just the config-file tests
node --test tests/frontend/                  # frontend logic only (<1 s)
python3 -m unittest tests.test_backfill -v   # backfill tool (<1 s)
python3 -m unittest tests.test_smoke -v      # browser smoke tests (~10 s)
python3 -m unittest discover -s tests -v     # backend + smoke together
```

### Running one test

```bash
python3 -m unittest tests.test_app.MonitorTests.test_outage_starts_at_first_failed_check_after_threshold
node --test --test-name-pattern="weekHour" tests/frontend/
```

### Choosing a Chrome binary

The smoke tests look for `google-chrome`, `google-chrome-stable`, `chromium`,
`chromium-browser`, and the standard macOS app paths. To use a specific binary:

```bash
CHROME=/usr/bin/chromium python3 -m unittest tests.test_smoke -v
```

On Linux, the tests pass `--no-sandbox` automatically, which most containers and CI
runners need.

### Running on the Raspberry Pi

The backend tests are worth running on the Pi after an upgrade, because they prove
the Pi's Python works with the app:

```bash
cd ~/powersensor && python3 -m unittest tests.test_app -v
```

This needs the `tests/` folder copied to the Pi (`scp -r tests …`). The app itself
doesn't use it. The frontend and browser suites are meant for your development
machine.

## What the tests cover

### Backend (`tests/test_app.py`)

- **Config file:**
  - The shipped `config.ini` lists every setting and matches the built-in defaults
    (except `site_name`).
  - Values come back with the right types, and missing keys or sections keep their
    defaults.
  - Keys are case-insensitive and whitespace is trimmed.
  - `%` is allowed in values.
  - Relative, absolute and `~` paths for `db_path` resolve correctly.
  - These all raise a clear error: unknown sections or keys (typos), wrong types,
    out-of-range values, an empty `match` or `site_name`, a URL without `http://`,
    a `[DEFAULT]` section, a key outside any section, duplicate keys, and a missing
    file.
  - `main()` exits with code 2 on a bad or missing config, and with a good config it
    binds the configured host and port, creates the database at `db_path`, and uses
    `site_name`.
- **Store:**
  - The schema exists and the database runs in WAL mode.
  - Outage start and end come back to the exact second.
  - The `since` filter and newest-first ordering are correct.
  - The cache version only changes when an outage changes.
  - Raw readings come back in order, with inclusive time bounds.
  - Hundreds of outages and readings survive a close and reopen unchanged, so
    history is never compressed or merged.
- **State machine (`Monitor.record`):**
  - Failures below the threshold don't create an outage.
  - An outage starts at the *first* failed check once the threshold is reached.
  - One success ends an outage, and a success resets the failure count.
  - The threshold setting is respected.
  - Every result change is logged in `readings`, including short blips.
  - Readings carry on correctly from a previous run.
  - The heartbeat is written once per interval.
  - A restart after a long gap records an approximate (`inferred`) outage; a short
    gap records nothing.
  - A restart in the middle of an outage continues it.
  - The status snapshot has all its fields. Its local ISO times (`*_local`) carry a
    UTC offset and match the unix timestamps exactly. `utc_offset` is well-formed,
    and a known instant formats as `2026-10-21T09:53:18+05:30` in Asia/Kolkata.
  - The time zone name is found from `TZ`, `/etc/timezone`, the `/etc/localtime`
    link, or the abbreviation fallback.
  - The polling loop runs and stops promptly, and it survives a "database is locked"
    error: the next check retries the write.
- **Sensor check:** `Mains On` counts as power (case-insensitive, matched anywhere
  in the reply). Other text, HTTP errors, timeouts and an unreachable sensor all
  count as no power.
- **HTTP:**
  - Every placeholder in the page is filled, `core.js` is inlined, and the boot data
    is correct.
  - The site name is HTML-escaped.
  - Responses are gzipped only when the browser accepts it.
  - The status, outages (`days` handling and bounds) and readings endpoints work,
    and a bad request gets a 400.
  - The outage cache refreshes when a new outage appears.
  - Static files are served pre-compressed with a one-year cache.
  - Unknown paths and path-traversal attempts get a 404.
  - `HEAD` returns no body, and `/healthz` works.

### Frontend logic (`tests/frontend/core.test.js`)

The tests run in the **Asia/Kolkata (UTC+5:30)** time zone by default. A half-hour
offset exposes any code that rounds to UTC hours instead of local ones; that bug was
found and fixed when these tests were written. The DST cases switch to
America/New_York.

- **Formatting:** duration text (`dur`) and uptime percentages (`pct`), including
  rounding edge cases.
- **Overlaps:** outages clipped to a window, and ongoing outages measured up to now.
- **Buckets:**
  - 15-minute buckets for windows up to 6 hours (the 3h default) and hourly buckets
    up to 2 days, both aligned to the local clock.
  - Daily buckets that follow the local calendar.
  - Daylight-saving days of 23 and 25 hours.
- **Stats:**
  - Counts, downtime, uptime, average and longest outage, longest stretch with power,
    and average time between outages.
  - Window clipping versus full outage length.
  - Empty windows, and windows that start before history begins.
  - Days with a cut, counted in local days.
- **Chart data:**
  - The step chart's points.
  - Per-bucket counts and downtime, and uptime per bucket, including 0% for a period
    with no power at all (reproduced from a live outage).
  - The timeline: newest day first, midnight splitting, the day limit, and DST days.
  - Calendar month ranges.
  - Weekday × hour minutes, including the UTC+5:30 regression and splits across hours
    and days.
  - Start-hour counts.
  - Duration group boundaries.

### Backfill tool (`tests/test_backfill.py`)

Builds small `power-status*.log` files covering each case seen in the real logs:

- **Parsing:** millisecond and microsecond timestamps, a fixed `--utc-offset`, NUL
  padding, blank lines and garbage lines.
- **Merging:** files are taken in time order whatever their names. An exact
  duplicate file is skipped, and two loggers running at once merge without repeated
  rows. Only On/Off changes are kept, with exact times.
- **Outages:**
  - The 15 s threshold, with shorter blips kept only as readings.
  - Logs that end during an outage.
  - Outages that continue across files and across gaps in the logs.
- **Import:**
  - Only data from before the app's first start is imported, and an outage running
    at that moment is clipped. The app's own rows are untouched.
  - `tracking_since` moves back and `app_started` is recorded.
  - Running it twice changes nothing.
  - `--dry-run` writes nothing, and a database the app has never run against is
    refused.
- **Locking:** about 48,000 readings are imported with every write transaction
  under 0.5 s.
- **SQL file:** applying the generated file with the `sqlite3` tool, twice, gives
  exactly the same database as the direct import. This test is skipped if `sqlite3`
  isn't installed.

### Browser smoke (`tests/test_smoke.py`)

Starts the real app on seeded data and renders it in headless Chrome. The window is
tall enough that every lazily drawn chart comes into view. Each test checks:

- the range slider and all eight charts drew a canvas
- no chart reported an error (`data-error`) and the console shows none
- no "Loading chart…" placeholder is left
- the right status text and stat tiles are shown

**Desktop layout** (1000 px wide) has three scenarios: power on with 45 days of
history (including an inferred outage), an ongoing outage, and an empty history.

**Phone layout** uses a 500 px window. That's the narrowest headless Chrome allows,
and it's still under the 640 px phone breakpoint.

- **Now tab:** the 3 latest outages and "See all" are shown, and only the on/off
  chart is drawn. Charts on other tabs stay undrawn.
- **Opening `#trends` and `#patterns` directly:** the right tab is selected, and
  exactly that tab's charts draw without errors.
- **`#history`:** the outage list is shown.

Tab taps, the Back button, the ⇔ custom range, "More stats" and tap-a-day were also
checked by hand in emulated iPhone Chrome. The command-line smoke tests can't click.

## How the tests are built

- **Deterministic time.** Backend state-machine tests feed fixed timestamps straight
  into `Monitor.record(ok, ts)`. Frontend functions take `now` as an argument and
  never read the clock.
- **Isolation.** Each backend test gets its own temporary database. Thresholds are
  patched to the defaults, and config tests restore every setting afterwards, so
  your `config.ini` can't change the results. Only one test reads the shipped
  `config.ini`, to check it matches the defaults. Servers bind to port `0` (any free
  port).
- **Testable frontend.** All data logic lives in `static/core.js`: pure functions with
  no DOM, ECharts or clock access. The server inlines it into the page, and Node
  `require()`s the same file. `index.html` only formats text and builds chart
  options.

## Adding tests

- **New backend behaviour:** add to `tests/test_app.py`. Subclass `TempDB` for a
  fresh store, and use the `HttpTests` pattern for new endpoints.
- **New calculations for the page:** put the logic in `static/core.js` as a pure
  function that takes `now` as an argument. Export it, and test it in
  `tests/frontend/core.test.js`. Use `at(y, m, d, h, mi)` for local times and
  `withTZ('Zone/Name', fn)` for time zone specific cases.
- **New chart:** add its name to `CHARTS` in `tests/test_smoke.py` so the smoke test
  checks it draws.

## Troubleshooting

| Problem | Fix |
|---|---|
| `test_smoke` shows `skipped 'Chrome/Chromium not found'` | Install Chrome or Chromium, or set `CHROME=/path/to/binary` |
| Smoke test fails with only `CVDisplayLinkCreateWithCGDisplay` lines in the output | Harmless macOS headless noise; it's filtered out. Any real failure names the chart or console error |
| Smoke test times out on Linux | Make sure `--no-sandbox` is allowed (it's added automatically on Linux), or try `CHROME=chromium` |
| `node: bad option: --test` or `Cannot find module 'node:test'` | Upgrade to Node 18 or newer |
| `ModuleNotFoundError: No module named 'app'` | Run the commands from the repository root |
