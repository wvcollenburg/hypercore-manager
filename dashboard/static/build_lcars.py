#!/usr/bin/env python3
"""Builds index2.html (LCARS theme) from the original index.html."""

import os

original_path = r'z:/Projects/programs/hypercore-manager/dashboard/static/index.html'
output_path   = r'z:/Projects/programs/hypercore-manager/dashboard/static/index2.html'

with open(original_path, 'r', encoding='utf-8') as f:
    original_lines = f.readlines()

# Extract sections (0-indexed)
tab_html = ''.join(original_lines[872:1391])   # nav-tabs div + tab panel HTML
js_block  = ''.join(original_lines[1392:4265]) # <script> ... </script>
end_html  = ''.join(original_lines[4265:])     # update modal + closing HTML

# Patch JS: LCARS color palette
js_block = js_block.replace(
    "Chart.defaults.color = '#6b7a8d';",
    "Chart.defaults.color = '#CC7700';"
).replace(
    "Chart.defaults.borderColor = '#1e2a3a';",
    "Chart.defaults.borderColor = '#331100';"
).replace(
    "Chart.defaults.font.family = \"'JetBrains Mono', monospace\";",
    "Chart.defaults.font.family = \"'Antonio', 'JetBrains Mono', monospace\";"
).replace(
    "Chart.defaults.plugins.tooltip.backgroundColor = '#0f1923';",
    "Chart.defaults.plugins.tooltip.backgroundColor = '#0d0500';"
).replace(
    "Chart.defaults.plugins.tooltip.borderColor = '#2a3f55';",
    "Chart.defaults.plugins.tooltip.borderColor = '#FF9900';"
).replace(
    "Chart.defaults.plugins.tooltip.titleColor = '#06b6d4';",
    "Chart.defaults.plugins.tooltip.titleColor = '#FFCC99';"
).replace(
    "Chart.defaults.plugins.tooltip.bodyColor = '#c9d5e0';",
    "Chart.defaults.plugins.tooltip.bodyColor = '#CC9966';"
).replace(
    "const CHART_COLORS = [\n  '#3b82f6','#06b6d4','#10b981','#f59e0b','#ef4444',\n  '#8b5cf6','#ec4899','#14b8a6','#f97316','#6366f1'\n];",
    "const CHART_COLORS = [\n  '#FF9900','#FFCC99','#CC88FF','#66BBEE','#FF3300',\n  '#FFAA00','#9944BB','#33BB66','#4488BB','#BB77EE'\n];"
).replace(
    "const MODAL_CHART_COLORS = [\n  '#22d3ee','#34d399','#fb923c','#a78bfa','#f472b6',\n  '#facc15','#4ade80','#60a5fa','#f87171','#c084fc'\n];",
    "const MODAL_CHART_COLORS = [\n  '#FFCC99','#33BB66','#FF9900','#CC88FF','#66BBEE',\n  '#FFAA00','#9944BB','#4488BB','#FF3300','#BB77EE'\n];"
)

HEAD = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HyperCore · LCARS Interface</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='2' fill='%23000'/><polygon points='16,4 27,10 27,22 16,28 5,22 5,10' fill='none' stroke='%23FF9900' stroke-width='2'/><circle cx='16' cy='16' r='3' fill='%23FF9900'/></svg>">
<script>
(function(){
  var supported={en:'en',nl:'nl',fr:'fr',de:'de'};
  var cookie=document.cookie.split(';').map(function(c){return c.trim();})
    .find(function(c){return c.startsWith('hcm_lang=');});
  var code=cookie?cookie.split('=')[1]:null;
  if(!code||!supported[code]){
    var lang=((navigator.language||navigator.userLanguage||'en').toLowerCase()).substring(0,2);
    code=supported[lang]||'en';
  }
  window._hcmLang=code;
  document.write('<scr'+'ipt src="/static/lang/'+code+'.js"><\\/scr'+'ipt>');
})();
</script>
<script src="/static/vendor/chart.umd.min.js"></script>
<script src="/static/vendor/chartjs-adapter-date-fns.bundle.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Antonio:wght@400;700&display=swap" rel="stylesheet">
<style>
@import url('/static/vendor/fonts.css');

/* ═══════════════════════════════════════════════
   LCARS THEME · STARFLEET LIBRARY COMPUTER ACCESS &
   RETRIEVAL SYSTEM · HYPERCORE CLUSTER INTERFACE v2.0
   ═══════════════════════════════════════════════ */
:root{
  --lc-orange:   #FF9900;
  --lc-orange-d: #CC6600;
  --lc-peach:    #FFCC99;
  --lc-tan:      #CC9966;
  --lc-gold:     #FFAA00;
  --lc-lilac:    #CC88FF;
  --lc-purple:   #9944BB;
  --lc-mauve:    #BB77EE;
  --lc-red:      #FF3300;
  --lc-green:    #33BB66;
  --lc-blue:     #4488BB;
  --lc-cyan:     #66BBEE;
  --lc-bg:       #000000;
  --lc-bg-card:  #08040A;
  --lc-border:   #331100;
  --lc-text:     #FFCC99;
  --lc-text-dim: #CC7700;
  --lc-text-mut: #774400;
  --sidebar-w:   175px;
  --topbar-h:    72px;
  --botbar-h:    46px;
  --elbow-r:     32px;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Antonio','Segoe UI',sans-serif;background:var(--lc-bg);color:var(--lc-text);height:100vh;overflow:hidden;letter-spacing:.04em}

/* ─── FRAME ───────────────────────────────────────────────────────── */
.lcars-frame{position:relative;display:flex;flex-direction:column;height:100vh}
.lcars-top-row{display:flex;height:var(--topbar-h);flex-shrink:0;position:relative;z-index:10}
.lcars-bot-row{display:flex;height:var(--botbar-h);flex-shrink:0;position:relative;z-index:10}
.lcars-mid-row{display:flex;flex:1;overflow:hidden;position:relative}

/* ─── TOP ELBOW ───────────────────────────────────────────────── */
.lcars-cap-top{
  width:var(--sidebar-w);flex-shrink:0;background:var(--lc-orange);
  border-radius:var(--elbow-r) 0 0 0;position:relative;overflow:hidden
}
/* Inner concave corner: black cut with radius matching elbow */
.lcars-cap-top::after{
  content:'';position:absolute;bottom:-2px;right:-2px;
  width:calc(var(--elbow-r) + 4px);height:calc(var(--elbow-r) + 4px);
  background:var(--lc-bg);border-radius:var(--elbow-r) 0 0 0
}
.lcars-topbar{
  flex:1;background:var(--lc-orange);
  border-radius:0 var(--elbow-r) var(--elbow-r) 0;
  display:flex;align-items:center;padding:0 24px 0 16px;gap:12px;overflow:hidden
}
.lcars-topbar-title{font-size:22px;font-weight:700;color:#000;text-transform:uppercase;letter-spacing:.12em;white-space:nowrap;line-height:1}
.lcars-topbar-sub{font-size:11px;color:rgba(0,0,0,.5);text-transform:uppercase;letter-spacing:.14em;white-space:nowrap}
.lcars-topbar-divider{width:2px;height:34px;background:rgba(0,0,0,.2);flex-shrink:0}
.lcars-topbar-right{display:flex;align-items:center;gap:10px;margin-left:auto}
.lcars-hdr-btn{
  background:rgba(0,0,0,.15);border:1px solid rgba(0,0,0,.3);color:#000;
  font-family:'Antonio',sans-serif;font-size:12px;font-weight:700;
  text-transform:uppercase;letter-spacing:.1em;
  padding:5px 14px;border-radius:20px;cursor:pointer;transition:background .15s;white-space:nowrap
}
.lcars-hdr-btn:hover{background:rgba(0,0,0,.28)}
.lcars-hdr-btn.has-update{background:rgba(255,200,0,.35);border-color:rgba(255,200,0,.6)}
.lcars-status{display:flex;align-items:center;gap:6px}
.status-dot{width:8px;height:8px;border-radius:50%;background:var(--lc-green);animation:pulse-dot 2s ease-in-out infinite}
@keyframes pulse-dot{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(51,187,102,.6)}50%{opacity:.8;box-shadow:0 0 0 5px rgba(51,187,102,0)}}
.status-text{font-family:'JetBrains Mono',monospace;font-size:11px;color:rgba(0,0,0,.7);letter-spacing:0}
.lang-picker{position:relative}
.lang-btn{
  background:rgba(0,0,0,.15);border:1px solid rgba(0,0,0,.3);color:#000;
  font-family:'Antonio',sans-serif;font-size:12px;font-weight:700;
  padding:5px 10px;border-radius:20px;cursor:pointer;transition:background .15s;display:flex;align-items:center;gap:4px
}
.lang-btn:hover,.lang-btn.open{background:rgba(0,0,0,.28)}
.lang-dropdown{
  display:none;position:absolute;top:calc(100% + 6px);right:0;
  background:#1a0800;border:1px solid var(--lc-orange-d);
  padding:4px;min-width:90px;z-index:2000;box-shadow:0 8px 24px rgba(0,0,0,.7)
}
.lang-dropdown.open{display:block}
.lang-opt{padding:6px 10px;font-family:'Antonio',sans-serif;font-size:12px;font-weight:700;color:var(--lc-text-dim);cursor:pointer;text-transform:uppercase;letter-spacing:.1em}
.lang-opt:hover{background:#2a1000;color:var(--lc-orange)}
.lang-opt.active{color:var(--lc-orange)}
.fetch-spinner{width:14px;height:14px;border:2px solid rgba(255,153,0,.2);border-top-color:var(--lc-orange);border-radius:50%;animation:spin .7s linear infinite;display:none;flex-shrink:0}
body.fetching .fetch-spinner{display:block}
@keyframes spin{to{transform:rotate(360deg)}}

/* ─── BOTTOM ELBOW ─────────────────────────────────────────────── */
.lcars-cap-bot{
  width:var(--sidebar-w);flex-shrink:0;background:var(--lc-orange);
  border-radius:0 0 0 var(--elbow-r);position:relative;overflow:hidden
}
.lcars-cap-bot::after{
  content:'';position:absolute;top:-2px;right:-2px;
  width:calc(var(--elbow-r) + 4px);height:calc(var(--elbow-r) + 4px);
  background:var(--lc-bg);border-radius:0 0 0 var(--elbow-r)
}
.lcars-botbar{
  flex:1;background:var(--lc-orange);
  border-radius:var(--elbow-r) 0 0 var(--elbow-r);
  display:flex;align-items:center;padding:0 20px;gap:16px;overflow:hidden
}
.lcars-botbar-text{font-size:12px;font-weight:700;color:rgba(0,0,0,.55);text-transform:uppercase;letter-spacing:.15em;white-space:nowrap}
#lcarsStardate{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;color:#000;letter-spacing:.05em}
.lcars-botbar-right{margin-left:auto;font-size:11px;font-weight:700;color:rgba(0,0,0,.5);text-transform:uppercase;letter-spacing:.12em;display:flex;align-items:center;gap:6px}

/* ─── SIDEBAR ───────────────────────────────────────────────────────── */
.lcars-sidebar{width:var(--sidebar-w);flex-shrink:0;display:flex;flex-direction:column;padding:6px 0;overflow:hidden;background:var(--lc-bg)}
.lc-bar{width:100%;flex-shrink:0;margin:3px 0}
.lc-bar-sm{height:5px}.lc-bar-md{height:9px}.lc-bar-lg{height:18px}
.lc-bar-orange{background:var(--lc-orange-d)}.lc-bar-lilac{background:var(--lc-lilac)}
.lc-bar-gold{background:var(--lc-gold)}.lc-bar-peach{background:var(--lc-peach)}
.lc-bar-blue{background:var(--lc-blue)}.lc-bar-purple{background:var(--lc-purple)}
.lc-bar-green{background:var(--lc-green)}.lc-bar-tan{background:var(--lc-tan)}
.sidebar-spacer{flex:1}
.nav-tabs{display:none!important}
.nav-logout{display:none!important}
.main > .nav-tab{display:none!important}
.nav-tab{
  display:block;width:100%;border:none;border-radius:20px 0 0 20px;
  color:#000;font-family:'Antonio',sans-serif;font-size:12px;font-weight:700;
  text-transform:uppercase;letter-spacing:.08em;padding:9px 10px 9px 18px;
  text-align:left;cursor:pointer;transition:filter .15s;margin:2px 0;
  line-height:1.1;user-select:none;flex-shrink:0;background:var(--lc-peach)
}
.nav-tab:hover{filter:brightness(1.12)}
.nav-tab-infraviewer{background:var(--lc-peach)}
.nav-tab-balancer{background:var(--lc-lilac)}
.nav-tab-foresight{background:var(--lc-gold)}
.nav-tab-modules{background:var(--lc-blue)}
.nav-tab-settings{background:var(--lc-tan)}
.nav-tab-logout-btn{background:var(--lc-red);color:#fff;border-radius:20px 0 0 20px;margin-top:auto}
.nav-tab.active{filter:brightness(1.25);outline:2px solid rgba(255,255,255,.3);outline-offset:-2px}
.nav-tab-line1{display:block;font-size:9px;opacity:.65;letter-spacing:.1em}
.nav-tab-line2{display:block;font-size:12px;letter-spacing:.08em}

/* ─── CONTENT ──────────────────────────────────────────────────────── */
.lcars-content{flex:1;overflow:hidden;display:flex;flex-direction:column;position:relative}
.lcars-content::before{
  content:'';position:absolute;inset:0;pointer-events:none;z-index:1;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(255,153,0,.014) 2px,rgba(255,153,0,.014) 4px)
}
.main{flex:1;padding:14px 20px;overflow-y:auto;overflow-x:hidden;position:relative;z-index:2}
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-track{background:var(--lc-bg)}
::-webkit-scrollbar-thumb{background:var(--lc-orange-d)}
::-webkit-scrollbar-thumb:hover{background:var(--lc-orange)}

/* ─── TABS ────────────────────────────────────────────────────────────── */
.tab-content{display:none}
.tab-content.active{display:block}
.iv-sub-nav,.cb-sub-nav,.sentinel-sub-nav{
  display:flex;border-bottom:2px solid var(--lc-orange-d);
  margin-bottom:12px;margin-left:-20px;margin-right:-20px;
  padding:0 20px;background:#060200;overflow-x:auto;flex-shrink:0
}
.iv-sub-tab,.cb-sub-tab,.sentinel-sub-tab{
  padding:7px 13px;font-family:'Antonio',sans-serif;font-size:12px;font-weight:700;
  color:var(--lc-text-mut);text-transform:uppercase;letter-spacing:.1em;cursor:pointer;
  border-bottom:3px solid transparent;margin-bottom:-2px;transition:all .15s;user-select:none;white-space:nowrap
}
.iv-sub-tab:hover{color:var(--lc-orange)}
.cb-sub-tab:hover{color:var(--lc-lilac)}
.sentinel-sub-tab:hover{color:var(--lc-cyan)}
.iv-sub-tab.active{color:var(--lc-orange);border-bottom-color:var(--lc-orange)}
.cb-sub-tab.active{color:var(--lc-lilac);border-bottom-color:var(--lc-lilac)}
.sentinel-sub-tab.active{color:var(--lc-cyan);border-bottom-color:var(--lc-cyan)}
.iv-panel,.cb-panel,.sentinel-panel{display:none}
.iv-panel.active,.cb-panel.active,.sentinel-panel.active{display:block}

/* ─── CARDS ───────────────────────────────────────────────────────────── */
.cards-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:10px;margin-bottom:14px}
.card{
  background:var(--lc-bg-card);border:1px solid var(--lc-border);
  border-left:4px solid var(--lc-orange-d);padding:13px;transition:border-color .2s;position:relative
}
.card:hover{background:#0d060f}
.card.cpu-ok{border-left-color:var(--lc-green)}
.card.cpu-warn{border-left-color:var(--lc-gold)}
.card.cpu-danger{border-left-color:var(--lc-red)}
.card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:9px;padding-bottom:7px;border-bottom:1px solid var(--lc-border)}
.card-title{font-family:'Antonio',sans-serif;font-size:13px;font-weight:700;color:var(--lc-peach);text-transform:uppercase;letter-spacing:.1em}
.card-badge{font-family:'Antonio',sans-serif;font-size:10px;font-weight:700;padding:2px 10px;border-radius:20px;letter-spacing:.06em;text-transform:uppercase}
.badge-online{background:var(--lc-green);color:#000}
.badge-warning{background:var(--lc-gold);color:#000}
.badge-danger{background:var(--lc-red);color:#fff}
.metric-row{display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-top:1px solid #1a0800}
.metric-label{font-size:11px;color:var(--lc-text-dim);text-transform:uppercase;letter-spacing:.06em}
.metric-value{font-family:'Antonio',sans-serif;font-size:15px;font-weight:700;color:var(--lc-peach)}
.progress-bar{width:100%;height:5px;background:#1a0800;margin-top:3px;overflow:hidden}
.progress-fill{height:100%;transition:width .6s ease}

/* ─── CHARTS ──────────────────────────────────────────────────────────── */
.chart-container{background:var(--lc-bg-card);border:1px solid var(--lc-border);border-top:3px solid var(--lc-orange-d);padding:13px;margin-bottom:13px}
.chart-title{
  font-family:'Antonio',sans-serif;font-size:13px;font-weight:700;color:var(--lc-peach);margin-bottom:9px;
  display:flex;align-items:center;gap:8px;text-transform:uppercase;letter-spacing:.1em;
  border-bottom:1px solid var(--lc-border);padding-bottom:7px
}
.chart-title-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.chart-wrapper{position:relative;height:240px}
.charts-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:13px}
@media(max-width:900px){.charts-row{grid-template-columns:1fr}}

/* ─── TIME RANGE ───────────────────────────────────────────────────── */
.time-range-bar{display:flex;align-items:center;gap:5px;margin-bottom:10px;flex-wrap:wrap;padding:5px 0;border-bottom:1px solid var(--lc-border)}
.time-range-btn{
  background:transparent;border:1px solid var(--lc-border);color:var(--lc-text-dim);
  padding:3px 9px;border-radius:20px;font-family:'Antonio',sans-serif;font-size:11px;font-weight:700;
  text-transform:uppercase;letter-spacing:.06em;cursor:pointer;transition:all .15s;white-space:nowrap
}
.time-range-btn:hover{border-color:var(--lc-orange);color:var(--lc-orange)}
.time-range-btn.active{background:var(--lc-orange-d);border-color:var(--lc-orange);color:var(--lc-peach)}

/* ─── GROUP CONTROLS ─────────────────────────────────────────────────── */
.group-controls{display:flex;align-items:center;gap:8px;margin-bottom:10px;padding:7px 10px;background:var(--lc-bg-card);border:1px solid var(--lc-border)}
.group-label{font-family:'Antonio',sans-serif;font-size:11px;color:var(--lc-text-dim);text-transform:uppercase;letter-spacing:.08em;white-space:nowrap}
.group-btn{
  background:transparent;border:1px solid var(--lc-border);color:var(--lc-text-dim);
  padding:3px 9px;border-radius:20px;font-family:'Antonio',sans-serif;font-size:11px;font-weight:700;
  text-transform:uppercase;letter-spacing:.06em;cursor:pointer;transition:all .15s;white-space:nowrap
}
.group-btn:hover{border-color:var(--lc-cyan);color:var(--lc-cyan)}
.group-btn.active{background:var(--lc-blue);border-color:var(--lc-cyan);color:#000}
.group-header{
  grid-column:1/-1;font-family:'Antonio',sans-serif;font-size:12px;font-weight:700;
  color:var(--lc-cyan);padding:6px 0 4px;border-bottom:1px solid var(--lc-border);margin-bottom:4px;
  display:flex;align-items:center;gap:8px;text-transform:uppercase;letter-spacing:.1em
}
.group-header-dot{width:6px;height:6px;border-radius:50%;background:var(--lc-cyan)}

/* ─── SUB-ITEMS ─────────────────────────────────────────────────────── */
.sub-section{margin-top:9px;padding-top:9px;border-top:1px dashed var(--lc-border)}
.sub-section-title{font-family:'Antonio',sans-serif;font-size:10px;font-weight:700;color:var(--lc-text-mut);text-transform:uppercase;letter-spacing:.1em;margin-bottom:5px}
.sub-item{background:#050205;border:1px solid var(--lc-border);padding:7px 9px;margin-bottom:4px;font-size:11px}
.sub-item-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.sub-item-name{font-family:'Antonio',sans-serif;font-weight:700;font-size:11px;color:var(--lc-peach)}
.sub-item-row{display:flex;justify-content:space-between;padding:2px 0}
.sub-item-label{color:var(--lc-text-dim);font-size:10px;text-transform:uppercase}
.sub-item-value{font-family:'Antonio',sans-serif;font-size:11px;font-weight:700;color:var(--lc-peach)}
.health-dot{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:4px}
.health-ok{background:var(--lc-green)}
.health-bad{background:var(--lc-red)}

/* ─── LOG TABLE ───────────────────────────────────────────────────────── */
.log-table{width:100%;border-collapse:collapse;font-size:12px}
.log-table th{text-align:left;padding:6px 9px;font-family:'Antonio',sans-serif;font-size:10px;font-weight:700;color:var(--lc-text-mut);text-transform:uppercase;letter-spacing:.1em;border-bottom:2px solid var(--lc-orange-d);position:sticky;top:0;background:var(--lc-bg-card)}
.log-table td{padding:6px 9px;border-bottom:1px solid var(--lc-border);font-family:'Antonio',sans-serif;font-size:12px;color:var(--lc-text-dim)}
.log-table tr:hover td{background:#0d0608;color:var(--lc-peach)}
.engine-tag{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
.engine-reactive{background:rgba(68,136,187,.2);color:var(--lc-cyan)}
.engine-predictive{background:rgba(153,68,187,.2);color:var(--lc-lilac)}
.dry-run-tag{display:inline-block;padding:2px 6px;border-radius:20px;font-size:9px;font-weight:700;text-transform:uppercase;background:rgba(255,170,0,.15);color:var(--lc-gold)}
.arrow{color:var(--lc-cyan);margin:0 4px}
.log-scroll{max-height:460px;overflow-y:auto}

/* ─── MODULES ─────────────────────────────────────────────────────────── */
.modules-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:9px}
.module-item{background:var(--lc-bg-card);border:1px solid var(--lc-border);padding:12px;cursor:pointer;transition:all .2s;display:flex;align-items:center;gap:10px}
.module-item:hover{border-color:var(--lc-orange-d);background:#0d060f}
.module-icon{width:32px;height:32px;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0;background:var(--lc-border)}
.module-name{font-family:'Antonio',sans-serif;font-size:12px;font-weight:700;color:var(--lc-peach);text-transform:uppercase}
.module-desc{font-size:11px;color:var(--lc-text-dim);margin-top:2px}
.loading,.empty-state{text-align:center;padding:46px 20px;color:var(--lc-text-dim);font-size:13px;text-transform:uppercase;letter-spacing:.12em}
.spinner{width:28px;height:28px;border:2px solid var(--lc-border);border-top-color:var(--lc-orange);border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 14px}

/* ─── SETTINGS ───────────────────────────────────────────────────────── */
.settings-group{background:var(--lc-bg-card);border:1px solid var(--lc-border);border-top:3px solid var(--lc-orange-d);padding:15px;margin-bottom:11px}
.settings-group-header{display:flex;align-items:center;gap:8px;margin-bottom:15px;padding-bottom:9px;border-bottom:1px solid var(--lc-border)}
.settings-group-title{font-family:'Antonio',sans-serif;font-size:12px;font-weight:700;letter-spacing:.1em;color:var(--lc-peach);flex:1;text-transform:uppercase}
.dirty-badge{font-family:'Antonio',sans-serif;font-size:10px;font-weight:700;color:var(--lc-gold);background:rgba(255,170,0,.1);border:1px solid rgba(255,170,0,.3);border-radius:20px;padding:2px 10px;text-transform:uppercase;letter-spacing:.08em}
.apply-btn{background:transparent;border:1px solid var(--lc-orange-d);color:var(--lc-orange);font-family:'Antonio',sans-serif;font-size:11px;font-weight:700;padding:4px 13px;border-radius:20px;cursor:pointer;transition:all .2s;text-transform:uppercase;letter-spacing:.08em}
.apply-btn:hover{background:rgba(255,153,0,.1);border-color:var(--lc-orange)}
.apply-btn.dirty{background:rgba(255,153,0,.15);border-color:var(--lc-orange);color:var(--lc-peach)}
.ctrl-rack{display:flex;flex-wrap:wrap;gap:8px;align-items:stretch}
.ctrl-strip{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;padding:10px 9px;min-width:92px;background:#050205;border:1px solid var(--lc-border);transition:border-color .2s}
.ctrl-strip:hover{border-color:var(--lc-orange-d)}
.ctrl-strip-wide{min-width:200px;flex:1}
.ctrl-strip-bool{min-width:106px}
.ctrl-label{font-family:'Antonio',sans-serif;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:var(--lc-peach);text-align:center;white-space:nowrap}
.ctrl-label-row{display:flex;align-items:center;gap:4px;justify-content:center}
.ctrl-unit{font-family:'Antonio',sans-serif;font-size:10px;color:var(--lc-text-dim);margin-top:-3px}
.ctrl-range{-webkit-appearance:none;appearance:none;width:100%;min-width:80px;height:4px;border-radius:0;outline:none;cursor:pointer;background:var(--lc-border)}
.ctrl-range::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:0;background:var(--lc-orange);cursor:pointer;box-shadow:0 0 6px rgba(255,153,0,.5)}
.ctrl-range::-moz-range-thumb{width:16px;height:16px;border-radius:0;background:var(--lc-orange);cursor:pointer;border:none}
.ctrl-value-input{width:70px;text-align:center;background:var(--lc-border);border:1px solid var(--lc-orange-d);color:var(--lc-peach);font-family:'Antonio',sans-serif;font-size:15px;font-weight:700;padding:4px 5px;border-radius:0;outline:none}
.ctrl-value-input:focus{border-color:var(--lc-orange)}
.ctrl-value-input::-webkit-inner-spin-button,.ctrl-value-input::-webkit-outer-spin-button{opacity:.4}
.ctrl-text-input{width:100%;background:var(--lc-border);border:1px solid var(--lc-orange-d);color:var(--lc-peach);font-family:'Antonio',sans-serif;font-size:12px;padding:5px 7px;border-radius:0;outline:none}
.ctrl-text-input:focus{border-color:var(--lc-orange)}
.toggle-wrap{display:flex;flex-direction:column;align-items:center;gap:5px}
.toggle-switch{position:relative;width:46px;height:24px;cursor:pointer}
.toggle-switch input{opacity:0;width:0;height:0}
.toggle-track,.toggle-slider{position:absolute;inset:0;border-radius:0;background:var(--lc-border);border:1px solid var(--lc-orange-d);transition:all .25s}
.toggle-thumb{position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:0;background:var(--lc-text-mut);transition:all .25s}
.toggle-slider::after{content:'';position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:0;background:var(--lc-text-mut);transition:all .25s}
.toggle-switch input:checked+.toggle-track{background:rgba(255,153,0,.15);border-color:var(--lc-orange)}
.toggle-switch input:checked+.toggle-track .toggle-thumb{left:25px;background:var(--lc-orange);box-shadow:0 0 8px rgba(255,153,0,.6)}
.toggle-switch input:checked+.toggle-slider{background:rgba(255,153,0,.15);border-color:var(--lc-orange)}
.toggle-switch input:checked+.toggle-slider::after{left:25px;background:var(--lc-orange);box-shadow:0 0 8px rgba(255,153,0,.6)}
.toggle-state-label{font-family:'Antonio',sans-serif;font-size:11px;font-weight:700;color:var(--lc-text-dim)}
.seg-ctrl{display:flex;border:1px solid var(--lc-orange-d);overflow:hidden}
.seg-btn{flex:1;padding:5px 7px;background:var(--lc-border);border:none;border-right:1px solid var(--lc-orange-d);color:var(--lc-text-dim);font-family:'Antonio',sans-serif;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;cursor:pointer;transition:all .15s}
.seg-btn:last-child{border-right:none}
.seg-btn:hover{background:#1a0800;color:var(--lc-orange)}
.seg-btn.active{background:rgba(255,153,0,.2);color:var(--lc-orange)}
.info-btn{background:none;border:none;cursor:pointer;color:var(--lc-text-mut);font-size:10px;padding:0 2px;transition:color .15s;flex-shrink:0;font-family:sans-serif}
.info-btn:hover{color:var(--lc-orange)}
.info-popup{position:fixed;z-index:10000;background:#0d0500;border:1px solid var(--lc-orange);padding:13px 15px;width:300px;box-shadow:0 12px 40px rgba(0,0,0,.8),0 0 20px rgba(255,153,0,.12);display:none;pointer-events:auto}
.info-popup.visible{display:block}
.info-popup-title{font-family:'Antonio',sans-serif;font-size:10px;font-weight:700;letter-spacing:.12em;color:var(--lc-orange);margin-bottom:9px;text-transform:uppercase}
.info-section{margin-bottom:8px}
.info-section:last-child{margin-bottom:0}
.info-section-label{font-family:'Antonio',sans-serif;font-size:9px;font-weight:700;letter-spacing:1px;text-transform:uppercase;margin-bottom:3px}
.info-section-label.what{color:var(--lc-cyan)}
.info-section-label.benefits{color:var(--lc-green)}
.info-section-label.careful{color:var(--lc-gold)}
.info-section p{color:var(--lc-text);font-size:12px;line-height:1.6}

/* ─── VM MODAL ───────────────────────────────────────────────────────── */
.vm-detail-btn{background:rgba(68,136,187,.12);border:1px solid var(--lc-blue);cursor:pointer;color:var(--lc-cyan);font-family:'Antonio',sans-serif;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;padding:3px 9px;transition:all .15s;flex-shrink:0;line-height:1.4}
.vm-detail-btn:hover{background:rgba(68,136,187,.25)}
.vm-modal-backdrop{display:none;position:fixed;inset:0;z-index:10100;background:rgba(0,0,0,.88);align-items:center;justify-content:center}
.vm-modal-backdrop.visible{display:flex}
.vm-modal{background:#0a0206;border:1px solid var(--lc-orange);border-top:3px solid var(--lc-orange);padding:18px 22px;width:min(960px,95vw);max-height:90vh;overflow-y:auto;box-shadow:0 0 40px rgba(255,153,0,.2)}
.vm-modal-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.vm-modal-title{font-family:'Antonio',sans-serif;font-size:14px;font-weight:700;letter-spacing:.12em;color:var(--lc-cyan);text-transform:uppercase}
.vm-modal-close{background:none;border:none;cursor:pointer;color:var(--lc-text-mut);font-size:18px;line-height:1;padding:2px 6px;transition:color .15s}
.vm-modal-close:hover{color:var(--lc-orange)}
.vm-modal-charts{display:grid;grid-template-columns:1fr 1fr;gap:13px}
@media(max-width:680px){.vm-modal-charts{grid-template-columns:1fr}}
.vm-modal-chart-box{background:var(--lc-bg-card);border:1px solid var(--lc-border);border-top:2px solid var(--lc-orange-d);padding:10px 12px}
.vm-modal-chart-label{font-family:'Antonio',sans-serif;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--lc-text-dim);margin-bottom:7px}
.vm-modal-chart-wrap{position:relative;height:160px}

/* ─── UPDATE MODAL ───────────────────────────────────────────────────── */
.update-modal-backdrop{display:none;position:fixed;inset:0;z-index:10200;background:rgba(0,0,0,.88);align-items:center;justify-content:center}
.update-modal-backdrop.visible{display:flex}
.update-modal{background:#0a0206;border:1px solid var(--lc-orange);border-top:3px solid var(--lc-orange);padding:18px 22px;min-width:420px;max-width:500px;width:90vw;display:flex;flex-direction:column;gap:11px;box-shadow:0 0 40px rgba(255,153,0,.2)}
.update-modal-header{display:flex;align-items:center;justify-content:space-between}
.update-modal-title{font-family:'Antonio',sans-serif;font-size:14px;font-weight:700;color:var(--lc-peach);letter-spacing:.1em;text-transform:uppercase}
.update-modal-close{background:none;border:none;cursor:pointer;color:var(--lc-text-mut);font-size:18px;padding:2px 6px;transition:color .15s}
.update-modal-close:hover{color:var(--lc-orange)}
.update-meta{display:flex;gap:13px;font-family:'Antonio',sans-serif;font-size:11px;color:var(--lc-text-mut);flex-wrap:wrap}
.update-sha{color:var(--lc-cyan)}
.update-status-row{display:flex;align-items:center;justify-content:space-between;gap:10px}
.update-status-text{font-family:'Antonio',sans-serif;font-size:14px;font-weight:700;color:var(--lc-text-dim);text-transform:uppercase}
.update-status-text.available{color:var(--lc-gold)}
.update-status-text.uptodate{color:var(--lc-green)}
.update-status-text.checking{color:var(--lc-text-mut)}
.update-status-text.error{color:var(--lc-red)}
.update-actions{display:flex;gap:7px;flex-shrink:0}
.update-apply-btn{padding:4px 12px;font-family:'Antonio',sans-serif;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;cursor:pointer;background:rgba(255,170,0,.12);border:1px solid rgba(255,170,0,.4);color:var(--lc-gold);transition:all .2s}
.update-apply-btn:hover:not(:disabled){background:rgba(255,170,0,.25)}
.update-apply-btn:disabled{opacity:.5;cursor:default}
.update-log{background:#050205;border:1px solid var(--lc-border);padding:8px;font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--lc-text-dim);max-height:150px;overflow-y:auto;display:none;line-height:1.7}
.update-log.visible{display:block}
.update-changelog{background:#050205;border:1px solid var(--lc-border);padding:8px 11px;display:none;font-family:'Antonio',sans-serif;font-size:12px;color:var(--lc-text-dim);max-height:120px;overflow-y:auto;line-height:1.8}
.update-changelog.visible{display:block}
.update-changelog-title{font-size:10px;color:var(--lc-text-mut);text-transform:uppercase;letter-spacing:.1em;margin-bottom:5px}
.update-changelog-entry::before{content:'\\25B8  ';color:var(--lc-orange)}
.refresh-btn{background:transparent;border:1px solid var(--lc-orange-d);color:var(--lc-text-dim);padding:4px 12px;font-family:'Antonio',sans-serif;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;cursor:pointer;transition:all .2s}
.refresh-btn:hover{border-color:var(--lc-orange);color:var(--lc-orange)}
.update-badge-btn{background:none;border:1px solid transparent;cursor:pointer;padding:4px 9px;font-size:12px;font-weight:700;font-family:'Antonio',sans-serif;text-transform:uppercase;letter-spacing:.06em;color:rgba(0,0,0,.55);transition:all .2s;white-space:nowrap}
.update-badge-btn.has-update{color:#000;border-color:rgba(0,0,0,.3);background:rgba(255,200,0,.4);animation:upd-pulse 2.5s ease-in-out infinite}
@keyframes upd-pulse{0%,100%{box-shadow:0 0 0 0 rgba(0,0,0,0)}50%{box-shadow:0 0 0 3px rgba(0,0,0,.1)}}

/* ─── SENTINEL ───────────────────────────────────────────────────────── */
.sev-emergency,.sev-critical{color:var(--lc-red)}
.sev-warning{color:var(--lc-gold)}
.sev-info{color:var(--lc-green)}
.sev-badge{font-family:'Antonio',sans-serif;font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px;text-transform:uppercase;letter-spacing:.06em}
.sev-badge.sev-emergency,.sev-badge.sev-critical{background:rgba(255,51,0,.15);color:var(--lc-red)}
.sev-badge.sev-warning{background:rgba(255,170,0,.15);color:var(--lc-gold)}
.sev-badge.sev-attention{background:rgba(153,68,187,.15);color:var(--lc-lilac)}
.sev-badge.sev-info{background:rgba(51,187,102,.15);color:var(--lc-green)}
.rs-badge{font-family:'Antonio',sans-serif;font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px;text-transform:uppercase;letter-spacing:.06em}
.rs-badge.rs-act-now{background:rgba(255,170,0,.15);color:var(--lc-gold)}
.rs-badge.rs-recommended{background:rgba(102,187,238,.15);color:var(--lc-cyan)}
.rs-badge.rs-low-priority{background:rgba(80,80,80,.2);color:var(--lc-text-mut)}
.risk-high{background:rgba(255,51,0,.15);color:var(--lc-red);font-family:'Antonio',sans-serif;font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px}
.risk-medium{background:rgba(255,170,0,.15);color:var(--lc-gold);font-family:'Antonio',sans-serif;font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px}
.risk-low{background:rgba(51,187,102,.15);color:var(--lc-green);font-family:'Antonio',sans-serif;font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px}
.sentinel-table{width:100%;border-collapse:collapse;font-size:12px}
.sentinel-table th{text-align:left;padding:6px 9px;font-family:'Antonio',sans-serif;font-size:10px;font-weight:700;color:var(--lc-text-mut);text-transform:uppercase;letter-spacing:.1em;border-bottom:2px solid var(--lc-orange-d);background:var(--lc-bg-card);white-space:nowrap}
.sentinel-table td{padding:7px 9px;border-bottom:1px solid var(--lc-border);font-family:'Antonio',sans-serif;font-size:12px;color:var(--lc-text-dim)}
.sentinel-table tr:hover td{background:#0d060f;color:var(--lc-peach)}
.sentinel-table .vm-name{color:var(--lc-peach);font-weight:700}
.direction-over{color:var(--lc-cyan)}
.direction-under{color:var(--lc-gold)}
.health-gauge{display:flex;align-items:center;gap:17px;background:var(--lc-bg-card);border:1px solid var(--lc-border);border-left:4px solid var(--lc-orange);padding:15px 19px;margin-bottom:11px}
.health-score-circle{width:72px;height:72px;border-radius:0;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-family:'Antonio',sans-serif;font-size:25px;font-weight:700;border:2px solid var(--lc-orange-d)}
.health-meta{flex:1}
.health-meta-row{display:flex;align-items:center;justify-content:space-between;padding:3px 0;font-size:12px}
.health-meta-label{color:var(--lc-text-dim);text-transform:uppercase;letter-spacing:.06em}
.health-meta-value{font-family:'Antonio',sans-serif;font-size:13px;font-weight:700;color:var(--lc-peach)}
.sentinel-tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:8px;margin-bottom:14px}
.sentinel-tile{background:var(--lc-bg-card);border:1px solid var(--lc-border);padding:12px;text-align:center}
.sentinel-tile-val{font-family:'Antonio',sans-serif;font-size:28px;font-weight:700;margin-bottom:3px;color:var(--lc-peach)}
.sentinel-tile-label{font-size:11px;color:var(--lc-text-dim);text-transform:uppercase;letter-spacing:.08em}
.pdf-btn{display:inline-flex;align-items:center;gap:8px;background:transparent;border:1px solid var(--lc-orange-d);color:var(--lc-text-dim);padding:7px 15px;font-family:'Antonio',sans-serif;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;cursor:pointer;text-decoration:none;transition:all .2s}
.pdf-btn:hover{border-color:var(--lc-orange);color:var(--lc-orange)}
.pdf-btn.loading{opacity:.5;pointer-events:none}
.settings-toast{position:fixed;bottom:calc(var(--botbar-h) + 13px);right:16px;z-index:9999;background:#0a0206;border:1px solid var(--lc-border);padding:8px 15px;font-family:'Antonio',sans-serif;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--lc-peach);transform:translateY(50px);opacity:0;transition:all .3s cubic-bezier(.34,1.56,.64,1);pointer-events:none}
.settings-toast.show{transform:translateY(0);opacity:1}
.settings-toast.ok{border-color:var(--lc-green);color:var(--lc-green)}
.settings-toast.err{border-color:var(--lc-red);color:var(--lc-red)}

/* ─── USER MANAGER ─────────────────────────────────────────────────── */
.um-table{width:100%;border-collapse:collapse;font-size:12px}
.um-table th{padding:6px 9px;text-align:left;font-family:'Antonio',sans-serif;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--lc-text-mut);border-bottom:2px solid var(--lc-orange-d);white-space:nowrap}
.um-table td{padding:7px 9px;border-bottom:1px solid var(--lc-border);color:var(--lc-text-dim);vertical-align:middle}
.um-table tr:last-child td{border-bottom:none}
.um-table tr:hover td{background:#0d0608}
.um-table .um-username{color:var(--lc-peach);font-weight:700}
.um-badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;font-family:'Antonio',sans-serif;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.um-badge-admin{background:rgba(68,136,187,.15);color:var(--lc-blue);border:1px solid rgba(68,136,187,.3)}
.um-badge-user{background:var(--lc-border);color:var(--lc-text-mut);border:1px solid var(--lc-border)}
.um-badge-otp-on{background:rgba(51,187,102,.12);color:var(--lc-green);border:1px solid rgba(51,187,102,.3)}
.um-badge-otp-off{background:var(--lc-border);color:var(--lc-text-mut);border:1px solid var(--lc-border)}
.um-action-btn{background:var(--lc-border);border:1px solid var(--lc-orange-d);color:var(--lc-text-mut);padding:3px 9px;font-size:11px;font-family:'Antonio',sans-serif;font-weight:700;text-transform:uppercase;letter-spacing:.06em;cursor:pointer;transition:all .15s}
.um-action-btn:hover{border-color:var(--lc-orange-d);color:var(--lc-peach)}
.um-action-btn.danger:hover{border-color:var(--lc-red);color:var(--lc-red);background:rgba(255,51,0,.08)}
.um-add-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px 0 4px;border-top:1px solid var(--lc-border);margin-top:8px}
.um-input{background:var(--lc-border);border:1px solid var(--lc-orange-d);color:var(--lc-peach);border-radius:0;padding:5px 7px;font-size:12px;font-family:'Antonio',sans-serif;outline:none}
.um-input:focus{border-color:var(--lc-orange)}
.um-input::placeholder{color:var(--lc-text-mut);opacity:.7}
.um-select{background:var(--lc-border);border:1px solid var(--lc-orange-d);color:var(--lc-text-dim);border-radius:0;padding:5px 7px;font-size:12px;font-family:'Antonio',sans-serif;cursor:pointer;outline:none}
.um-primary-btn{background:rgba(68,136,187,.15);border:1px solid rgba(68,136,187,.4);color:var(--lc-blue);padding:5px 12px;font-family:'Antonio',sans-serif;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;cursor:pointer;transition:all .15s;white-space:nowrap}
.um-primary-btn:hover{background:rgba(68,136,187,.25);border-color:var(--lc-blue)}
.um-primary-btn:disabled{opacity:.4;pointer-events:none}
.um-error{color:var(--lc-red);font-size:11px;margin-top:3px}
.um-success{color:var(--lc-green);font-size:11px;margin-top:3px}
.um-section{margin-bottom:15px;padding-bottom:15px;border-bottom:1px solid var(--lc-border)}
.um-section:last-child{margin-bottom:0;padding-bottom:0;border-bottom:none}
.um-section-title{font-family:'Antonio',sans-serif;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--lc-text-mut);margin-bottom:6px}
.um-field-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:5px}
.um-otp-qr{display:flex;gap:15px;align-items:flex-start;flex-wrap:wrap;margin-top:7px}
.um-otp-qr img{border:1px solid var(--lc-orange-d);display:block}
.um-secret-box{background:var(--lc-border);border:1px solid var(--lc-orange-d);padding:6px 9px;font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--lc-text-dim);word-break:break-all;max-width:260px}
.um-hint{font-size:11px;color:var(--lc-text-mut);margin-top:3px}
.um-dev-notice{display:inline-block;padding:2px 9px;font-size:10px;font-family:'Antonio',sans-serif;font-weight:700;text-transform:uppercase;background:rgba(255,170,0,.1);color:var(--lc-gold);border:1px solid rgba(255,170,0,.25);margin-left:8px}
#dataQualityBanner{display:none;position:fixed;bottom:var(--botbar-h);left:0;right:0;z-index:9999;background:#2a0500;border-top:1px solid var(--lc-red);color:#FF6644;font-size:12px;font-family:'Antonio',sans-serif;font-weight:700;text-transform:uppercase;letter-spacing:.1em;padding:5px 15px;text-align:center}
#dataQualityBanner.visible{display:block}
/* Form elements */
input[type=text],input[type=password],input[type=number],select,textarea{background:var(--lc-border);border:1px solid var(--lc-orange-d);color:var(--lc-peach);font-family:'Antonio',sans-serif;padding:5px 8px;outline:none;border-radius:0}
input[type=text]:focus,input[type=password]:focus,input[type=number]:focus,select:focus{border-color:var(--lc-orange)}
input::placeholder{color:var(--lc-text-mut);opacity:.7}
label{color:var(--lc-text-dim);font-family:'Antonio',sans-serif;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
button{font-family:'Antonio',sans-serif}
code{background:var(--lc-border);padding:2px 5px;color:var(--lc-peach);font-family:'JetBrains Mono',monospace;font-size:11px}
</style>
</head>
"""

BODY_FRAME = """\
<body>
<div class="lcars-frame">

  <!-- TOP ROW: elbow cap + header bar -->
  <div class="lcars-top-row">
    <div class="lcars-cap-top"></div>
    <div class="lcars-topbar">
      <div>
        <div class="lcars-topbar-title">HyperCore</div>
        <div class="lcars-topbar-sub">Cluster Interface</div>
      </div>
      <div class="lcars-topbar-divider"></div>
      <div class="lcars-status">
        <div class="status-dot" id="statusDot"></div>
        <span class="status-text" id="statusText">Connecting…</span>
      </div>
      <div class="lcars-topbar-right">
        <div class="fetch-spinner"></div>
        <button class="lcars-hdr-btn" onclick="refreshAll()">↻ Refresh</button>
        <button class="update-badge-btn lcars-hdr-btn" id="updateBadgeBtn" onclick="openUpdateModal()">Update</button>
        <div class="lang-picker" id="langPicker">
          <button class="lang-btn" id="langBtn" onclick="toggleLangDropdown(event)">
            <span style="font-size:13px">\U0001f310</span><span style="font-size:9px;opacity:.6">▾</span>
          </button>
          <div class="lang-dropdown" id="langDropdown">
            <div class="lang-opt" data-lang="en" onclick="setLang('en')">EN</div>
            <div class="lang-opt" data-lang="nl" onclick="setLang('nl')">NL</div>
            <div class="lang-opt" data-lang="fr" onclick="setLang('fr')">FR</div>
            <div class="lang-opt" data-lang="de" onclick="setLang('de')">DE</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- MIDDLE ROW: sidebar + content -->
  <div class="lcars-mid-row">

    <!-- LCARS SIDEBAR NAV -->
    <div class="lcars-sidebar">
      <div class="lc-bar lc-bar-md lc-bar-orange"></div>
      <div class="lc-bar lc-bar-sm lc-bar-lilac"></div>
      <div class="lc-bar lc-bar-sm lc-bar-gold"></div>
      <div class="lc-bar lc-bar-sm lc-bar-peach"></div>
      <div style="height:8px"></div>

      <button class="nav-tab nav-tab-infraviewer active" data-tab="infraviewer" onclick="switchTab('infraviewer')">
        <span class="nav-tab-line1">SC//</span>
        <span class="nav-tab-line2">Infraviewer</span>
      </button>
      <button class="nav-tab nav-tab-balancer" data-tab="clusterbalancer" onclick="switchTab('clusterbalancer')">
        <span class="nav-tab-line1">SC//</span>
        <span class="nav-tab-line2">Clusterbalancer</span>
      </button>
      <button class="nav-tab nav-tab-foresight" data-tab="sentinel" onclick="switchTab('sentinel')">
        <span class="nav-tab-line1">SC//</span>
        <span class="nav-tab-line2">Foresight</span>
      </button>

      <div style="height:10px"></div>
      <div class="lc-bar lc-bar-sm lc-bar-blue"></div>
      <div class="lc-bar lc-bar-sm lc-bar-purple"></div>
      <div style="height:10px"></div>

      <button class="nav-tab nav-tab-modules" data-tab="modules" onclick="switchTab('modules')">
        <span class="nav-tab-line1">System</span>
        <span class="nav-tab-line2">Modules</span>
      </button>
      <button class="nav-tab nav-tab-settings" data-tab="settings" onclick="switchTab('settings')">
        <span class="nav-tab-line1">⚙</span>
        <span class="nav-tab-line2">Settings</span>
      </button>

      <div class="sidebar-spacer"></div>
      <div class="lc-bar lc-bar-sm lc-bar-orange"></div>
      <div class="lc-bar lc-bar-sm lc-bar-tan"></div>

      <a href="/logout" style="text-decoration:none;display:block">
        <div class="nav-tab nav-tab-logout-btn">
          <span class="nav-tab-line1">⏻</span>
          <span class="nav-tab-line2">Sign Out</span>
        </div>
      </a>
      <div class="lc-bar lc-bar-sm lc-bar-orange"></div>
    </div><!-- /.lcars-sidebar -->

    <!-- CONTENT AREA -->
    <div class="lcars-content">
      <!-- Hidden nav-tabs: JS uses .nav-tab[data-tab] to manage active state -->
      <div class="nav-tabs" style="display:none!important">
        <div class="nav-tab" data-tab="infraviewer"></div>
        <div class="nav-tab" data-tab="clusterbalancer"></div>
        <div class="nav-tab" data-tab="sentinel"></div>
        <div class="nav-tab" data-tab="modules"></div>
        <div class="nav-tab" data-tab="settings"></div>
      </div>

"""

BODY_CLOSE = """\
      <div class="settings-toast" id="settingsToast"></div>
    </div><!-- /.lcars-content -->
  </div><!-- /.lcars-mid-row -->

  <!-- BOTTOM ROW: elbow cap + footer bar -->
  <div class="lcars-bot-row">
    <div class="lcars-cap-bot"></div>
    <div class="lcars-botbar">
      <span class="lcars-botbar-text">Stardate</span>
      <span id="lcarsStardate">—</span>
      <div class="lcars-topbar-divider"></div>
      <span class="lcars-botbar-text">HyperCore Manager</span>
      <div class="lcars-botbar-right" id="lcarsSystemMsg">All Systems Nominal</div>
    </div>
  </div>

</div><!-- /.lcars-frame -->
"""

LCARS_EXTRA_JS = """\
<script>
// LCARS: Live stardate clock
(function(){
  function sd(){
    var n=new Date(),y=n.getFullYear();
    var s=new Date(y,0,1),e=new Date(y+1,0,1);
    var f=(n-s)/(e-s);
    var maj=(y-1987)*1000+Math.floor(f*1000);
    var dec=Math.floor((n.getHours()*60+n.getMinutes())/144);
    return maj+'.'+dec;
  }
  function u(){var el=document.getElementById('lcarsStardate');if(el)el.textContent=sd();}
  u();setInterval(u,60000);
})();

// LCARS: patch switchTab so sidebar buttons track active state
(function(){
  var orig=window.switchTab;
  window.switchTab=function(t){
    orig(t);
    document.querySelectorAll('.lcars-sidebar .nav-tab[data-tab]').forEach(function(b){
      b.classList.toggle('active',b.dataset.tab===t);
    });
  };
})();
</script>
"""

# Assemble and write
output = HEAD + BODY_FRAME + tab_html + BODY_CLOSE + js_block + LCARS_EXTRA_JS + end_html

with open(output_path, 'w', encoding='utf-8') as f:
    f.write(output)

print(f"Done. {len(output)} chars, {output.count(chr(10))} lines -> {output_path}")
