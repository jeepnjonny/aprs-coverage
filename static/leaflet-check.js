// The map library comes from a CDN. If it didn't load, say so instead of
// leaving a blank page. (A file, not inline, so the page's CSP can forbid
// inline scripts.)
if (!window.L) {
  const e = document.getElementById('map-error');
  e.textContent = "The map library couldn't be loaded (network or content blocker). Try reloading the page.";
  e.hidden = false;
}
