/* APRS coverage map: public page */
(() => {
  'use strict';

  // Hues kept well apart so no two read as "the same colour" on the map. No
  // reds: red is reserved for the outline of squares where beacons were lost.
  const PALETTE = ['#2f81f7', '#2da44e', '#f2d024', '#8250df', '#e85aad',
                   '#1fb6c9', '#f0883e', '#8b5a2b', '#1f3a93', '#9acd32', '#4b0082'];
  const FAIL_RED = '#d1242f';
  // Reliability: share of beacons that reached APRS-IS. Diverging red (lost) to
  // blue (heard) with a gray middle; checked for colour-blind separation.
  const REL_BINS = [[0.9, '#1a4fb5', '90–100%'], [0.75, '#4a93f0', '75–90%'], [0.5, '#8c8c8c', '50–75%'],
                    [0.25, '#e8853a', '25–50%'], [0, '#c4212b', 'under 25%']];
  // Fewer beacons, less certain: drawn fainter. [min beacons, opacity factor, label]
  const REL_CONF = [[12, 1.0, '12+'], [5, 0.75, '5–11'], [2, 0.5, '2–4'], [1, 0.3, '1']];
  // [min lost beacons, outline opacity, outline width, legend label]
  const FAIL_STEPS = [[12, 1.0, 3, '12+'], [5, 0.85, 2.5, '5–11'], [2, 0.65, 2, '2–4'], [1, 0.45, 1.5, '1']];
  const SHADE = [[31, 1.0, '31+'], [12, 0.82, '12–30'], [5, 0.64, '5–11'], [2, 0.45, '2–4'], [1, 0.28, '1']];
  const UNTRACED = '__untraced__', OTHER = '__other__';
  const $ = id => document.getElementById(id);

  // ── persisted UI state (per-viewer convenience only) ──────────────────────
  const DEFAULTS = {
    preset: '604800', from: null, to: null, toNow: false, color: 'single', singleColor: '#2f81f7',
    shade: true, cell: 1000, opacity: 60, borders: true, stations: true, live: true, me: false, follow: false,
    basemap: 'osm', match: 'first', mobiles: null, heard: null,   // null = all selected
    hw: null, hwScope: 'both',                                    // hardware filter; null = all
    fails: false,                                                 // red outline where beacons were lost
    estimates: true,                                              // reliability: estimate unlogged trackers
  };
  let S = { ...DEFAULTS };
  try { Object.assign(S, JSON.parse(localStorage.getItem('acov-ui') || '{}')); } catch {}
  const save = () => { try { localStorage.setItem('acov-ui', JSON.stringify(S)); } catch {} };

  // ── map + basemaps ────────────────────────────────────────────────────────
  const map = L.map('map', { zoomControl: false, preferCanvas: true });
  L.control.zoom({ position: 'topright' }).addTo(map);
  L.control.scale({ imperial: true, position: 'bottomleft' }).addTo(map);

  const BASEMAPS = {
    osm: () => L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' }),
    otm: () => L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
      maxZoom: 17, attribution: 'Map data &copy; OpenStreetMap contributors, SRTM | Style &copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)' }),
    usgs: () => L.tileLayer('https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}', {
      maxZoom: 16, attribution: 'Tiles courtesy of the <a href="https://usgs.gov/">U.S. Geological Survey</a>' }),
    usgsimg: () => L.tileLayer('https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryTopo/MapServer/tile/{z}/{y}/{x}', {
      maxZoom: 16, attribution: 'Tiles courtesy of the <a href="https://usgs.gov/">U.S. Geological Survey</a>' }),
  };
  let baseLayer = null;
  function setBasemap(key) {
    if (!BASEMAPS[key]) key = 'osm';
    if (baseLayer) map.removeLayer(baseLayer);
    baseLayer = BASEMAPS[key]().addTo(map);
    baseLayer.bringToBack();
  }

  // Squares (canvas, there can be thousands) sit below live flashes and station
  // markers. Those two are few, so they are SVG in their own higher panes: SVG
  // only takes the mouse where something is drawn, so hovering a marker shows
  // the marker's tooltip and hovering anywhere else still reaches the squares.
  // (A canvas on top would swallow every mouse event over the whole map.)
  map.createPane('live').style.zIndex = 420;
  map.createPane('stations').style.zIndex = 430;
  map.createPane('me').style.zIndex = 440;
  const cellRenderer = L.canvas({ padding: 0.3 });
  const liveRenderer = L.svg({ pane: 'live' });
  const stationRenderer = L.svg({ pane: 'stations' });
  const cellLayer = L.layerGroup().addTo(map);
  const stationLayer = L.layerGroup().addTo(map);
  const liveLayer = L.layerGroup().addTo(map);
  const meRenderer = L.svg({ pane: 'me' });

  // ── data ──────────────────────────────────────────────────────────────────
  let CFG = null, STATIONS = [], MOBILES = [], COVERAGE = null, FAILS = null, RELI = null;
  const isRel = () => S.color === 'reliability';
  const infraCalls = () => STATIONS.filter(s => s.kind !== 'mobile').map(s => s.callsign);

  async function getJSON(url) {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error(`${url}: ${r.status}`);
    return r.json();
  }

  function timeRange() {
    const now = Math.floor(Date.now() / 1000);
    if (S.preset === 'all') return { start: 0, end: now };
    if (S.preset === 'custom') {
      const f = S.from ? Math.floor(new Date(S.from).getTime() / 1000) : now - 86400;
      const t = S.toNow || !S.to ? now : Math.floor(new Date(S.to).getTime() / 1000);
      return { start: f, end: t };
    }
    return { start: now - parseInt(S.preset, 10), end: now };
  }
  // "Relative" = the window ends now, so it moves and live packets belong in it.
  const isRelative = () => S.preset !== 'custom' || S.toNow;

  function queryParams(withHeard = true) {
    const { start, end } = timeRange();
    const p = new URLSearchParams({ start, end, cell: S.cell });
    const mobiles = effMobiles(), heard = effHeard();
    if (mobiles !== null) p.set('mobiles', mobiles.join(','));
    if (withHeard) {
      if (heard !== null) p.set('heard_by', heard.join(','));
      p.set('match', S.match);
      p.set('color', isRel() ? 'single' : S.color);
    }
    return p;
  }

  // ── colours ───────────────────────────────────────────────────────────────
  function hashColor(key) {
    let h = 0;
    for (const ch of key) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return PALETTE[h % PALETTE.length];
  }
  // Infrastructure stations each get their own colour: an admin-set colour wins,
  // the rest take palette entries in callsign order, skipping any too close to a
  // colour an admin chose. Past the palette, golden-angle hues keep them apart.
  const rgb = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
  // Same test as is_reddish() on the server. A stored red (set before red was
  // reserved) is treated as no colour, so the station gets a palette colour.
  function isReddish(hex) {
    if (!/^#[0-9a-f]{6}$/i.test(hex || '')) return false;
    const [r, g, b] = rgb(hex).map(v => v / 255);
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2, d = mx - mn;
    if (!d) return false;
    const sat = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
    let h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    h = (h * 60 + 360) % 360;
    return (h >= 340 || h <= 18) && sat >= 0.35 && l >= 0.2 && l <= 0.8;
  }
  const ownColor = s => (s && s.color && !isReddish(s.color) ? s.color : null);
  const singleColor = () => (isReddish(S.singleColor) ? DEFAULTS.singleColor : S.singleColor);
  const near = (a, b) => { const [p, q] = [rgb(a), rgb(b)]; return Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]) < 60; };
  let colorCache = { src: null, map: new Map() };
  function infraColors() {
    if (colorCache.src === STATIONS) return colorCache.map;
    const out = new Map(), used = [];
    const infra = STATIONS.filter(s => s.kind !== 'mobile')
      .sort((a, b) => a.callsign.localeCompare(b.callsign));
    for (const s of infra) if (ownColor(s)) { out.set(s.callsign, s.color); used.push(s.color); }
    let pi = 0, extra = 0;
    for (const s of infra) {
      if (ownColor(s)) continue;
      let c = null;
      while (pi < PALETTE.length && !c) {
        const cand = PALETTE[pi++];
        if (!used.some(u => near(u, cand))) c = cand;
      }
      while (!c) {  // golden-angle hues, skipping the reserved reds
        const hue = Math.round((extra++ * 137.508) % 360);
        if (hue > 25 && hue < 330) c = `hsl(${hue}, 65%, 45%)`;
      }
      out.set(s.callsign, c);
    }
    colorCache = { src: STATIONS, map: out };
    return out;
  }
  function keyColor(key) {
    if (key === '(untraced)') return '#8b949e';
    const st = findStation(key);  // wildcard / object registrations keep their colour
    if (ownColor(st)) return st.color;
    if (st && st.kind !== 'mobile') return infraColors().get(st.callsign) || hashColor(key);
    return hashColor(key);
  }
  const shadeFactor = n => {
    if (!S.shade) return 1;
    for (const [min, f] of SHADE) if (n >= min) return f;
    return SHADE[SHADE.length - 1][1];
  };

  // ── rendering ─────────────────────────────────────────────────────────────
  function ago(ts) {
    if (!ts) return '—';
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return `${Math.round(s)}s ago`;
    if (s < 5400) return `${Math.round(s / 60)}m ago`;
    if (s < 172800) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  }
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

  function rangeText() {
    if (S.preset === 'custom') {
      const { start, end } = timeRange();
      const d = t => new Date(t * 1000).toLocaleDateString();
      return `${d(start)} – ${S.toNow ? 'now' : d(end)}`;
    }
    const sel = $('preset');
    return sel.options[sel.selectedIndex]?.text || '';
  }

  // Registered callsigns may be wildcards (K7ABC*, K7ABC-?), like the server's fnmatch.
  const globRe = p => new RegExp('^' + p.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.') + '$');
  // As on the server, callsign patterns never match SENDER:OBJECT keys and vice versa.
  const patternMatches = (pattern, call) =>
    pattern.includes(':') === call.includes(':') && globRe(pattern).test(call);
  const findStation = call => STATIONS.find(s => s.callsign === call)
    || STATIONS.find(s => /[*?]/.test(s.callsign) && patternMatches(s.callsign, call));
  // ── hardware filter ───────────────────────────────────────────────────────
  // Narrows the mobile and igate/digi selections to stations whose hardware is
  // ticked. '' = not set (also unregistered stations and untraced hops).
  const NOT_SET = '';
  const hwOf = call => { const st = findStation(call); return st ? st.hardware || NOT_SET : NOT_SET; };
  const hwName = k => k ? ((CFG && CFG.hardware && CFG.hardware[k]) || k) : 'Not set';
  const hwOn = scope => S.hw !== null && (S.hwScope === 'both' || S.hwScope === scope);
  const hwPass = call => S.hw.includes(hwOf(call));
  const allHeard = () => [...infraCalls(), OTHER, UNTRACED];
  // What actually goes to the server: null = no filter at all.
  function effMobiles() {
    if (!hwOn('mobiles')) return S.mobiles;
    return (S.mobiles ?? MOBILES).filter(hwPass);
  }
  function effHeard() {
    if (!hwOn('infra')) return S.heard;
    return (S.heard ?? allHeard()).filter(k => k.startsWith('__') ? S.hw.includes(NOT_SET) : hwPass(k));
  }

  // The station's label if it has one, otherwise just the callsign. Use this
  // wherever a station is shown; the callsign goes in a hover title instead.
  const stationName = call => {
    if (!call || call === '(untraced)') return 'Untraced hop';
    const st = findStation(call);
    return st && st.label ? st.label : call;
  };
  // Escaped name for HTML, with the callsign on hover when a label replaced it.
  const nameHtml = call => {
    const name = stationName(call);
    return name === call || !call || call === '(untraced)'
      ? esc(name) : `<span title="${esc(call)}">${esc(name)}</span>`;
  };

  // One "who / how many" list for a square tooltip, largest first.
  function tipList(title, counts, swatches) {
    const list = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    if (!list.length) return '';
    const MAX = 6;
    let html = `<div class="tip-h">${title} (${list.length})</div><table>`;
    for (const [k, c] of list.slice(0, MAX)) {
      const sw = swatches ? `<span class="swatch" style="background:${keyColor(k)}"></span>` : '';
      html += `<tr><td>${sw}${nameHtml(k)}</td><td class="num">${c.toLocaleString()}</td></tr>`;
    }
    html += '</table>';
    if (list.length > MAX) html += `<div class="muted">+${list.length - MAX} more</div>`;
    return html;
  }

  const sizeText = m => m >= 1000 ? `${m / 1000} km` : `${m} m`;
  function cellTip(n, first, last, heard, mobs, fail) {
    const lost = fail ? failTip(fail) : '';
    if (!n) return `<div class="tip-top"><b>No packets heard</b> <span class="muted">· ${sizeText(COVERAGE.cell_m)} square</span></div>${lost}`;
    return `<div class="tip-top"><b>${n.toLocaleString()} packet${n === 1 ? '' : 's'}</b>
        <span class="muted">· ${sizeText(COVERAGE.cell_m)} square</span></div>
      <div class="muted">First ${ago(first)} · last ${ago(last)}</div>${lost}`
      + tipList('Heard by', heard, true)
      + tipList('Mobiles', mobs, false);
  }
  function failTip([failed, sent, lastFail]) {
    return `<div class="tip-fail"><span class="fail-swatch"></span><b>${failed.toLocaleString()} of ${sent.toLocaleString()}</b>
      logged beacon${sent === 1 ? '' : 's'} from here never reached APRS-IS
      <div class="muted">Last lost ${ago(lastFail)}</div></div>`;
  }
  const failStep = n => FAIL_STEPS.find(([min]) => n >= min) || FAIL_STEPS[FAIL_STEPS.length - 1];

  // ── reliability view ──────────────────────────────────────────────────────
  const relBin = pct => REL_BINS.find(([min]) => pct >= min) || REL_BINS[REL_BINS.length - 1];
  const relConf = n => (REL_CONF.find(([min]) => n >= min) || REL_CONF[REL_CONF.length - 1])[1];
  const filtered = () => effHeard() !== null;

  function relTip([, , h, n, eh, en, last], cov) {
    const pct = Math.round(h / n * 100);
    const est = n - en;
    let html = `<div class="tip-top"><span class="swatch" style="background:${relBin(h / n)[1]}"></span>
        <b>${pct}% heard</b> <span class="muted">· ${sizeText(RELI.cell_m)} square</span></div>
      <div>${h.toLocaleString()} of ${n.toLocaleString()} beacon${n === 1 ? '' : 's'} reached APRS-IS${filtered() ? ' via the selected igates / digis' : ''}</div>`;
    if (en) html += `<div class="muted">${eh.toLocaleString()} of ${en.toLocaleString()} from transmit logs (exact)</div>`;
    if (est) html += `<div class="muted">${est.toLocaleString()} estimated from gaps between packets heard</div>`;
    if (n < 5) html += `<div class="muted">Few beacons: treat as a hint, not a verdict</div>`;
    html += `<div class="muted">Last beacon ${ago(last)}</div>`;
    if (cov) html += tipList('Heard by', cov[5], true) + tipList('Mobiles', cov[6], false);
    return html;
  }

  function renderReliability() {
    const { dlat, dlon, cells } = RELI;
    const op = S.opacity / 100;
    const cov = new Map((COVERAGE ? COVERAGE.cells : []).map(c => [`${c[0]},${c[1]}`, c]));
    for (const cell of cells) {
      const [x, y, h, n] = cell;
      const color = relBin(h / n)[1];
      const rect = L.rectangle([[y * dlat, x * dlon], [(y + 1) * dlat, (x + 1) * dlon]], {
        renderer: cellRenderer, color, weight: S.borders ? 0.6 : 0, opacity: S.borders ? Math.min(1, op + 0.2) : 0,
        fillColor: color, fillOpacity: op * (S.shade ? relConf(n) : 1),
      });
      rect.bindTooltip(() => relTip(cell, cov.get(`${x},${y}`)), { className: 'cell-tip', sticky: true });
      cellLayer.addLayer(rect);
    }
    renderLegend(new Map());
    const pct = RELI.sent ? Math.round(RELI.heard / RELI.sent * 100) : 0;
    $('summary').textContent = `${cells.length.toLocaleString()} squares · ${pct}% of ${RELI.sent.toLocaleString()} beacons heard`
      + (RELI.exact ? ` · ${RELI.exact.toLocaleString()} from transmit logs` : '');
  }

  function renderCells() {
    cellLayer.clearLayers();
    if (isRel()) { if (RELI) renderReliability(); return; }
    if (!COVERAGE) return;
    const { dlat, dlon, cells } = COVERAGE;
    const op = S.opacity / 100;
    const seenKeys = new Map();
    const fails = S.fails && FAILS && FAILS.cell_m === COVERAGE.cell_m ? FAILS.byKey : null;
    for (const [x, y, n, last, by, heard, mobs, first] of cells) {
      let color = singleColor(), dominant = null;
      const entries = Object.entries(by).sort((a, b) => b[1] - a[1]);
      if (S.color !== 'single' && entries.length) {
        dominant = entries[0][0];
        color = keyColor(dominant);
        for (const [k, c] of entries) seenKeys.set(k, (seenKeys.get(k) || 0) + c);
      }
      const bounds = [[y * dlat, x * dlon], [(y + 1) * dlat, (x + 1) * dlon]];
      const rect = L.rectangle(bounds, {
        renderer: cellRenderer, color, weight: S.borders ? 0.6 : 0, opacity: S.borders ? Math.min(1, op + 0.2) : 0,
        fillColor: color, fillOpacity: op * shadeFactor(n),
      });
      rect.bindTooltip(() => cellTip(n, first, last, heard, mobs), { className: 'cell-tip', sticky: true });
      cellLayer.addLayer(rect);
    }
    // Lost beacons: a red outline only, drawn over the reception colour so it
    // doesn't change it. Redder and thicker with more lost beacons. Its
    // tooltip covers the square's reception too, since it sits on top.
    if (fails) {
      const byCell = new Map(cells.map(c => [`${c[0]},${c[1]}`, c]));
      for (const [key, f] of fails) {
        const [x, y] = key.split(',').map(Number);
        const [, opacity, weight] = failStep(f[0]);
        const cell = byCell.get(key);
        // Inset a little so neighbouring frames stay separate instead of merging into a grid.
        const ix = dlon * 0.06, iy = dlat * 0.06;
        const rect = L.rectangle([[y * dlat + iy, x * dlon + ix], [(y + 1) * dlat - iy, (x + 1) * dlon - ix]], {
          renderer: cellRenderer, color: FAIL_RED, weight, opacity, fill: true, fillOpacity: 0,
        });
        rect.bindTooltip(() => cell ? cellTip(cell[2], cell[7], cell[3], cell[5], cell[6], f) : cellTip(0, 0, 0, {}, {}, f),
          { className: 'cell-tip', sticky: true });
        cellLayer.addLayer(rect);
      }
    }
    renderLegend(seenKeys);
    const { packets } = COVERAGE;
    $('summary').textContent = `${cells.length.toLocaleString()} squares · ${packets.toLocaleString()} packets`
      + (fails ? ` · ${FAILS.failed.toLocaleString()} lost beacon${FAILS.failed === 1 ? '' : 's'}` : '');
  }

  function renderLegend(keys) {
    const el = $('legend');
    if (isRel()) {
      if (!RELI || !RELI.cells.length) { el.hidden = true; return; }
      let html = `<h3>Share of beacons heard</h3>` + REL_BINS.map(([, c, label]) =>
        `<div class="row"><span class="swatch" style="background:${c}"></span>${label}</div>`).join('');
      if (S.shade) {
        html += `<h3>Beacons per square</h3><div class="scale">` + [...REL_CONF].reverse().map(([, f, label]) =>
          `<div><span style="background:var(--muted);opacity:${(S.opacity / 100) * f}"></span>${label}</div>`).join('') + '</div>';
      }
      el.innerHTML = html;
      el.hidden = false;
      return;
    }
    const hasFails = S.fails && FAILS && FAILS.cells.length;
    if (!COVERAGE || (!COVERAGE.cells.length && !hasFails)) { el.hidden = true; return; }
    const base = S.color === 'single' ? singleColor() : 'var(--muted)';
    let html = '';
    if (S.shade) {
      html += `<h3>Packets per square</h3><div class="scale">` +
        [...SHADE].reverse().map(([, f, label]) =>
          `<div><span style="background:${base};opacity:${(S.opacity / 100) * f}"></span>${label}</div>`).join('') + '</div>';
    }
    if (S.color !== 'single') {
      html += `<h3>${S.color === 'heard_by' ? 'Heard by' : 'Mobile'}</h3>` +
        [...keys.entries()].sort((a, b) => b[1] - a[1]).map(([k]) =>
          `<div class="row"><span class="swatch" style="background:${keyColor(k)}"></span>${nameHtml(k)}</div>`).join('');
    }
    if (hasFails) {
      html += `<h3>Lost beacons per square</h3><div class="scale">` +
        [...FAIL_STEPS].reverse().map(([, o, w, label]) =>
          `<div><span class="fail-box" style="border-width:${w}px;opacity:${o}"></span>${label}</div>`).join('') + '</div>';
    }
    el.innerHTML = html;
    el.hidden = !html;
  }

  const KIND_NAME = { igate: 'Igate', digi: 'Digipeater', digi_igate: 'Digi + igate' };

  function stationCard(s, color, popup = false) {
    const st = (STATS && STATS.infra.find(r => r.callsign === s.callsign)) || null;
    const n = v => (v ?? 0).toLocaleString();
    const row = (k, v) => `<tr><th>${k}</th><td>${v}</td></tr>`;
    let html = `<div class="station-card">
      <div class="sc-head"><span class="swatch" style="background:${color}"></span>
        <b>${esc(s.label || s.callsign)}</b> <span class="muted">${KIND_NAME[s.kind] || ''}${s.hardware ? ' · ' + esc(hwName(s.hardware)) : ''}</span></div>`;
    if (s.label) html += `<div class="sc-label">${esc(s.callsign)}</div>`;
    html += `<div class="sc-range muted">${esc(rangeText())}</div><table>`;
    if (!st) {
      html += row('Stats', STATS ? 'none in this range' : 'loading…');
    } else {
      html += row('Heard direct', `${n(st.heard_direct)} packet${st.heard_direct === 1 ? '' : 's'}`)
        + row('Squares covered', n(st.cells))
        + row('Mobiles heard', n(st.mobiles))
        + row('Max range', st.max_range_km == null ? '—' : popup && st.max_range_at
          ? `<span class="sc-maxrange" title="Hover or tap to show on the map">${st.max_range_km} km</span>`
          : `${st.max_range_km} km`);
      if (s.kind !== 'digi') html += row('Gated to APRS-IS', n(st.gated));
    }
    html += row('Last heard a mobile', ago(s.last_heard))
      + row('Own beacon', ago(s.last_beacon))
      + '</table></div>';
    return html;
  }

  function renderStations() {
    stationLayer.clearLayers();
    if (!S.stations) return;
    for (const s of STATIONS) {
      if (s.kind === 'mobile' || s.lat == null) continue;
      const color = s.color || infraColors().get(s.callsign) || hashColor(s.callsign);
      const m = L.circleMarker([s.lat, s.lon], {
        renderer: stationRenderer,
        radius: s.kind === 'digi' ? 6 : 8, color: '#fff', weight: 2, fillColor: color, fillOpacity: 1,
        dashArray: s.kind === 'digi_igate' ? '3 2' : null,
      });
      // Built when shown, so it always reflects the latest stats and time range.
      m.bindTooltip(() => stationCard(s, color), { className: 'station-tip', direction: 'top', offset: [0, -8] });
      m.bindPopup(() => stationCard(s, color, true));
      m.on('popupopen', e => { m.closeTooltip(); wireMaxRange(e.popup.getElement(), s, color); });
      m.on('popupclose', hideMaxRange);
      stationLayer.addLayer(m);
    }
  }

  // ── max-range highlight: a temporary line + marker to the farthest point heard ──
  const MAX_RANGE_MS = 8000;  // how long a click/tap keeps it up
  const maxRangeLayer = L.layerGroup().addTo(map);
  let maxRangeTimer = null;

  function hideMaxRange() {
    clearTimeout(maxRangeTimer);
    maxRangeTimer = null;
    maxRangeLayer.clearLayers();
  }

  function showMaxRange(s, color, st) {
    hideMaxRange();
    const from = [s.lat, s.lon], to = st.max_range_at;
    L.polyline([from, to], { renderer: stationRenderer, color, weight: 3, opacity: 0.9, dashArray: '6 5', interactive: false })
      .addTo(maxRangeLayer);
    L.circleMarker(to, { renderer: stationRenderer, radius: 6, color: '#fff', weight: 2, fillColor: color, fillOpacity: 1, interactive: false })
      .bindTooltip(`${st.max_range_km} km`, { permanent: true, direction: 'right', offset: [8, 0], className: 'cell-tip' })
      .addTo(maxRangeLayer);
  }

  function wireMaxRange(root, s, color) {
    const el = root && root.querySelector('.sc-maxrange');
    if (!el) return;
    // Read stats when used, in case they refreshed after the card was built.
    const current = () => STATS && STATS.infra.find(r => r.callsign === s.callsign && r.max_range_at);
    el.addEventListener('mouseenter', () => { const st = current(); if (st) showMaxRange(s, color, st); });
    el.addEventListener('mouseleave', () => { if (!maxRangeTimer) hideMaxRange(); });
    el.addEventListener('click', () => {
      const st = current();
      if (!st) return;
      showMaxRange(s, color, st);
      maxRangeTimer = setTimeout(hideMaxRange, MAX_RANGE_MS);
    });
  }

  // ── filters UI ────────────────────────────────────────────────────────────
  function buildCheckList(el, items, selected, onChange) {
    el.innerHTML = '';
    if (!items.length) { el.innerHTML = '<p class="muted">None yet.</p>'; return; }
    for (const it of items) {
      const lab = document.createElement('label');
      lab.className = 'check';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = it.value;
      cb.checked = selected === null || selected.includes(it.value);
      cb.addEventListener('change', onChange);
      lab.append(cb);
      if (it.color) { const sw = document.createElement('span'); sw.className = 'swatch'; sw.style.background = it.color; lab.append(sw); }
      lab.append(document.createTextNode(it.label));
      if (it.title) lab.title = it.title;
      if (it.meta) { const m = document.createElement('span'); m.className = 'meta'; m.textContent = it.meta; lab.append(m); }
      el.append(lab);
    }
  }
  function readCheckList(el) {
    const boxes = [...el.querySelectorAll('input[type=checkbox]')];
    const sel = boxes.filter(b => b.checked).map(b => b.value);
    return sel.length === boxes.length ? null : sel;
  }

  // Reads a list the hardware filter may have narrowed: stations it hides keep
  // whatever selection they had, so changing the hardware back restores them.
  function readNarrowed(el, prev, universe) {
    const shown = new Set([...el.querySelectorAll('input[type=checkbox]')].map(b => b.value));
    const sel = new Set(readCheckList(el) ?? shown);
    for (const k of universe) if (!shown.has(k) && (prev === null || prev.includes(k))) sel.add(k);
    return universe.every(k => sel.has(k)) ? null : universe.filter(k => sel.has(k));
  }

  function buildFilters() {
    // Hardware actually in use, in the server's order, then "Not set".
    const used = new Set(STATIONS.map(s => s.hardware || NOT_SET));
    const hwItems = [...Object.keys((CFG && CFG.hardware) || {}).filter(k => used.has(k)), NOT_SET]
      .map(k => ({ value: k, label: hwName(k), meta: String(STATIONS.filter(s => (s.hardware || NOT_SET) === k).length) }));
    buildCheckList($('hw-list'), hwItems, S.hw, () => { S.hw = readCheckList($('hw-list')); save(); buildFilters(); refresh(); });
    const hide = (scope, call) => hwOn(scope) && !hwPass(call);
    buildCheckList($('mobiles-list'), MOBILES.filter(m => !hide('mobiles', m))
      .map(m => ({ value: m, label: stationName(m), title: m, color: S.color === 'mobile' ? keyColor(m) : null,
                   meta: findStation(m)?.hardware ? hwName(findStation(m).hardware) : '' }))
      .sort((a, b) => a.label.localeCompare(b.label)),
      S.mobiles, () => { S.mobiles = readNarrowed($('mobiles-list'), S.mobiles, MOBILES); save(); refresh(); });
    const infra = STATIONS.filter(s => s.kind !== 'mobile' && !hide('infra', s.callsign)).map(s => ({
      value: s.callsign, label: s.label || s.callsign, title: s.callsign, color: keyColor(s.callsign),
      meta: [{ igate: 'igate', digi: 'digi', digi_igate: 'digi+ig' }[s.kind], s.hardware ? hwName(s.hardware) : ''].filter(Boolean).join(' · '),
    })).sort((a, b) => a.label.localeCompare(b.label));
    if (!hwOn('infra') || S.hw.includes(NOT_SET)) {
      infra.push({ value: OTHER, label: 'Other (unregistered stations)' });
      infra.push({ value: UNTRACED, label: 'Untraced digi hop (unknown)', color: '#8b949e' });
    }
    buildCheckList($('heard-list'), infra, S.heard, () => { S.heard = readNarrowed($('heard-list'), S.heard, allHeard()); save(); refresh(); });
  }

  let STATS = null;
  async function loadStats() {
    try { STATS = await getJSON('api/stats?' + queryParams(false)); } catch { return; }
    const byCall = Object.fromEntries(STATIONS.map(s => [s.callsign, s]));
    const tb = $('infra-stats').querySelector('tbody');
    tb.innerHTML = STATS.infra.map(r => {
      const st = byCall[r.callsign];
      return `<tr class="${st && st.lat != null ? 'clickable' : ''}" data-call="${esc(r.callsign)}">
        <td><span class="swatch" style="background:${keyColor(r.callsign)}"></span> ${nameHtml(r.callsign)}${r.registered ? '' : ' <span class="muted">*</span>'}</td>
        <td>${r.heard_direct}</td><td>${r.cells}</td><td>${r.max_range_km ?? '—'}</td><td>${r.gated}</td><td>${ago(r.last_heard)}</td></tr>`;
    }).join('') || '<tr><td colspan="6" class="muted">No packets in range.</td></tr>';
    tb.querySelectorAll('tr.clickable').forEach(tr => tr.addEventListener('click', () => {
      const st = byCall[tr.dataset.call];
      if (st) map.flyTo([st.lat, st.lon], Math.max(map.getZoom(), 12));
    }));
    $('mobile-stats').querySelector('tbody').innerHTML = STATS.mobiles.map(r =>
      `<tr><td>${nameHtml(r.mobile)}</td><td>${r.packets}</td><td>${r.cells}</td><td>${ago(r.last)}</td></tr>`).join('')
      || '<tr><td colspan="4" class="muted">No packets in range.</td></tr>';
  }

  let loadSeq = 0;
  async function refresh() {
    const seq = ++loadSeq;
    try {
      const [cov, fails, rel] = await Promise.all([
        getJSON('api/coverage?' + queryParams()),
        S.fails && !isRel() ? getJSON('api/failures?' + queryParams(false)).catch(() => null) : null,
        isRel() ? getJSON(`api/reliability?${queryParams()}&estimates=${S.estimates}`) : null,
      ]);
      if (seq !== loadSeq) return;  // a newer request superseded this one
      COVERAGE = cov;
      RELI = rel;
      FAILS = fails && { ...fails, byKey: new Map(fails.cells.map(c => [`${c[0]},${c[1]}`, c.slice(2)])) };
      renderCells();
    } catch (e) {
      $('summary').textContent = 'Failed to load coverage: ' + e.message;
    }
    loadStats();
  }

  // ── live feed ─────────────────────────────────────────────────────────────
  let liveTimer = null, logTimer = null;
  function setLive(state, text) {
    $('live').className = 'live ' + state;
    $('live-text').textContent = text;
  }
  function startLive() {
    const es = new EventSource('api/live');
    es.onopen = () => checkStatus();
    es.onerror = () => setLive('bad', 'reconnecting…');
    es.onmessage = ev => {
      let p; try { p = JSON.parse(ev.data); } catch { return; }
      if (S.live) {
        const c = L.circleMarker([p.lat, p.lon], { renderer: liveRenderer, radius: 7, color: keyColor(p.heard_by || '(untraced)'), weight: 2, fillOpacity: 0.9, className: 'pulse' })
          .bindTooltip(`${esc(stationName(p.mobile))} via ${p.heard_by ? esc(stationName(p.heard_by)) : 'untraced hop'}${p.igate !== p.heard_by ? ' → ' + esc(stationName(p.igate)) : ''}`);
        liveLayer.addLayer(c);
        setTimeout(() => liveLayer.removeLayer(c), 30000);
      }
      if (!MOBILES.includes(p.mobile)) { MOBILES.push(p.mobile); MOBILES.sort(); buildFilters(); }
      if (isRelative()) {
        clearTimeout(liveTimer); liveTimer = setTimeout(refresh, 2500);
        // Trackers' transmit logs arrive from the app about a minute later; look
        // again then so the lost-beacon and reliability counts catch up.
        if (isRel() || S.fails) { clearTimeout(logTimer); logTimer = setTimeout(refresh, 90000); }
      }
      setLive('ok', 'live · ' + stationName(p.mobile));
    };
  }
  async function checkStatus() {
    try {
      const st = await getJSON('api/status');
      if (st.connected) setLive('ok', 'live' + (st.last_packet_ts ? ' · last ' + ago(st.last_packet_ts) : ''));
      else setLive('bad', st.enabled ? 'APRS-IS offline' : 'feed disabled');
    } catch { setLive('bad', 'server unreachable'); }
  }

  // ── my location (browser geolocation; never sent to the server) ──────────
  let meWatch = null, meDot = null, meAcc = null;
  function setMeStatus(text) { $('me-status').textContent = text || ''; $('me-status').hidden = !text; }
  function centerOnMe() {
    if (!meDot) return;
    map.panTo(meDot.getLatLng(), { animate: true });
  }
  function onMePosition(pos) {
    const ll = [pos.coords.latitude, pos.coords.longitude], acc = pos.coords.accuracy || 0;
    if (!meDot) {
      meAcc = L.circle(ll, { renderer: meRenderer, radius: acc, color: '#1a73e8', weight: 1, opacity: 0.4, fillOpacity: 0.1, interactive: false }).addTo(map);
      meDot = L.circleMarker(ll, { renderer: meRenderer, radius: 6, color: '#ffffff', weight: 2, fillColor: '#1a73e8', fillOpacity: 1 })
        .bindTooltip('You are here').addTo(map);
      if (!S.follow) map.panTo(ll);  // show it once when first turned on
    } else {
      meDot.setLatLng(ll); meAcc.setLatLng(ll).setRadius(acc);
    }
    meDot.setTooltipContent(`You are here (±${Math.round(acc)} m)`);
    setMeStatus('');
    if (S.follow) centerOnMe();
  }
  function onMeError(err) {
    const msg = { 1: 'Location permission denied. Allow it in your browser settings.',
                  2: 'Location unavailable.', 3: 'Waiting for a location fix…' }[err.code] || err.message;
    setMeStatus(msg);
    if (err.code === 1) { S.me = false; save(); syncMe(); }
  }
  function syncMe() {
    $('show-me').checked = S.me;
    $('follow-me').disabled = !S.me;
    if (S.me && meWatch === null) {
      if (!('geolocation' in navigator) || !window.isSecureContext) {
        setMeStatus('Location needs a browser with geolocation over HTTPS.');
        S.me = false; save(); $('show-me').checked = false; $('follow-me').disabled = true;
        return;
      }
      setMeStatus('Finding your location…');
      meWatch = navigator.geolocation.watchPosition(onMePosition, onMeError,
        { enableHighAccuracy: true, maximumAge: 5000, timeout: 30000 });
    } else if (!S.me && meWatch !== null) {
      navigator.geolocation.clearWatch(meWatch);
      meWatch = null;
      if (meDot) { map.removeLayer(meDot); map.removeLayer(meAcc); meDot = meAcc = null; }
      if ($('me-status').textContent === 'Finding your location…') setMeStatus('');
    }
    if (S.me && S.follow) centerOnMe();
  }
  // Dragging the map yourself stops following, like most map apps.
  map.on('dragstart', () => {
    if (S.follow) { S.follow = false; save(); $('follow-me').checked = false; }
  });

  // ── controls wiring ───────────────────────────────────────────────────────
  function bind(id, key, { parse = v => v, prop = 'value', after = refresh } = {}) {
    const el = $(id);
    el[prop] = S[key];
    el.addEventListener(el.type === 'range' || el.type === 'color' ? 'input' : 'change', () => {
      S[key] = parse(el[prop]);
      save();
      after();
    });
  }
  function syncVisibility() {
    $('custom-range').hidden = S.preset !== 'custom';
    $('single-color-row').hidden = S.color !== 'single';
    $('rel-opts').hidden = !isRel();
    $('fails-opts').hidden = isRel();  // the reliability colours already show lost beacons
    $('shade-text').textContent = isRel() ? 'Fade squares with few beacons (less certain)' : 'Shade by packet count';
    $('single-red').hidden = S.color !== 'single' || !isReddish(S.singleColor);
    $('opacity-val').textContent = S.opacity + '%';
  }

  function initControls() {
    const cellSel = $('cell');
    cellSel.innerHTML = CFG.cell_sizes.map(m => `<option value="${m}">${m >= 1000 ? m / 1000 + ' km' : m + ' m'}</option>`).join('');
    if (!CFG.cell_sizes.includes(+S.cell)) S.cell = 1000;

    if (![...$('preset').options].some(o => o.value === S.preset)) S.preset = DEFAULTS.preset;  // e.g. a removed preset
    bind('preset', 'preset', { after: () => { syncVisibility(); if (S.preset !== 'custom') refresh(); } });
    bind('color-mode', 'color', { after: () => { syncVisibility(); buildFilters(); refresh(); } });
    bind('single-color', 'singleColor', { after: () => { syncVisibility(); renderCells(); } });
    bind('show-fails', 'fails', { prop: 'checked' });
    bind('estimates', 'estimates', { prop: 'checked' });
    bind('shade', 'shade', { prop: 'checked', after: renderCells });
    bind('cell', 'cell', { parse: v => parseInt(v, 10) });
    bind('opacity', 'opacity', { parse: v => parseInt(v, 10), after: () => { syncVisibility(); renderCells(); } });
    bind('borders', 'borders', { prop: 'checked', after: renderCells });
    bind('show-stations', 'stations', { prop: 'checked', after: renderStations });
    bind('show-live', 'live', { prop: 'checked', after: () => { if (!S.live) liveLayer.clearLayers(); } });
    bind('show-me', 'me', { prop: 'checked', after: syncMe });
    bind('follow-me', 'follow', { prop: 'checked', after: syncMe });
    syncMe();
    bind('basemap', 'basemap', { after: () => setBasemap(S.basemap) });
    bind('match', 'match');
    bind('hw-scope', 'hwScope', { after: () => { buildFilters(); refresh(); } });

    const toLocal = ts => new Date(ts * 1000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    const now = Math.floor(Date.now() / 1000);
    $('from').value = S.from || toLocal(now - 86400);
    $('to').value = S.to || toLocal(now);
    $('to-now').checked = S.toNow;
    $('to').disabled = S.toNow;
    $('to-now').addEventListener('change', () => { $('to').disabled = $('to-now').checked; });
    $('apply-range').addEventListener('click', () => {
      S.from = $('from').value; S.to = $('to').value; S.toNow = $('to-now').checked; save(); refresh();
    });

    document.querySelectorAll('[data-all],[data-none]').forEach(b => b.addEventListener('click', () => {
      const which = b.dataset.all || b.dataset.none;
      const val = b.dataset.all ? null : [];
      if (which === 'mobiles') S.mobiles = val; else if (which === 'hw') S.hw = val; else S.heard = val;
      save(); buildFilters(); refresh();
    }));

    document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(x => { x.classList.toggle('active', x === t); x.setAttribute('aria-selected', x === t); });
      document.querySelectorAll('.tab-body').forEach(b => { b.hidden = b.id !== 'tab-' + t.dataset.tab; });
    }));

    const toggle = $('panel-toggle');
    if (window.matchMedia('(max-width: 640px)').matches) { $('panel').classList.add('collapsed'); toggle.setAttribute('aria-expanded', 'false'); }
    toggle.addEventListener('click', () => {
      const collapsed = $('panel').classList.toggle('collapsed');
      toggle.setAttribute('aria-expanded', String(!collapsed));
    });
    syncVisibility();
  }

  // ── account link: "Join / Log in", or where a logged-in user manages things ──
  fetch('api/me').then(r => r.json()).then(({ user }) => {
    if (!user) return;
    const a = $('account-link');
    a.textContent = user.role === 'admin' ? 'Admin' : user.status === 'pending' ? 'My sign-up' : 'My nodes';
    a.title = `Logged in as ${user.username}`;
  }).catch(() => {});

  // ── boot ──────────────────────────────────────────────────────────────────
  (async () => {
    CFG = await getJSON('api/config');
    map.fitBounds(CFG.default_bounds);
    setBasemap(S.basemap);
    [STATIONS, MOBILES] = await Promise.all([getJSON('api/stations'), getJSON('api/mobiles')]);
    initControls();
    buildFilters();
    renderStations();
    await refresh();
    renderStations();  // popups use stats
    startLive();
    setInterval(checkStatus, 60000);
    // Short windows that end now (15 minutes, 24 hours) also need old packets to
    // drop off when nothing new arrives, so redraw them every minute.
    setInterval(() => {
      const { start, end } = timeRange();
      if (isRelative() && end - start <= 86400 && !document.hidden) refresh();
    }, 60000);
    setInterval(async () => {  // pick up station edits and marker positions
      try { STATIONS = await getJSON('api/stations'); renderStations(); } catch {}
    }, 300000);
  })().catch(e => { $('summary').textContent = 'Failed to start: ' + e.message; });
})();
