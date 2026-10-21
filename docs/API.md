# HTTP API

The dashboard's server exposes a small, **read-only** JSON API. The web page itself
uses it, and other tools and scripts can too. The machine-readable spec is
[`openapi.yaml`](openapi.yaml) (OpenAPI 3.1). Load it into Swagger UI, Postman,
Insomnia or a client generator.

| Endpoint | What it returns |
|---|---|
| [`GET /api/status`](#get-apistatus) | Current power state, since when, last check, time zone |
| [`GET /api/outages`](#get-apioutages) | Outages in the last *N* days, in a compact form (what the dashboard uses) |
| [`GET /api/outages/range`](#get-apioutagesrange) | Outages overlapping a chosen window of **at most 7 days**, with local times and totals |
| [`GET /api/readings`](#get-apireadings) | The raw sensor log: every On/Off change in a time range |
| [`GET /healthz`](#get-healthz) | `ok` if the web server is up |
| [`GET /`](#get-) | The dashboard page |
| [`GET /static/{file}`](#get-staticfile) | The page's static files (the chart library) |

## Conventions

- **Base URL:** wherever the app runs, e.g. `http://192.168.1.201:8080` on the LAN,
  or your tunnel URL (see ADMINISTRATION.md §4). The page uses relative URLs, so a
  reverse proxy under a sub-path works too.
- **Methods:** only `GET` and `HEAD` (`HEAD` returns the same headers with no body).
  There is no authentication, and nothing can be changed through the API.
- **Timestamps** are **Unix seconds**, which identify an exact moment whatever the
  time zone. Imported history (see ADMINISTRATION.md §9) has millisecond
  fractions, e.g. `1792380529.805`; the app's own records are whole seconds.
  Fields ending in `_local` are the same moment in the Pi's local time, in ISO 8601
  with the UTC offset included (`2026-10-19T08:58:49+05:30`).
- **Caching:** every API response is `Cache-Control: no-store`, so clients always see
  live data. Responses over 512 bytes are gzip-compressed if the client sends
  `Accept-Encoding: gzip`.
- **Errors:** a request the server can't use gets HTTP **400**: JSON from
  `/api/outages/range`, and plain text from `/api/readings`. An unknown path gets
  **404** with the plain text `not found`.
- **Cross-origin:** no CORS headers are sent. `curl`, scripts and server-side code
  can call the API from anywhere, but a web page on *another* site can't read it from
  the browser.

All examples below are real responses from the app, shortened where marked `…`.

---

## `GET /api/status`

The current state. The dashboard polls this every 10 s.

**Request**

```bash
curl http://192.168.1.201:8080/api/status
```

**Response** `200 OK`

```json
{
  "state": "off",
  "since": 1792556598,
  "checked": 1792561066,
  "now": 1792561071,
  "tracking_since": 1743191163,
  "poll": 5.0,
  "v": 3,
  "timezone": "Asia/Kolkata",
  "utc_offset": "+05:30",
  "since_local": "2026-10-21T09:53:18+05:30",
  "checked_local": "2026-10-21T11:07:46+05:30",
  "now_local": "2026-10-21T11:07:51+05:30"
}
```

| Field | Meaning |
|---|---|
| `state` | `on`, `off`, or `unknown` (just started, no decision yet) |
| `since` | When the current state began. When `on`: the end of the last outage (or `tracking_since` if there's never been one). When `off`: the outage's start |
| `checked` | Time of the last sensor check; `null` before the first one. If it's more than about 30 s old, the monitor isn't checking |
| `now` | The server's clock. Clients can use it to correct for a wrong local clock |
| `tracking_since` | Start of the history (the first log line, if old logs were imported) |
| `poll` | Seconds between sensor checks |
| `v` | A counter that changes whenever an outage is added or ends. Re-fetch outages when it changes |
| `timezone`, `utc_offset` | The Pi's time zone (detected automatically) and its current UTC offset |
| `since_local`, `checked_local`, `now_local` | The same moments as local ISO 8601 times; `checked_local` is `null` before the first check |

Is the power on right now? A one-line check:

```bash
curl -s http://192.168.1.201:8080/api/status | python3 -c "import sys,json; s=json.load(sys.stdin); print(s['state'], 'since', s['since_local'])"
# off since 2026-10-21T09:53:18+05:30
```

---

## `GET /api/outages`

Outages from the last `days` days, newest first, in a compact form. This is the
dashboard's own data feed.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `days` | integer | `30` | How many days back. `0` returns **all** history. Clamped to 0–3650; a non-number falls back to 30 |

**Request**

```bash
curl "http://192.168.1.201:8080/api/outages?days=1"
```

**Response** `200 OK`

```json
{
  "days": 1,
  "outages": [
    [1792556598, null, 0],
    [1792510787, 1792511461, 1]
  ]
}
```

Each outage is `[start, end, approximate]`:
- `end` is `null` while the outage is ongoing.
- `approximate` is `1` when the start time was estimated because the monitor itself
  was offline (e.g. the Pi lost power too).

An outage is included if it **ended within** the period, or is still ongoing. At most
20,000 rows are returned. For readable output, or a specific window, use
`/api/outages/range`.

---

## `GET /api/outages/range`

Outages that **overlap** a time window of **at most 7 days**, oldest first. Each one
comes with local times, its full length, and how much of it falls inside the window.
This is the endpoint to use from other tools.

| Parameter | Required | Format |
|---|---|---|
| `from` | yes | Start of the window, inclusive |
| `to` | yes | End of the window, exclusive. Must be after `from` and **no more than 7 days (604,800 s) later** |

Both accept **Unix seconds** (`1792377000`) or **ISO 8601**: `2026-10-19`,
`2026-10-19T08:00`, `2026-10-19 08:00:00`, `2026-10-19T08:00+05:30`, or
`2026-10-19T02:30Z`.
- A date or time **without an offset** is the Pi's local time.
- In a URL, write `+` as **`%2B`**, or `curl` sends it as a space. Easier: let curl
  encode it with `-G --data-urlencode`.

**Request**

```bash
curl -G http://192.168.1.201:8080/api/outages/range \
     --data-urlencode "from=2026-10-19T08:00+05:30" \
     --data-urlencode "to=2026-10-19T14:00+05:30"
```

**Response** `200 OK`

```json
{
  "from": 1792377000.0,
  "to": 1792398600.0,
  "from_local": "2026-10-19T08:00:00+05:30",
  "to_local": "2026-10-19T14:00:00+05:30",
  "timezone": "Asia/Kolkata",
  "max_range_s": 604800,
  "count": 3,
  "total_outage_s": 3707.532,
  "outages": [
    {
      "start": 1792380529.805,
      "end": 1792383633.268,
      "start_local": "2026-10-19T08:58:49+05:30",
      "end_local": "2026-10-19T09:50:33+05:30",
      "ongoing": false,
      "approximate": false,
      "duration_s": 3103.463,
      "in_range_s": 3103.463
    },
    {
      "start": 1792383905.765,
      "end": 1792383946.983,
      "start_local": "2026-10-19T09:55:05+05:30",
      "end_local": "2026-10-19T09:55:46+05:30",
      "ongoing": false,
      "approximate": false,
      "duration_s": 41.218,
      "in_range_s": 41.218
    },
    {
      "start": 1792397905.78,
      "end": 1792398468.631,
      "start_local": "2026-10-19T13:48:25+05:30",
      "end_local": "2026-10-19T13:57:48+05:30",
      "ongoing": false,
      "approximate": false,
      "duration_s": 562.851,
      "in_range_s": 562.851
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `from`, `to` (+ `_local`) | The window actually used, after parsing |
| `count` | Number of outages that overlap the window |
| `total_outage_s` | Seconds without power **inside** the window (sum of `in_range_s`) |
| `outages[].start`, `end` (+ `_local`) | The **whole** outage, even if it began before `from` or ends after `to`. `end` is `null` while ongoing |
| `outages[].duration_s` | Full length of the outage (up to now, if ongoing) |
| `outages[].in_range_s` | The part of it inside the window (and up to now) |
| `outages[].ongoing` | `true` if power is still out |
| `outages[].approximate` | `true` if the start was estimated (the monitor itself was offline) |
| `max_range_s` | The window limit, 604800 (7 days) |

Other examples:

```bash
# a whole local day
curl "http://192.168.1.201:8080/api/outages/range?from=2026-10-19&to=2026-10-20"

# the last 24 hours, with unix seconds
curl "http://192.168.1.201:8080/api/outages/range?from=$(( $(date +%s) - 86400 ))&to=$(date +%s)"
```

**Errors** `400 Bad Request` (JSON)

```bash
curl "http://192.168.1.201:8080/api/outages/range?from=2026-10-01&to=2026-10-09"
```
```json
{"error": "time range too large: 8.00 days requested, at most 7 days (604800 s) allowed; query a longer period in several smaller ranges", "max_range_s": 604800}
```

| Cause | `error` text |
|---|---|
| Window over 7 days | `time range too large: 8.00 days requested, at most 7 days (604800 s) allowed; …` |
| `from` or `to` missing | `missing 'to' (unix seconds or ISO 8601, e.g. 2026-10-14, 2026-10-14T09:00 or 2026-10-14T09:00+05:30)` |
| Not a time | `'from' is not a time: 'yesterday' (…)` |
| `to` not after `from` | `'to' must be after 'from'` |
| Before 1970, after 2100, or not finite | `'from' is out of range: 'inf'` |

To cover more than a week, call it once per week and add the results together.

---

## `GET /api/readings`

The raw sensor log: one row each time the check result **changed**, including blips
too short to count as an outage. This is the most detailed data there is, and
`outages` is derived from it.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `from` | Unix seconds (integer) | `to` − 86400 | Start, inclusive |
| `to` | Unix seconds (integer) | now | End, inclusive |

At most 100,000 rows are returned, oldest first. ISO dates aren't accepted here.

**Request**

```bash
curl "http://192.168.1.201:8080/api/readings?from=1792380000&to=1792390000"
```

**Response** `200 OK`

```json
{
  "from": 1792380000,
  "to": 1792390000,
  "readings": [
    [1792380529.805, 0],
    [1792383633.268, 1],
    [1792383700.67, 0],
    [1792383707.284, 1],
    [1792383905.765, 0],
    [1792383946.983, 1],
    …
  ]
}
```

Each reading is `[ts, ok]`. `ok` is `1` when the sensor answered "Mains On" and `0`
when it didn't. The state holds until the next row. Above, for example, the 7-second
gap from `…700.67` to `…707.284` was a blip: too short to be an outage.

**Error** `400 Bad Request` (plain text): `from/to must be unix seconds`

---

## `GET /healthz`

`200 OK` with the body `ok` (plain text). This only proves the **web server** is up.
To check that sensor checks are happening, look at `checked` in `/api/status` (see
ADMINISTRATION.md §3).

```bash
curl http://192.168.1.201:8080/healthz
# ok
```

## `GET /`

The dashboard (HTML). The current status and full outage history are embedded in the
page, so it renders from a single request. `Cache-Control: no-store`.

## `GET /static/{file}`

The page's static files: currently `echarts.min.js` and `core.js`. They're served
gzip-compressed with `Cache-Control: public, max-age=31536000, immutable`. The page
adds `?v=<hash>` to the URL, so a new version is always fetched fresh. An unknown
file gets **404**.
