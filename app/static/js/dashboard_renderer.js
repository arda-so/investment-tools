/* ══════════════════════════════════════════════════════════════════════════════
   Dashboard Renderer — Premium investor dashboard
   ══════════════════════════════════════════════════════════════════════════════
   DashboardView.build(data) → HTML string
   DashboardView.init(data)  → attach event listeners + start auto-refresh
══════════════════════════════════════════════════════════════════════════════ */
(function(){
"use strict";

/* ─── Utilities ─────────────────────────────────────────────────────── */
function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function fmtPct(v){
  var n = Number(v||0);
  if(!isFinite(n)) return '-';
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}
function fmtUsd(v){
  var n = Number(v||0);
  if(!isFinite(n) || n===0) return '-';
  var sign = n >= 0 ? '+' : '';
  if(Math.abs(n) >= 1e6) return sign + '$' + (n/1e6).toFixed(2) + 'M';
  if(Math.abs(n) >= 1e3) return sign + '$' + (n/1e3).toFixed(1) + 'K';
  return sign + '$' + n.toFixed(0);
}
function dayPillHtml(day, size){
  var cls = size === 'sm' ? 'ds-chg-pill sm' : 'ds-chg-pill';
  var ds = String(day || '-');
  var dn = parseFloat(ds.replace('%',''));
  if(ds.charAt(0) === '+' || dn > 0) return '<span class="'+cls+' up">'+esc(ds)+'</span>';
  if(ds.charAt(0) === '-' || dn < 0) return '<span class="'+cls+' down">'+esc(ds)+'</span>';
  return '<span class="'+cls+' flat">'+esc(ds)+'</span>';
}
function timeAgo(ts){
  if(!ts) return '';
  try {
    var d;
    if(/^\d{10,}$/.test(String(ts))) d = new Date(Number(ts)*1000);
    else d = new Date(ts);
    if(isNaN(d.getTime())) return '';
    var diff = Math.max(0, (Date.now() - d.getTime())/1000);
    if(diff < 60) return 'just now';
    if(diff < 3600) return Math.floor(diff/60)+'m ago';
    if(diff < 86400) return Math.floor(diff/3600)+'h ago';
    return Math.floor(diff/86400)+'d ago';
  } catch(e){ return ''; }
}

/* ─── Build full HTML ───────────────────────────────────────────────── */
function build(data){
  var html = '<div class="brief-shell" id="dsDashboardRoot">';

  // Row 0: Header bar (greeting + market status + attention)
  html += buildHeaderBar(data);

  // Row 1: Market Ribbon with big-mover highlights
  html += buildMarketRibbon(data);

  // Row 2: Morning Brief (full-width hero)
  html += buildMorningBriefHero(data);

  // Row 3: Bento Grid (AI Insights left tall | News + Earnings stacked right)
  html += '<section class="ds-bento">';
  html += buildAiInsights(data);
  html += '<div class="ds-bento-right">';
  html += buildNewsWire(data);
  html += buildEarningsCalendar(data);
  html += '</div>';
  html += '</section>';

  // Row 4: Sectors Heatmap
  html += buildSectorsGrid(data);

  // Row 5: Market Data Grid (always visible)
  html += buildMarketDataGrid(data);

  // Row 6: Cross-Asset Relationships
  html += buildCrossAsset(data);

  html += '</div>';
  return html;
}

/* ─── Row 0: Header Bar ────────────────────────────────────────────── */
function buildHeaderBar(data){
  var greeting = data.greeting_time || 'Good morning';
  var status = data.market_status || 'closed';
  var statusLabel = {open:'Market Open',closed:'Market Closed','pre-market':'Pre-Market','after-hours':'After Hours'}[status] || status;
  var statusCls = status === 'open' ? 'open' : (status === 'pre-market' || status === 'after-hours' ? 'pre' : 'closed');
  var attn = Number(data.attention_count||0);
  var relPerf = Number(data.relative_perf||0);
  var pfDay = Number(data.portfolio_day_pct||0);
  var spDay = Number(data.sp500_day_pct||0);

  var html = '<header class="ds-header-bar">';
  html += '<div class="ds-header-left">';
  html += '<h2 class="ds-greeting">'+esc(greeting)+'</h2>';
  html += '<span class="ds-market-status ds-status-'+statusCls+'">';
  if(status==='open') html += '<span class="ds-status-dot pulse"></span>';
  else html += '<span class="ds-status-dot"></span>';
  html += statusLabel+'</span>';
  html += '</div>';

  html += '<div class="ds-header-right">';
  // Relative performance
  var rpDir = relPerf >= 0 ? 'up' : 'down';
  html += '<div class="ds-header-metric">';
  html += '<span class="ds-header-metric-label">You vs S&P</span>';
  html += '<span class="ds-header-metric-val '+rpDir+'">'+fmtPct(relPerf)+'</span>';
  html += '</div>';
  html += '<div class="ds-header-metric">';
  html += '<span class="ds-header-metric-label">Portfolio</span>';
  html += '<span class="ds-header-metric-val '+(pfDay>=0?'up':'down')+'">'+fmtPct(pfDay)+'</span>';
  html += '</div>';
  html += '<div class="ds-header-metric">';
  html += '<span class="ds-header-metric-label">S&P 500</span>';
  html += '<span class="ds-header-metric-val '+(spDay>=0?'up':'down')+'">'+fmtPct(spDay)+'</span>';
  html += '</div>';
  if(attn > 0){
    html += '<div class="ds-attention-badge">'+attn+'<span class="ds-attn-label"> need review</span></div>';
  }
  html += '</div></header>';
  return html;
}

/* ─── Row 1: Market Ribbon ─────────────────────────────────────────── */
function buildMarketRibbon(data){
  var m = (data.market && data.market.items) || {};
  var tickers = [
    {key:'sp500', label:'S&P 500', accent:'blue'},
    {key:'nasdaq', label:'NASDAQ', accent:'purple'},
    {key:'dow', label:'DOW', accent:'green'},
    {key:'russell2000', label:'RUSSELL', accent:'amber'},
    {key:'vix', label:'VIX', accent:'red'},
    {key:'us10y', label:'10Y', accent:'blue'},
    {key:'crude', label:'OIL', accent:'amber'},
    {key:'gold', label:'GOLD', accent:'gold'},
    {key:'eurusd', label:'EUR/USD', accent:'green'}
  ];
  var html = '<section class="ds-ribbon">';
  for(var i=0; i<tickers.length; i++){
    var t = tickers[i];
    var row = m[t.key] || {};
    var ds = String(row.day || '-');
    var dn = parseFloat(ds.replace('%',''));
    var dir = dn > 0 ? 'up' : (dn < 0 ? 'down' : 'flat');
    var bigMover = Math.abs(dn) >= 3;
    html += '<div class="ds-ribbon-item ds-ribbon-'+t.accent+(bigMover ? ' ds-big-mover '+dir : '')+'">';
    html += '<span class="ds-ribbon-label">'+t.label+'</span>';
    html += '<span class="ds-ribbon-price">'+esc(row.price||'-')+'</span>';
    html += '<span class="ds-ribbon-chg '+dir+'">'+esc(ds)+'</span>';
    html += '</div>';
  }
  html += '</section>';
  return html;
}

/* ─── Row 2: Morning Brief Hero ───────────────────────────────────── */
function buildMorningBriefHero(data){
  var rp = data.report_panels || {};
  var points = rp.morning_points || [];
  var updated = rp.morning_updated || '-';
  // Determine overall sentiment from portfolio day change
  var pfDay = Number(data.portfolio_day_pct||0);
  var sentCls = pfDay > 0.3 ? 'ds-sent-up' : (pfDay < -0.3 ? 'ds-sent-down' : 'ds-sent-flat');

  var html = '<article class="ds-hero-card ds-hero-brief '+sentCls+'">';
  html += '<div class="ds-hero-head">';
  html += '<div class="ds-hero-icon-wrap"><span class="ds-hero-icon">&#x2600;</span></div>';
  html += '<div><h3 class="ds-hero-title">Morning Brief</h3>';
  html += '<span class="ds-hero-meta">Updated '+esc(updated)+'</span></div>';
  html += '</div>';
  if(!points.length){
    html += '<div class="ds-empty-state">No morning brief available yet. Data generates automatically from your portfolio and market conditions.</div>';
  } else {
    // Headline: first bullet as hero text
    html += '<div class="ds-brief-headline">'+esc(points[0])+'</div>';
    if(points.length > 1){
      html += '<div class="ds-brief-list">';
      for(var i=1; i<points.length; i++){
        var pt = points[i];
        var cat = 'info';
        var icon = '&#x1F4CA;';
        var low = pt.toLowerCase();
        if(low.indexOf('portfolio')===0 || low.indexOf('portfolio:')>=0){ cat='portfolio'; icon='&#x1F4BC;'; }
        else if(low.indexOf('macro')===0 || low.indexOf('macro:')>=0){ cat='macro'; icon='&#x1F30D;'; }
        else if(low.indexOf('risk')===0 || low.indexOf('risk:')>=0){ cat='risk'; icon='&#x26A0;'; }
        else if(low.indexOf('watch')===0 || low.indexOf('watch:')>=0){ cat='watch'; icon='&#x1F440;'; }
        else if(low.indexOf('catalyst')===0 || low.indexOf('catalyst:')>=0){ cat='catalyst'; icon='&#x26A1;'; }
        else if(low.indexOf('news')>=0){ cat='news'; icon='&#x1F4F0;'; }
        else if(low.indexOf('overnight')>=0){ cat='macro'; icon='&#x1F30D;'; }
        html += '<div class="ds-brief-row ds-brief-'+cat+'">';
        html += '<span class="ds-brief-icon">'+icon+'</span>';
        html += '<span class="ds-brief-text">'+esc(pt)+'</span>';
        html += '</div>';
      }
      html += '</div>';
    }
  }
  html += '</article>';
  return html;
}

/* ─── Row 3a: AI Insights ──────────────────────────────────────────── */
function buildAiInsights(data){
  var proposals = data.proposals || [];
  var breaches = data.thesis_breach_alerts || [];
  var cascades = data.cascade_alerts || [];
  var html = '<article class="ds-col-card ds-col-insights">';
  html += '<div class="ds-col-head">';
  html += '<h3 class="ds-col-title"><span class="ds-col-icon">&#x1F916;</span> AI Insights</h3>';
  html += '<button class="ds-btn-subtle" type="button" id="dsRefreshInsights">Refresh</button>';
  html += '</div>';

  var tabs = [
    {id:'proposals', label:'Proposals', count: proposals.length},
    {id:'thesis', label:'Thesis', count: breaches.length},
    {id:'cascade', label:'Cascades', count: cascades.length},
    {id:'accuracy', label:'Accuracy'},
    {id:'lows', label:'52W Lows'},
    {id:'audit', label:'Audit'},
    {id:'health', label:'Health'}
  ];
  html += '<div class="ds-col-tabs">';
  for(var i=0; i<tabs.length; i++){
    var active = i === 0 ? ' is-active' : '';
    html += '<button type="button" class="ds-tab-pill'+active+'" data-ds-ai-tab="'+tabs[i].id+'">'+tabs[i].label;
    if(tabs[i].count > 0) html += '<span class="ds-tab-count">'+tabs[i].count+'</span>';
    html += '</button>';
  }
  html += '</div>';

  html += '<div class="ds-col-body">';

  // Proposals — compact rows, top 5 visible, expand for rest
  html += '<div class="ds-ai-panel" data-ds-ai-panel="proposals">';
  if(!proposals.length){
    html += '<div class="ds-empty-state">No open proposals</div>';
  } else {
    var showCount = Math.min(proposals.length, 5);
    for(var pi=0; pi<proposals.length; pi++){
      var p = proposals[pi];
      var pScore = Number(p.priority_score||0);
      var conf = Math.round((p.confidence||0)*100);
      // Get first insight as one-line summary
      var summary = '';
      if(p.insights && p.insights.length) summary = String(p.insights[0].text||'').substring(0,100);
      else if(p.title) summary = p.title;

      var hiddenCls = pi >= showCount ? ' ds-proposal-overflow' : '';
      html += '<div class="ds-proposal-row'+hiddenCls+'"'+(pi>=showCount?' hidden':'')+'>';
      // Left: ticker + direction
      html += '<div class="ds-proposal-left">';
      html += '<span class="ds-tk-link" data-ds-ticker="'+esc(p.ticker)+'">'+esc(p.ticker)+'</span>';
      html += '<span class="ds-meta-tag">'+esc(p.direction||'REVIEW')+'</span>';
      html += '</div>';
      // Center: summary
      html += '<div class="ds-proposal-summary">'+esc(summary)+(summary.length>=100?'&hellip;':'')+'</div>';
      // Right: confidence + priority + actions
      html += '<div class="ds-proposal-right">';
      html += '<span class="ds-meta-tag">'+conf+'%</span>';
      html += '<span class="ds-priority-badge '+(pScore>=9?'high':(pScore>=7?'med':'low'))+'">P'+pScore.toFixed(0)+'</span>';
      html += '<a class="ds-btn-primary ds-btn-sm" href="/ticker/'+esc(p.ticker)+'" style="text-decoration:none">Open</a>';
      html += '<button class="ds-btn-ghost ds-btn-sm" data-ds-reject-toggle="'+p.id+'">&#x2715;</button>';
      html += '</div>';
      html += '</div>';
      // Hidden reject input
      html += '<div class="ds-reject-wrap" id="ds-reject-'+p.id+'" hidden>';
      html += '<div style="display:flex;gap:8px;align-items:center;padding:6px 0 6px 8px;">';
      html += '<input type="text" class="ds-reject-reason" data-ds-reject-input="'+p.id+'" placeholder="Reason (optional)">';
      html += '<button class="ds-btn-primary ds-btn-sm" data-ds-reject-confirm="'+p.id+'">Dismiss</button>';
      html += '</div></div>';
    }
    if(proposals.length > showCount){
      html += '<button class="ds-btn-text ds-show-all-proposals" data-ds-show-all="proposals">View all '+proposals.length+' proposals &rarr;</button>';
    }
  }
  html += '</div>';

  html += buildThesisPanel(data);
  html += buildCascadePanel(data);
  html += buildAccuracyPanel(data);
  html += buildLowsPanel(data);
  html += buildAuditPanel(data);
  html += buildHealthPanel(data);

  html += '</div></article>';
  return html;
}

/* ── Thesis Alerts ── */
function buildThesisPanel(data){
  var alerts = data.thesis_breach_alerts || [];
  var html = '<div class="ds-ai-panel" data-ds-ai-panel="thesis" hidden>';
  if(!alerts.length){
    html += '<div class="ds-empty-state">No open thesis breach alerts</div>';
  } else {
    for(var i=0; i<alerts.length; i++){
      var a = alerts[i];
      html += '<div class="ds-insight-card" id="ds-breach-'+a.id+'">';
      html += '<div class="ds-insight-top">';
      html += '<span class="ds-tk-link" data-ds-ticker="'+esc(a.ticker)+'">'+esc(a.ticker)+'</span>';
      var sevCls = (a.severity==='critical'||a.severity==='high') ? 'high' : 'med';
      html += '<span class="ds-priority-badge '+sevCls+'">'+esc((a.severity||'medium').toUpperCase())+'</span>';
      html += '</div>';
      html += '<div class="ds-insight-detail"><p class="ds-insight-detail-text">'+esc(a.breach_detail||'-')+'</p></div>';
      if(a.actual_values) html += '<div class="ds-meta-sm">'+esc(a.actual_values)+'</div>';
      html += '<button class="ds-btn-ghost" data-ds-dismiss-breach="'+a.id+'">Dismiss</button>';
      html += '</div>';
    }
  }
  html += '</div>';
  return html;
}

/* ── Cascade Alerts ── */
function buildCascadePanel(data){
  var alerts = data.cascade_alerts || [];
  var html = '<div class="ds-ai-panel" data-ds-ai-panel="cascade" hidden>';
  if(!alerts.length){
    html += '<div class="ds-empty-state">No open cascade alerts</div>';
  } else {
    for(var i=0; i<alerts.length; i++){
      var c = alerts[i];
      html += '<div class="ds-insight-card" id="ds-cascade-'+c.id+'">';
      html += '<div class="ds-insight-top">';
      html += '<div>';
      html += '<span class="ds-tk-link" data-ds-ticker="'+esc(c.trigger_ticker)+'">'+esc(c.trigger_ticker)+'</span>';
      html += '<span class="ds-cascade-arrow">&rarr;</span>';
      html += '<span class="ds-tk-link" data-ds-ticker="'+esc(c.affected_ticker)+'">'+esc(c.affected_ticker)+'</span>';
      html += '</div>';
      var cSevCls = (c.severity==='critical'||c.severity==='high') ? 'high' : 'med';
      html += '<span class="ds-priority-badge '+cSevCls+'">'+Math.round((c.confidence||0)*100)+'%</span>';
      html += '</div>';
      html += '<div class="ds-insight-detail"><p class="ds-insight-detail-text">'+esc(c.effect_summary||'-')+'</p></div>';
      if(c.relationship) html += '<div class="ds-meta-sm">Link: '+esc(c.relationship)+'</div>';
      html += '<button class="ds-btn-ghost" data-ds-dismiss-cascade="'+c.id+'">Dismiss</button>';
      html += '</div>';
    }
  }
  html += '</div>';
  return html;
}

/* ── Accuracy ── */
function buildAccuracyPanel(data){
  var a = data.ai_accuracy || {};
  var html = '<div class="ds-ai-panel" data-ds-ai-panel="accuracy" hidden>';
  if(!a.ok){
    html += '<div class="ds-empty-state">No measured outcomes yet</div>';
  } else {
    html += '<div class="ds-accuracy-grid">';
    var hrCls = (a.overall_hit_rate||0)>=60?'up':((a.overall_hit_rate||0)>=40?'flat':'down');
    html += '<div class="ds-accuracy-card"><div class="ds-accuracy-num '+hrCls+'">'+Number(a.overall_hit_rate||0).toFixed(1)+'%</div>';
    html += '<div class="ds-accuracy-label">Hit Rate</div>';
    html += '<div class="ds-meta-sm">'+(a.total_measured||0)+' measured</div></div>';
    var arCls = (a.acceptance_rate||0)>=50?'up':((a.acceptance_rate||0)>=25?'flat':'down');
    html += '<div class="ds-accuracy-card"><div class="ds-accuracy-num '+arCls+'">'+(a.acceptance_rate!=null?Number(a.acceptance_rate).toFixed(1)+'%':'&mdash;')+'</div>';
    html += '<div class="ds-accuracy-label">Accept Rate</div>';
    html += '<div class="ds-meta-sm">'+(a.proposals_executed||0)+' / '+(a.proposals_total||0)+'</div></div>';
    var srCls = (a.agent_success_rate||0)>=90?'up':((a.agent_success_rate||0)>=70?'flat':'down');
    html += '<div class="ds-accuracy-card"><div class="ds-accuracy-num '+srCls+'">'+(a.agent_success_rate!=null?Number(a.agent_success_rate).toFixed(1)+'%':'&mdash;')+'</div>';
    html += '<div class="ds-accuracy-label">Agent Success</div>';
    html += '<div class="ds-meta-sm">'+(a.agent_runs_completed||0)+' / '+(a.agent_runs_total||0)+'</div></div>';
    html += '</div>';
    if(a.by_window){
      html += '<table class="ds-data-table"><thead><tr><th>Window</th><th>Total</th><th>Correct</th><th>Hit Rate</th><th>Avg Return</th></tr></thead><tbody>';
      var windows = Object.keys(a.by_window);
      for(var w=0; w<windows.length; w++){
        var wk = windows[w], s = a.by_window[wk];
        html += '<tr><td>'+esc(wk)+'</td><td>'+s.total+'</td><td>'+s.correct+'</td>';
        html += '<td class="'+(s.hit_rate>=60?'up':(s.hit_rate>=40?'flat':'down'))+'">'+Number(s.hit_rate).toFixed(1)+'%</td>';
        html += '<td class="'+(s.avg_return_pct>=0?'up':'down')+'">'+(s.avg_return_pct>=0?'+':'')+Number(s.avg_return_pct).toFixed(1)+'%</td></tr>';
      }
      html += '</tbody></table>';
    }
  }
  html += '</div>';
  return html;
}

/* ── 52-Week Lows ── */
function buildLowsPanel(data){
  var rp = data.report_panels || {};
  var both = rp.lows_both || [];
  var w52 = rp.lows_52 || [];
  var html = '<div class="ds-ai-panel" data-ds-ai-panel="lows" hidden>';
  if(!both.length && !w52.length){
    html += '<div class="ds-empty-state">No low-lists found</div>';
  } else {
    html += '<div class="ds-feed-scroll ds-lows-feed">';
    for(var i=0; i<both.length; i++){
      var r = both[i];
      html += '<div class="ds-low-row">';
      html += '<div class="ds-low-left">';
      html += '<span class="ds-tk-link" data-ds-ticker="'+esc(r.ticker)+'">'+esc(r.ticker)+'</span>';
      if(r.is_at_low) html += '<span class="ds-atlow-dot"></span>';
      html += '<span class="ds-meta-sm">'+esc(r.company||'-')+'</span>';
      html += '</div>';
      html += '<span class="ds-low-badge all-time">ALL TIME LOW</span>';
      html += '<span class="ds-meta-sm">'+esc(r.above_low)+'</span>';
      html += '</div>';
    }
    for(var j=0; j<w52.length; j++){
      var s = w52[j];
      html += '<div class="ds-low-row">';
      html += '<div class="ds-low-left">';
      html += '<span class="ds-tk-link" data-ds-ticker="'+esc(s.ticker)+'">'+esc(s.ticker)+'</span>';
      if(s.is_at_low) html += '<span class="ds-atlow-dot"></span>';
      html += '<span class="ds-meta-sm">'+esc(s.company||'-')+'</span>';
      html += '</div>';
      html += '<span class="ds-low-badge year">YEAR LOW</span>';
      html += '<span class="ds-meta-sm">'+esc(s.above_low)+'</span>';
      html += '</div>';
    }
    html += '</div>';
  }
  html += '</div>';
  return html;
}

/* ── Audit Log ── */
function buildAuditPanel(data){
  var timeline = data.ai_audit_timeline || [];
  var html = '<div class="ds-ai-panel" data-ds-ai-panel="audit" hidden>';
  if(!timeline.length){
    html += '<div class="ds-empty-state">No audit events yet</div>';
  } else {
    html += '<div class="ds-feed-scroll">';
    for(var i=0; i<timeline.length; i++){
      var ev = timeline[i];
      var k = ev.kind || '';
      html += '<div class="ds-audit-row">';
      html += '<span class="ds-audit-time">'+esc((ev.ts||'').substring(11,16))+'</span>';
      var typeCls = k==='proposal_executed'?'up':(k.indexOf('reject')>=0?'down':'');
      html += '<span class="ds-meta-tag '+typeCls+'">'+esc(k.replace(/_/g,' '))+'</span>';
      html += '<span class="ds-audit-label">'+esc(ev.label||'-')+'</span>';
      html += '</div>';
    }
    html += '</div>';
  }
  html += '</div>';
  return html;
}

/* ── Health ── */
function buildHealthPanel(data){
  var di = data.data_integrity || {};
  var prof = di.profile || {};
  var iq = di.interview_queue || {};
  var ar = di.agent_runs_7d || {};
  var html = '<div class="ds-ai-panel" data-ds-ai-panel="health" hidden>';
  html += '<div class="ds-accuracy-grid">';
  var pCls = (prof.pct||0)>=80?'up':((prof.pct||0)>=40?'flat':'down');
  html += '<div class="ds-accuracy-card"><div class="ds-accuracy-num '+pCls+'">'+(prof.pct||0)+'%</div>';
  html += '<div class="ds-accuracy-label">Profile</div>';
  html += '<div class="ds-meta-sm">'+(prof.filled||0)+' / '+(prof.total||0)+' keys</div></div>';
  html += '<div class="ds-accuracy-card"><div class="ds-accuracy-num '+((iq.open||0)>0?'flat':'up')+'">'+(iq.open||0)+'</div>';
  html += '<div class="ds-accuracy-label">Queue Open</div>';
  html += '<div class="ds-meta-sm">'+(iq.done||0)+' done</div></div>';
  html += '<div class="ds-accuracy-card"><div class="ds-accuracy-num up">'+(ar.success||0)+'</div>';
  html += '<div class="ds-accuracy-label">Agent Runs OK</div>';
  html += '<div class="ds-meta-sm">'+(ar.error||0)+' errors</div></div>';
  html += '</div></div>';
  return html;
}

/* ─── Row 3b: News Wire ────────────────────────────────────────────── */
function buildNewsWire(data){
  var home = data.home || {};
  var news = (home.news_general || []).concat(home.news_company || []).slice(0,10);
  var html = '<article class="ds-col-card ds-col-news">';
  html += '<div class="ds-col-head">';
  html += '<h3 class="ds-col-title"><span class="ds-col-icon">&#x1F4F0;</span> Live News</h3>';
  html += '<div class="ds-live-indicator"><span class="ds-live-dot"></span><span>LIVE</span></div>';
  html += '</div>';
  html += '<div class="ds-col-body">';
  if(news.length){
    for(var i=0; i<news.length; i++){
      var n = news[i];
      var src = String(n.source||'News');
      var ago = timeAgo(n.published_at||n.ts||'');
      // Simple sentiment: keywords
      var title = String(n.title||'').toLowerCase();
      var sent = 'neutral';
      if(/surge|rally|jump|soar|beat|gain|record high|upgrade/.test(title)) sent = 'positive';
      else if(/crash|plunge|drop|fall|miss|cut|downgrade|fear|risk|warn/.test(title)) sent = 'negative';
      var newsLink = n.link || n.url || '';
      html += '<div class="ds-news-item ds-news-'+sent+'"'+(newsLink?' data-ds-news-url="'+esc(newsLink)+'"':'')+' style="cursor:'+(newsLink?'pointer':'default')+'">';
      html += '<div class="ds-news-header">';
      html += '<span class="ds-news-source">'+esc(src)+'</span>';
      if(ago) html += '<span class="ds-news-ago">'+ago+'</span>';
      html += '</div>';
      html += '<div class="ds-news-text">';
      if(n.ticker) html += '<span class="ds-news-ticker" data-ds-ticker="'+esc(n.ticker)+'">'+esc(n.ticker)+'</span> ';
      html += esc(n.title);
      html += '</div></div>';
    }
  } else {
    html += '<div class="ds-empty-state">No headlines available</div>';
  }
  html += '</div></article>';
  return html;
}

/* ─── Row 3c: Earnings Calendar ────────────────────────────────────── */
function buildEarningsCalendar(data){
  var rp = data.report_panels || {};
  var rows = rp.earnings_week || [];
  var html = '<article class="ds-col-card ds-col-earnings">';
  html += '<div class="ds-col-head">';
  html += '<h3 class="ds-col-title"><span class="ds-col-icon">&#x1F4C5;</span> Earnings</h3>';
  html += '<span class="ds-col-meta">'+(rp.earnings_upcoming_count||0)+' upcoming</span>';
  html += '</div>';

  html += '<div class="ds-col-tabs">';
  var filters = ['all','reported','upcoming','beat','miss'];
  for(var f=0; f<filters.length; f++){
    html += '<button class="ds-tab-pill'+(f===0?' is-active':'')+'" type="button" data-ds-earn-filter="'+filters[f]+'">'+filters[f].charAt(0).toUpperCase()+filters[f].slice(1)+'</button>';
  }
  html += '</div>';

  html += '<div class="ds-col-body ds-feed-scroll earnings-feed">';
  var lastDate = '';
  for(var i=0; i<rows.length; i++){
    var r = rows[i];
    var d = r.date || '-';
    if(d !== lastDate){ lastDate = d; html += '<div class="ds-earn-date">'+esc(d)+'</div>'; }
    var tl = (r.time||'').toLowerCase();
    var tclass = tl.indexOf('pre')>=0?'pre':(tl.indexOf('after')>=0||tl.indexOf('post')>=0?'post':'day');
    var ttxt = tclass==='pre'?'Pre':(tclass==='post'?'Post':'Day');
    var rep = r.reported==='1'?'reported':'upcoming';
    var v = (r.verdict||'').toUpperCase();
    var verdictKey = v==='BEAT'?'beat':(r.reported==='1'?'miss':'-');

    html += '<div class="ds-earn-row" data-ds-earn-kind="'+rep+'" data-ds-earn-result="'+verdictKey+'">';
    html += '<span class="ds-earn-time '+tclass+'">'+ttxt+'</span>';
    html += '<div class="ds-earn-ident">';
    // Ticker avatar (first letter circle)
    var sym = String(r.symbol||'?');
    html += '<div class="ds-earn-avatar">'+esc(sym.charAt(0))+'</div>';
    html += '<div>';
    html += '<span class="ds-tk-link" data-ds-ticker="'+esc(r.symbol)+'">'+esc(r.symbol)+'</span>';
    html += '<span class="ds-earn-company">'+esc(r.company||r.name||'-')+'</span>';
    html += '</div></div>';
    html += '<div class="ds-earn-results">';
    if(r.reported==='1'){
      if(v==='BEAT') html += '<span class="ds-result-badge beat">BEAT '+esc(r.surprise_txt||'')+'</span>';
      else if(v==='MISS') html += '<span class="ds-result-badge miss">MISS '+esc(r.surprise_txt||'')+'</span>';
      else html += '<span class="ds-result-badge reported">Reported</span>';
    }
    html += '</div>';
    html += '<div class="ds-earn-status">';
    html += '<span class="ds-result-badge '+rep+'">'+rep.charAt(0).toUpperCase()+rep.slice(1)+'</span>';
    html += '</div></div>';
  }
  if(!rows.length) html += '<div class="ds-empty-state">No upcoming earnings</div>';
  html += '</div></article>';
  return html;
}

/* ─── Row 4: Sectors Heatmap ──────────────────────────────────────── */
function buildSectorsGrid(data){
  var m = (data.market && data.market.items) || {};
  var sectors = [
    {key:'xlk',label:'Tech'},{key:'xlf',label:'Financials'},{key:'xle',label:'Energy'},
    {key:'xlv',label:'Healthcare'},{key:'xlc',label:'Comms'},{key:'xli',label:'Industrials'},
    {key:'xlb',label:'Materials'},{key:'xlre',label:'Real Estate'},{key:'xlu',label:'Utilities'},
    {key:'xlp',label:'Staples'},{key:'xly',label:'Discret.'}
  ];
  var hasAny = false;
  for(var i=0;i<sectors.length;i++){ if(m[sectors[i].key] && m[sectors[i].key].price !== '-') hasAny=true; }
  if(!hasAny) return '';

  var html = '<section class="ds-sectors-section">';
  var mktS = data.market || {};
  var lhS = Number(mktS.live_hits||0);
  var freshClsS = lhS > 0 ? 'fresh' : 'stale';
  html += '<div class="ds-mkt-header"><h3 class="ds-mkt-title"><span class="ds-col-icon">&#x1F3AF;</span> Sector Performance</h3>';
  html += '<div class="ds-mkt-freshness"><span class="ds-fresh-dot ds-fresh-'+freshClsS+'"></span>';
  if(mktS.as_of) html += '<span class="ds-fresh-time">'+esc(mktS.as_of)+'</span>';
  html += '</div></div>';
  html += '<div class="ds-sectors-grid">';
  for(var i=0;i<sectors.length;i++){
    var s = sectors[i];
    var row = m[s.key] || {};
    if(!row.price || row.price === '-') continue;
    var ds = String(row.day || '-');
    var dn = parseFloat(ds.replace('%',''));
    var dir = dn > 0 ? 'up' : (dn < 0 ? 'down' : 'flat');
    // Intensity: stronger color for bigger moves
    var intensity = Math.min(Math.abs(dn) / 3, 1); // cap at 3%
    html += '<div class="ds-sector-tile ds-sector-'+dir+'" data-ds-mkt-key="'+s.key+'" style="--intensity:'+intensity.toFixed(2)+'">';
    html += '<div class="ds-sector-name">'+esc(s.label)+'</div>';
    html += '<div class="ds-sector-chg '+dir+'">'+esc(ds)+'</div>';
    html += '<div class="ds-sector-price">'+esc(row.price)+'</div>';
    html += '<div class="ds-spark-panel ds-spark-sector" id="ds-spark-'+s.key+'" hidden>';
    html += '<div class="ds-spark-periods">';
    html += '<button class="ds-spark-btn is-active" data-period="1y">1Y</button>';
    html += '<button class="ds-spark-btn" data-period="3mo">3M</button>';
    html += '<button class="ds-spark-btn" data-period="6mo">6M</button>';
    html += '<button class="ds-spark-btn" data-period="2y">2Y</button>';
    html += '<button class="ds-spark-btn" data-period="5y">5Y</button>';
    html += '</div>';
    html += '<canvas class="ds-spark-canvas" width="320" height="80"></canvas>';
    html += '<div class="ds-spark-meta"></div>';
    html += '</div>';
    html += '</div>';
  }
  html += '</div></section>';
  return html;
}

/* ─── Row 5: Market Data Grid ──────────────────────────────────────── */
function buildMarketDataGrid(data){
  var m = (data.market && data.market.items) || {};
  var mkt = data.market || {};
  var asOf = mkt.as_of || '';
  var liveHits = Number(mkt.live_hits||0);
  var totalKeys = Object.keys(m).length;
  // Freshness: green if >50% live, amber if some, red if none
  var freshCls = liveHits > totalKeys*0.5 ? 'fresh' : (liveHits > 0 ? 'aging' : 'stale');
  var freshLabel = liveHits > 0 ? liveHits+'/'+totalKeys+' live' : 'cached';
  var html = '<section class="ds-mkt-section">';
  html += '<div class="ds-mkt-header"><h3 class="ds-mkt-title">Market Data</h3>';
  html += '<div class="ds-mkt-freshness">';
  html += '<span class="ds-fresh-dot ds-fresh-'+freshCls+'"></span>';
  html += '<span class="ds-fresh-label">'+esc(freshLabel)+'</span>';
  if(asOf) html += '<span class="ds-fresh-time">'+esc(asOf)+'</span>';
  html += '</div></div>';
  html += '<div class="ds-mkt-three">';

  html += '<div class="ds-mkt-card ds-mkt-rates">';
  html += '<div class="ds-mkt-card-head"><span class="ds-mkt-card-icon">&#x1F4C9;</span><span>Rates & Risk</span></div>';
  html += buildMktItems(m,
    ['us2y','us5y','us10y','us30y','vix','hyg','lqd','tip','bndx','emb'],
    ['US 2Y','US 5Y','US 10Y','US 30Y','VIX','High Yield','Inv Grade','TIPS (Infl)','Intl Bonds','EM Bonds'],
    true);
  html += '</div>';

  html += '<div class="ds-mkt-card ds-mkt-commodities">';
  html += '<div class="ds-mkt-card-head"><span class="ds-mkt-card-icon">&#x1F6E2;</span><span>Commodities</span></div>';
  var commKeys = ['crude','brent','natgas','gold','silver','copper','platinum','palladium','aluminum','wheat','corn','soybeans','coffee','cocoa','cotton','sugar','lumber','ironore','heatoil','gasoline'];
  var commLabels = ['Crude Oil','Brent','Nat Gas','Gold','Silver','Copper','Platinum','Palladium','Aluminum','Wheat','Corn','Soybeans','Coffee','Cocoa','Cotton','Sugar','Lumber','Iron Ore','Heating Oil','Gasoline'];
  html += buildMktItems(m, commKeys, commLabels, true);
  html += '</div>';

  html += '<div class="ds-mkt-card ds-mkt-currencies">';
  html += '<div class="ds-mkt-card-head"><span class="ds-mkt-card-icon">&#x1F4B1;</span><span>Currencies</span></div>';
  html += buildMktItems(m, ['dxy','eurusd','eurgbp','usdjpy','gbpusd','usdcnh'], ['USD Index','EUR/USD','EUR/GBP','USD/JPY','GBP/USD','USD/CNH']);
  html += '</div>';

  html += '</div></section>';
  return html;
}

function buildMktItems(m, keys, labels, skipMissing){
  var html = '<div class="ds-mkt-items">';
  for(var i=0; i<keys.length; i++){
    var row = m[keys[i]] || {};
    if(skipMissing && (!row.price || row.price === '-')) continue;
    html += '<div class="ds-mkt-row" data-ds-mkt-key="'+keys[i]+'">';
    html += '<span class="ds-mkt-name">'+esc(labels[i])+'</span>';
    html += '<span class="ds-mkt-price">'+esc(row.price||'-')+'</span>';
    html += dayPillHtml(row.day, 'sm');
    html += '<span class="ds-spark-toggle" title="Show chart">&#x25B6;</span>';
    html += '</div>';
    html += '<div class="ds-spark-panel" id="ds-spark-'+keys[i]+'" hidden>';
    html += '<div class="ds-spark-periods">';
    html += '<button class="ds-spark-btn is-active" data-period="1y">1Y</button>';
    html += '<button class="ds-spark-btn" data-period="3mo">3M</button>';
    html += '<button class="ds-spark-btn" data-period="6mo">6M</button>';
    html += '<button class="ds-spark-btn" data-period="2y">2Y</button>';
    html += '<button class="ds-spark-btn" data-period="5y">5Y</button>';
    html += '</div>';
    html += '<canvas class="ds-spark-canvas" width="320" height="80"></canvas>';
    html += '<div class="ds-spark-meta"></div>';
    html += '</div>';
  }
  html += '</div>';
  return html;
}

/* ─── Row 5: Cross-Asset Relationships ─────────────────────────────── */
function buildCrossAsset(data){
  var ca = data.cross_asset || {};
  var rows = ca.rows || [];
  if(!rows.length) return '';
  var html = '<section class="ds-cross-section">';
  html += '<div class="ds-mkt-header">';
  html += '<h3 class="ds-mkt-title"><span class="ds-col-icon">&#x1F517;</span> Cross-Asset Relationships</h3>';
  html += '<span class="ds-col-meta">'+rows.length+' pairs tracked</span>';
  html += '</div>';
  html += '<div class="ds-cross-grid">';
  var top = rows.slice(0, 12);
  for(var i=0; i<top.length; i++){
    var r = top[i];
    var gap = Number(r.gap_1d_pct||0);
    var gapCls = Math.abs(gap) >= 0.3 ? (gap > 0 ? 'up' : 'down') : 'flat';
    html += '<div class="ds-cross-card">';
    html += '<div class="ds-cross-pair">';
    html += '<span class="ds-cross-driver">'+esc(r.driver)+'</span>';
    html += '<span class="ds-cascade-arrow">&rarr;</span>';
    html += '<span class="ds-cross-target">'+esc(r.target)+'</span>';
    html += '</div>';
    html += '<div class="ds-cross-stats">';
    html += '<div class="ds-cross-stat"><span class="ds-cross-stat-label">Corr 20d</span><span class="ds-cross-stat-val">'+Number(r.corr20||0).toFixed(2)+'</span></div>';
    html += '<div class="ds-cross-stat"><span class="ds-cross-stat-label">Beta 60d</span><span class="ds-cross-stat-val">'+Number(r.beta60||0).toFixed(2)+'</span></div>';
    html += '<div class="ds-cross-stat"><span class="ds-cross-stat-label">Gap</span><span class="ds-cross-stat-val '+gapCls+'">'+fmtPct(gap)+'</span></div>';
    html += '</div></div>';
  }
  html += '</div></section>';
  return html;
}

/* ─── Sparkline drawing (pure canvas, no lib) ────────────────────────── */
function drawSparkline(canvas, points, color){
  if(!canvas || !points || !points.length) return;
  var ctx = canvas.getContext('2d');
  var w = canvas.width = canvas.offsetWidth * (window.devicePixelRatio||1);
  var h = canvas.height = canvas.offsetHeight * (window.devicePixelRatio||1);
  ctx.clearRect(0,0,w,h);
  var vals = points.map(function(p){ return p.c; });
  var mn = Math.min.apply(null,vals), mx = Math.max.apply(null,vals);
  var range = mx - mn || 1;
  var pad = 4;
  // Gradient fill
  var grad = ctx.createLinearGradient(0,0,0,h);
  grad.addColorStop(0, color.replace(')',',0.15)').replace('rgb','rgba'));
  grad.addColorStop(1, color.replace(')',',0.01)').replace('rgb','rgba'));
  ctx.beginPath();
  for(var i=0;i<vals.length;i++){
    var x = pad + (i/(vals.length-1))*(w-2*pad);
    var y = h - pad - ((vals[i]-mn)/range)*(h-2*pad);
    if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  }
  // Fill under
  ctx.lineTo(pad+(vals.length-1)/(vals.length-1)*(w-2*pad), h);
  ctx.lineTo(pad, h);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();
  // Line
  ctx.beginPath();
  for(var i=0;i<vals.length;i++){
    var x = pad + (i/(vals.length-1))*(w-2*pad);
    var y = h - pad - ((vals[i]-mn)/range)*(h-2*pad);
    if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  }
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5 * (window.devicePixelRatio||1);
  ctx.stroke();
  // End dot
  var lastX = w - pad, lastY = h - pad - ((vals[vals.length-1]-mn)/range)*(h-2*pad);
  ctx.beginPath();
  ctx.arc(lastX, lastY, 3*(window.devicePixelRatio||1), 0, Math.PI*2);
  ctx.fillStyle = color;
  ctx.fill();
}

function loadSparkline(key, period){
  var panel = document.getElementById('ds-spark-'+key);
  if(!panel) return;
  var canvas = panel.querySelector('.ds-spark-canvas');
  var meta = panel.querySelector('.ds-spark-meta');
  if(meta) meta.textContent = 'Loading...';
  fetch('/api/market/history/'+key+'?period='+(period||'1y'))
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(!d.ok || !d.data || !d.data.length){
        if(meta) meta.textContent = 'No data available';
        return;
      }
      var pts = d.data;
      var first = pts[0].c, last = pts[pts.length-1].c;
      var chg = ((last - first)/first*100);
      var color = chg >= 0 ? 'rgb(16,185,129)' : 'rgb(239,68,68)';
      drawSparkline(canvas, pts, color);
      var hi = Math.max.apply(null, pts.map(function(p){return p.c;}));
      var lo = Math.min.apply(null, pts.map(function(p){return p.c;}));
      if(meta) meta.innerHTML = '<span>'+pts[0].d+' &rarr; '+pts[pts.length-1].d+'</span>'
        +'<span class="'+(chg>=0?'up':'down')+'">'+(chg>=0?'+':'')+chg.toFixed(2)+'%</span>'
        +'<span>H: '+hi.toFixed(2)+' &middot; L: '+lo.toFixed(2)+'</span>';
    })
    .catch(function(){ if(meta) meta.textContent = 'Failed to load'; });
}

/* ─── Init — event listeners + auto-refresh ─────────────────────────── */
function init(data){
  var root = document.getElementById('dsDashboardRoot');
  if(!root) return;

  // Auto-refresh market data every 60s
  setInterval(function(){
    fetch('/api/dashboard/snapshot').then(function(r){ return r.json(); }).then(function(d){
      if(!d.ok) return;
      // Update ribbon prices with animation
      var m = (d.market && d.market.items) || {};
      root.querySelectorAll('.ds-ribbon-item').forEach(function(el){
        var label = (el.querySelector('.ds-ribbon-label')||{}).textContent||'';
        var key = {
          'S&P 500':'sp500','NASDAQ':'nasdaq','DOW':'dow','RUSSELL':'russell2000',
          'VIX':'vix','10Y':'us10y','OIL':'crude','GOLD':'gold','EUR/USD':'eurusd'
        }[label];
        if(!key) return;
        var row = m[key]||{};
        var priceEl = el.querySelector('.ds-ribbon-price');
        var chgEl = el.querySelector('.ds-ribbon-chg');
        if(priceEl && row.price && priceEl.textContent !== String(row.price)){
          priceEl.textContent = row.price;
          priceEl.classList.add('ds-value-flash');
          setTimeout(function(){ priceEl.classList.remove('ds-value-flash'); }, 600);
        }
        if(chgEl && row.day){
          var dn = parseFloat(String(row.day).replace('%',''));
          chgEl.textContent = row.day;
          chgEl.className = 'ds-ribbon-chg '+(dn>0?'up':(dn<0?'down':'flat'));
          // Update big mover class
          el.classList.toggle('ds-big-mover', Math.abs(dn)>=3);
          el.classList.toggle('up', dn>0 && Math.abs(dn)>=3);
          el.classList.toggle('down', dn<0 && Math.abs(dn)>=3);
        }
      });
      // Update header metrics
      var rpEl = root.querySelector('.ds-header-right');
      if(rpEl && d.relative_perf !== undefined){
        var metrics = rpEl.querySelectorAll('.ds-header-metric-val');
        if(metrics[0]){
          metrics[0].textContent = fmtPct(d.relative_perf);
          metrics[0].className = 'ds-header-metric-val '+(d.relative_perf>=0?'up':'down');
        }
        if(metrics[1]){
          metrics[1].textContent = fmtPct(d.portfolio_day_pct);
          metrics[1].className = 'ds-header-metric-val '+(d.portfolio_day_pct>=0?'up':'down');
        }
        if(metrics[2]){
          metrics[2].textContent = fmtPct(d.sp500_day_pct);
          metrics[2].className = 'ds-header-metric-val '+(d.sp500_day_pct>=0?'up':'down');
        }
      }
      // Update freshness indicators
      var dm = d.market || {};
      var dlh = Number(dm.live_hits||0);
      var dtk = Object.keys(dm.items||{}).length;
      var dfCls = dlh > dtk*0.5 ? 'fresh' : (dlh > 0 ? 'aging' : 'stale');
      root.querySelectorAll('.ds-fresh-dot').forEach(function(dot){
        dot.className = 'ds-fresh-dot ds-fresh-'+dfCls;
      });
      root.querySelectorAll('.ds-fresh-label').forEach(function(el){
        el.textContent = dlh > 0 ? dlh+'/'+dtk+' live' : 'cached';
      });
      if(dm.as_of){
        root.querySelectorAll('.ds-fresh-time').forEach(function(el){ el.textContent = dm.as_of; });
      }
    }).catch(function(){});
  }, 60000);

  root.addEventListener('click', function(e){
    // AI Insight tabs
    var tabBtn = e.target.closest('[data-ds-ai-tab]');
    if(tabBtn){
      var name = tabBtn.getAttribute('data-ds-ai-tab');
      root.querySelectorAll('[data-ds-ai-tab]').forEach(function(b){ b.classList.toggle('is-active', b.getAttribute('data-ds-ai-tab')===name); });
      root.querySelectorAll('[data-ds-ai-panel]').forEach(function(p){
        if(p.getAttribute('data-ds-ai-panel')===name) p.removeAttribute('hidden');
        else p.setAttribute('hidden','hidden');
      });
      return;
    }

    // Show all proposals
    var showAll = e.target.closest('[data-ds-show-all]');
    if(showAll){
      root.querySelectorAll('.ds-proposal-overflow').forEach(function(el){ el.removeAttribute('hidden'); });
      showAll.remove();
      return;
    }

    // Show more (expand truncated insights)
    var showMore = e.target.closest('[data-ds-expand]');
    if(showMore){
      var eid = showMore.getAttribute('data-ds-expand');
      var exp = document.getElementById('ds-exp-'+eid);
      if(exp){ exp.hidden = !exp.hidden; showMore.textContent = exp.hidden ? showMore.textContent : 'show less'; }
      return;
    }

    // Earnings filter
    var earnFilter = e.target.closest('[data-ds-earn-filter]');
    if(earnFilter){
      var kind = earnFilter.getAttribute('data-ds-earn-filter');
      root.querySelectorAll('[data-ds-earn-filter]').forEach(function(b){ b.classList.toggle('is-active', b.getAttribute('data-ds-earn-filter')===kind); });
      root.querySelectorAll('.ds-earn-row').forEach(function(row){
        var rKind = row.getAttribute('data-ds-earn-kind')||'';
        var rRes = (row.getAttribute('data-ds-earn-result')||'').toLowerCase();
        var show = kind==='all' || (kind==='reported'&&rKind==='reported') || (kind==='upcoming'&&rKind==='upcoming')
          || (kind==='beat'&&rRes==='beat') || (kind==='miss'&&rRes==='miss');
        row.classList.toggle('is-hidden', !show);
      });
      return;
    }

    // Sparkline toggle (click row or arrow)
    var mktRow = e.target.closest('[data-ds-mkt-key]');
    if(mktRow && !e.target.closest('.ds-spark-btn')){
      var mkey = mktRow.getAttribute('data-ds-mkt-key');
      var sparkPanel = document.getElementById('ds-spark-'+mkey);
      if(sparkPanel){
        var wasHidden = sparkPanel.hidden;
        sparkPanel.hidden = !wasHidden;
        if(wasHidden){
          var activeBtn = sparkPanel.querySelector('.ds-spark-btn.is-active');
          var period = activeBtn ? activeBtn.getAttribute('data-period') : '1y';
          loadSparkline(mkey, period);
        }
      }
      return;
    }

    // Sparkline period button
    var sparkBtn = e.target.closest('.ds-spark-btn');
    if(sparkBtn){
      var sparkPanel2 = sparkBtn.closest('.ds-spark-panel');
      if(sparkPanel2){
        sparkPanel2.querySelectorAll('.ds-spark-btn').forEach(function(b){ b.classList.remove('is-active'); });
        sparkBtn.classList.add('is-active');
        var key2 = sparkPanel2.id.replace('ds-spark-','');
        loadSparkline(key2, sparkBtn.getAttribute('data-period'));
      }
      return;
    }

    // News item click → open source URL
    var newsItem = e.target.closest('[data-ds-news-url]');
    if(newsItem && !e.target.closest('[data-ds-ticker]')){
      var url = newsItem.getAttribute('data-ds-news-url');
      if(url) window.open(url, '_blank', 'noopener');
      return;
    }

    // Ticker clicks
    var tkEl = e.target.closest('[data-ds-ticker]');
    if(tkEl){
      var tk = tkEl.getAttribute('data-ds-ticker');
      if(tk && typeof window.openTicker === 'function') window.openTicker(tk);
      return;
    }

    // Reject toggle
    var rejToggle = e.target.closest('[data-ds-reject-toggle]');
    if(rejToggle){
      var pid = rejToggle.getAttribute('data-ds-reject-toggle');
      var wrap = document.getElementById('ds-reject-'+pid);
      if(wrap){ wrap.hidden = !wrap.hidden; }
      return;
    }

    // Execute proposal
    var execBtn = e.target.closest('[data-ds-execute]');
    if(execBtn){
      var exid = execBtn.getAttribute('data-ds-execute');
      fetch('/dashboard/proposals/'+exid+'/execute', {method:'POST'})
        .then(function(){ if(typeof window.loadDashboard==='function') window.loadDashboard(); });
      return;
    }

    // Reject confirm
    var rejConfirm = e.target.closest('[data-ds-reject-confirm]');
    if(rejConfirm){
      var rid = rejConfirm.getAttribute('data-ds-reject-confirm');
      var reasonInput = root.querySelector('[data-ds-reject-input="'+rid+'"]');
      var reason = reasonInput ? reasonInput.value : '';
      fetch('/dashboard/proposals/'+rid+'/reject', {
        method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'},
        body:'reason='+encodeURIComponent(reason)
      }).then(function(){ if(typeof window.loadDashboard==='function') window.loadDashboard(); });
      return;
    }

    // Dismiss breach
    var dismissBreach = e.target.closest('[data-ds-dismiss-breach]');
    if(dismissBreach){
      var bid = dismissBreach.getAttribute('data-ds-dismiss-breach');
      fetch('/api/agent/breach-alerts/'+bid+'/dismiss', {method:'POST'})
        .then(function(){
          var el = document.getElementById('ds-breach-'+bid);
          if(el) el.remove();
        });
      return;
    }

    // Dismiss cascade
    var dismissCascade = e.target.closest('[data-ds-dismiss-cascade]');
    if(dismissCascade){
      var cid = dismissCascade.getAttribute('data-ds-dismiss-cascade');
      fetch('/api/agent/cascade-alerts/'+cid+'/dismiss', {method:'POST'})
        .then(function(){
          var el = document.getElementById('ds-cascade-'+cid);
          if(el) el.remove();
        });
      return;
    }

    // Refresh insights
    if(e.target.closest('#dsRefreshInsights')){
      fetch('/dashboard/proposals/scan', {method:'POST'})
        .then(function(){ if(typeof window.loadDashboard==='function') window.loadDashboard(); });
      return;
    }
  });
}

/* ─── Export ─────────────────────────────────────────────────────────── */
window.DashboardView = { build: build, init: init };
})();
