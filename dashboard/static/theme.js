// theme.js: which theme the page shows. Loaded in <head>, before the first paint, so a dark-mode user never
// sees a flash of the light theme. Order: ?theme= in the URL, then the user's own choice (kept in this browser),
// then the system setting. The themes themselves are blocks in theme.css; HCM_THEMES lists them for the switch.
(function () {
  var KEY = 'hcm.theme';
  window.HCM_THEMES = ['light', 'dark'];
  function stored() { try { return localStorage.getItem(KEY); } catch (_) { return null; } }
  function system() { return window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; }
  function apply(t) {
    if (window.HCM_THEMES.indexOf(t) < 0) t = 'light';
    document.documentElement.dataset.theme = t;
    document.dispatchEvent(new CustomEvent('hcm-theme', { detail: t }));
  }
  var q = null;
  try { q = new URLSearchParams(location.search).get('theme'); } catch (_) {}
  apply(q || stored() || system());
  // follow the system while the user has not chosen
  if (window.matchMedia) {
    var mq = matchMedia('(prefers-color-scheme: dark)');
    var onChange = function () { if (!stored() && !q) apply(system()); };
    if (mq.addEventListener) mq.addEventListener('change', onChange); else if (mq.addListener) mq.addListener(onChange);
  }
  window.hcmTheme = function () { return document.documentElement.dataset.theme; };
  window.hcmSetTheme = function (t) { try { localStorage.setItem(KEY, t); } catch (_) {} apply(t); };
  window.hcmToggleTheme = function () { window.hcmSetTheme(window.hcmTheme() === 'dark' ? 'light' : 'dark'); };
  // a token's current value, for code that cannot use CSS (charts)
  window.hcmToken = function (name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); };
})();
