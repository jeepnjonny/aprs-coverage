# APRS Coverage Map: Project Plan (draft)

Target host: `apps.k7swi.org` (remote server)

## Goal

Map real-world APRS RF coverage. A grid cell counts as covered when an RF packet
from a registered station at that location reaches APRS-IS. Coverage can be shown
per igate/digipeater, per reporting station, or as a single overall layer. History
is stored locally because APRS-IS only keeps recent data.

## Proposed architecture

```
 APRS-IS (rotate.aprs2.net:14580, read-only passcode -1, server-side filter)
        |
        v
 [ingester]  long-running worker: parse -> validate -> attribute -> store
        |
        v
 [PostgreSQL + PostGIS]  raw packets + pre-aggregated grid cells
        ^
        |
 [web API]  FastAPI: coverage queries (GeoJSON/vector), station list, admin CRUD
        ^
        |
 [frontend]  Leaflet SPA: basemaps, filters, time range, opacity, color modes
        ^
        |
 [Caddy]  TLS (Let's Encrypt) + reverse proxy, apps.k7swi.org
```

All of it runs from one `docker compose` stack on the server.

### Proposed stack
| Layer | Choice | Why |
|---|---|---|
| Ingest | Python 3.12 + `aprslib` (or a small custom parser) | Mature APRS parsing (compressed, Mic-E, uncompressed) |
| API | FastAPI + SQLAlchemy | Async, simple, shares code with the ingester |
| DB | PostgreSQL 16 + PostGIS | Spatial queries, safe for concurrent ingest and reads, easy backups |
| Frontend | Leaflet + vanilla TS/Vite (no heavy framework) | Light, handles tile layers and opacity well |
| Proxy/TLS | Caddy | Automatic HTTPS |
| Deploy | Docker Compose + GitHub Actions (optional) | Reproducible, one-command deploy |

## APRS-IS ingest

- Connect read-only: `user N0CALL pass -1 vers aprs-coverage 0.1 filter ...`
- Build the server-side filter from the registry and reconnect when it changes:
  - `b/CALL1*/CALL2*`: packets *from* registered mobiles (SSID wildcards)
  - `e/IGATE1/IGATE2`: packets *gated by* registered igates (entry station after qAR)
  - `d/DIGI1/DIGI2`: packets *digipeated by* registered digis
- Keep only RF-originated packets: q-construct `qAR`/`qAO`. Drop `qAC`/`qAS`/`qAX`
  (internet-sourced, e.g. APRSdroid over TCP), which say nothing about RF coverage.
- Validation: drop 0,0 positions, ambiguous positions (`!` ambiguity > 0), impossible
  speed jumps, and packets with stale or duplicated timestamps.

### Coverage attribution
For each packet, work out who heard the mobile directly over RF:
- No used digi hops: the **igate** heard it directly.
- Used hops (`DIGI*`): the **first used digi** heard it directly; the igate heard the digi.
- Untraced hops (`WIDE2*` with no callsign) make attribution partial. Flag these.

Store the whole chain so the map can show either "heard direct by" or "any station in path".

## Data model (sketch)

- `stations`: callsign, SSID pattern, type (mobile/igate/digi/fixed), owner, notes,
  fixed lat/lon for igate/digi, active flag
- `packets`: received_at, source call, lat/lon (PostGIS point), raw packet, path,
  igate, first_hop, hop_count, attribution_quality
- `cell_hits` (aggregate): cell_id, resolution, source_call, heard_by, day, count,
  first_seen, last_seen. Updated on ingest so map queries stay fast.
- `admin_users`, `sessions`, `audit_log`

Raw packets stay indefinitely (they are small) so the grid can be rebuilt at any resolution later.

## Map / frontend

- Basemaps: OSM Standard, OpenTopoMap, USGS Topo, USGS Imagery (optional)
- Coverage layer: rectangles per cell, opacity slider, toggle cell borders
- Color modes:
  1. Single color (overall coverage)
  2. By igate/digi (who heard it directly), with a legend
  3. By reporting station (mobile)
  4. (suggested) By packet count or by most recent time heard (heatmap scale)
- Filters: multi-select mobiles, multi-select igates/digis, "direct only" toggle
- Time: presets (24h / 7d / 30d / 1y / all) + custom range, optional playback slider
- Markers for registered igates/digis, with click popups showing stats

## Admin (password protected)

- `/admin` login (Argon2-hashed password, HttpOnly secure session cookie, CSRF, login rate limit)
- CRUD for stations (mobiles, igates, digis) with SSID wildcard support
- Ingest status: connected/disconnected, packets/hour, last packet per station,
  alerts when an igate goes silent
- Actions: rebuild aggregates, export data

## Repo layout (planned)

```
aprs-coverage/
  backend/        FastAPI app + ingester (shared package)
  frontend/       Leaflet SPA (Vite)
  deploy/         docker-compose.yml, Caddyfile, backup scripts
  docs/           this plan, ops notes
```

## Milestones

1. Ingester + DB schema: log filtered RF packets to Postgres
2. Coverage API + basic map (single color, OSM)
3. Admin login + station registry, so the filter is built from the DB
4. Color modes, filters, time selector, topo basemaps, opacity
5. Deploy to apps.k7swi.org (Caddy/TLS, backups, monitoring)
6. Extras: exports, playback, igate health alerts, local TNC ingest

## Known limitations

- **APRS-IS dedupes.** When several igates gate the same packet, APRS-IS passes only
  the first one (about a 30 s window). Per-igate coverage will undercount for igates
  that overlap. Mitigation: optional direct feed from your own igates (Direwolf/KISS/APRS-IS
  client) where dupes are kept.
- No bulk backfill from APRS-IS. History starts at first run. (aprs.fi API is limited
  and rate-restricted.)
