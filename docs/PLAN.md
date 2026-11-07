# APRS Coverage Map: Project Plan

Host: `https://apps.k7swi.org/aprs-coverage/`, a directory of the existing nginx site on a Proxmox Linux guest

## Goal

Map real APRS RF coverage in SW Idaho (Emmett to Nampa, centred on Boise/Meridian).
When an RF packet from a registered mobile reaches APRS-IS, the small grid square it
came from is marked as covered. Squares can be colored as one overall layer, by the
igate/digi that heard the mobile, or by the mobile itself, and shaded by packet count
as a rough measure of signal strength. History is kept locally because APRS-IS
only holds recent data.

## Decisions

| Topic | Decision |
|---|---|
| Whose packets count | Only registered mobiles. All other traffic is ignored. |
| Grid | Fixed squares measured in meters (100 m, 250 m, 500 m, 1 km, 2 km; chosen on the map). No Maidenhead. |
| Map access | Public. The same data is already public on aprs.fi. |
| Admin | One admin account with a password. |
| Stack | Python (FastAPI + aprslib) and SQLite. No Docker, no Postgres. |
| Hosting | systemd service behind nginx, served under `/aprs-coverage/` of the existing `apps.k7swi.org` site through an included location snippet. The prefix is stripped by nginx and the pages use relative URLs. |
| Deploys | `git pull` plus `update.sh`, optionally run by cron through `auto-update.sh`. No GitHub Actions. |
| Backups | None. |
| Local RF feed | None. The server has no radio hardware. |

## Architecture

```
 APRS-IS (rotate.aprs2.net:14580, passcode -1 = read-only)
        |  filter b/<registered mobiles and igates/digis>
        v
 [ingester]  asyncio task inside the web process
        |    parse -> route attribution -> quality checks -> store
        v
 [SQLite, WAL mode]  packets, rejects, stations, settings, sessions
        ^
        |
 [FastAPI]  public map API + live feed (SSE) + admin API, one uvicorn worker
        ^
        |
 [nginx]  existing apps.k7swi.org site (TLS); location /aprs-coverage/ -> 127.0.0.1:8077
        ^
        |
 [browser]  Leaflet map (static/index.html, map.js) and admin page (static/admin.html)
```

There is one process and one database file. The ingester runs inside the web
process, so uvicorn must run with exactly one worker.

## APRS-IS ingest (`app/ingest.py`, `app/packets.py`)

- Logs in read-only as `APRS_LOGIN` with passcode `-1`. This connection never transmits;
  remote node config (below) sends on a separate connection.
- Server-side filter `b/` built from every active registered station. Mobiles may use a
  trailing `*` to match every SSID. When the admin changes the list, the new filter
  is sent live with `#filter`, without reconnecting.
- Packets from registered igates/digis are used only to update their own beaconed
  position for the map markers.
- Packets from registered mobiles must carry a position, or they are ignored.
- Trackers that beacon as APRS objects (more trackers than SSIDs) are registered as
  `SENDER:OBJECT`, e.g. `K7SWI:CAR01` or `K7SWI:CAR*`. The filter subscribes to the
  sender, and only objects with a matching name from that sender count; the packet is
  stored under the `SENDER:OBJECT` key. Plain callsign patterns never match objects.

### Coverage attribution

For each accepted packet, the path is split at the q-construct:

- No used digi hops: the **igate** heard the mobile directly.
- One or more used hops (`CALL*`): the **first used digipeater** heard the mobile directly.
- A used hop that is only a generic alias (`WIDE1*`, `WIDE2*`, a state alias) cannot be
  traced to a station. These packets are stored with `heard_by = NULL` and shown as
  "untraced hop".

The full chain (digis + igate) is stored too, so the map can filter on
"heard directly by" or "anywhere in the path".

### Data-quality filters

A packet is rejected, and logged in `rejects` for the admin page, if:

| Reason | Check |
|---|---|
| `not_rf` | q-construct is not `qAR`/`qAO`, or the path contains `TCPIP`/`TCPXX` (internet-sourced, for example APRSdroid over TCP) |
| `no_qconstruct` | no q-construct in the path |
| `ambiguous` | position ambiguity is set |
| `bad_position` | out of range, or at 0,0 |
| `out_of_region` | outside `REGION_BBOX`, if one is set |
| `duplicate` | same position from the same heard-by station within `DUPLICATE_WINDOW_S` |
| `speed_jump` | implies more than `MAX_SPEED_KMH` from the previous fix (1 km slack) |
| `unparseable` | aprslib could not parse it |

## Storage (`app/db.py`)

- `stations`: callsign (or pattern), kind (`mobile`, `igate`, `digi`, `digi_igate`),
  label, notes, optional fixed color, manual lat/lon, last beaconed lat/lon, active flag.
- `packets`: one row per accepted packet with time, mobile, position, speed, course,
  igate, heard_by, hop count, chain and the raw line. Exact positions are kept, so any
  grid size can be computed when the map asks.
- `rejects`, `settings` (admin password hash and similar), `sessions`.

Grid squares are computed in SQL at query time (`app/grid.py`). Square width is fixed by
`GRID_REF_LAT`, so squares stay the same shape across the region. Changing it moves
every square boundary.

## Map (`static/index.html`, `static/map.js`)

- Basemaps: OpenStreetMap, OpenTopoMap, USGS Topo, USGS Imagery + Topo.
- Coverage squares with an opacity slider, outline toggle and square-size selector.
- Color modes: single color, by igate/digi that heard it, by reporting mobile.
- Shading by packets per square (1, 2–4, 5–11, 12–30, 31+) with a legend.
- Filters: mobiles, igates/digis, "heard directly" vs "anywhere in path".
- Time presets plus a custom range.
- Igate/digi markers with stats popups.
- Live updates through Server-Sent Events (`/api/live`). New packets flash on the map.
- Stats tab: per igate/digi (direct packets, squares, farthest distance, packets gated)
  and per mobile (packets, squares, last heard). Click a row to zoom to it.

## Admin (`/aprs-coverage/admin`)

- Password login: scrypt hash, server-side sessions in an HttpOnly cookie scoped to the
  app's path (from `X-Forwarded-Prefix`), login rate limit.
  Write requests require a header that cross-site forms cannot send.
- Add, edit and remove stations. Toggle active, set label, color and manual position.
- APRS-IS feed status: connected, active filter, server banner, counters, last packet.
- Recent rejected packets with their reasons.
- Change the password from the page, or on the server with `python -m app.manage set-password`.

## Logs (`app/logbuf.py`, admin "Logs" tab)

- APRS-IS traffic: every line to and from both APRS-IS connections (feed and node config),
  with connects and disconnects, and the ingester's outcome for each received packet.
  The application log: this app's Python log records (INFO and up). Both are in-memory
  ring buffers (5000 and 2000 lines) that start empty after a restart.
- Passcodes (other than the feed's `-1`) and CSU unlock tokens are masked before a line is
  stored in either buffer.
- Accepted packets, rejected packets and node-config commands are read from their tables.
- `GET /api/admin/logs/<log>?after=<id>` returns entries newer than an id, so the page polls
  every 2 s for new lines while the tab is open. An `epoch` field changes on restart so the
  page knows the in-memory ids started over.

## Remote node config (`app/remotecfg.py`, admin "Node config" tab)

Reads and changes settings on active registered igates/digis that run the LoRa FieldOps
firmware, with the directed-message protocol in its `src/remote_cfg_utils.cpp`:
`CSR [codes]` reads (no auth), `CSU <token>` opens a timed write window for our login,
`CSW CODE=value,...` writes (all-or-nothing). The node answers with a message `CS ...`.

- Sent from the operator's own callsign, typed on the tab. The page keeps it in the
  browser's localStorage (per browser, across admin logins, not tied to the admin account,
  and not sent with every request as a cookie would be) and sends it with each command.
  The server never stores it. It must look like an amateur callsign, and may not be
  `APRS_LOGIN` or a registered igate/digi: APRS-IS drops the older connection when a
  callsign logs in twice.
- The server calculates the standard APRS-IS passcode (shown read-only on the page too)
  and opens a separate connection as that callsign with filter `g/<callsign>`, so node
  replies arrive on it. A login the server doesn't verify fails the command. The
  connection closes after 5 minutes without a command. The coverage feed is untouched.
- Rate limits, duplicate tracking and unlock windows are kept per (callsign, node),
  because the firmware applies them per sender.
- Commands are sent over APRS-IS. An igate gets them over its own connection; a digi only
  if an igate with IS-to-RF downlink has heard it directly in the last 30 minutes.
- One command in flight at a time. A command is resent every 12 s (same message number,
  at most 3 times) until the node acks it, with a 45 s limit to stay under nginx's 60 s
  proxy timeout. The node's reply is acked.
- The firmware answers `ERR TOOFAST` to commands under 3 s apart, so we wait 3.5 s
  between commands to one node. It also acks but ignores the same text from us within
  60 s, so an exact repeat is refused with a wait time. A second full read is sent as
  `CSR RO,TC,...` instead of `CSR` to get round this.
- Ack without a reply means remote config is off at the node (or the 60 s repeat filter).
- Values are checked against the firmware's rules before sending. Only a one-hop beacon
  path can be written, because the protocol separates fields with commas.
- The last reported values per node (`node_config`) and a log of commands and replies
  (`node_config_log`, tokens masked) are stored. Tokens are never stored.

## Status

Done:

1. Ingester, attribution and quality filters, tested against live APRS-IS traffic
2. SQLite storage
3. Public map with every feature listed above
4. Admin page and station registry
5. Deploy scripts: `setup.sh`, `update.sh`, `auto-update.sh`, nginx location snippet
6. Unit and API tests (`pytest`)

Next:

- Deploy to apps.k7swi.org and register the real stations

Later, maybe:

- Alerts when a registered igate/digi goes silent
- Mobile tracks
- Data export

## Known limitations

- **APRS-IS drops duplicates.** When several igates gate the same packet, APRS-IS
  passes only the first one (about a 30 s window). Per-igate coverage undercounts
  where igates overlap. Overall coverage is not affected.
- **No backfill.** History starts when the ingester first runs.
- **Untraced hops.** A packet that went through a digi that does not insert its
  callsign cannot be credited to that digi.
