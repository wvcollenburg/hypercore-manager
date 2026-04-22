#!/usr/bin/env python3
"""Builds index3.html (LCARS-II) by enhancing index2.html with extra LCARS effects."""

base_path   = r'z:/Projects/programs/hypercore-manager/dashboard/static/index2.html'
output_path = r'z:/Projects/programs/hypercore-manager/dashboard/static/index3.html'

with open(base_path, 'r', encoding='utf-8') as f:
    html = f.read()

# ─── EXTRA CSS ────────────────────────────────────────────────────────────────
LCARS2_CSS = """
/* ═══════════════════════════════════════════════════════════════════
   LCARS-II  ·  ENHANCED EFFECTS LAYER
   ═══════════════════════════════════════════════════════════════════ */

/* 1 · CRT SCANLINES ─────────────────────────────────────────────── */
body::before{
  content:'';position:fixed;inset:0;z-index:10000;pointer-events:none;
  background:repeating-linear-gradient(
    0deg,transparent 0,transparent 3px,
    rgba(0,0,0,0.07) 3px,rgba(0,0,0,0.07) 4px
  );
}

/* 2 · PHOSPHOR FLICKER ──────────────────────────────────────────── */
@keyframes lcars-flicker{
  0%,100%{opacity:1}
  1%{opacity:.96}2%{opacity:1}
  59%{opacity:1}60%{opacity:.97}61%{opacity:1}
}
body{animation:lcars-flicker 9s ease-in-out infinite}

/* 3 · TOPBAR / BOTBAR GLOW ──────────────────────────────────────── */
.lcars-topbar{box-shadow:0 6px 28px rgba(255,153,0,.28),inset 0 -2px 0 rgba(0,0,0,.2)}
.lcars-botbar{box-shadow:0 -6px 28px rgba(255,153,0,.22)}
.lcars-cap-top,.lcars-cap-bot{box-shadow:6px 0 20px rgba(255,153,0,.18)}

/* 4 · SIDEBAR BAR STAGGERED PULSE ───────────────────────────────── */
@keyframes lc-bar-pulse{0%,100%{opacity:1}50%{opacity:.35}}
.lc-bar-orange{animation:lc-bar-pulse 2.8s ease-in-out infinite}
.lc-bar-lilac {animation:lc-bar-pulse 3.4s ease-in-out infinite;animation-delay:.5s}
.lc-bar-gold  {animation:lc-bar-pulse 2.5s ease-in-out infinite;animation-delay:1.0s}
.lc-bar-peach {animation:lc-bar-pulse 3.8s ease-in-out infinite;animation-delay:1.5s}
.lc-bar-blue  {animation:lc-bar-pulse 3.1s ease-in-out infinite;animation-delay:.7s}
.lc-bar-purple{animation:lc-bar-pulse 2.3s ease-in-out infinite;animation-delay:1.2s}
.lc-bar-tan   {animation:lc-bar-pulse 4.0s ease-in-out infinite;animation-delay:.3s}

/* 5 · STATUS DOT — ENHANCED PULSE ──────────────────────────────── */
@keyframes lcars-status{
  0%,100%{transform:scale(1);box-shadow:0 0 6px rgba(51,187,102,.9),0 0 0 0 rgba(51,187,102,.5)}
  50%{transform:scale(1.4);box-shadow:0 0 18px rgba(51,187,102,1),0 0 0 9px rgba(51,187,102,0)}
}
.status-dot{animation:lcars-status 1.6s ease-in-out infinite!important}

/* 6 · ACTIVE NAV TAB GLOW ───────────────────────────────────────── */
.lcars-sidebar .nav-tab.active{
  box-shadow:0 0 16px rgba(255,204,153,.45),inset 0 0 10px rgba(255,255,255,.06)
}

/* 7 · CARD LCARS LEFT ACCENT ────────────────────────────────────── */
.card{
  border-left:3px solid var(--lc-orange-d)!important;
  border-radius:0 8px 8px 0!important;
  transition:border-left-color .2s,box-shadow .2s!important;
}
.card:hover{
  border-left-color:var(--lc-orange)!important;
  box-shadow:0 0 18px rgba(255,153,0,.13)!important;
}

/* 8 · TIME-RANGE BUTTONS — LCARS BLOCK ──────────────────────────── */
.time-range-btn{
  font-family:'Antonio',sans-serif!important;font-weight:700!important;
  font-size:11px!important;text-transform:uppercase!important;letter-spacing:.1em!important;
  border:1px solid var(--lc-orange-d)!important;border-radius:3px!important;
  background:transparent!important;color:var(--lc-orange-d)!important;
  padding:4px 10px!important;cursor:pointer;transition:all .15s!important;
}
.time-range-btn:hover{
  border-color:var(--lc-orange)!important;color:var(--lc-orange)!important;
  background:rgba(255,153,0,.08)!important;
}
.time-range-btn.active{
  background:var(--lc-orange)!important;color:#000!important;
  border-color:var(--lc-orange)!important;
  box-shadow:0 0 10px rgba(255,153,0,.45)!important;
}

/* 9 · BADGE GLOW ────────────────────────────────────────────────── */
.badge-online{box-shadow:0 0 8px rgba(51,187,102,.65)!important}
.badge-offline{box-shadow:0 0 8px rgba(255,51,0,.45)!important}
.badge-warning{box-shadow:0 0 8px rgba(255,170,0,.55)!important}

/* 10 · METRIC VALUES — SUBTLE GLOW ──────────────────────────────── */
.metric-value,.metric-big,.node-metric-value{
  text-shadow:0 0 10px rgba(255,153,0,.4)
}

/* 11 · INPUTS — LCARS STYLE ─────────────────────────────────────── */
input[type=number],input[type=text],input[type=password],select,textarea{
  border-color:var(--lc-orange-d)!important;
  background:rgba(255,153,0,.04)!important;
  transition:border-color .15s,box-shadow .15s!important;
}
input[type=number]:focus,input[type=text]:focus,
input[type=password]:focus,select:focus{
  border-color:var(--lc-orange)!important;
  box-shadow:0 0 10px rgba(255,153,0,.3)!important;
  outline:none!important;
}

/* 12 · CHART TITLE DIVIDERS ─────────────────────────────────────── */
.chart-title{
  border-bottom:1px solid var(--lc-border)!important;
  padding-bottom:5px!important;margin-bottom:6px!important;
}

/* 13 · SECTION CORNER BRACKETS ──────────────────────────────────── */
.settings-section,.module-card,.sentinel-score-card{position:relative!important}
.settings-section::before,.module-card::before,.sentinel-score-card::before{
  content:'';position:absolute;top:6px;left:6px;
  width:12px;height:12px;
  border-top:2px solid var(--lc-orange);border-left:2px solid var(--lc-orange);
  pointer-events:none;
}
.settings-section::after,.module-card::after,.sentinel-score-card::after{
  content:'';position:absolute;bottom:6px;right:6px;
  width:12px;height:12px;
  border-bottom:2px solid var(--lc-orange-d);border-right:2px solid var(--lc-orange-d);
  pointer-events:none;
}

/* 14 · LCARS SCROLLBAR ──────────────────────────────────────────── */
.main::-webkit-scrollbar{width:5px}
.main::-webkit-scrollbar-track{background:var(--lc-bg)}
.main::-webkit-scrollbar-thumb{background:var(--lc-orange-d);border-radius:3px}
.main::-webkit-scrollbar-thumb:hover{background:var(--lc-orange)}

/* 15 · APPLY / ACTION BUTTONS ───────────────────────────────────── */
.apply-btn,.refresh-btn,.update-apply-btn{
  font-family:'Antonio',sans-serif!important;font-weight:700!important;
  text-transform:uppercase!important;letter-spacing:.1em!important;
  transition:box-shadow .15s!important;
}
.apply-btn:hover,.refresh-btn:hover,.update-apply-btn:hover{
  box-shadow:0 0 12px rgba(255,153,0,.45)!important;
}

/* 16 · ALERT PANEL FLASH ────────────────────────────────────────── */
@keyframes lcars-alert-flash{
  0%,100%{border-color:var(--lc-red)}50%{border-color:rgba(255,51,0,.2)}
}
.data-quality-banner[style*="display"]{
  animation:lcars-alert-flash 1.2s ease-in-out infinite;
}

/* 17 · BOTBAR SYSTEM MSG FADE ───────────────────────────────────── */
.lcars-botbar-right{transition:opacity .4s ease}

/* 18 · PAGE TITLE ───────────────────────────────────────────────── */
"""

# ─── EXTRA JS ─────────────────────────────────────────────────────────────────
LCARS2_JS = """\
<script>
/* ═══ LCARS-II ENHANCED EFFECTS ═══ */

// Boot sequence: title scramble on load
(function(){
  var el=document.querySelector('.lcars-topbar-title');
  if(!el)return;
  var orig=el.textContent;
  var chars='█▓▒░▄▀ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  var iter=0,max=orig.length;
  var iv=setInterval(function(){
    el.textContent=orig.split('').map(function(c,i){
      if(c===' ')return ' ';
      if(i<iter)return c;
      return chars[Math.floor(Math.random()*chars.length)];
    }).join('');
    iter++;
    if(iter>max){clearInterval(iv);el.textContent=orig;}
  },45);
})();

// Cycling starfleet status messages in the bottom bar
(function(){
  var msgs=[
    'All Systems Nominal',
    'Cluster Health: Optimal',
    'Subspace Link Active',
    'Telemetry Nominal',
    'Database Sync Complete',
    'Bio-neural Gel Packs Online',
    'Navigational Array Online',
    'Tactical Systems Standby',
    'Life Support Optimal',
    'Warp Core Stable',
  ];
  var mi=0;
  var el=document.getElementById('lcarsSystemMsg');
  if(!el)return;
  el.style.transition='opacity .4s';
  setInterval(function(){
    el.style.opacity='0';
    setTimeout(function(){
      mi=(mi+1)%msgs.length;
      el.textContent=msgs[mi];
      el.style.opacity='1';
    },420);
  },6000);
})();

// Stardate display: also show in botbar sub-label
(function(){
  var el=document.getElementById('lcarsStardate');
  if(el)el.style.cssText+='text-shadow:0 0 8px rgba(255,153,0,.7);letter-spacing:.12em';
})();

// Blinking cursor appended to status text while connecting
(function(){
  var st=document.getElementById('statusText');
  if(!st)return;
  var obs=new MutationObserver(function(){
    if(st.textContent.indexOf('…')>-1){
      st.style.animation='none';
    }
  });
  obs.observe(st,{childList:true,characterData:true,subtree:true});
})();
</script>
"""

# ─── INJECT ───────────────────────────────────────────────────────────────────
# Inject CSS at end of the single <style> block
html = html.replace('</style>', LCARS2_CSS + '\n</style>', 1)

# Inject JS just before </body>
html = html.replace('</body>', LCARS2_JS + '\n</body>')

# Update page title and favicon colour hint
html = html.replace(
    '<title>HyperCore · LCARS Interface</title>',
    '<title>HyperCore · LCARS-II Interface</title>'
)

with open(output_path, 'w', encoding='utf-8') as f:
    f.write(html)

chars = len(html)
lines = html.count('\n') + 1
print(f'Done. {chars} chars, {lines} lines -> {output_path}')
