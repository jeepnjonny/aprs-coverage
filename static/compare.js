/* APRS coverage: compare two mobiles or two igates/digis over time */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const pct = v => v == null ? '—' : `${(v * 100).toFixed(v >= 0.995 || v < 0.1 ? 1 : 0)}%`;
  const pctRange = (lo, hi) => lo == null ? '' : `${Math.round(lo * 100)}–${Math.round(hi * 100)}%`;
  const num = n => n == null ? '—' : Number(n).toLocaleString();
  const pval = p => p == null ? 'p = —' : p < 0.001 ? 'p < 0.001' : `p = ${p.toFixed(3)}`;
  const TZ = -new Date().getTimezoneOffset();  // minutes east of UTC
  const SVGNS = 'http://www.w3.org/2000/svg';

  let ME = null, STATIONS = [], MOBILES = [], DATA = null, seq = 0, NOTES = [];
  const isChange = () => $('mode').value === 'change';
  const isNode = call => STATIONS.some(s => s.kind !== 'mobile' && s.callsign === call);

  // The server's error message: a string, or FastAPI's list of field errors (422).
  const errText = (d, status) => typeof d.detail === 'string' ? d.detail
    : Array.isArray(d.detail) ? d.detail.map(e => e.msg).join('; ') : `HTTP ${status}`;
  async function get(url) {
    const r = await fetch(url);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(errText(d, r.status));
    return d;
  }
  async function send(method, url, body) {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
      body: body ? JSON.stringify(body) : undefined });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(errText(d, r.status));
    return d;
  }

  // ── controls and URL state ─────────────────────────────────────────────────
  const CTRL = ['mode', 'a', 'b', 'range', 'bucket', 'from', 'to', 'together', 'sep', 'interval', 'match',
                'station', 'change', 'change-at', 'period', 'same-route'];
  function readUrl() {
    const q = new URLSearchParams(location.search);
    for (const k of CTRL) {
      if (!q.has(k)) continue;
      const el = $(k);
      if (el.type === 'checkbox') el.checked = q.get(k) === '1'; else el.dataset.want = q.get(k);
    }
  }
  function writeUrl() {
    const q = new URLSearchParams();
    for (const k of CTRL) {
      const el = $(k);
      if (el.closest('[hidden]')) continue;
      if (el.type === 'checkbox') q.set(k, el.checked ? '1' : '0'); else if (el.value) q.set(k, el.value);
    }
    try { history.replaceState(null, '', `${location.pathname}?${q}`); } catch { /* ignore */ }
  }
  function applyWant(el) {
    if (el.dataset.want != null && [...(el.options || [])].some(o => o.value === el.dataset.want) || el.tagName === 'INPUT' && el.dataset.want != null) {
      el.value = el.dataset.want;
    }
    delete el.dataset.want;
  }

  // Which controls each mode uses
  function syncMode() {
    const mode = $('mode').value, change = isChange();
    for (const id of ['a-field', 'b-field', 'range-field', 'bucket-field']) $(id).hidden = change;
    for (const id of ['station-field', 'change-field', 'period-field', 'change-opts']) $(id).hidden = !change;
    if (change) $('custom-range').hidden = true; else range();
    $('change-at-field').hidden = !change || $('change').value !== 'date';
    const node = change && isNode($('station').value);
    $('mobile-opts').hidden = mode !== 'mobiles';
    $('node-opts').hidden = !(mode === 'nodes' || node);
    $('same-route-text').textContent = node ? 'Same areas only' : 'Same route only';
    $('same-route-help').textContent = node
      ? 'Counts only squares mobiles were heard from in both periods, so different driving doesn\'t look like a change.'
      : 'Counts only squares driven in both periods, so a detour or a new road doesn\'t look like a change.';
  }

  function fillChangeStations() {
    const el = $('station'), cur = el.dataset.want ?? el.value;
    const nodes = STATIONS.filter(s => s.kind !== 'mobile');
    el.innerHTML = `<optgroup label="Mobiles">${MOBILES.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('')}</optgroup>`
      + `<optgroup label="Igates and digis">${nodes.map(s => `<option value="${esc(s.callsign)}">${esc(s.callsign + (s.label ? ` — ${s.label}` : ''))}</option>`).join('')}</optgroup>`;
    const all = [...MOBILES, ...nodes.map(s => s.callsign)];
    el.value = all.includes(cur) ? cur : all[0] || '';
    delete el.dataset.want;
  }

  // The change to compare around: one of the station's notes, or a date.
  async function fillChanges() {
    const el = $('change'), want = el.dataset.want ?? el.value;
    delete el.dataset.want;
    const station = $('station').value;
    const notes = station ? await get(`api/events?device=${encodeURIComponent(station)}`).catch(() => []) : [];
    if ($('station').value !== station) return;  // picked another station meanwhile; its call fills the list
    NOTES = notes;
    const day = ts => new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
    el.innerHTML = NOTES.map(n => `<option value="n${n.id}">${esc(n.text)}, ${esc(day(n.ts))}</option>`).join('')
      + '<option value="date">Pick a date…</option>';
    el.value = [...el.options].some(o => o.value === want) ? want : el.options[0].value;
    if (!$('change-at').value) {
      const d = new Date(Date.now() - 7 * 86400000); d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
      $('change-at').value = d.toISOString().slice(0, 16);
    }
    syncMode();
  }
  function changeNote() { return NOTES.find(n => `n${n.id}` === $('change').value) || null; }
  function changeAt() {
    const n = changeNote();
    return n ? n.ts : Math.floor(Date.parse($('change-at').value) / 1000);
  }

  function fillStations() {
    const mode = $('mode').value;
    if (mode === 'change') { fillChangeStations(); syncMode(); return; }
    const list = mode === 'mobiles'
      ? MOBILES.map(m => ({ value: m, text: m }))
      : STATIONS.filter(s => s.kind !== 'mobile').map(s => ({ value: s.callsign, text: s.callsign + (s.label ? ` — ${s.label}` : '') }));
    for (const [id, idx] of [['a', 0], ['b', 1]]) {
      const el = $(id), cur = el.dataset.want ?? el.value;
      el.innerHTML = list.map(o => `<option value="${esc(o.value)}">${esc(o.text)}</option>`).join('');
      el.value = list.some(o => o.value === cur) ? cur : (list[idx] || list[0] || {}).value || '';
      delete el.dataset.want;
    }
    syncMode();
  }

  function range() {
    const v = $('range').value;
    $('custom-range').hidden = v !== 'custom';
    const now = Math.floor(Date.now() / 1000);
    if (v === 'all') return { start: 0, end: now };
    if (v === 'custom') {
      const f = $('from').value ? Date.parse($('from').value + 'T00:00') / 1000 : now - 90 * 86400;
      const t = $('to').value ? Date.parse($('to').value + 'T23:59:59') / 1000 : now;
      return { start: Math.floor(f), end: Math.floor(t) };
    }
    return { start: now - Number(v) * 86400, end: now };
  }

  async function load() {
    const mode = $('mode').value, a = $('a').value, b = $('b').value;
    ++seq;  // whatever happens next, any request still in flight is stale
    writeUrl();
    if (mode === 'change') return loadChange();
    if (!a || !b) { status(mode === 'mobiles' ? 'No mobiles have been heard yet.' : 'Register two igates or digis first.'); return; }
    if (a === b) { status('Pick two different stations.'); return; }
    const { start, end } = range();
    const q = new URLSearchParams({ a, b, start, end, bucket: $('bucket').value, tz: TZ });
    if (mode === 'mobiles') {
      if ($('together').checked) { q.set('together', 'true'); q.set('sep_m', $('sep').value || 200); }
      if ($('interval').value) q.set('interval', $('interval').value);
    } else q.set('match', $('match').value);
    const my = ++seq;
    status('Loading…');
    try {
      const d = await get(`api/compare/${mode}?${q}`);
      if (my !== seq) return;
      DATA = d;
      status('');
      render(d);
    } catch (err) { if (my === seq) status(err.message, true); }
  }
  function status(text, isErr) {
    $('status').textContent = text;
    $('status').className = isErr ? 'error' : 'muted';
    if (text && !text.startsWith('Loading')) {
      $('result').hidden = true; $('change-result').hidden = true;
      DATA = null;  // so a resize doesn't bring the hidden results back
    }
  }

  async function loadChange() {
    ++seq;
    const station = $('station').value;
    if (!station) { status('No stations have been heard yet.'); return; }
    const at = changeAt();
    if (!at) { status('Pick the date of the change.'); return; }
    if (at > Date.now() / 1000) { status('The change has to be in the past.'); return; }
    const q = new URLSearchParams({ station, at, tz: TZ, cell: 500, same_route: $('same-route').checked });
    if ($('period').value === 'now') q.set('until_now', 'true'); else q.set('days', $('period').value);
    if (isNode(station)) q.set('match', $('match').value);
    const my = ++seq;
    status('Loading…');
    try {
      const d = await get(`api/compare/change?${q}`);
      if (my !== seq) return;
      DATA = d;
      status('');
      renderChange(d);
    } catch (err) { if (my === seq) status(err.message, true); }
  }

  // ── rendering ──────────────────────────────────────────────────────────────
  const METHOD = {
    exact: 'Exact: from its uploaded transmit log',
    estimated: 'Estimated from gaps between packets heard',
    mixed: 'Exact where a transmit log was uploaded, estimated elsewhere',
  };

  function tile(side, name, main, ci, lines) {
    return `<div class="tile-name"><span class="dot-${side}"></span> ${esc(name)}</div>
      <div class="tile-value">${main}</div>
      <div class="muted small">${ci ? `95% range ${ci}` : '&nbsp;'}</div>
      ${lines.map(l => `<div class="small">${l}</div>`).join('')}`;
  }

  function render(d) {
    if (d.kind === 'change') return renderChange(d);
    $('result').hidden = false;
    $('change-result').hidden = true;
    $('notes-wrap').hidden = false;
    const notes = [];
    let series, verdict;
    const name = { a: d.kind === 'mobiles' ? d.a.mobile : d.a, b: d.kind === 'mobiles' ? d.b.mobile : d.b };
    if (d.kind === 'mobiles') {
      const ta = d.a.total, tb = d.b.total;
      for (const [side, s] of [['a', d.a], ['b', d.b]]) {
        const t = s.total;
        $(`tile-${side}`).innerHTML = tile(side, s.mobile, pct(t.pct), pctRange(t.lo, t.hi), [
          t.sent ? `${num(t.heard)} of ${num(t.sent)} transmissions reached APRS-IS` : 'Not enough data',
          `<span class="muted">${esc(METHOD[t.method] || 'No estimate: too few packets to work out the beacon interval')}`
            + (t.method && t.method !== 'exact' && s.interval_s ? ` at ${s.interval_s} s` + (s.interval_given ? '' : ' (estimated)') : '') + '</span>',
          `<span class="muted">Direct to an igate ${pct(t.direct_pct)} · stations per packet ${t.stations ?? '—'}</span>`,
        ]);
      }
      verdict = verdictText(name.a, ta.pct, name.b, tb.pct, d.p_value, ta.sent && tb.sent,
        'got', 'of its transmissions through');
      if (!d.together) notes.push('These two may have been in different places. For a fair test of the devices, tick <b>Only when side by side</b> and run both in one vehicle.');
      if (ta.method !== 'exact' || tb.method !== 'exact') notes.push('Estimates assume a steady beacon rate. Smart beaconing makes them rough; a fixed rate, or a transmit log uploaded from the app, makes them exact.');
      if (d.together && !(ta.sent && tb.sent)) notes.push(`No packets found with both within ${d.sep_m} m of each other.`);
      series = [
        { side: 'a', name: name.a, pts: d.a.buckets.map(r => ({ t: r.t, v: r.pct, lo: r.lo, hi: r.hi, k: r.heard, n: r.sent })) },
        { side: 'b', name: name.b, pts: d.b.buckets.map(r => ({ t: r.t, v: r.pct, lo: r.lo, hi: r.hi, k: r.heard, n: r.sent })) },
      ];
      $('chart-title').textContent = 'Share of transmissions heard';
    } else {
      const t = d.total;
      for (const side of ['a', 'b']) {
        const heard = t.both + t[`${side}_only`];
        $(`tile-${side}`).innerHTML = tile(side, d[side], pct(t[`${side}_pct`]), pctRange(t[`${side}_lo`], t[`${side}_hi`]), [
          `Heard ${num(heard)} of ${num(t.total)} transmissions`,
          `<span class="muted">${num(t[`${side}_only`])} that the other didn't</span>`,
        ]);
      }
      verdict = verdictText(d.a, t.a_pct, d.b, t.b_pct, d.p_value, t.total, 'heard', 'of the transmissions anyone heard');
      notes.push(`Both heard ${num(t.both)}; neither heard ${num(t.neither)} (other stations did). The test compares
        the ${num(t.a_only + t.b_only)} transmissions only one of them heard.`);
      if (!d.dupes_feed) notes.push('<b>The duplicates feed is off for this period.</b> APRS-IS passes on only the first copy of each packet, so a station that was second to gate a packet looks like it never heard it. Results are biased towards whichever igate is faster.');
      notes.push('Different sites see different traffic. Swapping the two devices between sites every week or two, with a device note at each swap, cancels that out.');
      series = ['a', 'b'].map(side => ({ side, name: d[side],
        pts: d.buckets.map(r => ({ t: r.t, v: r[`${side}_pct`], lo: r[`${side}_lo`], hi: r[`${side}_hi`], k: r.both + r[`${side}_only`], n: r.total })) }));
      $('chart-title').textContent = 'Share of transmissions heard';
    }
    $('verdict').innerHTML = verdict;
    $('notes').innerHTML = notes.map(n => `<li>${n}</li>`).join('');
    $('legend').innerHTML = series.map(s => `<span><span class="dot-${s.side}"></span> ${esc(s.name)}</span>`).join('');
    drawChart($('chart'), series, d.events, d.bucket);
    renderTable(d, series);
    renderBands(d);
    renderRx(d);
    renderEvents(d);
  }

  function verdictText(a, pa, b, pb, p, enough, verb, what) {
    if (!enough || pa == null || pb == null) return 'Not enough data in this period to compare them yet.';
    const diff = Math.abs(pa - pb) * 100;
    const lead = pa >= pb ? a : b;
    const head = `<b>${esc(a)}</b> ${verb} ${pct(pa)} ${what}; <b>${esc(b)}</b> ${verb} ${pct(pb)}.`;
    if (p == null) return head;
    if (p < 0.05) return `${head} ${esc(lead)} is ahead by ${diff.toFixed(1)} points, and a gap this big is unlikely to be chance (${pval(p)}).`;
    return `${head} The ${diff.toFixed(1)}-point gap could still be chance (${pval(p)}). Keep both running and check back.`;
  }

  function label(t, bucket) {
    const d = new Date(t * 1000);
    if (bucket === 'month') return d.toLocaleDateString(undefined, { month: 'short', year: 'numeric' });
    const s = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    return bucket === 'week' ? `Week of ${s}` : s;
  }

  function renderTable(d, series) {
    $('bucket-name').textContent = d.bucket;
    const byT = new Map();
    series.forEach((s, i) => s.pts.forEach(p => { (byT.get(p.t) || byT.set(p.t, [null, null]).get(p.t))[i] = p; }));
    const rows = [...byT.entries()].sort((x, y) => y[0] - x[0]);
    const cell = p => p && p.n ? `<td class="num">${pct(p.v)}</td><td class="num muted">${num(p.k)} / ${num(p.n)}</td>` : '<td class="num muted">—</td><td></td>';
    $('table').innerHTML = `<thead><tr><th>${esc(d.bucket[0].toUpperCase() + d.bucket.slice(1))}</th>
        <th class="num" colspan="2"><span class="dot-a"></span> ${esc(series[0].name)}</th>
        <th class="num" colspan="2"><span class="dot-b"></span> ${esc(series[1].name)}</th></tr></thead>
      <tbody>${rows.map(([t, [pa, pb]]) => `<tr><td>${esc(label(t, d.bucket))}</td>${cell(pa)}${cell(pb)}</tr>`).join('')
        || '<tr><td colspan="5" class="muted">No data.</td></tr>'}</tbody>`;
  }

  function renderBands(d) {
    const card = $('bands-card');
    card.hidden = d.kind !== 'nodes' || !Object.keys(d.bands).length;
    if (card.hidden) return;
    const sides = [['a', d.a], ['b', d.b]];
    const first = d.bands[d.a] || d.bands[d.b];
    const rng = b => b.to_km == null ? `${b.from_km}+ km` : `${b.from_km}–${b.to_km} km`;
    const bar = (side, b) => !b ? '<td class="muted">no position</td>' : !b.n ? '<td class="muted">—</td>'
      : `<td><div class="hbar"><span class="fill-${side}" style="width:${(b.pct * 100).toFixed(1)}%"></span></div>
          <span class="small">${pct(b.pct)} <span class="muted">of ${num(b.n)}</span></span></td>`;
    $('bands').innerHTML = `<thead><tr><th>From the station</th>${sides.map(([s, c]) => `<th><span class="dot-${s}"></span> ${esc(c)}</th>`).join('')}</tr></thead>
      <tbody>${first.map((b, i) => `<tr><td>${rng(b)}</td>${sides.map(([s, c]) => bar(s, d.bands[c]?.[i])).join('')}</tr>`).join('')}</tbody>`;
  }

  function renderRx(d) {
    const card = $('rx-card');
    const rows = [];
    if (d.kind === 'mobiles') {
      for (const s of [d.a, d.b]) {
        for (const e of s.rx.echoes) rows.push([s.mobile, `${e.via} repeated it`, e.n, e.echo_rate, e.rssi, e.snr]);
        for (const e of s.rx.beacons) rows.push([s.mobile, `${e.via} beacon`, e.n, null, e.rssi, e.snr]);
      }
    }
    card.hidden = !rows.length;
    if (card.hidden) return;
    const sig = (v, u) => v == null ? '—' : `${v} ${u}`;
    $('rx').innerHTML = `<thead><tr><th>Mobile</th><th>Heard</th><th class="num">Packets</th><th class="num">Echo rate</th>
        <th class="num">RSSI</th><th class="num">SNR</th></tr></thead>
      <tbody>${rows.map(r => `<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td><td class="num">${num(r[2])}</td>
        <td class="num">${r[3] == null ? '—' : pct(r[3])}</td><td class="num">${sig(r[4], 'dBm')}</td><td class="num">${sig(r[5], 'dB')}</td></tr>`).join('')}</tbody>`;
  }

  function renderEvents(d) {
    const devs = d.kind === 'change' ? [d.station] : d.kind === 'mobiles' ? [d.a.mobile, d.b.mobile] : [d.a, d.b];
    const current = d.kind === 'change' ? changeNote() : null;
    $('events').innerHTML = d.events.length ? d.events.map(e => `<li><span class="muted">${new Date(e.ts * 1000).toLocaleString()}</span>
        <b>${esc(e.device)}</b> ${esc(e.text)} <span class="muted small">(${e.source === 'app' ? 'from the app' : 'added here'})</span>
        ${current && current.id === e.id ? '<span class="small">(the change compared above)</span>'
          : `<button class="link" data-change="${e.id}" data-device="${esc(e.device)}">Compare before and after</button>`}
        ${ME && (ME.role === 'admin' || ME.id === e.user_id) ? `<button class="link" data-del="${e.id}">delete</button>` : ''}</li>`).join('')
      : '<li class="muted">No notes in this period.</li>';
    const keep = $('ev-device').value;  // a re-render (e.g. on resize) keeps the user's pick
    $('ev-device').innerHTML = devs.map(x => `<option>${esc(x)}</option>`).join('');
    if (devs.includes(keep)) $('ev-device').value = keep;
    if (!$('ev-ts').value) {
      const n = new Date(); n.setMinutes(n.getMinutes() - n.getTimezoneOffset());
      $('ev-ts').value = n.toISOString().slice(0, 16);
    }
  }

  // ── one station, before and after a change ─────────────────────────────────
  const shortDay = ts => new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  const span = side => `${shortDay(side.start)} to ${shortDay(side.end)}`;
  const points = (a, b) => Math.round((a - b) * 100);

  function renderChange(d) {
    $('result').hidden = true;
    $('change-result').hidden = false;
    $('notes-wrap').hidden = false;
    const node = d.station_kind === 'node';
    const note = changeNote();
    const noteText = note ? note.text : `Change on ${shortDay(d.at)}`;
    $('change-title').textContent = node ? `What ${d.station} heard` : `Beacons from ${d.station} that got through`;
    drawSlope($('slope'), d, noteText);

    const b = d.before, a = d.after;
    const unit = node ? 'transmissions' : 'beacons';
    let verdict;
    if (!b.sent || !a.sent) {
      verdict = `Nothing to compare yet: ${num(b.sent)} ${unit} before the change and ${num(a.sent)} after.`;
    } else if (Math.min(b.sent, a.sent) < 20) {
      verdict = `Too little data to judge yet (${num(b.sent)} ${unit} before, ${num(a.sent)} after). Keep driving the route and check back.`;
    } else {
      const delta = points(a.pct, b.pct);
      const lead = node
        ? `After the change, ${esc(d.station)} heard ${pct(a.pct)} of the transmissions anyone heard from these squares`
        : `After the change, ${pct(a.pct)} of beacons got through`;
      const move = delta === 0 ? `, the same as before.` : `, ${delta > 0 ? 'up' : 'down'} from ${pct(b.pct)}.`;
      const chance = d.p_value == null || delta === 0 ? '' : d.p_value < 0.05
        ? ` A difference this size is unlikely to be chance (${pval(d.p_value)}).`
        : ` That could still be chance (${pval(d.p_value)}); give it more time.`;
      verdict = lead + move + chance;
    }
    $('change-verdict').innerHTML = verdict;

    const notes = [`Before is ${span(b)}; after is ${span(a)}.`];
    const sq = d.squares;
    if (d.same_route) {
      notes.push(sq.both
        ? `Compared over the ${num(sq.both)} squares ${node ? 'heard from' : 'driven'} in both periods (${num(sq.before)} before, ${num(sq.after)} after).`
        : `No squares were ${node ? 'heard from' : 'driven'} in both periods. Untick <b>Same ${node ? 'areas' : 'route'} only</b>, or pick a longer period.`);
    }
    const lenB = b.end - b.start, lenA = a.end - a.start;
    if (lenA < lenB - 3600) notes.push(`The after period is still running: ${Math.max(1, Math.round(lenA / 86400))} of ${Math.round(lenB / 86400)} days so far.`);
    if (!node) {
      notes.push(d.exact ? `${num(d.exact)} beacons from transmit logs (exact); the rest estimated from gaps between packets heard.`
        : 'No transmit log in these periods: lost beacons are estimated from gaps between packets heard.');
    } else if (!d.dupes_feed) {
      notes.push('Without the duplicates feed APRS-IS keeps only the first copy of a packet, so this station can look worse than it is.');
    }
    if (d.p_value != null && d.p_value < 0.05) notes.push('Anything else that changed between the periods (beacon rate, weather, a digi going down) shows up here too.');
    $('change-notes').innerHTML = notes.map(n => `<li>${n}</li>`).join('');

    const ser = ['before', 'after'].map(side => ({
      side, name: side === 'before' ? 'Before' : 'After',
      pts: d.days_series.filter(r => r.side === side).map(r => ({ t: r.t, v: r.pct, lo: r.lo, hi: r.hi, k: r.heard, n: r.sent })),
    }));
    const marks = [...d.events];
    if (!note) marks.push({ ts: d.at, device: d.station, text: 'the change' });
    drawChart($('change-chart'), ser, marks, 'day');

    const item = r => {
      const [h1, n1] = r.before, [h2, n2] = r.after;
      const url = `https://www.openstreetmap.org/?mlat=${r.lat}&mlon=${r.lon}#map=15/${r.lat}/${r.lon}`;
      return `<li><a href="${url}" target="_blank" rel="noopener">${r.lat.toFixed(3)}, ${r.lon.toFixed(3)}</a>
        <span class="where-pct">${Math.round(h1 / n1 * 100)}% to ${Math.round(h2 / n2 * 100)}%</span>
        <span class="muted small">${num(n1)} and ${num(n2)} ${unit}</span></li>`;
    };
    $('where-card').hidden = !d.better.length && !d.worse.length;
    $('where-better').innerHTML = d.better.map(item).join('') || '<li class="muted">None</li>';
    $('where-worse').innerHTML = d.worse.map(item).join('') || '<li class="muted">None</li>';
    renderEvents(d);
  }

  // The signature element: a slopegraph from Before to After, the change between.
  function drawSlope(box, d, noteText) {
    box.innerHTML = '';
    const W = Math.max(300, box.clientWidth), narrow = W < 520, H = narrow ? 230 : 250;
    const side = narrow ? 84 : 150;
    const m = { l: side, r: side, t: 50, b: 18 };
    const xb = m.l, xa = W - m.r, xm = W / 2;
    const y = v => m.t + (1 - v) * (H - m.t - m.b);
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: 'slope-svg' }, box);
    const b = d.before, a = d.after;
    box.setAttribute('aria-label', `Before the change ${pct(b.pct)}, after ${pct(a.pct)}.`);

    for (const v of [0, 0.5, 1]) {
      el('line', { x1: xb, x2: xa, y1: y(v), y2: y(v), class: 'slope-grid' }, svg);
      el('text', { x: xm + 6, y: y(v) + 13, class: 'slope-axis' }, svg).textContent = `${v * 100}%`;
    }
    el('line', { x1: xm, x2: xm, y1: m.t - 8, y2: H - m.b, class: 'slope-change' }, svg);
    const nt = el('text', { x: xm, y: 16, class: 'slope-note', 'text-anchor': 'middle' }, svg);
    nt.textContent = noteText.length > (narrow ? 34 : 60) ? noteText.slice(0, narrow ? 33 : 59) + '…' : noteText;
    el('text', { x: xm, y: 31, class: 'slope-axis', 'text-anchor': 'middle' }, svg).textContent = shortDay(d.at);

    const ends = [['before', xb, b, 'end', -14], ['after', xa, a, 'start', 14]];
    if (b.pct != null && a.pct != null) {
      el('line', { x1: xb, y1: y(b.pct), x2: xa, y2: y(a.pct), class: 'slope-line' }, svg);
      const delta = points(a.pct, b.pct);
      const dt = el('text', { x: xm, y: (y(b.pct) + y(a.pct)) / 2 - 12, class: 'slope-delta', 'text-anchor': 'middle' }, svg);
      dt.textContent = delta === 0 ? 'no change' : `${delta > 0 ? '+' : '−'}${Math.abs(delta)} point${Math.abs(delta) === 1 ? '' : 's'}`;
    }
    for (const [cls, x, s, anchor, dx] of ends) {
      const label = cls === 'before' ? 'Before' : 'After';
      if (s.pct == null) {
        el('text', { x: x + dx, y: y(0.5), class: 'slope-sub', 'text-anchor': anchor }, svg).textContent = `${label}: no data`;
        continue;
      }
      if (s.lo != null) el('line', { x1: x, x2: x, y1: y(s.hi), y2: y(s.lo), class: `slope-ci slope-ci-${cls}` }, svg);
      el('circle', { cx: x, cy: y(s.pct), r: 7, class: `slope-pt pt-${cls}` }, svg);
      const ty = Math.min(Math.max(y(s.pct), m.t + 22), H - m.b - 30);
      el('text', { x: x + dx, y: ty + 2, class: 'slope-big', 'text-anchor': anchor }, svg).textContent = pct(s.pct);
      el('text', { x: x + dx, y: ty + 20, class: 'slope-sub', 'text-anchor': anchor }, svg).textContent = label;
      el('text', { x: x + dx, y: ty + 35, class: 'slope-sub', 'text-anchor': anchor }, svg)
        .textContent = `${num(s.heard)} of ${num(s.sent)}`;
    }
  }

  // ── chart: two lines over time with confidence bands and note markers ─────
  function el(tag, attrs, parent) {
    const e = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  }

  function drawChart(box, series, events, bucket) {
    box.innerHTML = '';
    const all = series.flatMap(s => s.pts.filter(p => p.v != null));
    if (!all.length) { box.innerHTML = '<p class="muted">Nothing to plot yet.</p>'; return; }
    const W = Math.max(300, box.clientWidth), H = W < 520 ? 220 : 280;
    const m = { l: 44, r: 12, t: 14, b: 28 };
    const ts = [...new Set(all.map(p => p.t))].sort((x, y) => x - y);
    let t0 = ts[0], t1 = ts[ts.length - 1];
    if (t0 === t1) { t0 -= 86400; t1 += 86400; }
    const lows = all.map(p => p.lo ?? p.v);
    const yMin = Math.max(0, Math.floor(Math.min(...lows) * 10) / 10 - 0.1);
    const x = t => m.l + (t - t0) / (t1 - t0) * (W - m.l - m.r);
    const y = v => m.t + (1 - (v - yMin) / (1 - yMin)) * (H - m.t - m.b);
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: 'chart-svg' }, box);
    box.setAttribute('aria-label', `Line chart of ${series.map(s => s.name).join(' and ')} by ${bucket}; the table below has the same numbers.`);

    // grid and axes
    const steps = [];
    for (let v = yMin; v <= 1.0001; v += (1 - yMin) <= 0.3 ? 0.05 : 0.1) steps.push(Math.round(v * 100) / 100);
    for (const v of steps) {
      el('line', { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), class: 'grid' }, svg);
      el('text', { x: m.l - 6, y: y(v) + 4, class: 'axis', 'text-anchor': 'end' }, svg).textContent = `${Math.round(v * 100)}%`;
    }
    const nLabels = Math.max(2, Math.floor((W - m.l - m.r) / 90));
    const every = Math.max(1, Math.ceil(ts.length / nLabels));
    ts.forEach((t, i) => {
      if (i % every) return;
      const anchor = ts.length < 2 ? 'middle' : i === 0 ? 'start' : x(t) > W - m.r - 30 ? 'end' : 'middle';
      el('text', { x: x(t), y: H - 8, class: 'axis', 'text-anchor': anchor }, svg)
        .textContent = label(t, bucket).replace('Week of ', '');
    });

    // note markers
    for (const e of events || []) {
      if (e.ts < t0 || e.ts > t1) continue;
      const g = el('g', { class: 'event' }, svg);
      el('line', { x1: x(e.ts), x2: x(e.ts), y1: m.t, y2: H - m.b }, g);
      el('title', {}, g).textContent = `${new Date(e.ts * 1000).toLocaleDateString()} ${e.device}: ${e.text}`;
      el('rect', { x: x(e.ts) - 6, y: m.t, width: 12, height: H - m.t - m.b, fill: 'transparent' }, g);
    }

    // bands, then lines, then points
    for (const s of series) {
      const pts = s.pts.filter(p => p.v != null && p.lo != null);
      if (pts.length > 1) {
        const d = pts.map((p, i) => `${i ? 'L' : 'M'}${x(p.t)},${y(p.hi)}`).join('')
          + pts.slice().reverse().map(p => `L${x(p.t)},${y(p.lo)}`).join('') + 'Z';
        el('path', { d, class: `band band-${s.side}` }, svg);
      }
    }
    for (const s of series) {
      const pts = s.pts.filter(p => p.v != null);
      if (pts.length > 1) el('path', { d: pts.map((p, i) => `${i ? 'L' : 'M'}${x(p.t)},${y(p.v)}`).join(''), class: `line line-${s.side}` }, svg);
      for (const p of pts) el('circle', { cx: x(p.t), cy: y(p.v), r: pts.length > 40 ? 2.5 : 4, class: `pt pt-${s.side}` }, svg);
    }

    // hover: crosshair and tooltip for the nearest period
    const cross = el('line', { y1: m.t, y2: H - m.b, class: 'cross', visibility: 'hidden' }, svg);
    const hit = el('rect', { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: 'transparent' }, svg);
    const tip = $('tip');
    const move = ev => {
      const r = svg.getBoundingClientRect();
      const px = (ev.clientX - r.left) * (W / r.width);
      const t = ts.reduce((best, c) => Math.abs(x(c) - px) < Math.abs(x(best) - px) ? c : best, ts[0]);
      cross.setAttribute('x1', x(t)); cross.setAttribute('x2', x(t)); cross.setAttribute('visibility', 'visible');
      const rows = series.map(s => {
        const p = s.pts.find(q => q.t === t);
        return `<tr><td><span class="dot-${s.side}"></span> ${esc(s.name)}</td><td class="num">${p && p.v != null ? pct(p.v) : '—'}</td>
          <td class="num muted">${p && p.n ? `${num(p.k)}/${num(p.n)}` : ''}</td></tr>`;
      }).join('');
      tip.innerHTML = `<div class="tip-h">${esc(label(t, bucket))}</div><table>${rows}</table>`;
      tip.hidden = false;
      const tx = Math.min(ev.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
      tip.style.left = `${tx}px`; tip.style.top = `${ev.clientY + 14 + window.scrollY}px`;
    };
    hit.addEventListener('pointermove', move);
    hit.addEventListener('pointerdown', move);
    hit.addEventListener('pointerleave', () => { tip.hidden = true; cross.setAttribute('visibility', 'hidden'); });
  }

  // ── device notes ───────────────────────────────────────────────────────────
  $('event-form').addEventListener('submit', async e => {
    e.preventDefault();
    const m = $('ev-msg');
    try {
      await send('POST', 'api/events', { device: $('ev-device').value, ts: Math.floor(Date.parse($('ev-ts').value) / 1000),
        text: $('ev-text').value.trim() });
      $('ev-text').value = ''; m.className = 'okmsg'; m.textContent = 'Added.';
      if (isChange()) await fillChanges();  // the new note belongs in the "change" list
      load();
    } catch (err) { m.className = 'error'; m.textContent = err.message; }
  });
  $('events').addEventListener('click', async e => {
    const c = e.target.closest('[data-change]');
    if (c) {  // jump to the before/after comparison for this note
      $('mode').value = 'change';
      $('station').dataset.want = c.dataset.device;
      $('change').dataset.want = `n${c.dataset.change}`;
      fillStations();
      if ($('station').value !== c.dataset.device) {
        status(`${c.dataset.device} isn't in the station list, so it can't be compared here.`, true);
        return;
      }
      await fillChanges();
      load();
      window.scrollTo({ top: 0, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
      return;
    }
    const b = e.target.closest('[data-del]');
    if (!b) return;
    try {
      await send('DELETE', `api/events/${b.dataset.del}`);
      if (isChange()) await fillChanges();  // it may have been the change being compared
      load();
    }
    catch (err) { $('ev-msg').className = 'error'; $('ev-msg').textContent = err.message; }
  });

  // ── start ──────────────────────────────────────────────────────────────────
  for (const id of CTRL) {
    $(id).addEventListener('change', async () => {
      if (id === 'mode') { fillStations(); if (isChange()) await fillChanges(); }
      if (id === 'station') await fillChanges();
      if (id === 'change') syncMode();
      if (id === 'range' && $('range').value === 'custom') { range(); return; }
      load();
    });
  }
  let rs;
  window.addEventListener('resize', () => { clearTimeout(rs); rs = setTimeout(() => DATA && render(DATA), 150); });

  (async () => {
    readUrl();
    for (const k of ['mode', 'range', 'bucket', 'match', 'from', 'to', 'sep', 'interval', 'period', 'change-at']) applyWant($(k));
    range();
    try {
      [STATIONS, MOBILES] = await Promise.all([get('api/stations'), get('api/mobiles')]);
    } catch (err) { status(err.message, true); return; }
    try {
      ME = (await get('api/me')).user;
    } catch { ME = null; }
    if (ME) {
      $('account-link').textContent = ME.username;
      const ok = ME.status === 'active';
      $('event-form').hidden = !ok;
      $('event-login').hidden = ok;
      if (!ok) $('event-login').textContent = 'Notes can be added once your account is approved.';
    }
    fillStations();
    if (isChange()) await fillChanges();
    load();
  })();
})();
