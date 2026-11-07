/* APRS coverage: admin page */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const fmt = ts => ts ? new Date(ts * 1000).toLocaleString() : '—';
  const KIND = { mobile: 'Mobile', igate: 'Igate', digi: 'Digi', digi_igate: 'Digi + igate' };

  async function api(method, url, body) {
    const r = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await r.json().catch(() => ({}));
    if (r.status === 401 && !url.endsWith('/login')) { showLogin(); throw new Error('Login required'); }
    if (!r.ok) {
      const d = data.detail;
      throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map(x => x.msg).join('; ') : `HTTP ${r.status}`);
    }
    return data;
  }

  function showLogin() { $('login-card').hidden = false; $('app').hidden = true; $('logout').hidden = true; $('password').focus(); }
  function showApp() { $('login-card').hidden = true; $('app').hidden = false; $('logout').hidden = false; loadAll(); showTab(['nodecfg', 'logs'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'stations'); }

  $('login-form').addEventListener('submit', async e => {
    e.preventDefault();
    $('login-err').textContent = '';
    try { await api('POST', 'api/admin/login', { password: $('password').value }); $('password').value = ''; showApp(); }
    catch (err) { $('login-err').textContent = err.message; }
  });
  $('logout').addEventListener('click', async () => { await api('POST', 'api/admin/logout'); showLogin(); });

  // ── status ────────────────────────────────────────────────────────────────
  async function loadStatus() {
    const s = await api('GET', 'api/admin/status');
    const i = s.ingest;
    const counts = Object.entries(i.counts).filter(([k]) => k !== 'rejects_since_prune')
      .map(([k, v]) => `${k}: ${v}`).join(', ') || '—';
    const rows = [
      ['State', i.connected ? '<span class="okmsg">Connected</span>' : `<span class="error">Disconnected</span> ${esc(i.error)}`],
      ['Server', `${esc(i.server)} as ${esc(i.login || '(APRS_LOGIN not set)')} (read-only, passcode -1)`],
      ['Server says', esc(i.server_line) || '—'],
      ['Filter', `<code>${esc(i.filter) || '—'}</code>`],
      ['Connected since', fmt(i.connected_since)],
      ['Last accepted packet', fmt(i.last_packet_ts)],
      ['Counters (since restart)', esc(counts)],
      ['Stored packets', `${s.db.packets.toLocaleString()} (${fmt(s.db.first)} → ${fmt(s.db.last)})`],
      ['Database', `<code>${esc(s.db_path)}</code>`],
    ];
    $('status').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
  }

  // ── stations ──────────────────────────────────────────────────────────────
  let stations = [];
  async function loadStations() {
    stations = await api('GET', 'api/admin/stations');
    $('stations').innerHTML = stations.map(s => {
      const lat = s.lat ?? s.auto_lat, lon = s.lon ?? s.auto_lon;
      const pos = s.kind === 'mobile' ? '—' : lat != null
        ? `${lat.toFixed(5)}, ${lon.toFixed(5)} <span class="muted">${s.lat != null ? 'manual' : 'auto'}</span>`
        : '<span class="muted">waiting for beacon</span>';
      return `<tr>
        <td>${s.color ? `<span class="swatch" style="background:${esc(s.color)}"></span> ` : ''}<b>${esc(s.callsign)}</b></td>
        <td>${KIND[s.kind]}</td><td>${esc(s.label)}${s.notes ? `<br><span class="muted small">${esc(s.notes)}</span>` : ''}</td>
        <td>${pos}</td><td>${s.kind === 'mobile' ? '—' : fmt(s.last_beacon)}</td><td>${s.active ? 'yes' : 'no'}</td>
        <td class="actions"><button class="btn secondary" data-edit="${s.id}">Edit</button><button class="btn purge" data-purge="${s.id}" title="${s.kind === 'mobile'
          ? 'Delete every stored packet from this mobile. The registration is kept.'
          : 'Delete the stored packets this station heard directly (its coverage). The registration is kept.'}">Purge data</button><button class="btn danger" data-del="${s.id}">Delete</button></td>
      </tr>`;
    }).join('') || '<tr><td colspan="7" class="muted">No stations yet. Add your mobiles, igates and digis above.</td></tr>';
    document.querySelectorAll('[data-edit]').forEach(b => b.addEventListener('click', () => editStation(+b.dataset.edit)));
    document.querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', () => deleteStation(+b.dataset.del, b)));
    document.querySelectorAll('[data-purge]').forEach(b => b.addEventListener('click', () => purgeStation(+b.dataset.purge, b)));
  }

  async function purgeStation(id, btn) {
    const s = stations.find(x => x.id === id);
    if (!s || btn.disabled) return;
    if (btn.dataset.armed !== '1') {
      // First click: show how much would go, then wait for a second click.
      btn.disabled = true;
      try {
        const { count } = await api('GET', `api/admin/stations/${id}/packets`);
        if (!count) { alertMsg(`No stored packets for ${s.callsign}.`); return; }
        btn.dataset.armed = '1';
        btn.textContent = `Purge ${count.toLocaleString()}?`;
        btn.classList.add('armed');
        setTimeout(() => {
          btn.dataset.armed = ''; btn.textContent = 'Purge data'; btn.classList.remove('armed');
        }, 4000);
      } catch (e) { alertMsg(e.message, true); }
      finally { btn.disabled = false; }
      return;
    }
    try {
      const { deleted } = await api('DELETE', `api/admin/stations/${id}/packets`);
      alertMsg(`Purged ${deleted.toLocaleString()} packets for ${s.callsign}.`);
      loadAll();
    } catch (e) { alertMsg(e.message, true); }
  }

  function kindChanged() {
    const mobile = $('s-kind').value === 'mobile';
    document.querySelectorAll('.infra-only').forEach(el => { el.hidden = mobile; });
    $('kind-help').textContent = mobile
      ? 'Mobiles: use an exact callsign-SSID (K7ABC-9), or K7ABC* / K7ABC-* to cover every SSID. For trackers that beacon as APRS objects, use SENDER:OBJECT (K7SWI:CAR01, or K7SWI:CAR* for a fleet). Packets from mobiles are what draw coverage.'
      : 'Igates/digis: exact callsign-SSID as it appears in packet paths. Leave the position blank to take it from the station’s own beacons.';
  }
  $('s-kind').addEventListener('change', kindChanged);

  function resetForm() {
    $('station-form').reset();
    $('s-id').value = '';
    $('s-active').checked = true;
    $('form-title').textContent = 'Register a station';
    $('s-save').textContent = 'Add station';
    $('s-cancel').hidden = true;
    kindChanged();
  }
  $('s-cancel').addEventListener('click', resetForm);

  function editStation(id) {
    const s = stations.find(x => x.id === id);
    if (!s) return;
    $('s-id').value = s.id;
    $('s-callsign').value = s.callsign;
    $('s-kind').value = s.kind;
    $('s-label').value = s.label;
    $('s-notes').value = s.notes;
    $('s-usecolor').checked = !!s.color;
    $('s-color').value = s.color || '#2f81f7';
    $('s-lat').value = s.lat ?? '';
    $('s-lon').value = s.lon ?? '';
    $('s-active').checked = !!s.active;
    $('form-title').textContent = `Edit ${s.callsign}`;
    $('s-save').textContent = 'Save changes';
    $('s-cancel').hidden = false;
    kindChanged();
    $('station-form').scrollIntoView({ behavior: 'smooth' });
  }

  async function deleteStation(id, btn) {
    // Two-click confirm instead of window.confirm()
    if (btn.dataset.armed !== '1') {
      btn.dataset.armed = '1';
      btn.textContent = 'Confirm';
      setTimeout(() => { btn.dataset.armed = ''; btn.textContent = 'Delete'; }, 4000);
      return;
    }
    try { await api('DELETE', `api/admin/stations/${id}`); loadAll(); }
    catch (e) { alertMsg(e.message, true); }
  }

  function alertMsg(text, isErr) {
    const m = $('form-msg');
    m.className = isErr ? 'error' : 'okmsg';
    m.textContent = text;
    if (!isErr) setTimeout(() => { if (m.textContent === text) m.textContent = ''; }, 4000);
  }

  $('station-form').addEventListener('submit', async e => {
    e.preventDefault();
    const num = v => v === '' ? null : Number(v);
    const mobile = $('s-kind').value === 'mobile';
    const body = {
      callsign: $('s-callsign').value.trim(),
      kind: $('s-kind').value,
      label: $('s-label').value.trim(),
      notes: $('s-notes').value.trim(),
      color: $('s-usecolor').checked ? $('s-color').value : null,
      lat: mobile ? null : num($('s-lat').value),
      lon: mobile ? null : num($('s-lon').value),
      active: $('s-active').checked,
    };
    const id = $('s-id').value;
    try {
      if (id) await api('PUT', `api/admin/stations/${id}`, body);
      else await api('POST', 'api/admin/stations', body);
      alertMsg(id ? 'Saved.' : `Added ${body.callsign.toUpperCase()}.`);
      resetForm();
      loadAll();
    } catch (err) { alertMsg(err.message, true); }
  });
  $('s-color').addEventListener('input', () => { $('s-usecolor').checked = true; });

  // ── rejects ───────────────────────────────────────────────────────────────
  async function loadRejects() {
    const rows = await api('GET', 'api/admin/rejects?limit=100');
    $('rejects').innerHTML = rows.map(r =>
      `<tr><td>${fmt(r.ts)}</td><td>${esc(r.source)}</td><td>${esc(r.reason)}</td><td><pre class="raw">${esc(r.raw)}</pre></td></tr>`).join('')
      || '<tr><td colspan="4" class="muted">None.</td></tr>';
  }

  // ── password ──────────────────────────────────────────────────────────────
  $('pw-form').addEventListener('submit', async e => {
    e.preventDefault();
    const m = $('pw-msg');
    try {
      await api('POST', 'api/admin/password', { current: $('pw-cur').value, new: $('pw-new').value });
      m.className = 'okmsg'; m.textContent = 'Password changed.';
      $('pw-form').reset();
    } catch (err) { m.className = 'error'; m.textContent = err.message; }
  });

  function loadAll() {
    loadStatus().catch(() => {});
    loadStations().catch(() => {});
    loadRejects().catch(() => {});
  }

  // ── tabs ──────────────────────────────────────────────────────────────────
  function showTab(name) {
    document.querySelectorAll('.admin-tabs .tab').forEach(b => {
      b.classList.toggle('active', b.dataset.tab === name);
      b.setAttribute('aria-selected', b.dataset.tab === name);
    });
    document.querySelectorAll('[data-pane]').forEach(p => { p.hidden = p.dataset.pane !== name; });
    try { history.replaceState(null, '', name === 'stations' ? location.pathname + location.search : `#${name}`); } catch { /* ignore */ }
    if (name === 'nodecfg') loadRemote().catch(e => rcMsg(e.message, 'error'));
    if (name === 'logs') startLogs().catch(e => { $('lg-err').textContent = e.message; });
  }
  document.querySelectorAll('.admin-tabs .tab').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
  const activeTab = () => document.querySelector('.admin-tabs .tab.active')?.dataset.tab;

  // ── node config ───────────────────────────────────────────────────────────
  const rc = { fields: {}, nodes: [], status: {}, selected: null, busy: false, unlockAt: {} };
  const NUMERIC = new Set(['int', 'float']);
  const node = () => rc.nodes.find(n => n.callsign === rc.selected);

  // Sending callsign: kept in this browser only (localStorage), sent with each
  // command. The server checks it and computes the passcode itself; the one
  // shown here uses the same standard APRS-IS hash.
  const CALL_KEY = 'acov.nodecfg.callsign';
  const CALL_RE = /^[A-Z0-9]{1,2}[0-9][A-Z]{1,4}(-[A-Z0-9]{1,2})?$/;
  function aprsPasscode(call) {
    const base = call.toUpperCase().split('-')[0].slice(0, 10);
    let h = 0x73e2;
    for (let i = 0; i < base.length; i += 2) {
      h ^= base.charCodeAt(i) << 8;
      if (i + 1 < base.length) h ^= base.charCodeAt(i + 1);
    }
    return h & 0x7fff;
  }
  const sendAs = () => { const c = $('rc-call').value.trim().toUpperCase(); return CALL_RE.test(c) ? c : ''; };
  function callChanged() {
    const raw = $('rc-call').value.trim().toUpperCase();
    const ok = CALL_RE.test(raw);
    $('rc-pass').value = ok ? aprsPasscode(raw) : '';
    $('rc-call').setCustomValidity(raw && !ok ? 'Callsign like K7ABC or K7ABC-CF' : '');
    try { ok ? localStorage.setItem(CALL_KEY, raw) : raw || localStorage.removeItem(CALL_KEY); } catch { /* storage blocked */ }
    updateButtons();
  }
  try { $('rc-call').value = localStorage.getItem(CALL_KEY) || ''; } catch { /* storage blocked */ }
  $('rc-call').addEventListener('input', callChanged);
  $('rc-call').addEventListener('change', () => { $('rc-call').value = $('rc-call').value.trim().toUpperCase(); callChanged(); loadRemote().catch(() => {}); });
  callChanged();

  function rcMsg(text, cls) { const m = $('rc-msg'); m.className = cls || ''; m.textContent = text; }

  async function loadRemote() {
    const call = sendAs();
    const d = await api('GET', `api/admin/remote${call ? `?login=${encodeURIComponent(call)}` : ''}`);
    rc.fields = d.fields; rc.nodes = d.nodes; rc.status = d.status;
    rc.unlockAt = {};
    for (const [call, secs] of Object.entries(d.status.unlocked)) rc.unlockAt[call] = Date.now() + secs * 1000;
    if (rc.selected && !node()) rc.selected = null;
    const conn = d.status.connection;
    $('rc-status').innerHTML = [
      !call ? '<span class="warnmsg">Enter your callsign to send commands.</span>'
        : conn.login ? `<span class="okmsg">Connected to APRS-IS as ${esc(conn.login)}</span> <span class="muted small">${esc(conn.server_line)}</span>`
        : `<span class="muted">Connects to ${esc(d.status.server)} as ${esc(call)} on the first command.</span>`,
      d.status.busy ? `<span class="warnmsg">Waiting on ${esc(d.status.busy)}.</span>` : '',
    ].join(' ');
    renderNodes();
    renderLog(d.log);
    renderNode(false);
  }

  function roleName(n) {
    const ro = n.fields.RO;
    return ro == null ? '<span class="muted">not read</span>' : esc(rc.fields.RO.values[ro] ?? ro);
  }

  function renderNodes() {
    $('rc-nodes').innerHTML = rc.nodes.map(n => `<tr class="${n.callsign === rc.selected ? 'selected' : ''}">
        <td><b>${esc(n.callsign)}</b>${n.label ? `<br><span class="muted small">${esc(n.label)}</span>` : ''}</td>
        <td>${KIND[n.kind]}</td><td>${roleName(n)}</td><td>${fmt(n.updated_at)}</td>
        <td class="actions"><button class="btn secondary" data-rc="${esc(n.callsign)}">${n.callsign === rc.selected ? 'Selected' : 'Configure'}</button></td>
      </tr>`).join('')
      || '<tr><td colspan="5" class="muted">No active igates or digis. Register them on the Stations tab.</td></tr>';
    document.querySelectorAll('[data-rc]').forEach(b => b.addEventListener('click', () => selectNode(b.dataset.rc)));
  }

  function selectNode(call) {
    if (rc.busy) return;
    rc.selected = call;
    $('rc-token').value = '';
    rcMsg('');
    renderNodes();
    renderNode(true);
    $('rc-node').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function inputFor(code, spec, value) {
    const v = value ?? '';
    if (spec.type === 'enum') {
      const opts = Object.entries(spec.values).map(([k, label]) =>
        `<option value="${esc(k)}"${k === String(v) ? ' selected' : ''}>${esc(label)} (${esc(k)})</option>`).join('');
      return `<select data-code="${code}">${v === '' ? '<option value="" selected>—</option>' : ''}${opts}</select>`;
    }
    const attrs = spec.type === 'int' ? `type="number" step="1" min="${spec.min}" max="${spec.max}"`
      : spec.type === 'float' ? `type="number" step="any" min="${spec.min}" max="${spec.max}"`
      : `type="text" maxlength="${spec.max_len || 9}"`;
    return `<input ${attrs} data-code="${code}" value="${esc(v)}" placeholder="${esc(spec.help || '')}">`;
  }

  function shown(code, value) {
    if (value == null) return '<span class="muted">?</span>';
    const spec = rc.fields[code];
    if (spec.type === 'enum') return `${esc(spec.values[value] ?? value)} <span class="muted">(${esc(value)})</span>`;
    return value === '' ? '<span class="muted">(empty)</span>' : `<code>${esc(value)}</code>`;
  }

  // Rebuild the inputs (resetInputs) or only refresh the "On node" column,
  // keeping whatever the admin has typed.
  function renderNode(resetInputs) {
    const n = node();
    $('rc-node').hidden = !n;
    if (!n) return;
    $('rc-title').textContent = `${n.callsign}${n.label ? ` · ${n.label}` : ''}`;
    $('rc-read-at').textContent = n.updated_at
      ? `Values last reported by the node ${fmt(n.updated_at)}.`
      : 'Not read yet. Read the node first so changes are made against its current settings.';
    if (resetInputs || !$('rc-fields').children.length) {
      $('rc-fields').innerHTML = Object.entries(rc.fields).map(([code, spec]) => `<tr data-row="${code}">
          <td>${esc(spec.label)} <span class="muted small">${code}</span>${spec.reboot ? '<br><span class="reboot">reboots node</span>' : ''}</td>
          <td data-cur="${code}"></td>
          <td>${inputFor(code, spec, n.fields[code])}</td>
        </tr>`).join('');
      $('rc-fields').querySelectorAll('[data-code]').forEach(el => {
        el.addEventListener('input', markChanged);
        el.addEventListener('change', markChanged);
      });
    }
    for (const code of Object.keys(rc.fields)) {
      $('rc-fields').querySelector(`[data-cur="${code}"]`).innerHTML = shown(code, n.fields[code]);
    }
    markChanged();
    updateLock();
    updateButtons();
  }

  function isChanged(code, value) {
    const cur = node()?.fields[code];
    if (cur == null) return value !== '';
    if (NUMERIC.has(rc.fields[code].type)) return value !== '' && Number(value) !== Number(cur);
    return value !== String(cur);
  }

  function changes() {
    const out = {};
    $('rc-fields').querySelectorAll('[data-code]').forEach(el => {
      const v = el.value.trim();
      if (isChanged(el.dataset.code, v)) out[el.dataset.code] = v;
    });
    return out;
  }

  function markChanged() {
    const ch = changes();
    $('rc-fields').querySelectorAll('tr[data-row]').forEach(tr => tr.classList.toggle('changed', tr.dataset.row in ch));
    const n = Object.keys(ch).length;
    $('rc-write').textContent = n ? `Write ${n} change${n > 1 ? 's' : ''}` : 'Write changes';
  }

  function unlockedFor() {
    const at = rc.unlockAt[rc.selected];
    return at && at > Date.now() ? Math.round((at - Date.now()) / 1000) : 0;
  }

  function updateLock() {
    if (!rc.selected) return;
    const s = unlockedFor();
    const el = $('rc-lock');
    el.className = s ? 'okmsg small' : 'muted small';
    el.textContent = s ? `Unlocked, ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')} left` : 'Locked';
  }
  setInterval(() => { if (activeTab() === 'nodecfg') updateLock(); }, 1000);

  function updateButtons() {
    const off = rc.busy || !sendAs();
    ['rc-read', 'rc-unlock', 'rc-write'].forEach(id => { $(id).disabled = off; });
    $('rc-write').disabled = off || !node()?.updated_at;
  }

  // Runs one command, showing a live "waiting" counter. Returns the parsed
  // reply, or null after showing an error (HTTP error or a CS ERR reply).
  async function command(kind, body) {
    const call = rc.selected;
    rc.busy = true;
    updateButtons();
    const t0 = Date.now();
    const verb = { read: 'Reading', unlock: 'Unlocking', write: 'Writing to' }[kind];
    const tick = () => rcMsg(`${verb} ${call}… ${Math.round((Date.now() - t0) / 1000)} s (up to 45 s over RF)`, 'muted');
    tick();
    const timer = setInterval(tick, 1000);
    try {
      const r = await api('POST', `api/admin/remote/${encodeURIComponent(call)}/${kind}`, { login: sendAs(), ...body });
      if (r.type === 'ERR') { rcMsg(`${call}: ${r.message}`, 'error'); return null; }
      return r;
    } catch (e) {
      rcMsg(e.message, 'error');
      return null;
    } finally {
      clearInterval(timer);
      rc.busy = false;
      await loadRemote().catch(() => {});
    }
  }

  $('rc-read').addEventListener('click', async () => {
    const r = await command('read', {});
    if (r) { renderNode(true); rcMsg(`Read ${Object.keys(r.fields).length} settings from ${rc.selected}.`, 'okmsg'); }
  });

  async function unlock() {
    const token = $('rc-token').value;
    if (!token) { rcMsg('Enter the node’s unlock token.', 'error'); $('rc-token').focus(); return false; }
    const r = await command('unlock', { token });
    $('rc-token').value = '';  // also after a failure, so a wrong token isn't silently resent
    if (!r) return false;
    rcMsg(`${rc.selected} is unlocked for ${Math.round(r.seconds / 60)} min.`, 'okmsg');
    return true;
  }
  $('rc-unlock').addEventListener('click', unlock);

  $('rc-form').addEventListener('submit', async e => {
    e.preventDefault();
    const ch = changes();
    if (!Object.keys(ch).length) { rcMsg('Nothing changed.', 'muted'); return; }
    if (!unlockedFor() && !(await unlock())) return;
    const r = await command('write', { fields: ch });
    if (!r) return;
    renderNode(true);
    const done = Object.entries(r.fields).map(([k, v]) => `${k}=${v}`).join(', ');
    rcMsg(`${rc.selected} saved ${done}.${r.reboot ? ` It reboots in about ${r.reboot} s.` : ''}`, 'okmsg');
  });
  $('rc-reset').addEventListener('click', () => { renderNode(true); rcMsg(''); });

  function renderLog(rows) {
    const cls = r => r.startsWith('ERR') || r.startsWith('error') ? 'error' : r === 'unsolicited' ? 'muted' : 'okmsg';
    $('rc-log').innerHTML = rows.map(r => `<tr><td>${fmt(r.ts)}</td><td>${esc(r.callsign)}${r.login ? `<br><span class="muted small">from ${esc(r.login)}</span>` : ''}</td>
        <td><code>${esc(r.command)}</code></td><td class="${cls(r.result)}">${esc(r.result)}</td>
        <td><pre class="raw">${esc(r.detail)}</pre></td></tr>`).join('')
      || '<tr><td colspan="5" class="muted">Nothing sent yet.</td></tr>';
  }

  // ── logs ──────────────────────────────────────────────────────────────────
  // Keeps the newest N entries of one log in memory, polls for new ones every
  // 2 s while the tab is open and not paused, and re-renders the filtered view.
  const LG_PREFS = 'acov.logs.prefs';
  const LG_CHECKS = ['lg-regex', 'lg-case', 'lg-wrap', 'lg-utc'];
  const LEVEL_CLASS = { rx: 'lv-rx', tx: 'lv-tx', srv: 'lv-srv', sys: 'lv-sys', WARNING: 'lv-warn', ERROR: 'lv-err', CRITICAL: 'lv-err' };
  const lg = { sources: [], source: null, entries: [], visible: [], epoch: null, lastId: null,
    live: true, timer: null, loading: false, re: null, reErr: '' };

  function lgSavePrefs() {
    try {
      localStorage.setItem(LG_PREFS, JSON.stringify({
        source: lg.source, limit: $('lg-limit').value,
        ...Object.fromEntries(LG_CHECKS.map(id => [id, $(id).checked])),
      }));
    } catch { /* storage blocked */ }
  }
  function lgLoadPrefs() {
    try {
      const p = JSON.parse(localStorage.getItem(LG_PREFS) || '{}');
      LG_CHECKS.forEach(id => { if (typeof p[id] === 'boolean') $(id).checked = p[id]; });
      if ([...$('lg-limit').options].some(o => o.value === p.limit)) $('lg-limit').value = p.limit;
      return p;
    } catch { return {}; }
  }

  async function startLogs() {
    if (!lg.sources.length) {
      lg.sources = await api('GET', 'api/admin/logs');
      $('lg-source').innerHTML = lg.sources.map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');
      const p = lgLoadPrefs();
      $('lg-source').value = lg.sources.some(s => s.id === p.source) ? p.source : lg.sources[0].id;
      $('lg-view').classList.toggle('wrap', $('lg-wrap').checked);
    }
    if (lg.source !== $('lg-source').value) await switchSource();
    schedulePoll(lg.live ? 0 : 2000);
  }

  async function switchSource() {
    lg.source = $('lg-source').value;
    lg.entries = []; lg.lastId = null; lg.epoch = null;
    $('lg-desc').textContent = lg.sources.find(s => s.id === lg.source)?.description || '';
    $('lg-level').value = '';
    lgSavePrefs();
    renderLogView();
    await fetchLog();
  }

  async function fetchLog() {
    if (lg.loading) return;
    lg.loading = true;
    const src = lg.source;
    try {
      const max = +$('lg-limit').value;
      const q = lg.lastId == null ? `limit=${max}` : `after=${lg.lastId}&limit=5000`;
      const r = await api('GET', `api/admin/logs/${encodeURIComponent(src)}?${q}`);
      $('lg-err').textContent = '';
      if (src !== lg.source) return;  // switched while loading
      if (lg.epoch !== null && r.epoch !== lg.epoch) {
        // The server restarted and its in-memory log ids began again: start over.
        lg.entries = []; lg.lastId = null; lg.epoch = null;
        renderLogView();
        return;
      }
      lg.epoch = r.epoch;
      if (r.entries.length) {
        lg.entries.push(...r.entries);
        lg.lastId = r.entries[r.entries.length - 1].id;
        if (lg.entries.length > max) lg.entries.splice(0, lg.entries.length - max);
      }
      if (r.entries.length || lg.visible.length === 0) renderLogView();
    } catch (e) {
      $('lg-err').textContent = `Could not load the log: ${e.message}`;
    } finally {
      lg.loading = false;
    }
  }

  function schedulePoll(ms) { clearTimeout(lg.timer); lg.timer = setTimeout(pollLog, ms); }
  async function pollLog() {
    if (activeTab() !== 'logs' || $('app').hidden) return;  // showTab('logs') restarts it
    if (lg.live && !document.hidden) await fetchLog();
    schedulePoll(2000);
  }

  function buildMatcher() {
    const q = $('lg-q').value;
    lg.reErr = '';
    if (!q) return null;
    const src = $('lg-regex').checked ? q : q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    try { return new RegExp(src, $('lg-case').checked ? 'g' : 'gi'); }
    catch (e) { lg.reErr = `Invalid regex: ${e.message}`; return null; }
  }

  function fmtLogTs(ts) {
    const d = new Date(ts * 1000);
    if ($('lg-utc').checked) return d.toISOString().replace('T', ' ').slice(0, 19) + 'Z';
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  // Escape s and wrap the filter's matches in <mark>.
  function hl(s) {
    s = String(s ?? '');
    const re = lg.re;
    if (!re) return esc(s);
    re.lastIndex = 0;
    let out = '', i = 0, m;
    while ((m = re.exec(s))) {
      if (!m[0].length) { re.lastIndex++; continue; }  // zero-length match: avoid looping
      out += `${esc(s.slice(i, m.index))}<mark>${esc(m[0])}</mark>`;
      i = m.index + m[0].length;
    }
    return out + esc(s.slice(i));
  }

  const logLine = e => `${e.level}\t${e.tag}\t${e.text}\t${e.note}`;

  function renderLogView() {
    lg.re = buildMatcher();
    $('lg-q').classList.toggle('invalid', !!lg.reErr);
    $('lg-q').title = lg.reErr;

    // Type filter: the types present in what's loaded, keeping the selection.
    const sel = $('lg-level'), cur = sel.value;
    const levels = [...new Set(lg.entries.map(e => e.level))].sort();
    if (cur && !levels.includes(cur)) levels.push(cur);
    const opts = '<option value="">All types</option>' + levels.map(l => `<option value="${esc(l)}">${esc(l)}</option>`).join('');
    if (sel.dataset.opts !== opts) { sel.innerHTML = opts; sel.value = cur; sel.dataset.opts = opts; }

    const re = lg.re;
    lg.visible = lg.entries.filter(e => (!cur || e.level === cur) && (!re || (re.lastIndex = 0, re.test(logLine(e)))));
    const view = $('lg-view');
    view.innerHTML = lg.visible.length
      ? lg.visible.map(e => `<div class="ll ${LEVEL_CLASS[e.level] || ''}"><span class="lt">${fmtLogTs(e.ts)}</span> <span class="lv">${hl(e.level)}</span> <span class="ltag">${hl(e.tag)}</span> ${hl(e.text)}${e.note ? ` <span class="ln">${hl(e.note)}</span>` : ''}</div>`).join('')
      : `<div class="ll-empty muted">${lg.entries.length ? 'No lines match the filter.' : lg.lastId == null && lg.loading ? 'Loading…' : 'Nothing logged yet.'}</div>`;
    const filtered = re || cur;
    $('lg-count').textContent = `${filtered ? `${lg.visible.length.toLocaleString()} of ` : ''}${lg.entries.length.toLocaleString()} lines${lg.live ? '' : ' · paused'}`;
    if ($('lg-follow').checked) view.scrollTop = view.scrollHeight;
  }

  let lgDebounce;
  $('lg-q').addEventListener('input', () => { clearTimeout(lgDebounce); lgDebounce = setTimeout(renderLogView, 120); });
  $('lg-q').addEventListener('keydown', e => { if (e.key === 'Escape') { $('lg-q').value = ''; renderLogView(); } });
  ['lg-regex', 'lg-case', 'lg-utc'].forEach(id => $(id).addEventListener('change', () => { lgSavePrefs(); renderLogView(); }));
  $('lg-level').addEventListener('change', renderLogView);
  $('lg-source').addEventListener('change', () => switchSource());
  $('lg-limit').addEventListener('change', () => switchSource());  // reload to fill the new size
  $('lg-wrap').addEventListener('change', () => { $('lg-view').classList.toggle('wrap', $('lg-wrap').checked); lgSavePrefs(); });
  $('lg-follow').addEventListener('change', () => { if ($('lg-follow').checked) $('lg-view').scrollTop = $('lg-view').scrollHeight; });
  // Scrolling up pauses auto-scroll; scrolling back to the bottom resumes it.
  $('lg-view').addEventListener('scroll', () => {
    const v = $('lg-view');
    const atBottom = v.scrollHeight - v.scrollTop - v.clientHeight < 24;
    if ($('lg-follow').checked !== atBottom) $('lg-follow').checked = atBottom;
  });
  $('lg-live').addEventListener('click', () => {
    lg.live = !lg.live;
    $('lg-live').textContent = lg.live ? 'Pause' : 'Resume';
    renderLogView();
    if (lg.live) schedulePoll(0);
  });
  $('lg-clear').addEventListener('click', () => { lg.entries = []; renderLogView(); });
  $('lg-download').addEventListener('click', () => {
    const text = lg.visible.map(e => `${fmtLogTs(e.ts)} ${e.level} ${e.tag} ${e.text}${e.note ? `  [${e.note}]` : ''}`).join('\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text + '\n'], { type: 'text/plain' }));
    a.download = `${lg.source}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.log`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
  // "/" jumps to the filter box, as in most log viewers.
  document.addEventListener('keydown', e => {
    if (e.key === '/' && activeTab() === 'logs' && !/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName)) {
      e.preventDefault();
      $('lg-q').focus();
    }
  });

  kindChanged();
  fetch('api/admin/me').then(r => r.json()).then(d => d.logged_in ? showApp() : showLogin()).catch(showLogin);
  setInterval(() => { if (!$('app').hidden) loadStatus().catch(() => {}); }, 15000);
})();
