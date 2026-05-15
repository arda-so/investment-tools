/* ══════════════════════════════════════════════════════════════════════════════
   Universe Renderer — Client-side rendering for inline portfolio/watchlist/bluechips
   ══════════════════════════════════════════════════════════════════════════════
   Usage:
     UniverseView.build(data, initialTab) → HTML string
     UniverseView.init(data)              → attach event listeners
══════════════════════════════════════════════════════════════════════════════ */
(function(){
"use strict";
window._UV_VERSION = '2026-03-27-v3';

var _data = {};
var _tab = 'portfolio';
var _editMode = false;
var _wlView = 'cards';   // cards | table | compact
var _bcView = 'cards';   // cards | table | compact
var _acctFilter = 'all'; // 'all' or account name
var _acctMgmtOpen = false; // track if account management panel is open

function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function fmtMoney(v){
  var n = Number(v);
  if(!isFinite(n) || n === 0) return '-';
  if(n >= 1e12) return '$'+(n/1e12).toFixed(1)+'T';
  if(n >= 1e9) return '$'+(n/1e9).toFixed(1)+'B';
  if(n >= 1e6) return '$'+(n/1e6).toFixed(1)+'M';
  if(n >= 1e3) return '$'+(n/1e3).toFixed(1)+'K';
  return '$'+n.toFixed(0);
}
function fmtPx(v){
  var n = Number(v);
  if(!isFinite(n) || n === 0) return '-';
  return '$'+n.toFixed(2);
}
function fmtPct(v){
  var n = Number(v);
  if(!isFinite(n)) return '-';
  return (n >= 0 ? '+' : '') + n.toFixed(1) + '%';
}
function dayPillCls(v){ return Number(v||0) >= 0 ? 'up' : 'down'; }

function _post(url, body, cb){
  var fd = new FormData();
  Object.keys(body).forEach(function(k){ fd.append(k, body[k]); });
  fetch(url, {method:'POST', body:fd})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(cb) cb(d);
      if(d.ok) _refreshData('_post:' + url);
      else _toast(d.message || 'Error', true);
    })
    .catch(function(e){ _toast('Request failed: '+e.message, true); });
}

function _postNoRefresh(url, body, cb){
  var fd = new FormData();
  Object.keys(body).forEach(function(k){ fd.append(k, body[k]); });
  fetch(url, {method:'POST', body:fd})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(cb) cb(d);
      if(!d.ok) _toast(d.message || 'Error', true);
    })
    .catch(function(e){ _toast('Request failed: '+e.message, true); });
}

function _refreshData(source){
  fetch('/api/universe/full')
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(!d.ok) return;
      _data = d;
      // Update the parent space cache so loadUniverse doesn't re-render on top of us
      if(window._spaceCache) { window._spaceCache.universe = d; }
      if(window._spaceCacheAge) { window._spaceCacheAge.universe = Date.now(); }
      var content = document.getElementById('uvTabContent');
      if(content) content.innerHTML = renderTabContent(_tab);
      _updateSummary();
      _updateTabCounts();
      // Re-open account management panel if it was open
      if(_acctMgmtOpen){
        var panel = document.getElementById('uvAcctMgmtPanel');
        if(panel) panel.style.display = '';
      }
    });
}

function _updateSummary(){
  var el = document.getElementById('uvSummaryStrip');
  if(el){
    var metrics = _data.metrics || {};
    var allRows = metrics.portfolio_rows || [];
    var cash = _data.cash_rows || [];
    var accounts = _acctOptions();
    if(_acctFilter !== 'all'){
      var filtered = allRows.filter(function(r){ return (r.account||'Main') === _acctFilter; });
      var fMetrics = _computeFilteredMetrics(filtered, cash);
      el.innerHTML = buildSummaryStripInner(fMetrics, filtered, cash);
    } else if(accounts.length > 1){
      // Consolidated: show unique ticker count
      var consolidated = _consolidateRows(allRows);
      el.innerHTML = buildSummaryStripInner(metrics, consolidated, cash);
    } else {
      el.innerHTML = buildSummaryStripInner(metrics, allRows, cash);
    }
  }
}

function _computeFilteredMetrics(rows, cash){
  var stockVal = 0, dayPnl = 0, prevVal = 0;
  for(var i=0; i<rows.length; i++){
    var v = Number(rows[i].value||0);
    stockVal += v;
    var dp = Number(rows[i].day_pct||0);
    var pv = Math.abs(1 + dp/100) > 1e-9 ? v / (1 + dp/100) : v;
    prevVal += pv;
    dayPnl += v - pv;
  }
  var totalCash = 0;
  (cash||[]).forEach(function(r){ totalCash += Number(r.amount||0); });
  var aum = stockVal + totalCash;
  var aumDayPct = prevVal > 0 ? ((stockVal - prevVal) / prevVal * 100) : 0;
  return {
    aum_usd: aum,
    stock_value_usd: stockVal,
    aum_day_pnl_usd: dayPnl,
    aum_day_pct: aumDayPct,
    portfolio_rows: rows
  };
}

function _consolidateRows(allRows){
  var merged = {};
  var order = [];
  for(var i=0; i<allRows.length; i++){
    var r = allRows[i];
    var tk = r.ticker;
    if(!merged[tk]){
      merged[tk] = {ticker:tk, name:r.name||'', shares:0, value:0, weight_pct:0, price:Number(r.price||0), day_pct:r.day_pct, thesis:r.thesis||'', _totalCostBasis:0};
      order.push(tk);
    }
    var m = merged[tk];
    var sh = Number(r.shares||0);
    m.shares += sh;
    m._totalCostBasis += sh * Number(r.cost||0);
    m.value += Number(r.value||0);
    m.weight_pct += Number(r.weight_pct||0);
  }
  var out = [];
  for(var j=0; j<order.length; j++){
    var cm = merged[order[j]];
    cm.cost = cm.shares > 0 ? cm._totalCostBasis / cm.shares : 0;
    out.push(cm);
  }
  return out;
}

function _updateTabCounts(){
  var counts = {
    portfolio: ((_data.metrics||{}).portfolio_rows||[]).length,
    watchlist: (_data.watchlist_full||[]).length,
    bluechips: (_data.bluechips_full||[]).length,
    cash: (_data.cash_rows||[]).length
  };
  var root = document.getElementById('uvRoot');
  if(!root) return;
  root.querySelectorAll('[data-uv-tab]').forEach(function(btn){
    var tid = btn.getAttribute('data-uv-tab');
    if(counts[tid] != null){
      var label = btn.textContent.replace(/\s*\(\d+\)\s*$/, '');
      btn.textContent = label + ' (' + counts[tid] + ')';
    }
  });
}

function _toast(msg, isErr){
  var t = document.createElement('div');
  t.className = 'uv-toast' + (isErr ? ' error' : '');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(function(){ t.classList.add('show'); }, 10);
  setTimeout(function(){ t.classList.remove('show'); setTimeout(function(){ t.remove(); }, 300); }, 3000);
}

/* ─── Build full HTML ───────────────────────────────────────────────── */
function build(data, initialTab){
  _data = data || {};
  _tab = initialTab || 'portfolio';

  var metrics = _data.metrics || {};
  var portfolio = metrics.portfolio_rows || [];
  var watchlist = _data.watchlist_full || [];
  var bluechips = _data.bluechips_full || [];
  var cash = _data.cash_rows || [];

  var html = '<div class="uv-root" id="uvRoot"><div class="uv-shell">';

  // Summary strip
  html += '<div id="uvSummaryStrip">' + buildSummaryStripInner(metrics, portfolio, cash) + '</div>';

  // Toolbar
  html += '<div class="uv-toolbar">';
  html += '<button class="uv-toolbar-btn" id="uvEditToggle" style="' + (_tab === 'portfolio' || _tab === 'timeline' ? 'display:none;' : '') + '">' + (_editMode ? 'Done' : 'Edit') + '</button>';
  html += '</div>';

  // Tabs
  html += '<div class="uv-tabs">';
  var tabs = [
    {id:'portfolio', label:'Portfolio', count:portfolio.length},
    {id:'watchlist', label:'Watchlist', count:watchlist.length},
    {id:'bluechips', label:'Blue Chips', count:bluechips.length},
    {id:'cash', label:'Cash', count:cash.length},
    {id:'timeline', label:'Timeline'}
  ];
  for(var i=0; i<tabs.length; i++){
    var t = tabs[i];
    var active = t.id === _tab ? ' active' : '';
    html += '<button class="uv-tab-btn'+active+'" data-uv-tab="'+t.id+'">'+t.label;
    if(t.count != null) html += ' ('+t.count+')';
    html += '</button>';
  }
  html += '</div>';

  // Tab content
  html += '<div id="uvTabContent">';
  html += renderTabContent(_tab);
  html += '</div>';

  html += '</div></div>';
  return html;
}

function buildSummaryStripInner(metrics, portfolio, cash){
  var aum = Number(metrics.aum_usd||0);
  var stockVal = Number(metrics.stock_value_usd||0);
  var dayPnl = Number(metrics.aum_day_pnl_usd||0);
  var dayPct = Number(metrics.aum_day_pct||0);
  var totalCash = 0;
  (cash||[]).forEach(function(r){ totalCash += Number(r.amount||0); });

  var html = '<div class="uv-summary">';
  html += '<div class="uv-stat"><div class="uv-stat-label">AUM</div><div class="uv-stat-val hero">'+fmtMoney(aum)+'</div></div>';
  html += '<div class="uv-stat"><div class="uv-stat-label">Stock Value</div><div class="uv-stat-val">'+fmtMoney(stockVal)+'</div></div>';
  html += '<div class="uv-stat"><div class="uv-stat-label">Day P&L</div>';
  html += '<div class="uv-stat-val">'+fmtMoney(Math.abs(dayPnl))+'</div>';
  html += '<div class="uv-stat-sub '+dayPillCls(dayPct)+'">'+fmtPct(dayPct)+'</div></div>';
  html += '<div class="uv-stat"><div class="uv-stat-label">Positions</div><div class="uv-stat-val">'+(portfolio||[]).length+'</div></div>';
  if(totalCash > 0){
    html += '<div class="uv-stat"><div class="uv-stat-label">Cash</div><div class="uv-stat-val">'+fmtMoney(totalCash)+'</div></div>';
  }
  html += '</div>';
  return html;
}

function renderTabContent(tab){
  if(tab === 'portfolio') return renderPortfolio();
  if(tab === 'watchlist') return renderWatchlist();
  if(tab === 'bluechips') return renderBluechips();
  if(tab === 'cash') return renderCash();
  if(tab === 'timeline') return renderTimeline();
  return '';
}

/* ─── Portfolio Table + Forms ─────────────────────────────────────────── */
function _acctColorMap(){
  var map = {};
  var accts = _data.portfolio_accounts || [];
  for(var i=0; i<accts.length; i++) map[accts[i].account_name] = accts[i].color || '#6366f1';
  if(!map['Main']) map['Main'] = '#6366f1';
  return map;
}

function _acctOptions(){
  var accts = _data.portfolio_accounts || [];
  if(!accts.length) return [{account_name:'Main', color:'#6366f1'}];
  return accts;
}

function renderPortfolio(){
  var allRows = (_data.metrics || {}).portfolio_rows || [];
  var accounts = _acctOptions();
  var colorMap = _acctColorMap();

  // Account filter pills
  var html = '<div class="uv-acct-filters">';
  html += '<button class="uv-acct-pill'+(_acctFilter==='all'?' active':'')+'" data-uv-acct-filter="all" style="--pill-color:#94a3b8">All</button>';
  for(var ai=0; ai<accounts.length; ai++){
    var a = accounts[ai];
    var isActive = _acctFilter === a.account_name;
    html += '<button class="uv-acct-pill'+(isActive?' active':'')+'" data-uv-acct-filter="'+esc(a.account_name)+'" style="--pill-color:'+esc(a.color||'#6366f1')+'">'+esc(a.account_name)+'</button>';
  }
  html += '<button class="uv-acct-pill uv-acct-manage" data-uv-action="manage-accounts" style="--pill-color:#64748b">⚙</button>';
  html += '</div>';

  // Filter rows — and consolidate same-ticker rows when showing All
  var rows;
  var isConsolidated = false;
  if(_acctFilter === 'all' && accounts.length > 1){
    // Consolidate: merge rows with same ticker across accounts
    var merged = {};
    var mergeOrder = [];
    for(var mi=0; mi<allRows.length; mi++){
      var mr = allRows[mi];
      var mTk = mr.ticker;
      if(!merged[mTk]){
        merged[mTk] = {
          ticker: mTk, name: mr.name||'', shares: 0, cost: 0, price: Number(mr.price||0),
          day_pct: mr.day_pct, value: 0, weight_pct: 0, thesis: mr.thesis||'',
          _totalCostBasis: 0, _accounts: []
        };
        mergeOrder.push(mTk);
      }
      var m = merged[mTk];
      var mSh = Number(mr.shares||0);
      var mCo = Number(mr.cost||mr.avg_cost||0);
      m.shares += mSh;
      m._totalCostBasis += mSh * mCo;
      m.value += Number(mr.value||0);
      m.weight_pct += Number(mr.weight_pct||0);
      m._accounts.push({account: mr.account||'Main', shares: mSh, cost: mCo});
    }
    rows = [];
    for(var oi=0; oi<mergeOrder.length; oi++){
      var cm = merged[mergeOrder[oi]];
      cm.cost = cm.shares > 0 ? cm._totalCostBasis / cm.shares : 0;
      rows.push(cm);
    }
    isConsolidated = true;
  } else if(_acctFilter !== 'all'){
    rows = allRows.filter(function(r){ return (r.account||'Main') === _acctFilter; });
  } else {
    rows = allRows;
  }

  // Section header
  html += '<div class="uv-section-header"><h3 class="uv-section-title">Portfolio Holdings <span class="uv-section-badge">'+rows.length+'</span></h3>';
  html += '<button class="uv-action-btn" id="uvPortfolioAddBtn">+ Add Position</button></div>';

  // Add position form (hidden by default)
  html += '<div class="uv-form-panel" id="uvPortfolioAddForm" style="display:none;">';
  html += '<div class="uv-form-title">Add / Buy Position</div>';
  html += '<div class="uv-form-row">';
  html += '<input class="uv-input" id="uvPfTicker" placeholder="Ticker (e.g. AAPL)" autocomplete="off">';
  html += '<input class="uv-input" id="uvPfShares" placeholder="Shares" type="number" step="any">';
  html += '<input class="uv-input" id="uvPfCost" placeholder="Avg Cost ($)" type="number" step="any">';
  html += '</div>';
  html += '<div class="uv-form-row">';
  html += '<select class="uv-input" id="uvPfAccount" style="max-width:160px;">';
  for(var fi=0; fi<accounts.length; fi++){
    var selAcct = accounts[fi].account_name;
    var selDef = (_acctFilter !== 'all' && _acctFilter === selAcct) ? ' selected' : (fi===0 && _acctFilter==='all' ? ' selected' : '');
    html += '<option value="'+esc(selAcct)+'"'+selDef+'>'+esc(selAcct)+'</option>';
  }
  html += '</select>';
  html += '<input class="uv-input uv-input-wide" id="uvPfNote" placeholder="Note / thesis (optional)">';
  html += '</div>';
  html += '<div class="uv-form-actions">';
  html += '<button class="uv-btn uv-btn-primary" data-uv-action="portfolio-buy">Buy / Add</button>';
  html += '<button class="uv-btn uv-btn-ghost" data-uv-action="portfolio-add-cancel">Cancel</button>';
  html += '</div></div>';

  if(!rows.length) return html + '<div class="uv-card-empty">No portfolio holdings'+ (_acctFilter!=='all' ? ' in '+_acctFilter : '') +'. Click "+ Add Position" above.</div>';

  var hasMultiAcct = accounts.length > 1;
  html += '<div class="uv-table-wrap"><table class="uv-table"><thead><tr>';
  html += '<th>Ticker</th>';
  if(isConsolidated) html += '<th>Accounts</th>';
  html += '<th>Shares</th><th>Avg Cost</th><th>Price</th><th>Day</th><th>Value</th><th>Weight</th><th>Thesis</th>';
  if(!isConsolidated) html += '<th></th>';
  html += '</tr></thead><tbody>';

  for(var i=0; i<rows.length; i++){
    var r = rows[i];
    var dayC = dayPillCls(r.day_pct);
    var thesis = r.thesis || '';
    var thSnip = thesis ? esc(thesis.substring(0,50))+(thesis.length>50?'...':'') : '';
    html += '<tr>';
    html += '<td><span class="uv-tk" data-uv-ticker="'+esc(r.ticker)+'">'+esc(r.ticker)+'</span>';
    html += '<div class="uv-name">'+esc(r.name||'')+'</div></td>';
    if(isConsolidated){
      // Show colored account labels for each account this ticker is in
      html += '<td>';
      var accts = r._accounts || [{account: r.account||'Main'}];
      for(var aj=0; aj<accts.length; aj++){
        var aName = accts[aj].account;
        var aCol = colorMap[aName] || '#6366f1';
        html += '<span class="uv-acct-label" style="--acct-color:'+esc(aCol)+';margin-right:4px;" data-uv-acct-filter="'+esc(aName)+'">'+esc(aName)+'</span>';
      }
      html += '</td>';
    }
    html += '<td>'+(r.shares||'-')+'</td>';
    html += '<td>'+(r.cost>0?fmtPx(r.cost):'-')+'</td>';
    html += '<td>'+(r.price>0?fmtPx(r.price):'-')+'</td>';
    html += '<td><span class="uv-day-pill '+dayC+'">'+fmtPct(r.day_pct||0)+'</span></td>';
    html += '<td>'+(r.value>0?fmtMoney(r.value):'-')+'</td>';
    html += '<td>';
    html += '<div class="uv-weight-val">'+(r.weight_pct||0).toFixed(1)+'%</div>';
    html += '<div class="uv-weight-bar"><div class="uv-weight-fill" style="width:'+Math.min(100,r.weight_pct||0)+'%"></div></div>';
    html += '</td>';
    html += '<td class="uv-ticker-card-thesis" title="'+esc(thesis)+'">';
    html += thSnip || '<span class="uv-tk" data-uv-ticker="'+esc(r.ticker)+'" style="font-size:11px;color:var(--uv-accent);">Write thesis</span>';
    html += '</td>';
    if(!isConsolidated){
      var rAcct = r.account || 'Main';
      html += '<td class="uv-actions-cell">';
      html += '<button class="uv-btn-sm" data-uv-edit="'+esc(r.ticker)+'" data-uv-edit-acct="'+esc(rAcct)+'" data-uv-shares="'+(r.shares||0)+'" data-uv-cost="'+(r.avg_cost||r.cost||0)+'" style="border-color:var(--uv-accent,#6366f1);color:var(--uv-accent,#6366f1);">✎ Edit</button>';
      html += '<button class="uv-btn-sm uv-btn-sell" data-uv-sell="'+esc(r.ticker)+'" data-uv-sell-acct="'+esc(rAcct)+'" data-uv-shares="'+(r.shares||0)+'">Sell</button>';
      html += '<button class="uv-btn-sm uv-btn-danger" data-uv-exit="'+esc(r.ticker)+'" data-uv-exit-acct="'+esc(rAcct)+'">Exit</button>';
      html += '</td>';
    }
    html += '</tr>';
  }
  html += '</tbody></table></div>';

  // Sell form (hidden, shown per ticker)
  html += '<div class="uv-form-panel" id="uvSellForm" style="display:none;">';
  html += '<div class="uv-form-title">Sell / Reduce Position — <span id="uvSellTicker"></span></div>';
  html += '<div class="uv-form-row">';
  html += '<input class="uv-input" id="uvSellShares" placeholder="Shares to sell" type="number" step="any">';
  html += '<input class="uv-input" id="uvSellPrice" placeholder="Sell price ($)" type="number" step="any">';
  html += '</div>';
  html += '<div class="uv-form-row">';
  html += '<select class="uv-input" id="uvSellReason">';
  html += '<option value="">-- Reason --</option>';
  html += '<option value="thesis_broken">Thesis Broken</option>';
  html += '<option value="target_reached">Target Reached</option>';
  html += '<option value="better_opportunity">Better Opportunity</option>';
  html += '<option value="risk_management">Risk Management</option>';
  html += '<option value="rebalance">Rebalance</option>';
  html += '<option value="other">Other</option>';
  html += '</select>';
  html += '<input class="uv-input uv-input-wide" id="uvSellNote" placeholder="Note (optional)">';
  html += '</div>';
  html += '<div class="uv-form-actions">';
  html += '<button class="uv-btn uv-btn-warn" data-uv-action="portfolio-sell-confirm">Sell</button>';
  html += '<button class="uv-btn uv-btn-ghost" data-uv-action="sell-cancel">Cancel</button>';
  html += '</div></div>';

  // Edit form (hidden, shown per ticker)
  html += '<div class="uv-form-panel" id="uvEditForm" style="display:none;">';
  html += '<div class="uv-form-title">Edit Position — <span id="uvEditTicker"></span></div>';
  html += '<div class="uv-form-row">';
  html += '<input class="uv-input" id="uvEditShares" placeholder="Total shares" type="number" step="any">';
  html += '<input class="uv-input" id="uvEditCost" placeholder="Avg cost ($)" type="number" step="any">';
  html += '</div>';
  html += '<div class="uv-form-actions">';
  html += '<button class="uv-btn" style="background:var(--uv-accent,#6366f1);color:#fff;" data-uv-action="portfolio-edit-confirm">Save</button>';
  html += '<button class="uv-btn uv-btn-ghost" data-uv-action="edit-cancel">Cancel</button>';
  html += '</div></div>';

  // Account management panel (hidden)
  html += '<div class="uv-form-panel uv-acct-panel" id="uvAcctMgmtPanel" style="display:none;">';
  html += '<div class="uv-form-title">Manage Accounts</div>';
  html += '<div class="uv-acct-list">';
  for(var mi=0; mi<accounts.length; mi++){
    var ma = accounts[mi];
    var maColor = ma.color||'#6366f1';
    var maId = 'uvAcct_'+mi;
    html += '<div class="uv-acct-card">';
    // Top row: color dot + name + actions
    html += '<div class="uv-acct-card-head">';
    html += '<span class="uv-acct-dot" style="background:'+esc(maColor)+'"></span>';
    // Inline editable name
    html += '<input class="uv-acct-name-input" id="'+maId+'_name" value="'+esc(ma.account_name)+'" data-uv-acct-orig="'+esc(ma.account_name)+'" spellcheck="false">';
    html += '<button class="uv-acct-save-btn" data-uv-save-acct-name="'+esc(ma.account_name)+'" data-uv-name-input="'+maId+'_name" style="display:none;">Save</button>';
    if(ma.account_name !== 'Main'){
      html += '<button class="uv-btn-sm uv-btn-danger uv-acct-del" data-uv-delete-acct="'+esc(ma.account_name)+'">Delete</button>';
    }
    html += '</div>';
    // Color row: compact swatches
    html += '<div class="uv-acct-color-row">';
    var pickerColors = ['#6366f1','#8b5cf6','#3b82f6','#06b6d4','#10b981','#f59e0b','#ef4444','#ec4899','#f97316','#14b8a6'];
    for(var ci=0; ci<pickerColors.length; ci++){
      var isC = pickerColors[ci] === maColor;
      html += '<button class="uv-acct-color-dot'+(isC?' selected':'')+'" data-uv-acct-color="'+pickerColors[ci]+'" data-uv-acct-color-name="'+esc(ma.account_name)+'" style="background:'+pickerColors[ci]+'"></button>';
    }
    html += '</div>';
    html += '</div>';
  }
  html += '</div>';
  // New account row
  html += '<div class="uv-acct-new-row">';
  html += '<input class="uv-input" id="uvNewAcctName" placeholder="New account name...">';
  html += '<button class="uv-btn uv-btn-primary" data-uv-action="create-account">+ Create</button>';
  html += '</div>';
  html += '<div class="uv-form-actions">';
  html += '<button class="uv-btn uv-btn-ghost" data-uv-action="close-acct-mgmt">Done</button>';
  html += '</div></div>';

  return html;
}

/* ─── Category colors (Modern SaaS palette) ──────────────────────────── */
var _CAT_COLORS = [
  '#6366f1', '#8b5cf6', '#3b82f6', '#06b6d4', '#10b981',
  '#f59e0b', '#ef4444', '#ec4899', '#f97316', '#14b8a6',
  '#6d28d9', '#0ea5e9', '#84cc16', '#e11d48', '#5e6c84'
];
/* Gradient pairs for group headers */
var _CAT_GRADIENTS = {
  '#6366f1':['#6366f1','#818cf8'], '#8b5cf6':['#8b5cf6','#a78bfa'],
  '#3b82f6':['#3b82f6','#60a5fa'], '#06b6d4':['#06b6d4','#22d3ee'],
  '#10b981':['#10b981','#34d399'], '#f59e0b':['#f59e0b','#fbbf24'],
  '#ef4444':['#ef4444','#f87171'], '#ec4899':['#ec4899','#f472b6'],
  '#f97316':['#f97316','#fb923c'], '#14b8a6':['#14b8a6','#2dd4bf'],
  '#6d28d9':['#6d28d9','#8b5cf6'], '#0ea5e9':['#0ea5e9','#38bdf8'],
  '#84cc16':['#84cc16','#a3e635'], '#e11d48':['#e11d48','#fb7185'],
  '#5e6c84':['#5e6c84','#94a3b8']
};
function _catGradient(color){
  var pair = _CAT_GRADIENTS[color];
  if(pair) return 'linear-gradient(135deg,'+pair[0]+','+pair[1]+')';
  return 'linear-gradient(135deg,'+color+','+color+')';
}
var _CAT_EMOJIS = ['📈','📉','🇺🇸','🇪🇺','🇬🇧','🇨🇳','🇯🇵','🇰🇷','🇩🇪','🇫🇷','🇮🇳','🇧🇷','🇹🇷','🏦','💊','⚡','🛢️','🏗️','🚗','✈️','🛒','💻','📱','🎮','🤖','☁️','🔬','💎','🌱','🏠','🎯','⭐','🔥','💰','🏆','🌍','🛡️','📊','🧬','🍎'];
function _catColor(cat){
  // Use stored color from group settings if available
  var gs = (_data.watchlist_group_settings || {})[cat];
  if(gs && gs.color) return gs.color;
  if(!cat || cat === 'General') return '#5e6c84';
  var h = 0;
  for(var i=0; i<cat.length; i++) h = ((h << 5) - h + cat.charCodeAt(i)) | 0;
  return _CAT_COLORS[Math.abs(h) % _CAT_COLORS.length];
}
function _catEmoji(cat){
  var gs = (_data.watchlist_group_settings || {})[cat];
  return (gs && gs.emoji) ? gs.emoji : '';
}

/* ─── Get unique categories from data ─────────────────────────────────── */
function _getCategories(){
  var cats = [];
  var groups = _data.watchlist_grouped || [];
  for(var i=0; i<groups.length; i++){
    cats.push(groups[i].category || 'General');
  }
  if(!cats.length) cats.push('General');
  return cats;
}

/* ─── Watchlist Cards + Forms (Grouped by Category) ───────────────────── */
/* ─── View toggle buttons ─────────────────────────────────────────── */
function _viewToggle(current, prefix){
  var views = [
    {key:'cards',   icon:'⊞', label:'Cards'},
    {key:'table',   icon:'☰', label:'Table'},
    {key:'compact', icon:'≡', label:'Compact'}
  ];
  var h = '<div class="uv-view-toggle">';
  for(var i=0; i<views.length; i++){
    var v = views[i];
    var act = v.key === current ? ' active' : '';
    h += '<button class="uv-view-btn'+act+'" data-uv-view="'+v.key+'" data-uv-view-prefix="'+prefix+'" title="'+v.label+'">'+v.icon+'</button>';
  }
  h += '</div>';
  return h;
}

/* ─── Watchlist — Table view ─────────────────────────────────────── */
function _renderWlTable(gItems, color, cat){
  var thesisMap = _data.thesis_map || {};
  var categories = _getCategories();
  var h = '<div class="uv-view-table-wrap"><table class="uv-view-table"><thead><tr>';
  h += '<th style="width:44px"></th><th>Ticker</th><th>Name</th><th class="r">Price</th>';
  h += '<th class="r">Day</th><th class="r">Since Added</th><th class="r">MCap</th>';
  if(_editMode) h += '<th>Actions</th>';
  h += '</tr></thead><tbody>';
  for(var i=0; i<gItems.length; i++){
    var w = gItems[i];
    var tk = w.ticker || '';
    var dayC = dayPillCls(w.day_pct_safe);
    var saC = w.since_added_pct != null ? (Number(w.since_added_pct||0)>=0?'up':'down') : '';
    h += '<tr class="uv-view-table-row">';
    h += '<td><img class="uv-view-table-logo" src="https://financialmodelingprep.com/image-stock/'+esc(tk)+'.png" alt="" onerror="this.style.display=\'none\'"></td>';
    h += '<td><span class="uv-tk" data-uv-ticker="'+esc(tk)+'">'+esc(tk)+'</span></td>';
    h += '<td class="uv-view-table-name">'+esc(w.name||'')+'</td>';
    h += '<td class="r">'+fmtPx(w.price_now)+'</td>';
    h += '<td class="r"><span class="uv-day-pill '+dayC+'">'+fmtPct(w.day_pct_safe||0)+'</span></td>';
    h += '<td class="r">'+(w.since_added_pct != null ? '<span class="'+saC+'">'+fmtPct(w.since_added_pct)+'</span>':'-')+'</td>';
    h += '<td class="r">'+fmtMoney(w.market_cap)+'</td>';
    if(_editMode){
      h += '<td class="uv-actions-cell">';
      h += '<button class="uv-btn-sm uv-btn-primary" data-uv-promote-wl="'+esc(tk)+'">Buy</button>';
      h += '<select class="uv-btn-sm uv-wl-move-select" data-uv-move-wl="'+esc(tk)+'" data-uv-current-cat="'+esc(cat)+'">';
      h += '<option value="">Move…</option>';
      for(var mc=0; mc<categories.length; mc++){
        if(categories[mc] !== cat) h += '<option value="'+esc(categories[mc])+'">'+esc(categories[mc])+'</option>';
      }
      h += '</select>';
      h += '<button class="uv-btn-sm uv-btn-danger" data-uv-remove-wl="'+esc(tk)+'">✕</button>';
      h += '</td>';
    }
    h += '</tr>';
  }
  h += '</tbody></table></div>';
  return h;
}

/* ─── Watchlist — Compact view ───────────────────────────────────── */
function _renderWlCompact(gItems, color, cat){
  var categories = _getCategories();
  var h = '<div class="uv-view-compact">';
  for(var i=0; i<gItems.length; i++){
    var w = gItems[i];
    var tk = w.ticker || '';
    var dayC = dayPillCls(w.day_pct_safe);
    h += '<div class="uv-compact-row">';
    h += '<img class="uv-compact-logo" src="https://financialmodelingprep.com/image-stock/'+esc(tk)+'.png" alt="" onerror="this.style.display=\'none\'">';
    h += '<span class="uv-tk uv-compact-tk" data-uv-ticker="'+esc(tk)+'">'+esc(tk)+'</span>';
    h += '<span class="uv-compact-name">'+esc(w.name||'')+'</span>';
    h += '<span class="uv-compact-spacer"></span>';
    h += '<span class="uv-compact-px">'+fmtPx(w.price_now)+'</span>';
    h += '<span class="uv-day-pill '+dayC+' uv-compact-day">'+fmtPct(w.day_pct_safe||0)+'</span>';
    if(w.market_cap) h += '<span class="uv-compact-mcap">'+fmtMoney(w.market_cap)+'</span>';
    if(_editMode){
      h += '<button class="uv-btn-sm uv-btn-danger" data-uv-remove-wl="'+esc(tk)+'" style="margin-left:4px">✕</button>';
    }
    h += '</div>';
  }
  h += '</div>';
  return h;
}

/* ─── Blue Chips — Table view ────────────────────────────────────── */
function _renderBcTable(items){
  var h = '<div class="uv-view-table-wrap"><table class="uv-view-table"><thead><tr>';
  h += '<th style="width:44px"></th><th>Ticker</th><th>Name</th><th class="r">Price</th>';
  h += '<th class="r">Day</th><th class="r">Since Added</th><th class="r">MCap</th>';
  if(_editMode) h += '<th></th>';
  h += '</tr></thead><tbody>';
  for(var i=0; i<items.length; i++){
    var b = items[i];
    var dayC = dayPillCls(b.day_pct_safe);
    var saC = b.since_added_pct != null ? (Number(b.since_added_pct||0)>=0?'up':'down') : '';
    h += '<tr class="uv-view-table-row">';
    h += '<td><img class="uv-view-table-logo" src="https://financialmodelingprep.com/image-stock/'+esc(b.ticker)+'.png" alt="" onerror="this.style.display=\'none\'"></td>';
    h += '<td><span class="uv-tk" data-uv-ticker="'+esc(b.ticker)+'">'+esc(b.ticker)+'</span></td>';
    h += '<td class="uv-view-table-name">'+esc(b.name||'')+'</td>';
    h += '<td class="r">'+fmtPx(b.price_now)+'</td>';
    h += '<td class="r"><span class="uv-day-pill '+dayC+'">'+fmtPct(b.day_pct_safe||0)+'</span></td>';
    h += '<td class="r">'+(b.since_added_pct != null ? '<span class="'+saC+'">'+fmtPct(b.since_added_pct)+'</span>':'-')+'</td>';
    h += '<td class="r">'+fmtMoney(b.market_cap)+'</td>';
    if(_editMode){
      h += '<td><button class="uv-btn-sm uv-btn-danger" data-uv-remove-bc="'+esc(b.ticker)+'">✕</button></td>';
    }
    h += '</tr>';
  }
  h += '</tbody></table></div>';
  return h;
}

/* ─── Blue Chips — Compact view ──────────────────────────────────── */
function _renderBcCompact(items){
  var h = '<div class="uv-view-compact">';
  for(var i=0; i<items.length; i++){
    var b = items[i];
    var dayC = dayPillCls(b.day_pct_safe);
    h += '<div class="uv-compact-row">';
    h += '<img class="uv-compact-logo" src="https://financialmodelingprep.com/image-stock/'+esc(b.ticker)+'.png" alt="" onerror="this.style.display=\'none\'">';
    h += '<span class="uv-tk uv-compact-tk" data-uv-ticker="'+esc(b.ticker)+'">'+esc(b.ticker)+'</span>';
    h += '<span class="uv-compact-name">'+esc(b.name||'')+'</span>';
    h += '<span class="uv-compact-spacer"></span>';
    h += '<span class="uv-compact-px">'+fmtPx(b.price_now)+'</span>';
    h += '<span class="uv-day-pill '+dayC+' uv-compact-day">'+fmtPct(b.day_pct_safe||0)+'</span>';
    if(b.market_cap) h += '<span class="uv-compact-mcap">'+fmtMoney(b.market_cap)+'</span>';
    if(_editMode){
      h += '<button class="uv-btn-sm uv-btn-danger" data-uv-remove-bc="'+esc(b.ticker)+'" style="margin-left:4px">✕</button>';
    }
    h += '</div>';
  }
  h += '</div>';
  return h;
}

function renderWatchlist(){
  var items = _data.watchlist_full || [];
  var groups = _data.watchlist_grouped || [];
  var thesisMap = _data.thesis_map || {};
  var categories = _getCategories();

  // If no groups but items exist, create a single General group
  if(!groups.length && items.length){
    groups = [{category:'General', items:items}];
  }

  var html = '<div class="uv-section-header"><h3 class="uv-section-title">Watchlist <span class="uv-section-badge">'+items.length+'</span></h3>';
  html += '<div style="display:flex;gap:8px;align-items:center;">';
  html += _viewToggle(_wlView, 'wl');
  html += '<button class="uv-action-btn uv-action-btn-secondary" id="uvWlNewGroupBtn">+ New Group</button>';
  html += '<button class="uv-action-btn" id="uvWatchlistAddBtn">+ Add Ticker</button>';
  html += '</div></div>';

  // New group form (hidden) — with inline color + emoji picker
  html += '<div class="uv-form-panel" id="uvWlNewGroupForm" style="display:none;">';
  html += '<div class="uv-form-title">Create New Group</div>';
  html += '<div class="uv-form-row">';
  html += '<input class="uv-input uv-input-wide" id="uvWlNewGroupName" placeholder="Group name (e.g. Chinese Stocks, Energy Watchlist, AI Plays)">';
  html += '</div>';
  html += '<div class="uv-form-row" style="flex-direction:column;gap:8px;">';
  html += '<label style="font-size:11px;font-weight:700;color:var(--muted);">Choose a color</label>';
  html += '<div class="uv-inline-colors" id="uvWlNewGroupColors">';
  for(var ci=0; ci<_CAT_COLORS.length; ci++){
    var sel = ci===0 ? ' selected' : '';
    html += '<button type="button" class="uv-inline-color-swatch'+sel+'" style="background:'+_CAT_COLORS[ci]+'" data-uv-new-color="'+_CAT_COLORS[ci]+'"></button>';
  }
  html += '</div></div>';
  html += '<div class="uv-form-row" style="flex-direction:column;gap:8px;">';
  html += '<label style="font-size:11px;font-weight:700;color:var(--muted);">Choose an emoji (optional)</label>';
  html += '<div class="uv-inline-emoji-picker" id="uvWlNewGroupEmojis">';
  var quickEmojis = ['📈','🇨🇳','🇺🇸','🇪🇺','🇬🇧','🇯🇵','🇰🇷','🇮🇳','🇹🇷','⚡','🛢️','💊','🏦','💻','🤖','☁️','🚗','🌱','🔬','🎯','💎','🔥','🛡️','🏠'];
  for(var ei=0; ei<quickEmojis.length; ei++){
    html += '<button type="button" class="uv-inline-emoji-btn" data-uv-new-emoji="'+quickEmojis[ei]+'">'+quickEmojis[ei]+'</button>';
  }
  html += '</div></div>';
  html += '<div class="uv-form-actions">';
  html += '<button class="uv-btn uv-btn-primary" data-uv-action="wl-new-group-confirm">Create Group</button>';
  html += '<button class="uv-btn uv-btn-ghost" data-uv-action="wl-new-group-cancel">Cancel</button>';
  html += '</div></div>';

  // Add ticker form (hidden)
  html += '<div class="uv-form-panel" id="uvWatchlistAddForm" style="display:none;">';
  html += '<div class="uv-form-title">Add to Watchlist</div>';
  html += '<div class="uv-form-row">';
  html += '<input class="uv-input" id="uvWlTicker" placeholder="Ticker (e.g. TSLA)" autocomplete="off">';
  html += '<select class="uv-input" id="uvWlCategory">';
  for(var ci=0; ci<categories.length; ci++){
    html += '<option value="'+esc(categories[ci])+'">'+esc(categories[ci])+'</option>';
  }
  html += '</select>';
  html += '<input class="uv-input uv-input-wide" id="uvWlReason" placeholder="Why are you watching this?">';
  html += '</div>';
  html += '<div class="uv-form-actions">';
  html += '<button class="uv-btn uv-btn-primary" data-uv-action="watchlist-add-confirm">Add</button>';
  html += '<button class="uv-btn uv-btn-ghost" data-uv-action="watchlist-add-cancel">Cancel</button>';
  html += '</div></div>';

  if(!items.length) return html + '<div class="uv-card-empty">No watchlist tickers yet. Click "+ Add Ticker" above.</div>';

  // Render each group
  for(var g=0; g<groups.length; g++){
    var group = groups[g];
    var cat = group.category || 'General';
    var color = _catColor(cat);
    var gItems = group.items || [];

    var emoji = _catEmoji(cat);

    html += '<div class="uv-wl-group" data-wl-group="'+esc(cat)+'">';
    // Group header — full gradient background
    var gradient = _catGradient(color);
    html += '<div class="uv-wl-group-header" style="background:'+gradient+'">';
    html += '<div class="uv-wl-group-left">';
    html += '<button class="uv-wl-group-toggle" data-uv-toggle-group="'+esc(cat)+'" title="Collapse/Expand">▾</button>';
    // Emoji — always visible, clickable to change
    if(emoji){
      html += '<button class="uv-wl-group-emoji-btn" data-uv-emoji-group="'+esc(cat)+'" title="Change emoji">'+emoji+'</button>';
    } else {
      html += '<button class="uv-wl-group-emoji-btn uv-wl-group-emoji-add" data-uv-emoji-group="'+esc(cat)+'" title="Add emoji">+😀</button>';
    }
    html += '<span class="uv-wl-group-name">'+esc(cat)+'</span>';
    html += '<span class="uv-wl-group-count">'+gItems.length+'</span>';
    html += '</div>';
    html += '<div class="uv-wl-group-actions" style="position:relative;z-index:1;">';
    // Always show color picker button + edit actions
    html += '<button class="uv-wl-group-edit-btn" data-uv-color-group="'+esc(cat)+'" title="Change color">🎨 Color</button>';
    if(cat !== 'General'){
      html += '<button class="uv-wl-group-edit-btn" data-uv-rename-group="'+esc(cat)+'">✏️ Rename</button>';
      html += '<button class="uv-wl-group-edit-btn" data-uv-delete-group="'+esc(cat)+'" style="background:rgba(255,0,0,.2);">🗑️</button>';
    }
    html += '</div>';
    html += '</div>';

    // Group body — render based on selected view mode
    html += '<div class="uv-wl-group-body" id="uvWlGroupBody_'+esc(cat.replace(/\s+/g,'_'))+'">';
    if(!gItems.length){
      html += '<div style="padding:24px 20px;color:var(--muted);font-size:13px;text-align:center;">Empty group — use <b>+ Add Ticker</b> above to add tickers here.</div>';
      html += '</div></div>';
      continue;
    }
    if(_wlView === 'table'){
      html += _renderWlTable(gItems, color, cat);
    } else if(_wlView === 'compact'){
      html += _renderWlCompact(gItems, color, cat);
    } else {
      // Cards view (default)
      html += '<div class="uv-card-grid">';
      for(var i=0; i<gItems.length; i++){
        var w = gItems[i];
        var tk = w.ticker || '';
        var th = thesisMap[tk] || {};
        var dayC = dayPillCls(w.day_pct_safe);
        var thesis = th.thesis || '';
        if(thesis.toLowerCase().indexOf('added to watchlist')>=0 || thesis.toLowerCase().indexOf('added via')>=0) thesis = '';

        html += '<div class="uv-ticker-card" style="border-top-color:'+color+'">';
        html += '<div class="uv-ticker-card-top">';
        html += '<div class="uv-ticker-card-left">';
        html += '<img class="uv-ticker-card-logo" src="https://financialmodelingprep.com/image-stock/'+esc(tk)+'.png" alt="" onerror="this.style.display=\'none\'">';
        html += '<div class="uv-ticker-card-info">';
        html += '<span class="uv-tk" data-uv-ticker="'+esc(tk)+'">'+esc(tk)+'</span>';
        html += '<span class="uv-cname">'+esc(w.name||'')+'</span>';
        html += '</div></div>';
        html += '<div class="uv-ticker-card-price">';
        if(w.price_now) html += '<span class="uv-ticker-card-px">'+fmtPx(w.price_now)+'</span>';
        html += '<span class="uv-ticker-card-day '+dayC+'">'+fmtPct(w.day_pct_safe||0)+'</span>';
        html += '</div></div>';

        html += '<div class="uv-ticker-card-meta">';
        if(w.market_cap) html += '<span><span class="label">MCap</span> <span class="val">'+fmtMoney(w.market_cap)+'</span></span>';
        if(w.since_added_pct != null){
          var saCls = Number(w.since_added_pct||0)>=0?'up':'down';
          html += '<span><span class="label">Since Added</span> <span class="val '+saCls+'">'+fmtPct(w.since_added_pct)+'</span></span>';
        }
        html += '</div>';

        if(thesis){
          html += '<div class="uv-ticker-card-thesis">'+esc(thesis.substring(0,120))+(thesis.length>120?'...':'')+'</div>';
        }
        if(th.target_price){
          html += '<span class="uv-ticker-card-target">Target: '+fmtPx(th.target_price)+'</span>';
        }

        // Edit mode actions
        if(_editMode){
          html += '<div class="uv-card-actions">';
          html += '<button class="uv-btn-sm uv-btn-primary" data-uv-promote-wl="'+esc(tk)+'">Buy → Portfolio</button>';
          // Move to group dropdown
          html += '<select class="uv-btn-sm uv-wl-move-select" data-uv-move-wl="'+esc(tk)+'" data-uv-current-cat="'+esc(cat)+'">';
          html += '<option value="">Move to…</option>';
          for(var mc=0; mc<categories.length; mc++){
            if(categories[mc] !== cat) html += '<option value="'+esc(categories[mc])+'">'+esc(categories[mc])+'</option>';
          }
          html += '</select>';
          html += '<button class="uv-btn-sm uv-btn-danger" data-uv-remove-wl="'+esc(tk)+'">Remove</button>';
          html += '</div>';
        }

        html += '</div>';
      }
      html += '</div>';  // card-grid
    }
    html += '</div>';  // group-body
    html += '</div>';  // group
  }

  return html;
}

/* ─── Blue Chips Cards + Forms ────────────────────────────────────────── */
function renderBluechips(){
  var items = _data.bluechips_full || [];
  var html = '<div class="uv-section-header"><h3 class="uv-section-title">Blue Chips <span class="uv-section-badge">'+items.length+'</span></h3>';
  html += '<div style="display:flex;gap:8px;align-items:center;">';
  html += _viewToggle(_bcView, 'bc');
  html += '<button class="uv-action-btn" id="uvBluechipAddBtn">+ Add Blue Chip</button>';
  if(!items.length) html += '<button class="uv-action-btn uv-action-btn-secondary" data-uv-action="bluechips-seed">Seed Defaults</button>';
  html += '</div></div>';

  // Add form
  html += '<div class="uv-form-panel" id="uvBluechipAddForm" style="display:none;">';
  html += '<div class="uv-form-title">Add Blue Chip</div>';
  html += '<div class="uv-form-row">';
  html += '<input class="uv-input" id="uvBcTicker" placeholder="Ticker (e.g. BRK-B)" autocomplete="off">';
  html += '<input class="uv-input uv-input-wide" id="uvBcReason" placeholder="Reason (optional)">';
  html += '</div>';
  html += '<div class="uv-form-actions">';
  html += '<button class="uv-btn uv-btn-primary" data-uv-action="bluechip-add-confirm">Add</button>';
  html += '<button class="uv-btn uv-btn-ghost" data-uv-action="bluechip-add-cancel">Cancel</button>';
  html += '</div></div>';

  if(!items.length) return html + '<div class="uv-card-empty">No blue chip tickers tracked. Click "Seed Defaults" to add common large-caps.</div>';

  if(_bcView === 'table'){
    html += _renderBcTable(items);
  } else if(_bcView === 'compact'){
    html += _renderBcCompact(items);
  } else {
    // Cards view (default)
    html += '<div class="uv-card-grid">';
    for(var i=0; i<items.length; i++){
      var b = items[i];
      var dayC = dayPillCls(b.day_pct_safe);
      html += '<div class="uv-ticker-card">';
      html += '<div class="uv-ticker-card-top">';
      html += '<div class="uv-ticker-card-left">';
      html += '<img class="uv-ticker-card-logo" src="https://financialmodelingprep.com/image-stock/'+esc(b.ticker)+'.png" alt="" onerror="this.style.display=\'none\'">';
      html += '<div class="uv-ticker-card-info">';
      html += '<span class="uv-tk" data-uv-ticker="'+esc(b.ticker)+'">'+esc(b.ticker)+'</span>';
      html += '<span class="uv-cname">'+esc(b.name||'')+'</span>';
      html += '</div></div>';
      html += '<div class="uv-ticker-card-price">';
      if(b.price_now) html += '<span class="uv-ticker-card-px">'+fmtPx(b.price_now)+'</span>';
      html += '<span class="uv-ticker-card-day '+dayC+'">'+fmtPct(b.day_pct_safe||0)+'</span>';
      html += '</div></div>';

      html += '<div class="uv-ticker-card-meta">';
      if(b.market_cap) html += '<span><span class="label">MCap</span> <span class="val">'+fmtMoney(b.market_cap)+'</span></span>';
      if(b.since_added_pct != null){
        var saCls = Number(b.since_added_pct||0)>=0?'up':'down';
        html += '<span><span class="label">Since Added</span> <span class="val '+saCls+'">'+fmtPct(b.since_added_pct)+'</span></span>';
      }
      html += '</div>';

      if(_editMode){
        html += '<div class="uv-card-actions">';
        html += '<button class="uv-btn-sm uv-btn-danger" data-uv-remove-bc="'+esc(b.ticker)+'">Remove</button>';
        html += '</div>';
      }

      html += '</div>';
    }
    html += '</div>';
  }
  return html;
}

/* ─── Cash + Forms ────────────────────────────────────────────────────── */
function renderCash(){
  var rows = _data.cash_rows || [];
  var html = '<div class="uv-section-header"><h3 class="uv-section-title">Cash Balances <span class="uv-section-badge">'+rows.length+'</span></h3>';
  html += '<button class="uv-action-btn" id="uvCashAddBtn">+ Add Cash</button></div>';

  // Add form
  html += '<div class="uv-form-panel" id="uvCashAddForm" style="display:none;">';
  html += '<div class="uv-form-title">Add / Update Cash Balance</div>';
  html += '<div class="uv-form-row">';
  html += '<select class="uv-input" id="uvCashCcy">';
  html += '<option value="USD">USD</option><option value="EUR">EUR</option><option value="GBP">GBP</option>';
  html += '<option value="CHF">CHF</option><option value="JPY">JPY</option><option value="CAD">CAD</option>';
  html += '<option value="AUD">AUD</option><option value="TRY">TRY</option>';
  html += '</select>';
  html += '<input class="uv-input" id="uvCashAmt" placeholder="Amount" type="number" step="any">';
  html += '</div>';
  html += '<div class="uv-form-actions">';
  html += '<button class="uv-btn uv-btn-primary" data-uv-action="cash-add-confirm">Save</button>';
  html += '<button class="uv-btn uv-btn-ghost" data-uv-action="cash-add-cancel">Cancel</button>';
  html += '</div></div>';

  if(!rows.length) return html + '<div class="uv-card-empty">No cash balances recorded. Click "+ Add Cash" above.</div>';

  html += '<div class="uv-cash-strip">';
  for(var i=0; i<rows.length; i++){
    var r = rows[i];
    html += '<div class="uv-cash-item">';
    html += '<span class="uv-cash-ccy">'+esc(r.currency||'USD')+'</span>';
    html += '<span class="uv-cash-amt">'+fmtMoney(Number(r.amount||0))+'</span>';
    if(_editMode){
      html += '<button class="uv-btn-sm uv-btn-danger uv-cash-rm" data-uv-remove-cash="'+esc(r.currency||'USD')+'">✕</button>';
    }
    html += '</div>';
  }
  html += '</div>';
  return html;
}

/* ─── Timeline ──────────────────────────────────────────────────────── */
var _tlFilter = 'all';
function renderTimeline(){
  var events = _data.timeline_events || [];
  var html = '';

  // Filter tabs
  var kindCounts = {all:0, trade:0, decision:0, filing:0, insider:0, earnings:0, proposal:0};
  events.forEach(function(ev){ kindCounts.all++; var k = ev.kind||'decision'; if(kindCounts[k]!==undefined) kindCounts[k]++; });
  html += '<div class="uv-tl-filters">';
  var filterDefs = [
    {id:'all', label:'All', icon:''},
    {id:'trade', label:'Trades', icon:'💰'},
    {id:'decision', label:'Actions', icon:'📋'},
    {id:'filing', label:'Filings', icon:'🏛️'},
    {id:'insider', label:'Insider', icon:'👤'},
    {id:'earnings', label:'Earnings', icon:'📊'},
    {id:'proposal', label:'AI', icon:'🤖'}
  ];
  filterDefs.forEach(function(f){
    if(f.id !== 'all' && !kindCounts[f.id]) return;
    html += '<button class="uv-tl-filter'+(_tlFilter===f.id?' active':'')+'" data-tl-filter="'+f.id+'">'
      +(f.icon?f.icon+' ':'')+f.label
      +'<span class="uv-tl-filter-count">'+kindCounts[f.id]+'</span></button>';
  });
  html += '</div>';

  // Filter events
  var filtered = _tlFilter === 'all' ? events : events.filter(function(ev){ return ev.kind === _tlFilter; });

  if(!filtered.length) return html + '<div class="uv-card-empty" style="padding:24px;text-align:center;">No events in this category.</div>';

  // Group by date
  var groups = {};
  var dateOrder = [];
  filtered.forEach(function(ev){
    var dt = (ev.ts||ev.created_at||'').substring(0,10);
    if(!groups[dt]){ groups[dt] = []; dateOrder.push(dt); }
    groups[dt].push(ev);
  });

  html += '<div class="uv-tl2">';
  dateOrder.forEach(function(dt){
    // Date header
    var d = new Date(dt+'T12:00:00');
    var today = new Date().toISOString().substring(0,10);
    var yesterday = new Date(Date.now()-86400000).toISOString().substring(0,10);
    var dateLabel = dt === today ? 'Today' : dt === yesterday ? 'Yesterday' : d.toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric',year:'numeric'});
    html += '<div class="uv-tl2-date-head">'+esc(dateLabel)+'</div>';

    groups[dt].forEach(function(ev){
      var label = ev.label || ev.title || ev.text || '';
      if(!label){
        var parts = [];
        if(ev.action) parts.push(ev.action.replace(/_/g,' '));
        if(ev.ticker) parts.push(ev.ticker);
        if(ev.summary) parts.push(ev.summary);
        label = parts.join(' — ') || '-';
      }
      var icon = ev.icon || '📋';
      var color = ev.color || 'blue';
      var time = (ev.ts||'').substring(11,16);

      html += '<div class="uv-tl2-item">'
        +'<div class="uv-tl2-icon uv-tl2-'+esc(color)+'">'+icon+'</div>'
        +'<div class="uv-tl2-body">'
          +'<div class="uv-tl2-label">'+esc(label)+'</div>'
          +(time ? '<div class="uv-tl2-time">'+time+'</div>' : '')
        +'</div>'
      +'</div>';
    });
  });
  html += '</div>';
  return html;
}

/* ─── Init — attach event listeners ─────────────────────────────────── */
function init(data){
  _data = data || _data;
  var root = document.getElementById('uvRoot');
  if(!root) return;

  // Listen for color/emoji picker clicks (popups are on document.body, not inside root)
  document.addEventListener('click', function(e){
    var btn = e.target.closest('button') || e.target;
    var pickColor = btn.getAttribute ? btn.getAttribute('data-uv-pick-color') : null;
    if(pickColor !== null && btn.hasAttribute('data-uv-pick-color')){
      var pickGroup = btn.getAttribute('data-uv-pick-group');
      _post('/api/watchlist-groups/upsert', {group_name:pickGroup, color:pickColor, emoji:_catEmoji(pickGroup)});
      _closePopup();
      return;
    }
    var pickEmoji = btn.getAttribute ? btn.getAttribute('data-uv-pick-emoji') : null;
    if(pickEmoji !== null && btn.hasAttribute('data-uv-pick-emoji')){
      var pickGroup2 = btn.getAttribute('data-uv-pick-group');
      _post('/api/watchlist-groups/upsert', {group_name:pickGroup2, emoji:pickEmoji, color:_catColor(pickGroup2)});
      _closePopup();
      return;
    }
  });

  // Listen for account name input changes (show/hide Save button)
  root.addEventListener('input', function(e){
    var nameInput = e.target.closest('.uv-acct-name-input');
    if(nameInput){
      var orig = nameInput.getAttribute('data-uv-acct-orig') || '';
      var cur = nameInput.value.trim();
      var saveBtn = nameInput.nextElementSibling;
      if(saveBtn && saveBtn.hasAttribute('data-uv-save-acct-name')){
        saveBtn.style.display = (cur && cur !== orig) ? '' : 'none';
      }
    }
  });

  // Allow Enter key to save account name
  root.addEventListener('keydown', function(e){
    if(e.key === 'Enter'){
      var nameInput = e.target.closest('.uv-acct-name-input');
      if(nameInput){
        var saveBtn = nameInput.nextElementSibling;
        if(saveBtn && saveBtn.hasAttribute('data-uv-save-acct-name') && saveBtn.style.display !== 'none'){
          saveBtn.click();
        }
      }
    }
  });

  // Listen for select change (move ticker between groups)
  root.addEventListener('change', function(e){
    var moveSelect = e.target.closest('[data-uv-move-wl]');
    if(moveSelect && moveSelect.value){
      var moveTk = moveSelect.getAttribute('data-uv-move-wl');
      var newCat = moveSelect.value;
      _post('/my_universe/watchlist/set-category', {ticker:moveTk, category:newCat});
      _toast('Moving '+moveTk+' to '+newCat+'...');
      moveSelect.value = '';
    }
  });

  root.addEventListener('click', function(e){
    var btn = e.target.closest('button') || e.target;

    // Tab switching
    var tabBtn = e.target.closest('[data-uv-tab]');
    if(tabBtn){
      var tab = tabBtn.getAttribute('data-uv-tab');
      _tab = tab;
      root.querySelectorAll('[data-uv-tab]').forEach(function(b){ b.classList.toggle('active', b.getAttribute('data-uv-tab')===tab); });
      var content = document.getElementById('uvTabContent');
      if(content) content.innerHTML = renderTabContent(tab);
      var editToggle = document.getElementById('uvEditToggle');
      if(editToggle) editToggle.style.display = (tab === 'portfolio' || tab === 'timeline') ? 'none' : '';
      return;
    }

    // Timeline filter toggle
    var tlFilterBtn = e.target.closest('[data-tl-filter]');
    if(tlFilterBtn){
      _tlFilter = tlFilterBtn.getAttribute('data-tl-filter');
      var content = document.getElementById('uvTabContent');
      if(content) content.innerHTML = renderTimeline();
      return;
    }

    // View toggle (Cards / Table / Compact)
    var viewBtn = e.target.closest('[data-uv-view]');
    if(viewBtn){
      var view = viewBtn.getAttribute('data-uv-view');
      var prefix = viewBtn.getAttribute('data-uv-view-prefix');
      if(prefix === 'wl') _wlView = view;
      else if(prefix === 'bc') _bcView = view;
      var content = document.getElementById('uvTabContent');
      if(content) content.innerHTML = renderTabContent(_tab);
      return;
    }

    // Ticker clicks
    var tkEl = e.target.closest('[data-uv-ticker]');
    if(tkEl && !e.target.closest('button')){
      var tk = tkEl.getAttribute('data-uv-ticker');
      if(tk && typeof window.openTicker === 'function') window.openTicker(tk);
      return;
    }

    // Edit mode toggle (hidden on portfolio tab — portfolio has inline actions)
    if(btn.id === 'uvEditToggle'){
      _editMode = !_editMode;
      btn.textContent = _editMode ? 'Done' : 'Edit';
      var content2 = document.getElementById('uvTabContent');
      if(content2) content2.innerHTML = renderTabContent(_tab);
      return;
    }

    // --- Portfolio forms ---
    if(btn.id === 'uvPortfolioAddBtn'){
      _toggleForm('uvPortfolioAddForm'); return;
    }
    var action = btn.getAttribute('data-uv-action');
    if(action === 'portfolio-add-cancel'){
      _hideForm('uvPortfolioAddForm'); return;
    }
    if(action === 'portfolio-buy'){
      var pfTk = (document.getElementById('uvPfTicker')||{}).value||'';
      var pfSh = (document.getElementById('uvPfShares')||{}).value||'';
      var pfCo = (document.getElementById('uvPfCost')||{}).value||'';
      var pfNo = (document.getElementById('uvPfNote')||{}).value||'';
      var pfAc = (document.getElementById('uvPfAccount')||{}).value||'Main';
      if(!pfTk.trim()){ _toast('Ticker is required',true); return; }
      _post('/my_universe/portfolio/upsert', {ticker:pfTk.trim().toUpperCase(), side:'buy', shares:pfSh, cost:pfCo, note:pfNo, account:pfAc});
      _hideForm('uvPortfolioAddForm');
      _toast('Adding '+pfTk.trim().toUpperCase()+' to '+pfAc+'...');
      return;
    }

    // Sell button → show sell form
    var sellTk = btn.getAttribute('data-uv-sell');
    if(sellTk){
      var sf = document.getElementById('uvSellForm');
      if(sf){ sf.style.display=''; sf.scrollIntoView({behavior:'smooth'}); }
      var stEl = document.getElementById('uvSellTicker');
      if(stEl) stEl.textContent = sellTk;
      sf._ticker = sellTk;
      sf._account = btn.getAttribute('data-uv-sell-acct') || 'Main';
      var maxSh = btn.getAttribute('data-uv-shares')||'';
      var shIn = document.getElementById('uvSellShares');
      if(shIn){ shIn.value = ''; shIn.placeholder = 'Max: '+maxSh; }
      return;
    }
    if(action === 'sell-cancel'){
      _hideForm('uvSellForm'); return;
    }
    if(action === 'portfolio-sell-confirm'){
      var sf2 = document.getElementById('uvSellForm');
      var sTk = sf2 ? sf2._ticker : '';
      var sAc = sf2 ? (sf2._account||'Main') : 'Main';
      var sSh = (document.getElementById('uvSellShares')||{}).value||'';
      var sPr = (document.getElementById('uvSellPrice')||{}).value||'';
      var sRe = (document.getElementById('uvSellReason')||{}).value||'';
      var sNo = (document.getElementById('uvSellNote')||{}).value||'';
      if(!sTk){ _toast('No ticker selected',true); return; }
      _post('/my_universe/portfolio/upsert', {ticker:sTk, side:'sell', shares:sSh, cost:sPr, trade_reason:sRe, note:sNo, account:sAc});
      _hideForm('uvSellForm');
      _toast('Selling '+sTk+'...');
      return;
    }

    // Exit position (full sell)
    var exitTk = btn.getAttribute('data-uv-exit');
    if(exitTk){
      var exitAc = btn.getAttribute('data-uv-exit-acct') || 'Main';
      if(!confirm('Exit entire position in '+exitTk+' ('+exitAc+')?')) return;
      _post('/my_universe/portfolio/upsert', {ticker:exitTk, side:'sell', shares:'0', trade_reason:'exit', account:exitAc});
      _toast('Exiting '+exitTk+'...');
      return;
    }

    // Edit position → show edit form
    var editTk = btn.getAttribute('data-uv-edit');
    if(editTk){
      _hideForm('uvSellForm');
      var ef = document.getElementById('uvEditForm');
      if(ef){ ef.style.display=''; ef.scrollIntoView({behavior:'smooth'}); }
      var etEl = document.getElementById('uvEditTicker');
      if(etEl) etEl.textContent = editTk;
      ef._ticker = editTk;
      ef._account = btn.getAttribute('data-uv-edit-acct') || 'Main';
      var editShIn = document.getElementById('uvEditShares');
      var editCoIn = document.getElementById('uvEditCost');
      if(editShIn) editShIn.value = btn.getAttribute('data-uv-shares')||'';
      if(editCoIn) editCoIn.value = btn.getAttribute('data-uv-cost')||'';
      return;
    }
    if(action === 'edit-cancel'){
      _hideForm('uvEditForm'); return;
    }
    if(action === 'portfolio-edit-confirm'){
      var ef2 = document.getElementById('uvEditForm');
      var eTk = ef2 ? ef2._ticker : '';
      var eAc = ef2 ? (ef2._account||'Main') : 'Main';
      var eSh = (document.getElementById('uvEditShares')||{}).value||'';
      var eCo = (document.getElementById('uvEditCost')||{}).value||'';
      if(!eTk){ _toast('No ticker selected',true); return; }
      if(!eSh || !eCo){ _toast('Shares and avg cost required',true); return; }
      _post('/my_universe/portfolio/upsert', {ticker:eTk, side:'edit', shares:eSh, cost:eCo, account:eAc});
      _hideForm('uvEditForm');
      _toast('Updating '+eTk+'...');
      return;
    }

    // Account filter pill click
    var acctFilterVal = btn.getAttribute('data-uv-acct-filter');
    if(acctFilterVal !== null && btn.hasAttribute('data-uv-acct-filter')){
      _acctFilter = acctFilterVal;
      var content = document.getElementById('uvTabContent');
      if(content) content.innerHTML = renderPortfolio();
      _updateSummary();
      return;
    }

    // Account management
    if(action === 'manage-accounts'){
      _acctMgmtOpen = !_acctMgmtOpen;
      _toggleForm('uvAcctMgmtPanel'); return;
    }
    if(action === 'close-acct-mgmt'){
      _acctMgmtOpen = false;
      _hideForm('uvAcctMgmtPanel'); return;
    }
    if(action === 'create-account'){
      var newName = (document.getElementById('uvNewAcctName')||{}).value||'';
      if(!newName.trim()){ _toast('Account name is required',true); return; }
      _post('/api/portfolio-accounts/upsert', {account_name:newName.trim()});
      _acctMgmtOpen = false;
      _hideForm('uvAcctMgmtPanel');
      _toast('Creating account "'+newName.trim()+'"...');
      return;
    }
    // Account color change — optimistic UI update
    var acctColorVal = btn.getAttribute('data-uv-acct-color');
    if(acctColorVal !== null && btn.hasAttribute('data-uv-acct-color')){
      var acctColorName = btn.getAttribute('data-uv-acct-color-name');
      // Instant UI: update local data + re-render panel only
      var accts = _data.portfolio_accounts || [];
      for(var aci=0; aci<accts.length; aci++){
        if(accts[aci].account_name === acctColorName) accts[aci].color = acctColorVal;
      }
      // Update the card's dot + color swatches without full re-render
      var card = btn.closest('.uv-acct-card');
      if(card){
        var dot = card.querySelector('.uv-acct-dot');
        if(dot) dot.style.background = acctColorVal;
        card.querySelectorAll('.uv-acct-color-dot').forEach(function(d){
          d.classList.toggle('selected', d.getAttribute('data-uv-acct-color') === acctColorVal);
        });
      }
      // Update filter pills instantly
      var pills = document.querySelectorAll('.uv-acct-pill[data-uv-acct-filter="'+acctColorName+'"]');
      pills.forEach(function(p){ p.style.setProperty('--pill-color', acctColorVal); });
      // Save in background (no full refresh)
      var fd = new FormData();
      fd.append('account_name', acctColorName);
      fd.append('color', acctColorVal);
      fetch('/api/portfolio-accounts/upsert', {method:'POST', body:fd}).catch(function(){});
      return;
    }
    // Save renamed account (inline)
    var saveAcctName = btn.getAttribute('data-uv-save-acct-name');
    if(saveAcctName){
      var inputId = btn.getAttribute('data-uv-name-input');
      var inputEl = document.getElementById(inputId);
      if(!inputEl) return;
      var newName = inputEl.value.trim();
      if(!newName || newName === saveAcctName) return;
      _post('/api/portfolio-accounts/rename', {old_name:saveAcctName, new_name:newName});
      if(_acctFilter === saveAcctName) _acctFilter = newName;
      _toast('Renaming to "'+newName+'"...');
      return;
    }
    // Delete account
    var delAcct = btn.getAttribute('data-uv-delete-acct');
    if(delAcct){
      if(!confirm('Delete account "'+delAcct+'"? Positions will be moved to Main.')) return;
      _post('/api/portfolio-accounts/delete', {account_name:delAcct});
      if(_acctFilter === delAcct) _acctFilter = 'all';
      _toast('Deleting "'+delAcct+'"...');
      return;
    }

    // --- Watchlist forms ---
    if(btn.id === 'uvWatchlistAddBtn'){
      _toggleForm('uvWatchlistAddForm'); return;
    }
    if(btn.id === 'uvWlNewGroupBtn'){
      _toggleForm('uvWlNewGroupForm'); return;
    }
    if(action === 'wl-new-group-cancel'){
      _hideForm('uvWlNewGroupForm'); return;
    }
    if(action === 'wl-new-group-confirm'){
      var gName = (document.getElementById('uvWlNewGroupName')||{}).value||'';
      if(!gName.trim()){ _toast('Group name is required',true); return; }
      // Get selected color
      var selectedColor = _CAT_COLORS[0];
      var colorBtns = document.querySelectorAll('#uvWlNewGroupColors .uv-inline-color-swatch.selected');
      if(colorBtns.length) selectedColor = colorBtns[0].getAttribute('data-uv-new-color') || selectedColor;
      // Get selected emoji
      var selectedEmoji = '';
      var emojiBtns = document.querySelectorAll('#uvWlNewGroupEmojis .uv-inline-emoji-btn.selected');
      if(emojiBtns.length) selectedEmoji = emojiBtns[0].getAttribute('data-uv-new-emoji') || '';
      // Create group via API, then refresh to show it.
      var _gNameTrimmed = gName.trim();
      _post('/api/watchlist-groups/upsert', {group_name:_gNameTrimmed, color:selectedColor, emoji:selectedEmoji});
      _hideForm('uvWlNewGroupForm');
      _toast('Group "'+_gNameTrimmed+'" created! Use "+ Add Ticker" to add tickers to it.');
      return;
    }
    // Inline color swatch selection (new group form)
    var newColor = btn.getAttribute('data-uv-new-color');
    if(newColor !== null && btn.hasAttribute('data-uv-new-color')){
      var container = document.getElementById('uvWlNewGroupColors');
      if(container){
        container.querySelectorAll('.uv-inline-color-swatch').forEach(function(s){ s.classList.remove('selected'); });
        btn.classList.add('selected');
      }
      return;
    }
    // Inline emoji selection (new group form)
    var newEmoji = btn.getAttribute('data-uv-new-emoji');
    if(newEmoji !== null && btn.hasAttribute('data-uv-new-emoji')){
      var eContainer = document.getElementById('uvWlNewGroupEmojis');
      if(eContainer){
        var wasSelected = btn.classList.contains('selected');
        eContainer.querySelectorAll('.uv-inline-emoji-btn').forEach(function(s){ s.classList.remove('selected'); });
        if(!wasSelected) btn.classList.add('selected');
      }
      return;
    }
    if(action === 'watchlist-add-cancel'){
      _hideForm('uvWatchlistAddForm'); return;
    }
    if(action === 'watchlist-add-confirm'){
      var wlTk = (document.getElementById('uvWlTicker')||{}).value||'';
      var wlRe = (document.getElementById('uvWlReason')||{}).value||'';
      var wlCat = (document.getElementById('uvWlCategory')||{}).value||'General';
      if(!wlTk.trim()){ _toast('Ticker is required',true); return; }
      _post('/my_universe/watchlist/add', {ticker:wlTk.trim().toUpperCase(), reason:wlRe, category:wlCat});
      _hideForm('uvWatchlistAddForm');
      _toast('Adding '+wlTk.trim().toUpperCase()+' to '+wlCat+'...');
      return;
    }
    // Group collapse/expand
    var toggleGroup = btn.getAttribute('data-uv-toggle-group');
    if(toggleGroup){
      var bodyId = 'uvWlGroupBody_'+toggleGroup.replace(/\s+/g,'_');
      var bodyEl = document.getElementById(bodyId);
      if(bodyEl){
        var collapsed = bodyEl.style.display === 'none';
        bodyEl.style.display = collapsed ? '' : 'none';
        btn.textContent = collapsed ? '▾' : '▸';
      }
      return;
    }
    // Rename group
    var renameGroup = btn.getAttribute('data-uv-rename-group');
    if(renameGroup){
      var newName = prompt('Rename group "'+renameGroup+'" to:', renameGroup);
      if(!newName || !newName.trim() || newName.trim() === renameGroup) return;
      // Update category for all tickers in this group
      var gItems = [];
      var groups = _data.watchlist_grouped || [];
      for(var gi=0; gi<groups.length; gi++){
        if(groups[gi].category === renameGroup) gItems = groups[gi].items || [];
      }
      var pending = gItems.length;
      if(!pending) return;
      for(var ri=0; ri<gItems.length; ri++){
        _post('/my_universe/watchlist/set-category', {ticker:gItems[ri].ticker, category:newName.trim()}, function(){});
      }
      _toast('Renaming group to "'+newName.trim()+'"...');
      return;
    }
    // Delete group
    var deleteGroup = btn.getAttribute('data-uv-delete-group');
    if(deleteGroup){
      if(!confirm('Delete group "'+deleteGroup+'"? Tickers will move to General.')) return;
      _post('/api/watchlist-groups/delete', {group_name:deleteGroup});
      _toast('Deleting group "'+deleteGroup+'"...');
      return;
    }
    // Color picker for group
    var colorGroup = btn.getAttribute('data-uv-color-group');
    if(colorGroup){
      _showColorPicker(btn, colorGroup);
      return;
    }
    // Emoji picker for group
    var emojiGroup = btn.getAttribute('data-uv-emoji-group');
    if(emojiGroup){
      _showEmojiPicker(btn, emojiGroup);
      return;
    }
    // Color/emoji picker item clicks
    var pickColor = btn.getAttribute('data-uv-pick-color');
    if(pickColor){
      var pickGroup = btn.getAttribute('data-uv-pick-group');
      _post('/api/watchlist-groups/upsert', {group_name:pickGroup, color:pickColor, emoji:_catEmoji(pickGroup)});
      _closePopup();
      return;
    }
    var pickEmoji = btn.getAttribute('data-uv-pick-emoji');
    if(pickEmoji){
      var pickGroup2 = btn.getAttribute('data-uv-pick-group');
      _post('/api/watchlist-groups/upsert', {group_name:pickGroup2, emoji:pickEmoji, color:_catColor(pickGroup2)});
      _closePopup();
      return;
    }
    var rmWl = btn.getAttribute('data-uv-remove-wl');
    if(rmWl){
      if(!confirm('Remove '+rmWl+' from watchlist?')) return;
      _post('/my_universe/watchlist/remove', {ticker:rmWl});
      _toast('Removing '+rmWl+'...');
      return;
    }
    // Move ticker to different group
    var moveSelect = e.target.closest('[data-uv-move-wl]');
    if(moveSelect && moveSelect.tagName === 'SELECT' && moveSelect.value){
      var moveTk = moveSelect.getAttribute('data-uv-move-wl');
      var newCat = moveSelect.value;
      _post('/my_universe/watchlist/set-category', {ticker:moveTk, category:newCat});
      _toast('Moving '+moveTk+' to '+newCat+'...');
      moveSelect.value = '';
      return;
    }
    var promoteWl = btn.getAttribute('data-uv-promote-wl');
    if(promoteWl){
      // Show portfolio add form pre-filled
      _tab = 'portfolio';
      root.querySelectorAll('[data-uv-tab]').forEach(function(b){ b.classList.toggle('active', b.getAttribute('data-uv-tab')==='portfolio'); });
      var ct = document.getElementById('uvTabContent');
      if(ct) ct.innerHTML = renderTabContent('portfolio');
      var pf = document.getElementById('uvPortfolioAddForm');
      if(pf) pf.style.display = '';
      var pfIn = document.getElementById('uvPfTicker');
      if(pfIn) pfIn.value = promoteWl;
      return;
    }

    // --- Blue Chips forms ---
    if(btn.id === 'uvBluechipAddBtn'){
      _toggleForm('uvBluechipAddForm'); return;
    }
    if(action === 'bluechip-add-cancel'){
      _hideForm('uvBluechipAddForm'); return;
    }
    if(action === 'bluechip-add-confirm'){
      var bcTk = (document.getElementById('uvBcTicker')||{}).value||'';
      var bcRe = (document.getElementById('uvBcReason')||{}).value||'';
      if(!bcTk.trim()){ _toast('Ticker is required',true); return; }
      _post('/my_universe/bluechips/add', {ticker:bcTk.trim().toUpperCase(), reason:bcRe});
      _hideForm('uvBluechipAddForm');
      _toast('Adding '+bcTk.trim().toUpperCase()+' to blue chips...');
      return;
    }
    if(action === 'bluechips-seed'){
      if(!confirm('Seed blue chips with default large-cap tickers?')) return;
      _post('/my_universe/bluechips/seed', {});
      _toast('Seeding blue chips...');
      return;
    }
    var rmBc = btn.getAttribute('data-uv-remove-bc');
    if(rmBc){
      if(!confirm('Remove '+rmBc+' from blue chips?')) return;
      _post('/my_universe/bluechips/remove', {ticker:rmBc});
      _toast('Removing '+rmBc+'...');
      return;
    }

    // --- Cash forms ---
    if(btn.id === 'uvCashAddBtn'){
      _toggleForm('uvCashAddForm'); return;
    }
    if(action === 'cash-add-cancel'){
      _hideForm('uvCashAddForm'); return;
    }
    if(action === 'cash-add-confirm'){
      var cCcy = (document.getElementById('uvCashCcy')||{}).value||'USD';
      var cAmt = (document.getElementById('uvCashAmt')||{}).value||'0';
      if(Number(cAmt) <= 0){ _toast('Amount must be positive',true); return; }
      _post('/my_universe/cash/upsert', {currency:cCcy, amount:cAmt});
      _hideForm('uvCashAddForm');
      _toast('Updating cash...');
      return;
    }
    var rmCash = btn.getAttribute('data-uv-remove-cash');
    if(rmCash){
      _post('/my_universe/cash/remove', {currency:rmCash});
      _toast('Removing '+rmCash+'...');
      return;
    }
  });
}

/* ─── Popups (color picker, emoji picker) ──────────────────────────── */
function _closePopup(){
  var old = document.querySelector('.uv-popup');
  if(old) old.remove();
}

function _showColorPicker(anchor, groupName){
  _closePopup();
  var popup = document.createElement('div');
  popup.className = 'uv-popup uv-color-picker';
  var html = '<div class="uv-popup-title">Pick a color</div><div class="uv-color-grid">';
  for(var i=0; i<_CAT_COLORS.length; i++){
    var c = _CAT_COLORS[i];
    var active = c === _catColor(groupName) ? ' uv-color-active' : '';
    html += '<button class="uv-color-swatch'+active+'" style="background:'+c+'" data-uv-pick-color="'+c+'" data-uv-pick-group="'+esc(groupName)+'"></button>';
  }
  html += '</div>';
  // Remove emoji option
  html += '<button class="uv-popup-link" data-uv-pick-color="" data-uv-pick-group="'+esc(groupName)+'">Reset to default</button>';
  popup.innerHTML = html;
  _positionPopup(popup, anchor);
}

function _showEmojiPicker(anchor, groupName){
  _closePopup();
  var popup = document.createElement('div');
  popup.className = 'uv-popup uv-emoji-picker';
  var html = '<div class="uv-popup-title">Pick an emoji</div><div class="uv-emoji-grid">';
  for(var i=0; i<_CAT_EMOJIS.length; i++){
    var em = _CAT_EMOJIS[i];
    var active = em === _catEmoji(groupName) ? ' uv-emoji-active' : '';
    html += '<button class="uv-emoji-btn'+active+'" data-uv-pick-emoji="'+em+'" data-uv-pick-group="'+esc(groupName)+'">'+em+'</button>';
  }
  html += '</div>';
  html += '<button class="uv-popup-link" data-uv-pick-emoji="" data-uv-pick-group="'+esc(groupName)+'">Remove emoji</button>';
  popup.innerHTML = html;
  _positionPopup(popup, anchor);
}

function _positionPopup(popup, anchor){
  document.body.appendChild(popup);
  var rect = anchor.getBoundingClientRect();
  popup.style.position = 'fixed';
  popup.style.top = (rect.bottom + 6) + 'px';
  popup.style.left = Math.max(8, rect.left) + 'px';
  popup.style.zIndex = '99999';
  // Close on outside click
  setTimeout(function(){
    document.addEventListener('click', function handler(e){
      if(!popup.contains(e.target) && e.target !== anchor){
        popup.remove();
        document.removeEventListener('click', handler);
      }
    });
  }, 10);
}

function _toggleForm(id){
  var el = document.getElementById(id);
  if(!el) return;
  el.style.display = el.style.display === 'none' ? '' : 'none';
  if(el.style.display !== 'none'){
    el.scrollIntoView({behavior:'smooth'});
    // Auto-attach autocomplete to ticker inputs inside form
    var tickerInput = el.querySelector('input[id$="Ticker"]');
    if(tickerInput && !tickerInput._acAttached) _attachAutocomplete(tickerInput);
  }
}
function _hideForm(id){
  var el = document.getElementById(id);
  if(el) el.style.display = 'none';
}

/* ─── Ticker Autocomplete ──────────────────────────────────────────── */
var _acTimer = null;
function _attachAutocomplete(input){
  input._acAttached = true;
  // Create dropdown container
  var wrap = document.createElement('div');
  wrap.style.cssText = 'position:relative;display:inline-block;width:100%;';
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);

  var dropdown = document.createElement('div');
  dropdown.className = 'uv-ac-dropdown';
  dropdown.style.display = 'none';
  wrap.appendChild(dropdown);

  input.addEventListener('input', function(){
    var q = input.value.trim();
    if(_acTimer) clearTimeout(_acTimer);
    if(q.length < 1){ dropdown.style.display = 'none'; return; }
    _acTimer = setTimeout(function(){
      fetch('/api/ticker-search?q=' + encodeURIComponent(q) + '&limit=8')
        .then(function(r){ return r.json(); })
        .then(function(d){
          var results = (d.results || []);
          if(!results.length){ dropdown.style.display = 'none'; return; }
          var html = '';
          for(var i=0; i<results.length; i++){
            var r = results[i];
            html += '<div class="uv-ac-item" data-ac-ticker="'+esc(r.ticker)+'">';
            html += '<span class="uv-ac-ticker">'+esc(r.ticker)+'</span>';
            html += '<span class="uv-ac-name">'+esc(r.name||'')+'</span>';
            if(r.industry) html += '<span class="uv-ac-industry">'+esc(r.industry)+'</span>';
            html += '</div>';
          }
          dropdown.innerHTML = html;
          dropdown.style.display = '';
        })
        .catch(function(){ dropdown.style.display = 'none'; });
    }, 200);
  });

  dropdown.addEventListener('click', function(e){
    var item = e.target.closest('[data-ac-ticker]');
    if(!item) return;
    input.value = item.getAttribute('data-ac-ticker');
    dropdown.style.display = 'none';
    // Focus next input in form
    var nextInput = input.closest('.uv-form-row').nextElementSibling;
    if(nextInput){
      var ni = nextInput.querySelector('input,select');
      if(ni) ni.focus();
    }
  });

  // Close dropdown on outside click
  document.addEventListener('click', function(e){
    if(!wrap.contains(e.target)) dropdown.style.display = 'none';
  });

  // Keyboard navigation
  input.addEventListener('keydown', function(e){
    var items = dropdown.querySelectorAll('.uv-ac-item');
    if(!items.length || dropdown.style.display === 'none') return;
    var active = dropdown.querySelector('.uv-ac-item.active');
    var idx = -1;
    if(active){
      for(var i=0; i<items.length; i++){ if(items[i]===active){ idx=i; break; } }
    }
    if(e.key === 'ArrowDown'){
      e.preventDefault();
      if(active) active.classList.remove('active');
      idx = (idx+1) % items.length;
      items[idx].classList.add('active');
      items[idx].scrollIntoView({block:'nearest'});
    } else if(e.key === 'ArrowUp'){
      e.preventDefault();
      if(active) active.classList.remove('active');
      idx = idx <= 0 ? items.length-1 : idx-1;
      items[idx].classList.add('active');
      items[idx].scrollIntoView({block:'nearest'});
    } else if(e.key === 'Enter'){
      if(active){
        e.preventDefault();
        input.value = active.getAttribute('data-ac-ticker');
        dropdown.style.display = 'none';
      }
    } else if(e.key === 'Escape'){
      dropdown.style.display = 'none';
    }
  });
}

/* ─── Export ─────────────────────────────────────────────────────────── */
window.UniverseView = { build: build, init: init };
})();
