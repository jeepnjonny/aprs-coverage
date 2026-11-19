/* Helpers shared by the map, compare and account pages. Loaded before each
   page's own script; everything is under one global, AC. */
window.AC = (() => {
  'use strict';
  const $ = id => document.getElementById(id);
  // Escape text for innerHTML. null/undefined become ''.
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  // The server's error message: a string, or FastAPI's list of field errors (422).
  const errText = (d, status) => typeof d.detail === 'string' ? d.detail
    : Array.isArray(d.detail) ? d.detail.map(e => String(e.msg).replace(/^Value error, /, '')).join('; ')
    : `HTTP ${status}`;
  // fetch + JSON + error message in one. Writes carry the header the server's
  // CSRF check requires.
  async function request(method, url, body, opts = {}) {
    const init = { method, cache: opts.cache };
    if (method !== 'GET') {
      init.headers = { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' };
      if (body != null) init.body = JSON.stringify(body);
    }
    const r = await fetch(url, init);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(errText(d, r.status));
      err.status = r.status;
      throw err;
    }
    return d;
  }
  return { $, esc, errText, request };
})();
