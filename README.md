# APRS Coverage

A public map of real APRS RF coverage in SW Idaho, built from the packets of a
short list of registered mobile stations.

Live site: https://apps.k7swi.org/aprs-coverage/

When a registered mobile's position packet reaches APRS-IS over RF, the small
square it was sent from is marked as covered. Over time, drives fill in a map of
where the local igates and digipeaters can actually hear a mobile.

![Coverage map colored by the igate/digi that heard each square (demo data)](docs/screenshot.jpg)

*Screenshot uses synthetic demo data.*

## Features

- **Read-only APRS-IS feed.** Logs in with passcode `-1`, so the coverage feed never sends anything.
  Only the admin Node config tab sends, on its own connection (below).
  It filters for the registered stations only and ignores all other traffic.
  Trackers that report as APRS objects are registered as `SENDER:OBJECT` (`K7SWI:CAR01`).
- **RF only.** Packets that entered APRS-IS over the internet (`qAC`, APRSdroid over
  TCP, and so on) are dropped, because they say nothing about RF coverage.
- **Credit goes to the station that heard the mobile.** If a packet went through a
  digipeater, the square is credited to the first digi that heard it, not the igate
  that gated it.
- **Data-quality filters.** Drops ambiguous and 0,0 positions, duplicates, impossible
  speed jumps, and (optionally) anything outside a set region. Rejects are listed
  on the admin page with the reason and kept for 2 days.
- **Every station that heard a packet** (optional). APRS-IS passes on only the first copy
  of a packet. If your igates connect to your own aprsc, the app can also read its
  duplicates feed, so a packet heard by three igates credits all three. Set up on the
  admin page; see [Duplicates feed](#duplicates-feed-your-own-aprsc).
- **History.** Every accepted packet is stored in SQLite, so coverage can be viewed
  for any time range, not just what APRS-IS still holds.
- **Map.**
  - OpenStreetMap, OpenTopoMap, USGS Topo and USGS Imagery basemaps
  - Square size from 100 m to 2 km, adjustable overlay opacity
  - Color squares by igate/digi, by reporting mobile, or one color for overall coverage
  - Shade squares by packet count as a rough measure of signal strength
  - Filter by mobile and by igate/digi
  - Time presets (15 minutes to 12 months, or all time) or a custom range whose end can be "now"
  - Live updates: new packets appear and flash as they arrive
  - "Show my location" puts a dot at your phone or computer's own position (browser
    geolocation, never sent to the server), with an option to keep the map centered on it
  - Stats per igate/digi (squares, farthest distance, packets gated) and per mobile
- **Accounts** (`/aprs-coverage/admin`, linked from the map as *Join / Log in*). Anyone who runs
  an igate, digi or tracker can sign up with their callsign and the devices they run. The account
  and its devices stay pending, off the map and out of the APRS-IS filter, until an admin approves
  them. An approved node owner manages only their own devices (label, color, position, active,
  delete, add more for approval); a device's callsign and type are fixed once approved. Two roles:
  **admin** and **node owner**.
- **Admin pages.** Approve or reject sign-ups and new devices (with a reason the owner sees), manage
  users (role, disable, password reset, remote-config access), register stations directly and
  assign them to an owner, set the APRS-IS servers for the coverage and duplicates feeds, and see
  the connection status of both feeds and recent rejects. Station and feed changes take effect
  immediately, without a restart.
- **Node config tab** (admins, and node owners an admin has ticked "remote config" for, on their
  own nodes only, sending from their own callsign with any SSID). Reads and changes settings on registered
  [LoRa FieldOps](https://github.com/KJ7NYE/LoRa_FieldOps_APRS_Tracker) igates and digis over APRS
  messages (role, tactical callsign, symbol, beacon path, digi mode, beacon rate, GPS source,
  fixed position), using the firmware's CSR/CSU/CSW remote-config protocol, the same one
  CourseSentry uses. Commands are sent from the operator's own callsign, typed on the tab and
  remembered only in that browser; the app calculates its APRS-IS passcode and opens a
  separate, short-lived connection to send. Needs remote config turned on at the node
  (`remotecfg on`) and the node's unlock token for writes. Tokens are never stored or logged.
- **Logs tab** (admin). Live viewer for the APRS-IS traffic (the coverage feed and node-config
  connection line by line, plus duplicates-feed lines from registered stations, each with what
  the ingester did with it), the app's own log, and the accepted
  packet, rejected packet and node-config logs. Live search with highlighting (plain text or
  regex), type filter, pause, auto-scroll, wrap, UTC/local time, clear and download.

See [docs/PLAN.md](docs/PLAN.md) for the design, attribution rules and known limitations.

## How it works

```
APRS-IS (read-only) ----------\
                               > ingester -> SQLite -> FastAPI -> nginx -> Leaflet map
aprsc duplicates feed (opt.) --/
```

It is one Python process (FastAPI + uvicorn, with the APRS-IS readers running as
background tasks) and one SQLite file. Grid squares are computed when the map asks
for them, so the square size can be changed at any time without reprocessing data.

| Path | What it is |
|---|---|
| `app/ingest.py` | APRS-IS clients (coverage and duplicates feeds) and live-update broadcast |
| `app/packets.py` | Packet parsing, route attribution and quality checks (no I/O) |
| `app/grid.py` | Square grid math |
| `app/remotecfg.py` | Remote node config: CSR/CSU/CSW messages, retries, replies |
| `app/logbuf.py` | In-memory APRS-IS traffic and app logs for the admin Logs tab |
| `app/main.py` | Public API, live feed (SSE) and admin API |
| `app/auth.py` | Accounts, roles and sessions |
| `app/manage.py` | Command-line user admin (first admin, password reset, lockout recovery) |
| `static/` | Map and admin pages (plain JS + Leaflet, no build step) |
| `setup.sh`, `update.sh`, `auto-update.sh` | Server install, deploy and cron auto-update |

## Deploying

The scripts target a Debian or Ubuntu server (a Proxmox VM or LXC container works)
with nginx and systemd. The app installs to `/srv/aprs-coverage` and runs as
`www-data` on `127.0.0.1:8077`. nginx serves it as a directory of the existing
`apps.k7swi.org` site: **https://apps.k7swi.org/aprs-coverage/**.

The repo's [nginx-aprs-coverage.conf](nginx-aprs-coverage.conf) is not a whole site.
It is a few `location /aprs-coverage/ ...` blocks that get included in the
`apps.k7swi.org` server block, which already has the HTTPS certificate. nginx strips
the `/aprs-coverage` prefix before passing requests to the app, and the pages use
relative URLs, so the app works under any path.

### Before you start

- `apps.k7swi.org` is already served by nginx over HTTPS on this server.
- `git` and `sudo` are installed. A fresh Debian LXC often has neither:
  `apt install git sudo`.
- Nothing else on the server uses port 8077. If something does, change `PORT` in
  `setup.sh` and the two `proxy_pass` lines in `nginx-aprs-coverage.conf`.
- Nothing else on `apps.k7swi.org` uses the `/aprs-coverage/` path.

### First install

```bash
# 1. Get the code. Keep this checkout; updates are pulled into it.
sudo git clone https://github.com/jeepnjonny/aprs-coverage.git /opt/aprs-coverage-src
cd /opt/aprs-coverage-src

# 2. Install packages and the systemd service, copy the nginx snippet to
#    /etc/nginx/snippets/aprs-coverage.conf, and set the admin password.
sudo bash setup.sh

# 3. Set the APRS-IS login name. Use your callsign with an SSID no other station
#    uses, for example K7SWI-CV. It is read-only, so no passcode is needed.
sudo nano /srv/aprs-coverage/.env
sudo systemctl restart aprs-coverage
```

4. Hook it into the site (one time only). `setup.sh` prints which file holds the
   `apps.k7swi.org` server block. Add this line inside the HTTPS (`listen 443`)
   server block, next to the other apps:

   ```nginx
   include snippets/aprs-coverage.conf;
   ```

   Then check and reload nginx:

   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   ```

Then open https://apps.k7swi.org/aprs-coverage/admin, log in, and register your stations:

- **Mobiles**: an exact callsign-SSID (`K7ABC-9`), or `K7ABC*` to match every SSID.
  Only mobiles create coverage.
- **Igates / digipeaters**: their exact callsign-SSID. Give each one a position, or
  leave it blank and the map uses the position the station beacons.

The admin page shows the APRS-IS connection state. Coverage appears as soon as a
registered mobile is heard on RF. The admin login cookie is limited to
`/aprs-coverage/`, so it is never sent to the other apps on the site.

### Updating

Manually:

```bash
cd /opt/aprs-coverage-src
sudo git pull
sudo bash update.sh
```

`update.sh` copies the code to `/srv/aprs-coverage`, updates Python packages and
restarts the service. If the nginx snippet changed, it copies the new one to
`/etc/nginx/snippets`, checks it with `nginx -t`, and reloads nginx. If the check
fails, it puts the old snippet back. It never touches `.env` or the database in
`/srv/aprs-coverage/data`.

Automatically: `auto-update.sh` checks GitHub and deploys only when there are new
commits. Add it to root's crontab (`sudo crontab -e`) to check every 10 minutes:

```cron
*/10 * * * * /usr/bin/flock -n /tmp/aprs-coverage-update.lock bash /opt/aprs-coverage-src/auto-update.sh >> /var/log/aprs-coverage-update.log 2>&1
```

It refuses to run if the checkout has local edits, so edit `.env` in
`/srv/aprs-coverage`, not in the checkout.

### Running it

```bash
sudo systemctl status aprs-coverage          # is it running?
sudo journalctl -u aprs-coverage -f          # logs, including APRS-IS connects and filter changes
cd /srv/aprs-coverage && sudo -u www-data .venv/bin/python -m app.manage set-password [USER]  # set a password (default user: admin)
cd /srv/aprs-coverage && sudo -u www-data .venv/bin/python -m app.manage list-users           # users, roles, status
cd /srv/aprs-coverage && sudo -u www-data .venv/bin/python -m app.manage set-role USER admin  # make someone an admin
cd /srv/aprs-coverage && sudo -u www-data .venv/bin/python -m app.manage logout-all           # end every session
```

Upgrading from the single-password version turns that password into an admin
account named `admin` and logs everyone out once.

```bash
```

### Duplicates feed (your own aprsc)

APRS-IS forwards only the first copy of each packet. When several igates gate the
same packet, the server drops the later copies (for about 30 s), so the map normally
credits only the igate or digi whose copy arrived first. To see every station that
heard a packet:

1. Run your own [aprsc](https://github.com/hessu/aprsc) server, uplinked to APRS-IS
   with a full feed, and add a duplicates listener to `aprsc.conf`, for example:

   ```
   Listen "Client-Defined Filters" igate    tcp ::0 14580
   Listen "Duplicates"             dupefeed tcp ::0 14501
   Uplink "Core rotate"            full     tcp noam.aprs2.net 10152
   ```

   Only the coverage app needs the duplicates port, so firewall it to that host (or
   listen on `127.0.0.1` if both run on the same machine). See the aprsc
   documentation for the server ID, passcode and uplink settings.
2. Point your igates at your aprsc (the APRS-IS server and port fields in the
   igate settings). aprsc forwards their packets to APRS-IS as usual.
3. On the admin page, under **Feed servers**:
   - Set the coverage feed to your aprsc's filtered port (14580), so both feeds
     come from the same server.
   - Tick **Connect to an aprsc duplicates feed** and enter the server, the
     duplicates port and a login. The login is read-only (passcode -1) and must
     differ from `APRS_LOGIN`, for example `K7SWI-DF`.
   - Save. Both feeds reconnect at once; no restart needed.

How copies are counted:

- Copies with the same source and the same packet content within
  `DUPLICATE_WINDOW_S` are one transmission, whichever path each copy took.
- Each copy is stored as its own row, credited to the station that heard it.
- Squares, mobile packet totals and per-station counts (heard direct, gated)
  count each transmission once. A packet heard by SQB and KJ7NYE-10 counts once
  in the square and once for each of the two stations.
- A copy with the same route as one already stored (for example, on both feeds)
  is rejected as a duplicate.
- The duplicates feed has no filter, so the app checks every line it sends and
  only logs lines from registered stations (tag `dupes` in the Logs tab).

What it can't show: each igate's firmware also drops duplicates. An igate that
hears a mobile directly and again through a digi gates only the first copy, so you
see what each igate gated, not everything it received.

### Configuration

The APRS-IS servers can be changed on the admin page (**Feed servers**); those
settings are kept in the database and override `APRS_SERVER`/`APRS_PORT`. Everything
else lives in `/srv/aprs-coverage/.env`. See [.env.example](.env.example) for the
full list. Restart the service after changing them.

| Setting | Default | Meaning |
|---|---|---|
| `APRS_LOGIN` | (none) | APRS-IS login name, e.g. `K7SWI-CV`. Required. |
| `APRS_SERVER` / `APRS_PORT` | `noam.aprs2.net` / `14580` | Default APRS-IS server (North America pool; `rotate.aprs2.net` is worldwide). Overridden by the admin page's feed settings. The node-config connection always uses this one. |
| `APRS_ENABLED` | `1` | `0` turns the feed off (map still works) |
| `DB_PATH` | `/srv/aprs-coverage/data/coverage.sqlite` | Database file |
| `GRID_REF_LAT` | `43.6` | Latitude that sets square width. Pick once; changing it moves every square. |
| `MAX_SPEED_KMH` | `250` | Faster jumps between fixes are rejected |
| `DUPLICATE_WINDOW_S` | `30` | Same-position repeats within this window are rejected |
| `REGION_BBOX` | (off) | `min_lat,min_lon,max_lat,max_lon`; positions outside are rejected |
| `COOKIE_SECURE` | `1` | `0` only for local testing over plain HTTP |
| `SESSION_DAYS` | `30` | Admin login lifetime |

## Local development

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
$env:COOKIE_SECURE="0"; $env:APRS_LOGIN="N0CALL-CV"
.venv\Scripts\python -m app.manage set-password
.venv\Scripts\python -m uvicorn app.main:app --port 8077
```

Linux / macOS:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
export COOKIE_SECURE=0 APRS_LOGIN=N0CALL-CV
.venv/bin/python -m app.manage set-password
.venv/bin/python -m uvicorn app.main:app --port 8077
```

Then open http://localhost:8077. The database is created at `data/coverage.sqlite`.
Set `APRS_ENABLED=0` to work on the map without connecting to APRS-IS.
