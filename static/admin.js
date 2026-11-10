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
    if (r.status === 401 && !/api\/(login|signup)$/.test(url)) { showLoggedOut(); throw new Error('Login required'); }
    if (!r.ok) {
      const d = data.detail;
      throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map(x => x.msg.replace(/^Value error, /, '')).join('; ') : `HTTP ${r.status}`);
    }
    return data;
  }

  // Shows a message in a status span; success messages fade after a few seconds.
  function say(id, text, isErr) {
    const m = $(id);
    m.className = isErr ? 'error' : 'okmsg';
    m.textContent = text;
    if (!isErr && text) setTimeout(() => { if (m.textContent === text) m.textContent = ''; }, 4000);
  }

  // Two-click confirm (instead of window.confirm): the first click arms the
  // button for 4 s and returns false; a second click within that returns true.
  function armed(btn, confirmText = 'Confirm') {
    if (btn.dataset.armed === '1') return true;
    const orig = btn.textContent;
    btn.dataset.armed = '1';
    btn.textContent = confirmText;
    setTimeout(() => { btn.dataset.armed = ''; btn.textContent = orig; }, 4000);
    return false;
  }

  // ── who is logged in ──────────────────────────────────────────────────────
  // Admins see everything. Node owners see their own devices, their account,
  // and Node config if an admin allowed it. A pending user sees their
  // submission and their account.
  let ME = null;
  const isAdmin = () => ME?.role === 'admin';
  const TABS = () => [...document.querySelectorAll('.admin-tabs .tab')];

  function showLoggedOut() {
    ME = null;
    $('app').hidden = true; $('logout').hidden = true; $('whoami').textContent = '';
    const join = location.hash === '#join';
    $('login-card').hidden = join;
    $('join-card').hidden = !join;
    if (join) { if (!$('j-devices').children.length) addJoinDevice(); $('j-username').focus(); }
    else $('username').focus();
  }
  window.addEventListener('hashchange', () => { if (!ME) showLoggedOut(); });

  function tabAllowed(b) {
    const f = b.dataset.for;
    if (!f) return true;
    if (f === 'admin') return isAdmin();
    if (f === 'owner') return !isAdmin();
    if (f === 'config') return ME.can_config && ME.status === 'active';
    return false;
  }

  function showApp(user) {
    ME = user;
    $('login-card').hidden = true; $('join-card').hidden = true;
    $('app').hidden = false; $('logout').hidden = false;
    $('whoami').textContent = `${user.username}${isAdmin() ? ' (admin)' : ''}`;
    $('pending-banner').hidden = user.status !== 'pending';
    $('withdraw-card').hidden = user.status !== 'pending';
    TABS().forEach(b => { b.hidden = !tabAllowed(b); });
    if (isAdmin()) loadAll();
    const want = location.hash.slice(1);
    const tab = TABS().find(b => !b.hidden && b.dataset.tab === want);
    showTab(tab ? want : isAdmin() ? 'stations' : 'mine');
  }

  $('login-form').addEventListener('submit', async e => {
    e.preventDefault();
    $('login-err').textContent = '';
    try {
      const r = await api('POST', 'api/login', { username: $('username').value.trim(), password: $('password').value });
      $('password').value = '';
      showApp(r.user);
    } catch (err) { $('login-err').textContent = err.message; }
  });
  $('logout').addEventListener('click', async () => {
    await api('POST', 'api/logout').catch(() => {});
    try { history.replaceState(null, '', location.pathname + location.search); } catch { /* ignore */ }
    showLoggedOut();
  });

  // ── device fields (sign-up and My devices) ────────────────────────────────
  const KIND_HELP = {
    mobile: 'Mobiles: use an exact callsign-SSID (K7ABC-9), or K7ABC* / K7ABC-* to cover every SSID. For trackers that beacon as APRS objects, use SENDER:OBJECT (K7SWI:CAR01, or K7SWI:CAR* for a fleet). Packets from mobiles are what draw coverage.',
    infra: 'Igates/digis: exact callsign-SSID as it appears in packet paths. Leave the position blank to take it from the station’s own beacons.',
  };

  function deviceBlock(d, removable) {
    const fs = $('device-tpl').content.firstElementChild.cloneNode(true);
    const f = k => fs.querySelector(`[data-f="${k}"]`);
    if (d) {
      f('callsign').value = d.callsign; f('kind').value = d.kind; f('label').value = d.label; f('notes').value = d.notes;
      f('usecolor').checked = !!d.color; f('color').value = d.color || '#2f81f7';
      f('lat').value = d.lat ?? ''; f('lon').value = d.lon ?? ''; f('active').checked = !!d.active;
    }
    const rm = fs.querySelector('[data-remove]');
    rm.hidden = !removable;
    rm.addEventListener('click', () => { fs.remove(); numberJoinDevices(); });
    f('kind').addEventListener('change', () => deviceKindUi(fs));
    f('color').addEventListener('input', () => { f('usecolor').checked = true; });
    deviceKindUi(fs);
    return fs;
  }
  function deviceKindUi(fs) {
    const mobile = fs.querySelector('[data-f="kind"]').value === 'mobile';
    fs.querySelectorAll('.infra-only').forEach(el => { el.hidden = mobile; });
    fs.querySelector('[data-help]').textContent = KIND_HELP[mobile ? 'mobile' : 'infra'];
  }
  function readDevice(fs) {
    const f = k => fs.querySelector(`[data-f="${k}"]`);
    const num = v => v === '' ? null : Number(v);
    const mobile = f('kind').value === 'mobile';
    return {
      callsign: f('callsign').value.trim(), kind: f('kind').value,
      label: f('label').value.trim(), notes: f('notes').value.trim(),
      color: f('usecolor').checked ? f('color').value : null,
      lat: mobile ? null : num(f('lat').value), lon: mobile ? null : num(f('lon').value),
      active: f('active').checked,
    };
  }

  // ── sign-up ───────────────────────────────────────────────────────────────
  function numberJoinDevices() {
    const all = [...$('j-devices').children];
    all.forEach((fs, i) => {
      fs.querySelector('legend').firstChild.textContent = `Device ${i + 1} `;
      fs.querySelector('[data-remove]').hidden = all.length < 2;
    });
  }
  function addJoinDevice() { $('j-devices').append(deviceBlock(null, true)); numberJoinDevices(); }
  $('j-add').addEventListener('click', addJoinDevice);

  $('join-form').addEventListener('submit', async e => {
    e.preventDefault();
    $('join-err').textContent = '';
    try {
      const r = await api('POST', 'api/signup', {
        username: $('j-username').value.trim(), password: $('j-password').value,
        callsign: $('j-callsign').value.trim(), name: $('j-name').value.trim(), email: $('j-email').value.trim(),
        website: $('j-website').value, devices: [...$('j-devices').children].map(readDevice),
      });
      if (!r.user) { $('join-err').textContent = 'Thanks.'; return; }
      $('join-form').reset(); $('j-devices').innerHTML = '';
      try { history.replaceState(null, '', location.pathname + location.search); } catch { /* ignore */ }
      showApp(r.user);
    } catch (err) { $('join-err').textContent = err.message; }
  });

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
      ['Duplicates feed', dupeState(i.dupe)],
      ['Counters (since restart)', esc(counts)],
      ['Stored packets', `${s.db.packets.toLocaleString()}${s.db.dupes ? ` (${s.db.dupes.toLocaleString()} from the duplicates feed)` : ''}, ${fmt(s.db.first)} → ${fmt(s.db.last)}`],
      ['Database', `<code>${esc(s.db_path)}</code>`],
    ];
    $('status').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
  }

  function dupeState(d) {
    if (!d.enabled) return '<span class="muted">Off</span>';
    const state = d.connected ? '<span class="okmsg">Connected</span>' : `<span class="error">Disconnected</span> ${esc(d.error)}`;
    return `${state}${d.server ? ` — ${esc(d.server)} as ${esc(d.login)}` : ''}`
      + (d.server_line ? `<br><span class="muted small">${esc(d.server_line)}</span>` : '')
      + (d.connected ? `<br><span class="muted small">Last line ${fmt(d.last_line_ts)}</span>` : '');
  }

  // ── feed servers ──────────────────────────────────────────────────────────
  function showFeeds(f) {
    const s = f.settings;
    $('f-server').value = s.server; $('f-server').placeholder = f.default_server;
    $('f-port').value = s.port ?? ''; $('f-port').placeholder = f.default_port;
    $('f-dupe').checked = s.dupe_enabled;
    $('f-dserver').value = s.dupe_server;
    $('f-dport').value = s.dupe_port ?? '';
    $('f-dlogin').value = s.dupe_login;
    if (f.login && !s.dupe_login) $('f-dlogin').placeholder = `not ${f.login}, e.g. ${f.login.split('-')[0]}-DF`;
    dupeToggled();
  }
  function dupeToggled() {
    const on = $('f-dupe').checked;
    ['f-dserver', 'f-dport', 'f-dlogin'].forEach(id => { $(id).required = on; });
  }
  $('f-dupe').addEventListener('change', dupeToggled);
  async function loadFeeds() { showFeeds(await api('GET', 'api/admin/feeds')); }

  $('feeds-form').addEventListener('submit', async e => {
    e.preventDefault();
    const m = $('feeds-msg');
    const port = id => $(id).value === '' ? null : Number($(id).value);
    try {
      showFeeds(await api('PUT', 'api/admin/feeds', {
        server: $('f-server').value.trim(), port: port('f-port'),
        dupe_enabled: $('f-dupe').checked, dupe_server: $('f-dserver').value.trim(),
        dupe_port: port('f-dport'), dupe_login: $('f-dlogin').value.trim(),
      }));
      m.className = 'okmsg'; m.textContent = 'Saved.';
      setTimeout(() => { if (m.textContent === 'Saved.') m.textContent = ''; }, 4000);
      setTimeout(() => loadStatus().catch(() => {}), 2000);
    } catch (err) { m.className = 'error'; m.textContent = err.message; }
  });

  // ── stations ──────────────────────────────────────────────────────────────
  let stations = [];
  // Which APRS-IS server the station is logged into, from qAC on its own packets.
  function serverCell(s) {
    if (!s.is_server) return '<span class="muted">not seen</span>';
    const when = `<br><span class="muted small">as of ${fmt(s.is_server_ts)}</span>`;
    if (!s.own_server) return `<b>${esc(s.is_server)}</b>${when}`;
    return s.is_server === s.own_server
      ? `<span class="ok-text">✓ yours</span> <span class="muted small">(${esc(s.is_server)})</span>${when}`
      : `<span class="warnmsg">✗ ${esc(s.is_server)}</span>${when}`;
  }
  // Which feed an igate's gated packets arrived on (see the note under the table).
  function gatedCell(g) {
    if (!g) return '<span class="muted">—</span>';
    if (!g.main && !g.dupe) return '<span class="muted">nothing gated</span>';
    const tag = g.main && g.dupe ? 'APRS-IS + dupes' : g.main ? 'APRS-IS' : 'Dupes only';
    const line = (name, n, ts) => n ? `<br><span class="muted small">${name}: ${n}, last ${fmt(ts)}</span>` : '';
    return `<b>${tag}</b>${line('APRS-IS', g.main, g.main_last)}${line('Dupes', g.dupe, g.dupe_last)}`;
  }
  let users = [];
  function fillOwnerSelect() {
    const sel = $('s-owner'), cur = sel.value;
    sel.innerHTML = '<option value="">Admins</option>' + users.filter(u => u.role === 'node_owner')
      .map(u => `<option value="${u.id}">${esc(u.username)}${u.callsign ? ` (${esc(u.callsign)})` : ''}${u.status !== 'active' ? ` · ${u.status}` : ''}</option>`).join('');
    sel.value = cur;
  }
  async function loadStations() {
    const [all, us] = await Promise.all([api('GET', 'api/admin/stations'), api('GET', 'api/admin/users')]);
    users = us;
    fillOwnerSelect();
    stations = all.filter(s => s.status === 'approved');
    renderApprovals(all);
    const callCell = s => `<td>${s.color ? `<span class="swatch" style="background:${esc(s.color)}"></span> ` : ''}<b>${esc(s.callsign)}</b></td>`;
    const labelCell = s => `<td>${esc(s.label)}${s.notes ? `<br><span class="muted small">${esc(s.notes)}</span>` : ''}${s.owner ? `<br><span class="muted small">owner: ${esc(s.owner)}</span>` : ''}</td>`;
    const actions = s => `<td class="actions"><button class="btn secondary" data-edit="${s.id}">Edit</button><button class="btn purge" data-purge="${s.id}" title="${s.kind === 'mobile'
      ? 'Delete every stored packet from this mobile. The registration is kept.'
      : 'Delete the stored packets this station heard directly (its coverage). The registration is kept.'}">Purge data</button><button class="btn danger" data-del="${s.id}">Delete</button></td>`;
    const infra = stations.filter(s => s.kind !== 'mobile'), mobiles = stations.filter(s => s.kind === 'mobile');
    $('stations-infra').innerHTML = infra.map(s => {
      const lat = s.lat ?? s.auto_lat, lon = s.lon ?? s.auto_lon;
      const pos = lat != null
        ? `${lat.toFixed(5)}, ${lon.toFixed(5)} <span class="muted">${s.lat != null ? 'manual' : 'auto'}</span>`
        : '<span class="muted">waiting for beacon</span>';
      return `<tr>${callCell(s)}<td>${KIND[s.kind]}</td>${labelCell(s)}
        <td>${pos}</td><td>${fmt(s.last_beacon)}</td><td>${serverCell(s)}</td><td>${gatedCell(s.gated)}</td><td>${s.active ? 'yes' : 'no'}</td>${actions(s)}</tr>`;
    }).join('') || '<tr><td colspan="9" class="muted">No igates or digis yet. Add them above.</td></tr>';
    $('stations-mobile').innerHTML = mobiles.map(s =>
      `<tr>${callCell(s)}${labelCell(s)}<td>${s.active ? 'yes' : 'no'}</td>${actions(s)}</tr>`
    ).join('') || '<tr><td colspan="4" class="muted">No mobiles yet. Add them above.</td></tr>';
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
    $('kind-help').textContent = KIND_HELP[mobile ? 'mobile' : 'infra'];
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
    $('s-owner').value = s.owner_id ?? '';
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
      owner_id: $('s-owner').value === '' ? null : Number($('s-owner').value),
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
    const rows = await api('GET', 'api/admin/rejects?limit=10');
    $('rejects').innerHTML = rows.map(r =>
      `<tr><td>${fmt(r.ts)}</td><td>${esc(r.source)}</td><td>${esc(r.reason)}</td><td><pre class="raw">${esc(r.raw)}</pre></td></tr>`).join('')
      || '<tr><td colspan="4" class="muted">None.</td></tr>';
  }

  // ── approvals (admin) ─────────────────────────────────────────────────────
  const POS = s => s.lat != null ? `${s.lat.toFixed(5)}, ${s.lon.toFixed(5)}` : (s.kind === 'mobile' ? '' : 'position from beacon');
  function deviceSummary(s) {
    const bits = [KIND[s.kind], s.label, POS(s), s.active ? '' : 'inactive'].filter(Boolean).map(esc).join(' · ');
    return `${s.color ? `<span class="swatch" style="background:${esc(s.color)}"></span> ` : ''}<b>${esc(s.callsign)}</b> <span class="muted">${bits}</span>`
      + (s.notes ? `<br><span class="muted small">${esc(s.notes)}</span>` : '')
      + (s.status === 'rejected' && s.review_note ? `<br><span class="error small">Rejected: ${esc(s.review_note)}</span>` : '');
  }
  const deviceActions = s => `<span class="appr-actions">
      <button class="btn" data-approve-dev="${s.id}">Approve</button>
      ${s.status === 'pending' ? `<input type="text" class="note" data-note="${s.id}" placeholder="Reason (shown to them)" maxlength="300">
      <button class="btn danger" data-reject-dev="${s.id}">Reject</button>` : ''}</span>`;
  const deviceList = list => `<ul class="appr-list">${list.map(s => `<li>${deviceSummary(s)}${deviceActions(s)}</li>`).join('')}</ul>`;

  let apprAll = [];
  function renderApprovals(all) {
    apprAll = all;
    const pendUsers = users.filter(u => u.status === 'pending');
    const pendingIds = new Set(pendUsers.map(u => u.id));
    const of = id => all.filter(s => s.owner_id === id);
    $('appr-users').innerHTML = pendUsers.map(u => `<div class="appr">
        <div><b>${esc(u.username)}</b> · ${esc(u.callsign)}${u.name ? ` · ${esc(u.name)}` : ''}${u.email ? ` · <a href="mailto:${esc(u.email)}">${esc(u.email)}</a>` : ''}
          <span class="muted small">signed up ${fmt(u.created_at)}</span></div>
        ${of(u.id).length ? deviceList(of(u.id)) : '<p class="muted small">No devices (they withdrew them all).</p>'}
        <div class="appr-actions">
          <button class="btn" data-approve-all="${u.id}">Approve account and devices</button>
          <button class="btn secondary" data-approve-user="${u.id}">Approve account only</button>
          <button class="btn danger" data-reject-user="${u.id}">Reject sign-up</button>
        </div></div>`).join('') || '<p class="muted">None waiting.</p>';
    const newDevs = all.filter(s => s.status === 'pending' && !pendingIds.has(s.owner_id));
    $('appr-devices').innerHTML = newDevs.length ? deviceList(newDevs) : '<p class="muted">None waiting.</p>';
    // Say whose each new device is.
    $('appr-devices').querySelectorAll('li').forEach((li, i) => {
      const s = newDevs[i];
      li.insertAdjacentHTML('afterbegin', `<span class="muted small">${esc(s.owner || 'no owner')}:</span> `);
    });
    const rejected = all.filter(s => s.status === 'rejected');
    $('appr-rejected').innerHTML = rejected.length ? deviceList(rejected) : '<p class="muted">None.</p>';
    $('appr-rejected').querySelectorAll('li').forEach((li, i) => {
      li.insertAdjacentHTML('afterbegin', `<span class="muted small">${esc(rejected[i].owner || 'no owner')}:</span> `);
    });
    const n = pendUsers.length + newDevs.length;
    $('appr-count').hidden = !n;
    $('appr-count').textContent = n;
  }

  async function approvalAction(fn, msg) {
    try { await fn(); say('appr-msg', msg); }
    catch (e) { say('appr-msg', e.message, true); }
    loadStations().catch(() => {});
  }
  $('app').addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b || $('app').hidden || !isAdmin()) return;
    const d = b.dataset;
    if (d.approveDev) approvalAction(() => api('POST', `api/admin/stations/${d.approveDev}/approve`), 'Device approved. It is on the map now.');
    else if (d.rejectDev) {
      const note = document.querySelector(`[data-note="${d.rejectDev}"]`)?.value.trim() || '';
      approvalAction(() => api('POST', `api/admin/stations/${d.rejectDev}/reject`, { note }), 'Device rejected.');
    } else if (d.approveUser) approvalAction(() => api('PUT', `api/admin/users/${d.approveUser}`, { status: 'active' }), 'Account approved.');
    else if (d.approveAll) {
      const uid = +d.approveAll;
      approvalAction(async () => {
        await api('PUT', `api/admin/users/${uid}`, { status: 'active' });
        for (const s of apprAll.filter(s => s.owner_id === uid && s.status === 'pending')) {
          await api('POST', `api/admin/stations/${s.id}/approve`);
        }
      }, 'Account and devices approved.');
    } else if (d.rejectUser) {
      if (!armed(b, 'Delete account and devices?')) return;
      approvalAction(() => api('DELETE', `api/admin/users/${d.rejectUser}`), 'Sign-up rejected and deleted.');
    }
  });

  // ── users (admin) ─────────────────────────────────────────────────────────
  const ROLE = { admin: 'Admin', node_owner: 'Node owner' };
  async function loadUsers() {
    users = await api('GET', 'api/admin/users');
    fillOwnerSelect();
    $('users').innerHTML = users.map(u => `<tr>
        <td><b>${esc(u.username)}</b>${u.name ? `<br><span class="muted small">${esc(u.name)}</span>` : ''}${u.email ? `<br><a class="small" href="mailto:${esc(u.email)}">${esc(u.email)}</a>` : ''}</td>
        <td>${esc(u.callsign) || '<span class="muted">—</span>'}</td>
        <td>${ROLE[u.role]}</td>
        <td class="${u.status === 'active' ? '' : u.status === 'pending' ? 'warnmsg' : 'error'}">${u.status}</td>
        <td>${u.role === 'admin' ? '<span class="muted">always</span>' : u.can_config ? 'yes' : 'no'}</td>
        <td>${u.devices}${u.pending_devices ? ` <span class="muted small">(${u.pending_devices} pending)</span>` : ''}</td>
        <td>${fmt(u.last_login)}</td>
        <td class="actions"><button class="btn secondary" data-uedit="${u.id}">Edit</button>${u.id === ME.id ? '' : `<button class="btn danger" data-udel="${u.id}">Delete</button>`}</td>
      </tr>`).join('');
    document.querySelectorAll('[data-uedit]').forEach(b => b.addEventListener('click', () => editUser(+b.dataset.uedit)));
    document.querySelectorAll('[data-udel]').forEach(b => b.addEventListener('click', async () => {
      if (!armed(b)) return;
      try { await api('DELETE', `api/admin/users/${b.dataset.udel}`); loadUsers(); loadStations().catch(() => {}); }
      catch (e) { say('u-msg', e.message, true); }
    }));
  }

  function editUser(id) {
    const u = users.find(x => x.id === id);
    if (!u) return;
    $('u-id').value = u.id;
    $('u-title').textContent = `Edit ${u.username}`;
    $('u-callsign').value = u.callsign; $('u-name').value = u.name; $('u-email').value = u.email;
    $('u-role').value = u.role; $('u-status').value = u.status; $('u-config').checked = u.can_config;
    roleChanged();
    $('u-msg').textContent = ''; $('u-pw-msg').textContent = ''; $('u-pw').value = '';
    $('u-edit').hidden = false;
    $('u-edit').scrollIntoView({ behavior: 'smooth' });
  }
  function roleChanged() { $('u-config').disabled = $('u-role').value === 'admin'; }
  $('u-role').addEventListener('change', roleChanged);
  $('u-cancel').addEventListener('click', () => { $('u-edit').hidden = true; });

  $('u-form').addEventListener('submit', async e => {
    e.preventDefault();
    const id = +$('u-id').value;
    try {
      await api('PUT', `api/admin/users/${id}`, {
        callsign: $('u-callsign').value.trim(), name: $('u-name').value.trim(), email: $('u-email').value.trim(),
        role: $('u-role').value, status: $('u-status').value, can_config: $('u-config').checked,
      });
      say('u-msg', 'Saved.');
      if (id === ME.id) {  // your own role or status changed: re-check what you may see
        const { user } = await (await fetch('api/me')).json();
        if (!user) { showLoggedOut(); return; }
        if (user.role !== ME.role || user.status !== ME.status) { showApp(user); return; }
      }
      await loadUsers();
      loadStations().catch(() => {});
    } catch (err) { say('u-msg', err.message, true); }
  });

  $('u-pw-form').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      await api('POST', `api/admin/users/${$('u-id').value}/password`, { new: $('u-pw').value });
      $('u-pw').value = '';
      say('u-pw-msg', 'Password set.');
    } catch (err) { say('u-pw-msg', err.message, true); }
  });

  // ── my devices (node owners) ──────────────────────────────────────────────
  let mine = [];
  function myStatus(s) {
    if (s.status === 'pending') return '<span class="warnmsg">Waiting for approval</span>';
    if (s.status === 'rejected') return `<span class="error">Rejected</span>${s.review_note ? `<br><span class="small">${esc(s.review_note)}</span>` : ''}`;
    return s.active ? '<span class="okmsg">On the map</span>' : '<span class="muted">Approved, inactive</span>';
  }
  async function loadMine() {
    const d = await api('GET', 'api/account');
    mine = d.devices;
    $('my-devices').innerHTML = mine.map(s => `<tr>
        <td>${s.color ? `<span class="swatch" style="background:${esc(s.color)}"></span> ` : ''}<b>${esc(s.callsign)}</b></td>
        <td>${KIND[s.kind]}</td>
        <td>${esc(s.label)}${s.notes ? `<br><span class="muted small">${esc(s.notes)}</span>` : ''}</td>
        <td>${myStatus(s)}</td>
        <td>${s.kind === 'mobile' || s.status !== 'approved' ? '<span class="muted">—</span>' : `${fmt(s.last_beacon)}${s.gated ?`<br><span class="muted small">gated ${s.gated.main + s.gated.dupe} in 7 days</span>` : ''}`}</td>
        <td class="actions"><button class="btn secondary" data-myedit="${s.id}">Edit</button><button class="btn danger" data-mydel="${s.id}">Delete</button></td>
      </tr>`).join('') || '<tr><td colspan="6" class="muted">No devices yet. Add one below.</td></tr>';
    document.querySelectorAll('[data-myedit]').forEach(b => b.addEventListener('click', () => editMine(+b.dataset.myedit)));
    document.querySelectorAll('[data-mydel]').forEach(b => b.addEventListener('click', async () => {
      if (!armed(b)) return;
      try {
        await api('DELETE', `api/account/devices/${b.dataset.mydel}`);
        if (+$('my-id').value === +b.dataset.mydel) resetMine();
        say('my-msg', 'Deleted.');
        loadMine();
      } catch (e) { say('my-msg', e.message, true); }
    }));
  }

  function resetMine() {
    $('my-id').value = '';
    $('my-fields').replaceChildren(deviceBlock(null, false));
    $('my-fields').querySelector('legend').hidden = true;
    $('my-form-title').textContent = 'Add a device';
    $('my-save').textContent = 'Submit for approval';
    $('my-cancel').hidden = true;
  }
  function editMine(id) {
    const s = mine.find(x => x.id === id);
    if (!s) return;
    const fs = deviceBlock(s, false);
    fs.querySelector('legend').hidden = true;
    const approved = s.status === 'approved';
    if (approved) {  // the server refuses to change these after approval
      fs.querySelector('[data-f="callsign"]').readOnly = true;
      fs.querySelector('[data-f="kind"]').disabled = true;
    }
    $('my-fields').replaceChildren(fs);
    $('my-id').value = s.id;
    $('my-form-title').textContent = `Edit ${s.callsign}`;
    $('my-save').textContent = approved ? 'Save changes' : 'Resubmit for approval';
    $('my-cancel').hidden = false;
    $('my-form').scrollIntoView({ behavior: 'smooth' });
  }
  $('my-cancel').addEventListener('click', resetMine);
  $('my-form').addEventListener('submit', async e => {
    e.preventDefault();
    const body = readDevice($('my-fields').firstElementChild);
    const id = $('my-id').value;
    try {
      if (id) await api('PUT', `api/account/devices/${id}`, body);
      else await api('POST', 'api/account/devices', body);
      say('my-msg', id ? 'Saved.' : `Submitted ${body.callsign.toUpperCase()} for approval.`);
      resetMine();
      loadMine();
    } catch (err) { say('my-msg', err.message, true); }
  });

  // ── account (everyone) ────────────────────────────────────────────────────
  function fillAccount() {
    $('a-username').value = ME.username; $('a-callsign').value = ME.callsign;
    $('a-name').value = ME.name; $('a-email').value = ME.email;
  }
  $('acct-form').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      ME = (await api('PUT', 'api/account', { name: $('a-name').value.trim(), email: $('a-email').value.trim() })).user;
      fillAccount();
      say('acct-msg', 'Saved.');
    } catch (err) { say('acct-msg', err.message, true); }
  });
  $('pw-form').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      await api('POST', 'api/account/password', { current: $('pw-cur').value, new: $('pw-new').value });
      say('pw-msg', 'Password changed. Your other sessions were logged out.');
      $('pw-form').reset();
    } catch (err) { say('pw-msg', err.message, true); }
  });
  $('withdraw').addEventListener('click', async () => {
    if (!armed($('withdraw'), 'Really withdraw?')) return;
    try {
      await api('DELETE', 'api/account');
      try { history.replaceState(null, '', location.pathname + location.search); } catch { /* ignore */ }
      showLoggedOut();
    } catch (err) { say('withdraw-msg', err.message, true); }
  });

  function loadAll() {
    loadStatus().catch(() => {});
    loadFeeds().catch(() => {});
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
    const home = isAdmin() ? 'stations' : 'mine';
    try { history.replaceState(null, '', name === home ? location.pathname + location.search : `#${name}`); } catch { /* ignore */ }
    if (name === 'feeds') { loadStatus().catch(() => {}); loadRejects().catch(() => {}); }
    if (name === 'approvals') loadStations().catch(e => say('appr-msg', e.message, true));
    if (name === 'users') loadUsers().catch(e => say('u-msg', e.message, true));
    if (name === 'mine') { if (!$('my-fields').children.length) resetMine(); loadMine().catch(e => say('my-msg', e.message, true)); }
    if (name === 'account') fillAccount();
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
  // Node owners must send from their own callsign (any SSID); admins from any.
  const ownCallOk = c => !ME || isAdmin() || c.split('-')[0] === ME.callsign;
  const sendAs = () => { const c = $('rc-call').value.trim().toUpperCase(); return CALL_RE.test(c) && ownCallOk(c) ? c : ''; };
  function callChanged() {
    const raw = $('rc-call').value.trim().toUpperCase();
    const ok = CALL_RE.test(raw) && ownCallOk(raw);
    $('rc-pass').value = ok ? aprsPasscode(raw) : '';
    $('rc-call').setCustomValidity(raw && !ok
      ? (CALL_RE.test(raw) ? `Use your own callsign, ${ME.callsign}, with an SSID` : 'Callsign like K7ABC or K7ABC-CF') : '');
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
    const d = await api('GET', `api/remote${call ? `?login=${encodeURIComponent(call)}` : ''}`);
    rc.fields = d.fields; rc.nodes = d.nodes; rc.status = d.status;
    $('rc-own-help').hidden = !d.own_callsign;
    if (d.own_callsign) {
      $('rc-own-help').textContent = `Send from your callsign, ${d.own_callsign}, with an SSID that none of your nodes uses, e.g. ${d.own_callsign}-CF.`;
      $('rc-call').placeholder = `e.g. ${d.own_callsign}-CF`;
      callChanged();
    }
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
      || `<tr><td colspan="5" class="muted">${isAdmin() ? 'No active igates or digis. Register them on the Stations tab.'
        : 'None of your igates or digis is approved and active yet.'}</td></tr>`;
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
    const tick = () => rcMsg(`${verb} ${call}… ${Math.round((Date.now() - t0) / 1000)} s (up to 60 s over RF)`, 'muted');
    tick();
    const timer = setInterval(tick, 1000);
    try {
      const r = await api('POST', `api/remote/${encodeURIComponent(call)}/${kind}`, { login: sendAs(), ...body });
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
  $('rc-log-clear').addEventListener('click', async e => {
    if (!armed(e.currentTarget, 'Clear all?')) return;
    try {
      const r = await api('DELETE', 'api/remote/log');
      renderLog([]);
      say('rc-log-msg', `Cleared ${r.cleared} entr${r.cleared === 1 ? 'y' : 'ies'}.`);
    } catch (err) { say('rc-log-msg', err.message, true); }
  });

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
  fetch('api/me').then(r => r.json()).then(d => d.user ? showApp(d.user) : showLoggedOut()).catch(showLoggedOut);
  setInterval(() => { if (!$('app').hidden && isAdmin()) loadStatus().catch(() => {}); }, 15000);
})();
