// The top bar of the chosen direction, shared by the mockup pages: <div id="bar" data-active="Backup"></div>
document.addEventListener('DOMContentLoaded', () => {
  const el = document.getElementById('bar');
  if (!el) return;
  const active = el.dataset.active || 'Infrastructure';
  const mods = ['Infrastructure', 'Balancer', 'Forecasts', 'Backup', 'Settings'];
  el.className = 'bar';
  el.innerHTML = `
    <div class="logo"><i>H</i>HyperCore Manager</div>
    <div class="switch"><span class="dot"></span><span>Acme-Prod</span><small>Example IT › Acme › Production</small> ▾</div>
    <nav class="modules">${mods.map(m => `<a class="${m === active ? 'on' : ''}" href="#">${m}</a>`).join('')}</nav>
    <div class="right">⟳ 30 s ago · <span>EN</span><button class="theme" id="themeBtn" onclick="toggleTheme()">☾ Dark</button><span class="avatar">JD</span></div>`;
  themeLabel();
});
