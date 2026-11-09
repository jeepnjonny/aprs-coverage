/* APRS coverage map: public page */
(() => {
  'use strict';

  // Hues kept well apart so no two read as "the same colour" on the map.
  const PALETTE = ['#2f81f7', '#e5534b', '#2da44e', '#f2d024', '#8250df', '#e85aad',
                   '#1fb6c9', '#f0883e', '#8b5a2b', '#1f3a93', '#9acd32', '#4b0082'];
  const SHADE = [[31, 1.0, '31+'], [12, 0.82, '12–30'], [5, 0.64, '5–11'], [2, 0.45, '2–4'], [1, 0.28, '1']];
  const UNTRACED = '__untraced__', OTHER = '__other__';
  const $ = id => document.getElementById(id);

  // ── persisted UI state (per-viewer convenience only) ──────────────────────
  const DEFAULTS = {
    preset: '604800', from: null, to: null, color: 'single', singleColor: '#2f81f7',
    shade: true, cell: 1000, opacity: 60, borders: true, stations: true, live: true,
    basemap: 'osm', match: 'first', mobiles: null, heard: null,   // null = all selected
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
  const cellRenderer = L.canvas({ padding: 0.3 });
  const liveRenderer = L.svg({ pane: 'live' });
  const stationRenderer = L.svg({ pane: 'stations' });
  const cellLayer = L.layerGroup().addTo(map);
  const stationLayer = L.layerGroup().addTo(map);
  const liveLayer = L.layerGroup().addTo(map);

  // ── data ──────────────────────────────────────────────────────────────────
  let CFG = null, STATIONS = [], MOBILES = [], COVERAGE = null;
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
      const t = S.to ? Math.floor(new Date(S.to).getTime() / 1000) : now;
      return { start: f, end: t };
    }
    return { start: now - parseInt(S.preset, 10), end: now };
  }
  const isRelative = () => S.preset !== 'custom';

  function queryParams(withHeard = true) {
    const { start, end } = timeRange();
    const p = new URLSearchParams({ start, end, cell: S.cell });
    if (S.mobiles !== null) p.set('mobiles', S.mobiles.join(','));
    if (withHeard) {
      if (S.heard !== null) p.set('heard_by', S.heard.join(','));
      p.set('match', S.match);
      p.set('color', S.color);
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
  const near = (a, b) => { const [p, q] = [rgb(a), rgb(b)]; return Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]) < 60; };
  let colorCache = { src: null, map: new Map() };
  function infraColors() {
    if (colorCache.src === STATIONS) return colorCache.map;
    const out = new Map(), used = [];
    const infra = STATIONS.filter(s => s.kind !== 'mobile')
      .sort((a, b) => a.callsign.localeCompare(b.callsign));
    for (const s of infra) if (s.color) { out.set(s.callsign, s.color); used.push(s.color); }
    let pi = 0, extra = 0;
    for (const s of infra) {
      if (s.color) continue;
      let c = null;
      while (pi < PALETTE.length && !c) {
        const cand = PALETTE[pi++];
        if (!used.some(u => near(u, cand))) c = cand;
      }
      if (!c) c = `hsl(${Math.round((extra++ * 137.508) % 360)}, 65%, 45%)`;
      out.set(s.callsign, c);
    }
    colorCache = { src: STATIONS, map: out };
    return out;
  }
  function keyColor(key) {
    if (key === '(untraced)') return '#8b949e';
    const st = findStation(key);  // wildcard / object registrations keep their colour
    if (st && st.color) return st.color;
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
      return `${d(start)} – ${d(end)}`;
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

  function cellTip(n, first, last, heard, mobs) {
    const size = COVERAGE.cell_m >= 1000 ? `${COVERAGE.cell_m / 1000} km` : `${COVERAGE.cell_m} m`;
    return `<div class="tip-top"><b>${n.toLocaleString()} packet${n === 1 ? '' : 's'}</b>
        <span class="muted">· ${size} square</span></div>
      <div class="muted">First ${ago(first)} · last ${ago(last)}</div>`
      + tipList('Heard by', heard, true)
      + tipList('Mobiles', mobs, false);
  }

  function renderCells() {
    cellLayer.clearLayers();
    if (!COVERAGE) return;
    const { dlat, dlon, cells } = COVERAGE;
    const op = S.opacity / 100;
    const seenKeys = new Map();
    for (const [x, y, n, last, by, heard, mobs, first] of cells) {
      let color = S.singleColor, dominant = null;
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
    renderLegend(seenKeys);
    const { packets } = COVERAGE;
    $('summary').textContent = `${cells.length.toLocaleString()} squares · ${packets.toLocaleString()} packets`;
  }

  function renderLegend(keys) {
    const el = $('legend');
    if (!COVERAGE || !COVERAGE.cells.length) { el.hidden = true; return; }
    const base = S.color === 'single' ? S.singleColor : 'var(--muted)';
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
    el.innerHTML = html;
    el.hidden = !html;
  }

  const KIND_NAME = { igate: 'Igate', digi: 'Digipeater', digi_igate: 'Digi + igate' };

  function stationCard(s, color) {
    const st = (STATS && STATS.infra.find(r => r.callsign === s.callsign)) || null;
    const n = v => (v ?? 0).toLocaleString();
    const row = (k, v) => `<tr><th>${k}</th><td>${v}</td></tr>`;
    let html = `<div class="station-card">
      <div class="sc-head"><span class="swatch" style="background:${color}"></span>
        <b>${esc(s.label || s.callsign)}</b> <span class="muted">${KIND_NAME[s.kind] || ''}</span></div>`;
    if (s.label) html += `<div class="sc-label">${esc(s.callsign)}</div>`;
    html += `<div class="sc-range muted">${esc(rangeText())}</div><table>`;
    if (!st) {
      html += row('Stats', STATS ? 'none in this range' : 'loading…');
    } else {
      html += row('Heard direct', `${n(st.heard_direct)} packet${st.heard_direct === 1 ? '' : 's'}`)
        + row('Squares covered', n(st.cells))
        + row('Mobiles heard', n(st.mobiles))
        + row('Max range', st.max_range_km != null ? `${st.max_range_km} km` : '—');
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
      m.bindPopup(() => stationCard(s, color));
      stationLayer.addLayer(m);
    }
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

  function buildFilters() {
    buildCheckList($('mobiles-list'), MOBILES.map(m => ({ value: m, label: stationName(m), title: m, color: S.color === 'mobile' ? keyColor(m) : null }))
      .sort((a, b) => a.label.localeCompare(b.label)),
      S.mobiles, () => { S.mobiles = readCheckList($('mobiles-list')); save(); refresh(); });
    const infra = STATIONS.filter(s => s.kind !== 'mobile').map(s => ({
      value: s.callsign, label: s.label || s.callsign, title: s.callsign, color: keyColor(s.callsign),
      meta: { igate: 'igate', digi: 'digi', digi_igate: 'digi+ig' }[s.kind],
    })).sort((a, b) => a.label.localeCompare(b.label));
    infra.push({ value: OTHER, label: 'Other (unregistered stations)' });
    infra.push({ value: UNTRACED, label: 'Untraced digi hop (unknown)', color: '#8b949e' });
    buildCheckList($('heard-list'), infra, S.heard, () => { S.heard = readCheckList($('heard-list')); save(); refresh(); });
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
      const cov = await getJSON('api/coverage?' + queryParams());
      if (seq !== loadSeq) return;  // a newer request superseded this one
      COVERAGE = cov;
      renderCells();
    } catch (e) {
      $('summary').textContent = 'Failed to load coverage: ' + e.message;
    }
    loadStats();
  }

  // ── live feed ─────────────────────────────────────────────────────────────
  let liveTimer = null;
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
      if (isRelative()) { clearTimeout(liveTimer); liveTimer = setTimeout(refresh, 2500); }
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
    $('opacity-val').textContent = S.opacity + '%';
  }

  function initControls() {
    const cellSel = $('cell');
    cellSel.innerHTML = CFG.cell_sizes.map(m => `<option value="${m}">${m >= 1000 ? m / 1000 + ' km' : m + ' m'}</option>`).join('');
    if (!CFG.cell_sizes.includes(+S.cell)) S.cell = 1000;

    bind('preset', 'preset', { after: () => { syncVisibility(); if (S.preset !== 'custom') refresh(); } });
    bind('color-mode', 'color', { after: () => { syncVisibility(); buildFilters(); refresh(); } });
    bind('single-color', 'singleColor', { after: renderCells });
    bind('shade', 'shade', { prop: 'checked', after: renderCells });
    bind('cell', 'cell', { parse: v => parseInt(v, 10) });
    bind('opacity', 'opacity', { parse: v => parseInt(v, 10), after: () => { syncVisibility(); renderCells(); } });
    bind('borders', 'borders', { prop: 'checked', after: renderCells });
    bind('show-stations', 'stations', { prop: 'checked', after: renderStations });
    bind('show-live', 'live', { prop: 'checked', after: () => { if (!S.live) liveLayer.clearLayers(); } });
    bind('basemap', 'basemap', { after: () => setBasemap(S.basemap) });
    bind('match', 'match');

    const toLocal = ts => new Date(ts * 1000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    const now = Math.floor(Date.now() / 1000);
    $('from').value = S.from || toLocal(now - 86400);
    $('to').value = S.to || toLocal(now);
    $('apply-range').addEventListener('click', () => { S.from = $('from').value; S.to = $('to').value; save(); refresh(); });

    document.querySelectorAll('[data-all],[data-none]').forEach(b => b.addEventListener('click', () => {
      const which = b.dataset.all || b.dataset.none;
      const val = b.dataset.all ? null : [];
      if (which === 'mobiles') S.mobiles = val; else S.heard = val;
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
    setInterval(async () => {  // pick up station edits and marker positions
      try { STATIONS = await getJSON('api/stations'); renderStations(); } catch {}
    }, 300000);
  })().catch(e => { $('summary').textContent = 'Failed to start: ' + e.message; });
})();
