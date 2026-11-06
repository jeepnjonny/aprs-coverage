/* APRS coverage map: public page */
(() => {
  'use strict';

  const PALETTE = ['#2f81f7', '#f85149', '#3fb950', '#d29922', '#a371f7', '#f778ba',
                   '#39c5cf', '#ff7b39', '#8ddb4a', '#e3b341', '#6e7bff', '#ff5c8a'];
  const SHADE = [[31, 1.0, '31+'], [12, 0.82, '12–30'], [5, 0.64, '5–11'], [2, 0.45, '2–4'], [1, 0.28, '1']];
  const UNTRACED = '__untraced__', OTHER = '__other__';
  const $ = id => document.getElementById(id);

  // ── persisted UI state (per-viewer convenience only) ──────────────────────
  const DEFAULTS = {
    preset: '604800', from: null, to: null, color: 'single', singleColor: '#2f81f7',
    shade: true, cell: 500, opacity: 60, borders: true, stations: true, live: true,
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

  const cellRenderer = L.canvas({ padding: 0.3 });
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
  function keyColor(key) {
    if (key === '(untraced)') return '#8b949e';
    const st = STATIONS.find(s => s.callsign === key);
    return (st && st.color) || hashColor(key);
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

  function renderCells() {
    cellLayer.clearLayers();
    if (!COVERAGE) return;
    const { dlat, dlon, cells } = COVERAGE;
    const op = S.opacity / 100;
    const seenKeys = new Map();
    for (const [x, y, n, last, by] of cells) {
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
      let tip = `<b>${n} packet${n === 1 ? '' : 's'}</b> · last ${ago(last)}`;
      if (entries.length) {
        tip += '<br>' + entries.slice(0, 6).map(([k, c]) =>
          `<span class="swatch" style="background:${keyColor(k)}"></span> ${esc(k)}: ${c}`).join('<br>');
        if (entries.length > 6) tip += `<br>+${entries.length - 6} more`;
      }
      rect.bindTooltip(tip, { className: 'cell-tip', sticky: true });
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
          `<div class="row"><span class="swatch" style="background:${keyColor(k)}"></span>${esc(k)}</div>`).join('');
    }
    el.innerHTML = html;
    el.hidden = !html;
  }

  function renderStations() {
    stationLayer.clearLayers();
    if (!S.stations) return;
    for (const s of STATIONS) {
      if (s.kind === 'mobile' || s.lat == null) continue;
      const color = s.color || hashColor(s.callsign);
      const m = L.circleMarker([s.lat, s.lon], {
        radius: s.kind === 'digi' ? 6 : 8, color: '#fff', weight: 2, fillColor: color, fillOpacity: 1,
        dashArray: s.kind === 'digi_igate' ? '3 2' : null,
      });
      const kind = { igate: 'Igate', digi: 'Digipeater', digi_igate: 'Digi + igate' }[s.kind];
      m.bindTooltip(`${esc(s.callsign)}${s.label ? ' — ' + esc(s.label) : ''}`);
      m.bindPopup(() => {
        const st = (STATS && STATS.infra.find(r => r.callsign === s.callsign)) || {};
        return `<b>${esc(s.callsign)}</b> <span class="muted">${kind}</span>${s.label ? '<br>' + esc(s.label) : ''}
          <br>Heard direct: ${st.heard_direct ?? '—'} · squares: ${st.cells ?? '—'}
          <br>Max range: ${st.max_range_km != null ? st.max_range_km + ' km' : '—'} · gated: ${st.gated ?? '—'}
          <br>Last heard a mobile: ${ago(s.last_heard)}<br>Own beacon: ${ago(s.last_beacon)}`;
      });
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
    buildCheckList($('mobiles-list'), MOBILES.map(m => ({ value: m, label: m, color: S.color === 'mobile' ? keyColor(m) : null })),
      S.mobiles, () => { S.mobiles = readCheckList($('mobiles-list')); save(); refresh(); });
    const infra = STATIONS.filter(s => s.kind !== 'mobile').map(s => ({
      value: s.callsign, label: s.callsign + (s.label ? ` — ${s.label}` : ''), color: keyColor(s.callsign),
      meta: { igate: 'igate', digi: 'digi', digi_igate: 'digi+ig' }[s.kind],
    }));
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
      const name = r.callsign === '(untraced)' ? 'Untraced hop' : r.callsign;
      return `<tr class="${st && st.lat != null ? 'clickable' : ''}" data-call="${esc(r.callsign)}">
        <td><span class="swatch" style="background:${keyColor(r.callsign)}"></span> ${esc(name)}${r.registered ? '' : ' <span class="muted">*</span>'}</td>
        <td>${r.heard_direct}</td><td>${r.cells}</td><td>${r.max_range_km ?? '—'}</td><td>${r.gated}</td><td>${ago(r.last_heard)}</td></tr>`;
    }).join('') || '<tr><td colspan="6" class="muted">No packets in range.</td></tr>';
    tb.querySelectorAll('tr.clickable').forEach(tr => tr.addEventListener('click', () => {
      const st = byCall[tr.dataset.call];
      if (st) map.flyTo([st.lat, st.lon], Math.max(map.getZoom(), 12));
    }));
    $('mobile-stats').querySelector('tbody').innerHTML = STATS.mobiles.map(r =>
      `<tr><td>${esc(r.mobile)}</td><td>${r.packets}</td><td>${r.cells}</td><td>${ago(r.last)}</td></tr>`).join('')
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
        const c = L.circleMarker([p.lat, p.lon], { radius: 7, color: keyColor(p.heard_by || '(untraced)'), weight: 2, fillOpacity: 0.9, className: 'pulse' })
          .bindTooltip(`${esc(p.mobile)} via ${esc(p.heard_by || 'untraced')}${p.igate !== p.heard_by ? ' → ' + esc(p.igate) : ''}`);
        liveLayer.addLayer(c);
        setTimeout(() => liveLayer.removeLayer(c), 120000);
      }
      if (!MOBILES.includes(p.mobile)) { MOBILES.push(p.mobile); MOBILES.sort(); buildFilters(); }
      if (isRelative()) { clearTimeout(liveTimer); liveTimer = setTimeout(refresh, 2500); }
      setLive('ok', 'live · ' + p.mobile);
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
    if (!CFG.cell_sizes.includes(+S.cell)) S.cell = 500;

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
