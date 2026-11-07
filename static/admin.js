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
  function showApp() { $('login-card').hidden = true; $('app').hidden = false; $('logout').hidden = false; loadAll(); }

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
        <td class="actions"><button class="btn secondary" data-edit="${s.id}">Edit</button><button class="btn secondary" data-purge="${s.id}" title="${s.kind === 'mobile'
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
        btn.classList.replace('secondary', 'danger');
        setTimeout(() => {
          btn.dataset.armed = ''; btn.textContent = 'Purge data'; btn.classList.replace('danger', 'secondary');
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
      ? 'Mobiles: use an exact callsign-SSID (K7ABC-9), or K7ABC* / K7ABC-* to cover every SSID. Packets from mobiles are what draw coverage.'
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

  kindChanged();
  fetch('api/admin/me').then(r => r.json()).then(d => d.logged_in ? showApp() : showLogin()).catch(showLogin);
  setInterval(() => { if (!$('app').hidden) loadStatus().catch(() => {}); }, 15000);
})();
