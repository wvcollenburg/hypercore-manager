// Theme: ?theme= wins, then the user's choice, then the system setting. Loaded in <head> so the page never
// flashes the wrong theme.
(function () {
  const q = new URLSearchParams(location.search).get('theme');
  let t = q || (function () { try { return localStorage.getItem('hcm.theme'); } catch (_) { return null; } })();
  if (!t) t = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  document.documentElement.dataset.theme = t;
})();
function themeLabel() {
  const b = document.getElementById('themeBtn');
  if (b) b.textContent = document.documentElement.dataset.theme === 'dark' ? '☀ Light' : '☾ Dark';
}
function toggleTheme() {
  const t = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('hcm.theme', t); } catch (_) {}
  themeLabel();
}
document.addEventListener('DOMContentLoaded', themeLabel);
