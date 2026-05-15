/* ══════════════════════════════════════════════════════════════════════════════
   Ticker Hub Renderer — Shared JS for /ticker/X standalone & workspace inline
   ══════════════════════════════════════════════════════════════════════════════
   Usage:
     TickerHub.buildShell(ticker, companyName, hub, thesis) → HTML string
     TickerHub.init(hubData, ticker, researchNotes) — populates all panes
     TickerHub.refresh(ticker) — re-fetch + re-render
══════════════════════════════════════════════════════════════════════════════ */
(function(){
"use strict";

/* ─── Module-level state (set by init) ──────────────────────────────── */
var _TICKER = '';
var _HUB = {};
var _RESEARCH_NOTES = [];
var _TIMELINE_DATA = [];
var _rcSentiment = 'neutral';
var _rcFilter = 'all';
var _rcExpandedThread = null;

var _GROUP_PANES = {
  overview: ['overview','earnings','signals','transactions'],
  thesis: ['research','thinking','assumptions'],
  data: ['fundamentals','filings','lab','supplychain','moats','connections','notes','history']
};

var KIND_ICON = { event:'\u26A1', thought:'\uD83D\uDCAD', decision:'\u2705', action:'\uD83D\uDCCB',
                  note:'\uD83D\uDCDD', task:'\u2705', question:'\u2753', reference:'\uD83D\uDCCE', inbox:'\uD83D\uDCEC' };

/* ─── Utilities ─────────────────────────────────────────────────────── */
function escH(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function fmtDate(s){ return s ? String(s).substring(0,10) : ''; }
function setText(id,v){ var e=document.getElementById(id); if(e) e.textContent=v; }
function fmtMoney(v){
  if(v == null) return '-';
  var n = Number(v);
  if(isNaN(n)) return '-';
  n = Math.abs(n);
  if(n >= 1e12) return '$'+(n/1e12).toFixed(1)+'T';
  if(n >= 1e9) return '$'+(n/1e9).toFixed(1)+'B';
  if(n >= 1e6) return '$'+(n/1e6).toFixed(1)+'M';
  if(n >= 1e3) return '$'+(n/1e3).toFixed(1)+'K';
  return '$'+n.toFixed(0);
}
function fmtDelta(v){ var n=Number(v); return (n>=0?'+':'')+n.toFixed(1)+'%'; }

function _paneToGroup(pane){
  for(var g in _GROUP_PANES){ if(_GROUP_PANES[g].indexOf(pane)>=0) return g; }
  return 'overview';
}

/* ─── Build HTML shell ──────────────────────────────────────────────── */
function buildShell(ticker, companyName, hub, thesis){
  var h = hub || {};
  var th = thesis || {};
  var curPrice = h.current_price || (h.profile && h.profile.current_price) || '';
  var cov = h.data_coverage || {};

  var html = '';

  // Header — compact single-line
  html += '<div class="th-header">';
  html += '<div style="display:flex;align-items:center;gap:10px;flex:1;min-width:0;">';
  html += '<span class="th-ticker">$'+escH(ticker)+'</span>';
  html += '<span id="thHeaderPrice" style="font-size:17px;font-weight:700;color:var(--text);">'+escH(curPrice)+'</span>';
  html += '<span id="thHeaderChange" style="font-size:12px;font-weight:700;"></span>';
  if(companyName && companyName !== ticker) html += '<span class="th-company" style="margin-top:0;">'+escH(companyName)+'</span>';
  if(th.status){
    var badgeCls = (th.status==='active'||th.status==='watching') ? 'active' : 'inactive';
    html += '<span class="th-badge '+badgeCls+'">'+escH(th.status)+'</span>';
  }
  if(h.position) html += '<span class="th-badge position">Position</span>';
  if(cov.label){
    html += '<span style="font-size:10px;font-weight:700;color:#fff;background:'+(cov.color||'#64748b')+';padding:3px 8px;border-radius:999px;">';
    html += escH(cov.label); if(cov.stale) html += ' (stale)';
    html += '</span>';
  }
  html += '</div>';
  html += '<div class="th-actions">';
  html += '<button class="btn" id="analyzeNowBtn" onclick="TickerHub.triggerAgentAnalysis()">Analyze Now</button>';
  html += '<a href="/lab/'+escH(ticker)+'" class="btn" target="_blank">Lab</a>';
  html += '</div>';
  html += '</div>';

  // Single flat tab bar
  html += '<div class="th-sub-bar" id="sub-all">';
  html += '<button class="th-sub-btn active" data-pane="overview" onclick="TickerHub.switchTab(\'overview\',this)">Dashboard</button>';
  html += '<button class="th-sub-btn" data-pane="fundamentals" onclick="TickerHub.switchTab(\'fundamentals\',this)">Statements</button>';
  html += '<button class="th-sub-btn" data-pane="filings" onclick="TickerHub.switchTab(\'filings\',this)">Filings <span class="tab-count" id="cntFilings">0</span></button>';
  html += '<button class="th-sub-btn" data-pane="research" onclick="TickerHub.switchTab(\'research\',this)">Research <span class="tab-count" id="cntResearch">0</span></button>';
  html += '<button class="th-sub-btn" data-pane="supplychain" onclick="TickerHub.switchTab(\'supplychain\',this)">Supply Chain</button>';
  html += '<button class="th-sub-btn" data-pane="signals" onclick="TickerHub.switchTab(\'signals\',this)">Signals <span class="tab-count" id="cntSignals">0</span></button>';
  html += '<button class="th-sub-btn" data-pane="lab" onclick="TickerHub.switchTab(\'lab\',this)">Lab <span class="tab-count" id="cntLab">0</span></button>';
  html += '</div>';
  // Hidden counters for compatibility
  html += '<span id="cntEarnings" style="display:none">0</span><span id="cntThinking" style="display:none">0</span>';
  html += '<span id="cntConnections" style="display:none">0</span><span id="cntHistory" style="display:none">0</span>';
  html += '<span id="cntTransactions" style="display:none">0</span><span id="cntNotes" style="display:none">0</span>';
  html += '<span id="cntAssumptions" style="display:none">0</span><span id="cntRecords" style="display:none">0</span>';

  // 2-column layout
  html += '<div class="th-layout"><div class="th-main">';

  // --- PANE: Overview (Bento Grid Dashboard) ---
  html += '<div class="th-pane active" id="pane-overview">';
  html += '<div class="bento-grid" id="bentoGrid"></div>';
  html += '<div id="activeProposalBox" style="display:none;margin-bottom:18px;"></div>';
  html += '<div id="overviewMiniFinancials" style="margin-bottom:18px;"></div>';
  html += '<div id="overviewSignalsPreview" style="display:none;margin-bottom:18px;"></div>';
  // Hidden elements for compatibility
  html += '<div id="overviewGrid" style="display:none"></div>';
  html += '<div id="positionCard" style="display:none"><div id="positionBody"></div></div>';
  html += '<div id="profileCard" style="display:none"><div id="profileBody"></div></div>';
  html += '<div id="statsKV" style="display:none"></div>';
  html += '<div id="aiInsightText" style="display:none"></div>';
  html += '<span id="cntTimeline" style="display:none">0</span>';
  html += '<div id="overviewTimeline" style="display:none"></div>';
  html += '</div>';

  // --- PANE: Research ---
  html += '<div class="th-pane" id="pane-research">';
  html += '<div class="rc-input-wrap"><input id="rcNoteInput" type="text" placeholder="Quick note about '+escH(ticker)+'... press Enter to save" autocomplete="off" onkeydown="if(event.key===\'Enter\'&&this.value.trim())TickerHub.saveQuickNote()"><button class="rc-enter-btn" onclick="TickerHub.saveQuickNote()" title="Save note">&#x21B5;</button></div>';
  html += '<div id="rcSentimentBar" class="rc-sentiment-bar" style="display:none;"><span style="font-size:11px;color:var(--muted);font-weight:600;">Tag:</span>';
  html += '<button class="rc-sent-btn active" data-s="neutral" onclick="TickerHub.pickSentiment(\'neutral\',this)">Neutral</button>';
  html += '<button class="rc-sent-btn" data-s="supports" onclick="TickerHub.pickSentiment(\'supports\',this)">Supports thesis</button>';
  html += '<button class="rc-sent-btn" data-s="challenges" onclick="TickerHub.pickSentiment(\'challenges\',this)">Challenges thesis</button></div>';
  html += '<div class="rc-filters" id="rcFilters">';
  html += '<button class="rc-filter active" data-f="all" onclick="TickerHub.filterNotes(\'all\',this)">All <span id="rcCntAll">0</span></button>';
  html += '<button class="rc-filter" data-f="supports" onclick="TickerHub.filterNotes(\'supports\',this)">Supports <span id="rcCntSupports">0</span></button>';
  html += '<button class="rc-filter" data-f="challenges" onclick="TickerHub.filterNotes(\'challenges\',this)">Challenges <span id="rcCntChallenges">0</span></button>';
  html += '<button class="rc-filter" data-f="neutral" onclick="TickerHub.filterNotes(\'neutral\',this)">Neutral <span id="rcCntNeutral">0</span></button>';
  html += '<button class="rc-filter" data-f="pinned" onclick="TickerHub.filterNotes(\'pinned\',this)">Pinned <span id="rcCntPinned">0</span></button></div>';
  html += '<div id="rcNotesFeed"></div>';
  html += '<div id="rcScoreBar" class="rc-score-bar" style="display:none;"><div class="rc-score-track"><div id="rcScoreFill" class="rc-score-fill"></div></div><div id="rcScoreLabel" class="rc-score-label"></div></div>';
  html += '<div style="margin-top:20px;"><div class="th-section-head" style="display:flex;align-items:center;justify-content:space-between;">Research Threads <button class="btn" style="font-size:10px;padding:3px 10px;" onclick="TickerHub.createResearchThread()">+ New Thread</button></div><div id="researchList"></div></div>';
  html += '</div>';

  // --- PANE: Thinking ---
  html += '<div class="th-pane" id="pane-thinking"><div class="th-section-head" style="display:flex;align-items:center;justify-content:space-between;">Thinking Chains <button class="btn" style="font-size:10px;padding:3px 10px;" onclick="TickerHub.createThinkingChain()">+ New Chain</button></div><div id="thinkingList"></div></div>';

  // --- PANE: Signals ---
  html += '<div class="th-pane" id="pane-signals"><div class="th-section-head">AI Proposals</div><div id="signalsList"></div></div>';

  // --- PANE: Filings ---
  html += '<div class="th-pane" id="pane-filings">';
  html += '<div class="th-section-head" style="display:flex;align-items:center;justify-content:space-between;">SEC Filings';
  html += '<div style="display:flex;gap:6px;">';
  html += '<a class="btn" style="font-size:10px;padding:3px 10px;text-decoration:none;background:var(--ws-accent,#5b7ff5);color:#fff;border-color:var(--ws-accent,#5b7ff5);" href="/company_file/sec?t='+encodeURIComponent(ticker)+'" target="_blank" rel="noopener">SEC Hub &#8599;</a>';
  html += '<button class="btn" style="font-size:10px;padding:3px 10px;" onclick="TickerHub.doRefresh(\'/company_file/sec-sync\',\''+escH(ticker)+'\',this)">Sync</button>';
  html += '<button class="btn" style="font-size:10px;padding:3px 10px;" onclick="TickerHub.doRefresh(\'/company_file/sec-sync-full\',\''+escH(ticker)+'\',this)">Full Backfill</button>';
  html += '</div></div>';
  html += '<div id="filingsFilterBar" style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px;"></div>';
  html += '<div id="filingsList"></div>';
  html += '</div>';

  // --- PANE: Earnings ---
  html += '<div class="th-pane" id="pane-earnings">';
  html += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;">';
  html += '<button type="button" class="btn" onclick="TickerHub.doRefresh(\'/company_file/transcripts-refresh\',\''+escH(ticker)+'\',this)">Refresh Transcripts</button>';
  html += '<button type="button" class="btn" onclick="TickerHub.doRefresh(\'/company_file/sec-facts-refresh\',\''+escH(ticker)+'\',this)">Refresh SEC Facts</button></div>';
  html += '<div id="earningsBriefBox" style="margin-bottom:14px;"></div>';
  html += '<div class="th-section-head">Earnings Analysis (AI)</div><div id="earningsList"></div>';
  html += '<div class="th-section-head" style="margin-top:18px;">Earnings Calls (SEC)</div><div id="earningsCallsList"></div>';
  html += '<div class="th-section-head" style="margin-top:18px;">Earnings Releases (SEC)</div><div id="earningsReleasesList"></div>';
  html += '<div class="th-section-head" style="margin-top:18px;">Quarterly Result Signals (10-Q / 8-K / 10-K)</div><div id="quarterlySignalsList"></div>';
  html += '</div>';

  // --- PANE: History ---
  html += '<div class="th-pane" id="pane-history">';
  html += '<div class="th-quick-add"><input id="thQaInput" class="th-qa-input" type="text" placeholder="Add a thought, action, or decision for '+escH(ticker)+'&hellip;" autocomplete="off">';
  html += '<select id="thQaKind" class="th-qa-select"><option value="thought">Thought</option><option value="action">Action</option><option value="decision">Decision</option><option value="event">Event</option></select>';
  html += '<button class="btn" type="button" onclick="TickerHub.thAddRecord()">Add</button></div>';
  html += '<div class="th-section-head">Records <span class="count" id="cntRecords">0</span></div><div id="historyList"></div>';
  html += '</div>';

  // --- PANE: Connections ---
  html += '<div class="th-pane" id="pane-connections"><div class="th-section-head">Connected Objects</div><div id="connectionsList" style="display:flex;flex-wrap:wrap;gap:4px;"></div></div>';

  // --- PANE: Fundamentals ---
  html += '<div class="th-pane" id="pane-fundamentals">';
  html += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;">';
  html += '<button type="button" class="btn" onclick="TickerHub.doRefresh(\'/company_file/mini-statements-refresh\',\''+escH(ticker)+'\',this)">Refresh Financials</button>';
  html += '<button type="button" class="btn" onclick="TickerHub.doRefresh(\'/company_file/intel-refresh\',\''+escH(ticker)+'\',this)">Refresh Intel</button>';
  html += '<button type="button" class="btn" onclick="TickerHub.doRefresh(\'/company_file/price-metrics-refresh\',\''+escH(ticker)+'\',this)">Refresh Prices</button></div>';
  html += '<div class="th-section-head">Price Metrics</div><div class="th-grid" id="priceMetricsGrid"></div>';
  html += '<div class="th-section-head">5-Year Financials</div><div id="miniStatsDeltaSummary" style="font-size:12px;color:var(--muted);margin-bottom:8px;"></div><div id="miniStatementsBox"></div>';
  html += '<div class="th-section-head" style="margin-top:18px;">Revenue Segments</div><div id="revenueSegmentsBox"></div>';
  html += '<div class="th-section-head" style="margin-top:18px;">Share Buybacks (TTM)</div><div id="buybackBox"></div>';
  html += '<div class="th-section-head" style="margin-top:18px;">Insider Trades (Form 4)</div><div id="insiderTradesBox"></div>';
  html += '</div>';

  // --- PANE: Moats ---
  html += '<div class="th-pane" id="pane-moats">';
  html += '<div class="th-section-head">Investment Thesis (Moat Tags)</div>';
  html += '<div style="margin-bottom:18px;"><input type="hidden" name="ticker" value="'+escH(ticker)+'">';
  html += '<div id="moatTagsBox" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;"></div>';
  html += '<button type="button" class="btn" id="moatSaveBtn" onclick="TickerHub.saveMoats()">Save Thesis Tags</button></div>';
  html += '<div class="th-section-head">Competitors</div><div id="competitorsList"></div>';
  html += '<div class="th-section-head">Similar Companies</div><div id="similarList" style="display:flex;flex-wrap:wrap;gap:6px;"></div>';
  html += '</div>';

  // --- PANE: Supply Chain ---
  html += '<div class="th-pane" id="pane-supplychain">';
  html += '<div class="th-section-head">Supply Chain Map</div><div id="supplyChainBox"></div>';
  html += '<div style="margin-top:14px;padding:12px;border:none;border-radius:14px;background:var(--panel);box-shadow:0 1px 3px rgba(0,0,0,.04);">';
  html += '<div style="font-size:12px;font-weight:800;color:var(--text);margin-bottom:8px;">Add Relationship</div>';
  html += '<form id="scAddForm" style="display:grid;gap:8px;"><input type="hidden" name="ticker" value="'+escH(ticker)+'">';
  html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">';
  html += '<input id="scTicker" name="counterparty_ticker" placeholder="Ticker (e.g. VRT)" required style="border:none;border-radius:10px;padding:6px 10px;font-size:13px;background:var(--panel2);color:var(--text);">';
  html += '<input id="scName" name="counterparty_name" placeholder="Company name (optional)" style="border:none;border-radius:10px;padding:6px 10px;font-size:13px;background:var(--panel2);color:var(--text);"></div>';
  html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">';
  html += '<select id="scRelType" name="relationship_type" style="border:none;border-radius:10px;padding:6px 10px;font-size:13px;background:var(--panel2);color:var(--text);"><option value="supplier">Supplier</option><option value="customer">Customer</option><option value="partner">Partner</option></select>';
  html += '<input id="scConf" name="confidence" type="number" min="0" max="1" step="0.05" value="0.70" placeholder="Confidence 0-1" style="border:none;border-radius:10px;padding:6px 10px;font-size:13px;background:var(--panel2);color:var(--text);"></div>';
  html += '<textarea id="scEvidence" name="evidence" rows="2" placeholder="Evidence or note (optional)" style="border:none;border-radius:10px;padding:6px 10px;font-size:13px;background:var(--panel2);color:var(--text);"></textarea>';
  html += '<button type="submit" class="btn">Add Supply Link</button></form></div>';
  html += '</div>';

  // --- PANE: Notes ---
  html += '<div class="th-pane" id="pane-notes"><div class="th-section-head">Notes</div><div id="notesList"></div><div class="th-section-head" style="margin-top:18px;">Tasks</div><div id="tasksList"></div><div class="th-section-head" style="margin-top:18px;">Reminders</div><div id="remindersList"></div></div>';

  // --- PANE: Transactions ---
  html += '<div class="th-pane" id="pane-transactions"><div class="th-section-head">Buy/Sell History</div><div id="transactionsList"></div><div class="th-section-head" style="margin-top:18px;">Decision Log</div><div id="decisionsList"></div></div>';

  // --- PANE: Lab (lazy-loaded via LabView renderer) ---
  html += '<div class="th-pane" id="pane-lab"><div id="labContainer"><div style="padding:20px;color:var(--muted);">Loading lab…</div></div></div>';

  // --- PANE: Assumptions ---
  html += '<div class="th-pane" id="pane-assumptions"><div class="th-section-head">Position Assumptions</div>';
  html += '<div style="margin-bottom:14px;padding:10px 12px;border:none;border-radius:14px;background:var(--panel);box-shadow:0 1px 3px rgba(0,0,0,.04);">';
  html += '<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:flex-end;">';
  html += '<div style="flex:1;min-width:200px;"><div style="font-size:11px;font-weight:700;color:var(--muted);margin-bottom:3px;">Assumption</div><input id="asmText" type="text" class="th-qa-input" placeholder="e.g. iPhone growth stays above 5%"></div>';
  html += '<div style="width:120px;"><div style="font-size:11px;font-weight:700;color:var(--muted);margin-bottom:3px;">Type</div><select id="asmType" class="th-qa-select" style="width:100%;"><option value="qualitative">Qualitative</option><option value="price">Price</option><option value="metric">Financial Metric</option><option value="macro">Macro</option></select></div>';
  html += '<div style="width:120px;"><div style="font-size:11px;font-weight:700;color:var(--muted);margin-bottom:3px;">Metric (optional)</div><input id="asmMetric" type="text" class="th-qa-input" placeholder="e.g. oil, revenue"></div>';
  html += '<div style="width:60px;"><div style="font-size:11px;font-weight:700;color:var(--muted);margin-bottom:3px;">Op</div><select id="asmOp" class="th-qa-select" style="width:100%;"><option value=">=">&ge;</option><option value="<=">&le;</option><option value=">">&gt;</option><option value="<">&lt;</option></select></div>';
  html += '<div style="width:80px;"><div style="font-size:11px;font-weight:700;color:var(--muted);margin-bottom:3px;">Threshold</div><input id="asmThreshold" type="number" class="th-qa-input" placeholder="90"></div>';
  html += '<div style="width:90px;"><div style="font-size:11px;font-weight:700;color:var(--muted);margin-bottom:3px;">Importance</div><select id="asmImportance" class="th-qa-select" style="width:100%;"><option value="core">Core</option><option value="supporting">Supporting</option><option value="nice_to_have">Nice to Have</option></select></div>';
  html += '<button class="btn" type="button" onclick="TickerHub.addAssumption()">Add</button></div></div>';
  html += '<div id="assumptionsList"></div></div>';

  // --- PANE: Conviction ---
  html += '<div class="th-pane" id="pane-conviction"><div class="th-section-head">Conviction Score</div><div class="th-grid" id="convictionGrid"></div><div class="th-section-head" style="margin-top:18px;">Score Components</div><div class="th-grid" id="convictionComponents"></div><div class="th-section-head" style="margin-top:18px;">Conviction History</div><div id="convictionHistory"></div><div style="margin-top:12px;"><button class="btn" onclick="TickerHub.refreshConviction()">Recalculate Now</button></div></div>';

  // --- PANE: Triggers ---
  html += '<div class="th-pane" id="pane-triggers"><div class="th-section-head">Conditional Triggers</div>';
  html += '<div style="margin-bottom:14px;padding:10px 12px;border:none;border-radius:14px;background:var(--panel);box-shadow:0 1px 3px rgba(0,0,0,.04);">';
  html += '<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:flex-end;">';
  html += '<div style="flex:1;min-width:180px;"><div style="font-size:11px;font-weight:700;color:var(--muted);margin-bottom:3px;">Rule Name</div><input id="trgName" type="text" class="th-qa-input" placeholder="e.g. Review if price drops"></div>';
  html += '<div style="width:130px;"><div style="font-size:11px;font-weight:700;color:var(--muted);margin-bottom:3px;">Type</div><select id="trgType" class="th-qa-select" style="width:100%;"><option value="price_below">Price Below</option><option value="price_above">Price Above</option><option value="conviction_below">Conviction Below</option><option value="metric_check">Metric Check</option></select></div>';
  html += '<div style="width:80px;"><div style="font-size:11px;font-weight:700;color:var(--muted);margin-bottom:3px;">Threshold</div><input id="trgThreshold" type="number" class="th-qa-input" placeholder="150"></div>';
  html += '<div style="flex:1;min-width:150px;"><div style="font-size:11px;font-weight:700;color:var(--muted);margin-bottom:3px;">Action</div><input id="trgAction" type="text" class="th-qa-input" placeholder="e.g. Review thesis, consider trimming"></div>';
  html += '<button class="btn" type="button" onclick="TickerHub.addTrigger()">Add</button></div></div>';
  html += '<div id="triggersList"></div></div>';

  html += '</div>'; // .th-main

  // Right sidebar removed — user preference

  html += '</div>'; // .th-layout

  // Modals
  html += _buildModals(ticker);

  return html;
}

function _buildModals(ticker){
  var html = '';
  // Thesis modal
  html += '<div id="thesisModalBg" style="position:fixed;inset:0;background:rgba(15,23,42,.35);z-index:80;display:none;align-items:center;justify-content:center;padding:14px;" onclick="if(event.target===this)TickerHub.closeThesisModal()">';
  html += '<div style="width:min(560px,96vw);background:var(--panel);border:none;border-radius:14px;padding:18px;box-shadow:0 10px 30px rgba(15,23,42,.22);max-height:90vh;overflow-y:auto;">';
  html += '<h4 style="margin:0 0 12px;font-size:16px;font-weight:800;">Edit Thesis: '+escH(ticker)+'</h4>';
  html += '<label style="display:block;font-size:11px;font-weight:700;color:var(--muted);margin-bottom:4px;">Investment Thesis</label>';
  html += '<textarea id="tmThesisText" rows="4" placeholder="Why do you own/watch this?" style="width:100%;box-sizing:border-box;border:none;border-radius:10px;padding:8px 10px;font-size:13px;background:var(--panel2);color:var(--text);font-family:inherit;resize:vertical;min-height:80px;"></textarea>';
  html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px;">';
  html += '<div><label style="display:block;font-size:11px;font-weight:700;color:var(--muted);margin-bottom:4px;">Target Price ($)</label><input id="tmTarget" type="number" step="0.01" placeholder="e.g. 240" style="width:100%;box-sizing:border-box;border:none;border-radius:10px;padding:8px 10px;font-size:13px;background:var(--panel2);color:var(--text);"></div>';
  html += '<div><label style="display:block;font-size:11px;font-weight:700;color:var(--muted);margin-bottom:4px;">Time Horizon</label><input id="tmHorizon" placeholder="e.g. 12-18 months" style="width:100%;box-sizing:border-box;border:none;border-radius:10px;padding:8px 10px;font-size:13px;background:var(--panel2);color:var(--text);"></div></div>';
  html += '<label style="display:block;font-size:11px;font-weight:700;color:var(--muted);margin-bottom:4px;margin-top:10px;">Invalidation Criteria</label>';
  html += '<input id="tmInvalidation" placeholder="What would make you sell?" style="width:100%;box-sizing:border-box;border:none;border-radius:10px;padding:8px 10px;font-size:13px;background:var(--panel2);color:var(--text);">';
  html += '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">';
  html += '<button class="btn" onclick="TickerHub.closeThesisModal()">Cancel</button>';
  html += '<button class="btn" style="background:var(--accent);color:#fff;border-color:var(--accent);" onclick="TickerHub.saveThesis()">Save</button></div></div></div>';

  // New thread modal
  html += '<div id="newThreadModalBg" style="position:fixed;inset:0;background:rgba(15,23,42,.35);z-index:80;display:none;align-items:center;justify-content:center;padding:14px;" onclick="if(event.target===this)this.style.display=\'none\'">';
  html += '<div style="width:min(460px,96vw);background:var(--panel);border:none;border-radius:14px;padding:18px;box-shadow:0 10px 30px rgba(15,23,42,.22);">';
  html += '<h4 style="margin:0 0 12px;font-size:16px;font-weight:800;">New Research Thread</h4>';
  html += '<label style="display:block;font-size:11px;font-weight:700;color:var(--muted);margin-bottom:4px;">Title</label>';
  html += '<input id="ntThreadTitle" type="text" placeholder="e.g. '+escH(ticker)+' Deep Dive" style="width:100%;box-sizing:border-box;border:none;border-radius:10px;padding:8px 10px;font-size:13px;background:var(--panel2);color:var(--text);font-family:inherit;" onkeydown="if(event.key===\'Enter\')TickerHub.submitNewThread()">';
  html += '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;"><button class="btn" onclick="document.getElementById(\'newThreadModalBg\').style.display=\'none\'">Cancel</button><button class="btn" style="background:var(--accent);color:#fff;border-color:var(--accent);" onclick="TickerHub.submitNewThread()">Create</button></div></div></div>';

  // New chain modal
  html += '<div id="newChainModalBg" style="position:fixed;inset:0;background:rgba(15,23,42,.35);z-index:80;display:none;align-items:center;justify-content:center;padding:14px;" onclick="if(event.target===this)this.style.display=\'none\'">';
  html += '<div style="width:min(460px,96vw);background:var(--panel);border:none;border-radius:14px;padding:18px;box-shadow:0 10px 30px rgba(15,23,42,.22);">';
  html += '<h4 style="margin:0 0 12px;font-size:16px;font-weight:800;">New Thinking Chain</h4>';
  html += '<label style="display:block;font-size:11px;font-weight:700;color:var(--muted);margin-bottom:4px;">Title</label>';
  html += '<input id="ntChainTitle" type="text" placeholder="e.g. '+escH(ticker)+' \u2014 Investment thesis analysis" style="width:100%;box-sizing:border-box;border:none;border-radius:10px;padding:8px 10px;font-size:13px;background:var(--panel2);color:var(--text);font-family:inherit;" onkeydown="if(event.key===\'Enter\')TickerHub.submitNewChain()">';
  html += '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;"><button class="btn" onclick="document.getElementById(\'newChainModalBg\').style.display=\'none\'">Cancel</button><button class="btn" style="background:var(--accent);color:#fff;border-color:var(--accent);" onclick="TickerHub.submitNewChain()">Create</button></div></div></div>';

  return html;
}

/* ─── Group switching ──────────────────────────────────────────────── */
function switchGroup(group, btn){
  // Groups removed — just switch to first tab of the group
  switchTab(group === 'overview' ? 'overview' : (group === 'thesis' ? 'research' : 'fundamentals'), null);
}

/* ─── Tab switching (flat) ─────────────────────────────────────────── */
function switchTab(pane, btn){
  document.querySelectorAll('.th-sub-btn').forEach(function(b){ b.classList.remove('active'); });
  document.querySelectorAll('.th-pane').forEach(function(p){ p.classList.remove('active'); });
  if(btn) btn.classList.add('active');
  else {
    var tabBtn = document.querySelector('.th-sub-btn[data-pane="'+pane+'"]');
    if(tabBtn) tabBtn.classList.add('active');
  }
  var el = document.getElementById('pane-'+pane);
  if(el) el.classList.add('active');
  try{ localStorage.setItem('thub_tab_'+_TICKER, pane); }catch(_){}
  if(pane === 'lab' && !_labLoaded) _loadLabPane();
}

var _labLoaded = false;
var _labJSLoading = false;
var _labJSReady = false;

function _ensureLabJS(cb){
  if(_labJSReady){ cb(); return; }
  if(_labJSLoading){ var iv=setInterval(function(){ if(_labJSReady){ clearInterval(iv); cb(); }},50); return; }
  _labJSLoading = true;
  var link = document.createElement('link'); link.rel='stylesheet'; link.href='/static/css/lab.css';
  document.head.appendChild(link);
  var s = document.createElement('script'); s.src='/static/js/lab_renderer.js';
  s.onload = function(){ _labJSReady=true; _labJSLoading=false; cb(); };
  s.onerror = function(){ _labJSLoading=false; };
  document.head.appendChild(s);
}

function _loadLabPane(){
  var container = document.getElementById('labContainer');
  if(!container) return;
  Promise.all([
    fetch('/api/lab/'+encodeURIComponent(_TICKER)+'/context').then(function(r){ return r.json(); }),
    fetch('/api/lab/'+encodeURIComponent(_TICKER)+'/checklist').then(function(r){ return r.json(); })
  ]).then(function(results){
    var ctx = results[0];
    var checklist = results[1];
    _ensureLabJS(function(){
      container.innerHTML = LabView.build(_TICKER, ctx, checklist);
      LabView.init(_TICKER);
      _labLoaded = true;
    });
  }).catch(function(){
    container.innerHTML = '<div style="padding:20px;color:#ef4444;">Failed to load lab data.</div>';
  });
}

/* ─── Build functions ──────────────────────────────────────────────── */
function buildOverview(){
  var h = _HUB;
  var grid = document.getElementById('bentoGrid');
  if(!grid) return;
  var html = '';
  var pr = h.profile || {};

  // Update tab counts
  setText('cntResearch', (h.research_threads||[]).length);
  setText('cntThinking', (h.thinking_chains||[]).length);
  setText('cntSignals', (h.proposals||[]).length);
  setText('cntFilings', (h.filings||[]).length);
  setText('cntEarnings', (h.earnings||[]).length);
  setText('cntHistory', (h.records||[]).length);
  setText('cntConnections', (h.work_graph||[]).length);
  setText('cntRecords', (h.records||[]).length);
  setText('cntTimeline', (h.timeline||[]).length);
  setText('cntLab', (h.lab_results||[]).length);

  // --- Card 1: Company Profile (wide) ---
  if(pr.sector || pr.industry || pr.market_cap || pr.description){
    html += '<div class="bento-card bento-indigo wide">';
    html += '<div class="bento-icon">🏢</div>';
    html += '<div class="bento-title">Company Profile</div>';
    var profileParts = [];
    if(pr.sector) profileParts.push(escH(pr.sector));
    if(pr.industry) profileParts.push(escH(pr.industry));
    if(pr.market_cap){ var mc = pr.market_cap; profileParts.push('MCap '+(isNaN(Number(mc)) ? escH(String(mc)) : '$'+Number(mc).toLocaleString())); }
    html += '<div class="bento-sub">'+profileParts.join(' · ')+'</div>';
    if(pr.description) html += '<div class="bento-detail">'+escH(String(pr.description).substring(0,200))+(String(pr.description).length>200?'…':'')+'</div>';
    html += '</div>';
  }

  // --- Card 2: Buffett Score (only if data exists) ---
  var bc = h.buffett_checklist || {};
  if(bc.score != null && bc.total){
    var bcScore = bc.score || 0;
    var bcTotal = bc.total || 10;
    var bcPct = bcTotal > 0 ? Math.round((bcScore / bcTotal) * 100) : 0;
    var bcColor = bcPct >= 70 ? 'bento-green' : (bcPct >= 40 ? 'bento-amber' : 'bento-red');
    html += '<div class="bento-card '+bcColor+'" onclick="TickerHub.switchTab(\'fundamentals\',null)">';
    html += '<div class="bento-icon">✅</div>';
    html += '<div class="bento-title">Buffett Checklist</div>';
    html += '<div class="bento-value">'+bcPct+'%</div>';
    html += '<div class="bento-sub">'+bcScore+' / '+bcTotal+' criteria met</div>';
    html += '<div class="bento-footer">View details</div>';
    html += '</div>';
  }

  // --- Card 3: Price Returns (from price_metrics) ---
  var pm = h.price_metrics || {};
  var ytd = parseFloat(pm.ytd_return) || 0;
  var y1 = parseFloat(pm.m12_return) || 0;
  var y5 = parseFloat(pm.y5_return) || 0;
  var hasReturns = pm.ytd_return != null || pm.m12_return != null || pm.y5_return != null;
  if(hasReturns){
    var retColor = ytd >= 0 ? 'bento-green' : 'bento-red';
    html += '<div class="bento-card '+retColor+'">';
    html += '<div class="bento-icon">📈</div>';
    html += '<div class="bento-title">Price Returns</div>';
    html += '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:8px;">';
    html += '<div><div style="font-size:11px;opacity:.7;">YTD</div><div style="font-size:18px;font-weight:800;">'+(ytd>=0?'+':'')+ytd.toFixed(1)+'%</div></div>';
    html += '<div><div style="font-size:11px;opacity:.7;">12M</div><div style="font-size:18px;font-weight:800;">'+(y1>=0?'+':'')+y1.toFixed(1)+'%</div></div>';
    html += '<div><div style="font-size:11px;opacity:.7;">5Y</div><div style="font-size:18px;font-weight:800;">'+(y5>=0?'+':'')+y5.toFixed(1)+'%</div></div>';
    html += '</div></div>';
  }

  // --- Card 4: Financials ---
  var ms = h.mini_statements || {};
  var hasFinancials = ms && ms.years && ms.years.length > 0;
  if(hasFinancials){
    var revRow = (ms.rows||[]).find(function(r){ return r.key==='revenue'; }) || {};
    var lastRev = revRow.values ? revRow.values[revRow.values.length-1] : null;
    html += '<div class="bento-card bento-blue wide" onclick="TickerHub.switchTab(\'fundamentals\',null)">';
    html += '<div class="bento-icon">💰</div>';
    html += '<div class="bento-title">5-Year Financials</div>';
    if(lastRev) html += '<div class="bento-value">$'+(Number(lastRev)/1e9).toFixed(1)+'B</div>';
    html += '<div class="bento-sub">'+ms.years.join(' → ')+'</div>';
    html += '<div class="bento-footer">View statements</div>';
    html += '</div>';
  }

  // --- Card 5: SEC Filings ---
  var filings = h.filings || [];
  if(filings.length > 0){
    var latest = filings[0];
    html += '<div class="bento-card bento-slate" onclick="TickerHub.switchTab(\'filings\',null)">';
    html += '<div class="bento-icon">📑</div>';
    html += '<div class="bento-title">SEC Filings</div>';
    html += '<div class="bento-value">'+filings.length+'</div>';
    html += '<div class="bento-sub">Latest: '+escH(latest.form_type||'Filing')+' · '+fmtDate(latest.filed_date||latest.filing_date)+'</div>';
    html += '<div class="bento-footer">View all filings</div>';
    html += '</div>';
  }

  // --- Card 6: Supply Chain ---
  var sc = h.supply_chain || [];
  if(sc.length > 0){
    var topNames = sc.slice(0,3).map(function(s){ return escH(s.counterparty_ticker||s.name||''); }).join(', ');
    html += '<div class="bento-card bento-teal" onclick="TickerHub.switchTab(\'supplychain\',null)">';
    html += '<div class="bento-icon">🔗</div>';
    html += '<div class="bento-title">Supply Chain</div>';
    html += '<div class="bento-value">'+sc.length+'</div>';
    html += '<div class="bento-sub">'+topNames+'</div>';
    html += '<div class="bento-footer">View map</div>';
    html += '</div>';
  }

  // --- Card 7: AI Signals ---
  var signals = h.proposals || [];
  if(signals.length > 0){
    var latestSig = signals[0];
    html += '<div class="bento-card bento-purple" onclick="TickerHub.switchTab(\'signals\',null)">';
    html += '<div class="bento-icon">🤖</div>';
    html += '<div class="bento-title">AI Signals</div>';
    html += '<div class="bento-value">'+signals.length+'</div>';
    html += '<div class="bento-sub">'+escH((latestSig.action_type||latestSig.headline||'').substring(0,50))+'</div>';
    html += '<div class="bento-footer">View signals</div>';
    html += '</div>';
  }

  // --- Card 8: Earnings ---
  var earnings = h.earnings || [];
  if(earnings.length > 0){
    var nextEarning = earnings[0];
    html += '<div class="bento-card bento-amber" onclick="TickerHub.switchTab(\'earnings\',null)">';
    html += '<div class="bento-icon">📊</div>';
    html += '<div class="bento-title">Earnings</div>';
    html += '<div class="bento-value">'+earnings.length+'</div>';
    html += '<div class="bento-sub">Latest: '+fmtDate(nextEarning.report_date||nextEarning.date)+'</div>';
    html += '<div class="bento-footer">View earnings</div>';
    html += '</div>';
  }

  // --- Card 9: Thesis ---
  var th = _HUB.thesis || {};
  html += '<div class="bento-card bento-indigo" onclick="TickerHub.openThesisModal()">';
  html += '<div class="bento-icon">📝</div>';
  html += '<div class="bento-title">Investment Thesis</div>';
  if(th.thesis){
    html += '<div class="bento-detail">'+escH(String(th.thesis).substring(0,120))+(String(th.thesis).length>120?'…':'')+'</div>';
    if(th.target_price) html += '<div class="bento-sub" style="margin-top:4px;">Target: $'+Number(th.target_price).toFixed(2)+'</div>';
  } else {
    html += '<div class="bento-detail" style="opacity:.6;">No thesis written yet. Click to add one.</div>';
  }
  html += '<div class="bento-footer">'+(th.thesis?'Edit thesis':'Write thesis')+'</div>';
  html += '</div>';

  // --- Card 10: Position (if held) ---
  if(h.position){
    var p = h.position;
    html += '<div class="bento-card bento-green">';
    html += '<div class="bento-icon">💼</div>';
    html += '<div class="bento-title">Position</div>';
    if(p.market_value) html += '<div class="bento-value">$'+Number(p.market_value).toLocaleString()+'</div>';
    var posDetails = [];
    if(p.shares) posDetails.push(escH(p.shares)+' shares');
    if(p.avg_cost) posDetails.push('Avg $'+Number(p.avg_cost).toFixed(2));
    if(p.weight_pct) posDetails.push(Number(p.weight_pct).toFixed(1)+'% weight');
    html += '<div class="bento-sub">'+posDetails.join(' · ')+'</div>';
    html += '</div>';
  }

  // --- Card 11: Research ---
  var threads = h.research_threads || [];
  if(threads.length > 0){
    html += '<div class="bento-card bento-purple" onclick="TickerHub.switchTab(\'research\',null)">';
    html += '<div class="bento-icon">🔬</div>';
    html += '<div class="bento-title">Research</div>';
    html += '<div class="bento-value">'+threads.length+'</div>';
    html += '<div class="bento-sub">threads</div>';
    html += '<div class="bento-footer">View research</div>';
    html += '</div>';
  }

  grid.innerHTML = html;
}

function buildTimeline(container, items){
  if(!container) return;
  if(!items||!items.length){ container.innerHTML='<div class="th-empty">No activity yet.</div>'; return; }
  var html = '';
  items.forEach(function(e){
    html += '<div class="th-tl-item">';
    html += '<div class="th-tl-dot">'+escH(e.emoji||'')+'</div>';
    html += '<div class="th-tl-card">';
    html += '<div class="th-tl-title">'+escH(e.title)+'</div>';
    html += '<div class="th-tl-meta"><span>'+fmtDate(e.date)+'</span> <span class="th-item-badge">'+escH(e.type)+'</span></div>';
    if(e.detail) html += '<div class="th-tl-detail">'+escH(e.detail)+'</div>';
    html += '</div></div>';
  });
  container.innerHTML = html;
}

function buildResearch(){
  var threads = _HUB.research_threads || [];
  var el = document.getElementById('researchList');
  if(!el) return;
  if(!threads.length){ el.innerHTML='<div class="th-empty">No research threads yet.</div>'; return; }
  var html = '';
  threads.forEach(function(t){
    html += '<div class="th-list-item" style="display:block;cursor:pointer;" id="rcThread'+t.id+'">';
    html += '<div onclick="TickerHub.toggleThread('+t.id+')" style="display:flex;gap:10px;align-items:flex-start;">';
    html += '<div class="emoji">'+(t.emoji||'\uD83D\uDD2C')+'</div>';
    html += '<div class="body" style="flex:1;min-width:0;">';
    html += '<div class="title">'+escH(t.title||'Untitled Thread')+'</div>';
    html += '<div class="sub">'+fmtDate(t.updated_at||t.created_at);
    if(t.entry_count) html += ' &middot; '+t.entry_count+' messages';
    html += '</div>';
    if(t.thesis) html += '<div class="detail">'+escH(String(t.thesis).substring(0,120))+'</div>';
    html += '</div>';
    html += '<span style="font-size:12px;color:var(--muted);flex:0 0 auto;" id="rcThreadArrow'+t.id+'">&#x25B6;</span>';
    html += '</div>';
    html += '<div class="rc-thread-expand" id="rcThreadBody'+t.id+'" style="display:none;"></div>';
    html += '</div>';
  });
  el.innerHTML = html;
  renderNotes();
}

function toggleThread(threadId){
  var body = document.getElementById('rcThreadBody'+threadId);
  var arrow = document.getElementById('rcThreadArrow'+threadId);
  if(!body) return;
  if(body.style.display !== 'none'){
    body.style.display = 'none';
    if(arrow) arrow.innerHTML = '&#x25B6;';
    _rcExpandedThread = null;
    return;
  }
  if(_rcExpandedThread && _rcExpandedThread !== threadId){
    var prev = document.getElementById('rcThreadBody'+_rcExpandedThread);
    var prevA = document.getElementById('rcThreadArrow'+_rcExpandedThread);
    if(prev) prev.style.display = 'none';
    if(prevA) prevA.innerHTML = '&#x25B6;';
  }
  _rcExpandedThread = threadId;
  if(arrow) arrow.innerHTML = '&#x25BC;';
  body.style.display = 'block';
  body.innerHTML = '<div style="padding:8px 0;color:var(--muted);font-size:12px;">Loading...</div>';
  fetch('/workspace/thread/'+threadId)
    .then(function(r){ return r.json(); })
    .then(function(d){
      var t = d.thread || d;
      var entries = (t.entries || d.entries || []).reverse();
      var html = '';
      if(!entries.length){
        html += '<div style="padding:6px 0;font-size:12px;color:var(--muted);">No entries yet.</div>';
      } else {
        entries.forEach(function(e){
          html += '<div class="rc-thread-msg"><div class="msg-date">'+fmtDate(e.created_at)+' &middot; '+escH(e.kind||'note')+'</div>'+escH(e.content||'')+'</div>';
        });
      }
      html += '<div class="rc-thread-reply-wrap"><input id="rcReply'+threadId+'" type="text" placeholder="Add to thread..." onkeydown="if(event.key===\'Enter\')TickerHub.addThreadEntry('+threadId+')"><button onclick="TickerHub.addThreadEntry('+threadId+')">Add</button></div>';
      body.innerHTML = html;
    })
    .catch(function(){ body.innerHTML = '<div style="color:var(--muted);font-size:12px;">Failed to load thread.</div>'; });
}

function addThreadEntry(threadId){
  var inp = document.getElementById('rcReply'+threadId);
  if(!inp) return;
  var text = inp.value.trim();
  if(!text) return;
  inp.disabled = true;
  var fd = new FormData();
  fd.append('content', text);
  fd.append('kind', 'note');
  fetch('/workspace/thread/'+threadId+'/entry', { method:'POST', body: fd })
    .then(function(r){ return r.json(); })
    .then(function(d){
      inp.disabled = false;
      if(d.ok || d.id){
        inp.value = '';
        toggleThread(threadId);
        toggleThread(threadId);
      }
    })
    .catch(function(){ inp.disabled = false; });
}

function pickSentiment(s, btn){
  _rcSentiment = s;
  document.querySelectorAll('.rc-sent-btn').forEach(function(b){ b.classList.remove('active'); });
  btn.classList.add('active');
}

function filterNotes(f, btn){
  _rcFilter = f;
  document.querySelectorAll('.rc-filter').forEach(function(b){ b.classList.remove('active'); });
  btn.classList.add('active');
  renderNotes();
}

function saveQuickNote(){
  var inp = document.getElementById('rcNoteInput');
  if(!inp) return;
  var text = inp.value.trim();
  if(!text) return;
  inp.value = '';
  inp.disabled = true;
  var fd = new FormData();
  fd.append('ticker', _TICKER);
  fd.append('note', text);
  fd.append('sentiment', _rcSentiment);
  fetch('/api/research-note', { method:'POST', body: fd })
    .then(function(r){ return r.json(); })
    .then(function(d){
      inp.disabled = false;
      inp.focus();
      if(d.ok && d.note){
        _RESEARCH_NOTES.unshift(d.note);
        renderNotes();
      }
    })
    .catch(function(){ inp.disabled = false; });
  _rcSentiment = 'neutral';
  document.querySelectorAll('.rc-sent-btn').forEach(function(b){ b.classList.remove('active'); });
  var nb = document.querySelector('.rc-sent-btn[data-s="neutral"]');
  if(nb) nb.classList.add('active');
  var bar = document.getElementById('rcSentimentBar');
  if(bar) bar.style.display = 'none';
}

function togglePin(noteId, currentlyPinned){
  var fd = new FormData();
  fd.append('pinned', currentlyPinned ? 'false' : 'true');
  fetch('/api/research-note/'+noteId+'/update', { method:'POST', body: fd })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(d.ok){
        _RESEARCH_NOTES.forEach(function(n){ if(n.id===noteId) n.pinned = !currentlyPinned; });
        _RESEARCH_NOTES.sort(function(a,b){
          if(a.pinned && !b.pinned) return -1;
          if(!a.pinned && b.pinned) return 1;
          return new Date(b.created_at) - new Date(a.created_at);
        });
        renderNotes();
      }
    });
}

function changeSentiment(noteId, newSent){
  var fd = new FormData();
  fd.append('sentiment', newSent);
  fetch('/api/research-note/'+noteId+'/update', { method:'POST', body: fd })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(d.ok){
        _RESEARCH_NOTES.forEach(function(n){ if(n.id===noteId) n.sentiment = newSent; });
        renderNotes();
      }
    });
}

function deleteNote(noteId){
  fetch('/api/research-note/'+noteId+'/delete', { method:'POST' })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(d.ok){
        _RESEARCH_NOTES = _RESEARCH_NOTES.filter(function(n){ return n.id !== noteId; });
        renderNotes();
      }
    });
}

function renderNotes(){
  var feed = document.getElementById('rcNotesFeed');
  if(!feed) return;
  var filtered = _RESEARCH_NOTES;
  if(_rcFilter === 'pinned') filtered = _RESEARCH_NOTES.filter(function(n){ return n.pinned; });
  else if(_rcFilter !== 'all') filtered = _RESEARCH_NOTES.filter(function(n){ return n.sentiment === _rcFilter; });

  var all = _RESEARCH_NOTES.length;
  var sup = _RESEARCH_NOTES.filter(function(n){ return n.sentiment==='supports'; }).length;
  var chl = _RESEARCH_NOTES.filter(function(n){ return n.sentiment==='challenges'; }).length;
  var neu = _RESEARCH_NOTES.filter(function(n){ return n.sentiment==='neutral'; }).length;
  var pin = _RESEARCH_NOTES.filter(function(n){ return n.pinned; }).length;
  setText('rcCntAll', all);
  setText('rcCntSupports', sup);
  setText('rcCntChallenges', chl);
  setText('rcCntNeutral', neu);
  setText('rcCntPinned', pin);

  if(!filtered.length){
    feed.innerHTML = '<div class="th-empty" style="padding:20px 0;">No notes yet. Start typing above to capture your research.</div>';
  } else {
    var html = '';
    filtered.forEach(function(n){
      var sentClass = 's-' + (n.sentiment || 'neutral');
      var sentLabel = n.sentiment === 'supports' ? 'Supports thesis' : n.sentiment === 'challenges' ? 'Challenges thesis' : 'Neutral';
      var d = new Date(n.created_at);
      var dateStr = d.toLocaleDateString('en-US', {month:'short', day:'numeric'});
      html += '<div class="rc-note'+(n.pinned?' pinned':'')+'" data-id="'+n.id+'">';
      html += '<div class="rc-note-text">'+(n.pinned?'<span title="Pinned" style="margin-right:4px;">&#x1f4cc;</span>':'')+escH(n.note)+'</div>';
      html += '<div class="rc-note-meta">';
      html += '<span class="rc-sent-pill '+sentClass+'">'+sentLabel+'</span>';
      html += '<span class="rc-date">'+dateStr+'</span>';
      html += '<span class="rc-actions">';
      var nextSent = n.sentiment==='neutral'?'supports':n.sentiment==='supports'?'challenges':'neutral';
      var nextLabel = nextSent==='supports'?'Mark supports':nextSent==='challenges'?'Mark challenges':'Mark neutral';
      html += '<button class="rc-act-btn" onclick="TickerHub.changeSentiment('+n.id+',\''+nextSent+'\')" title="'+nextLabel+'">&#x1f3f7;</button>';
      html += '<button class="rc-act-btn" onclick="TickerHub.togglePin('+n.id+','+(n.pinned?'true':'false')+')" title="'+(n.pinned?'Unpin':'Pin')+'">&#x1f4cc;</button>';
      html += '<button class="rc-act-btn" onclick="TickerHub.deleteNote('+n.id+')" title="Delete">&times;</button>';
      html += '</span></div></div>';
    });
    feed.innerHTML = html;
  }

  var scoreBar = document.getElementById('rcScoreBar');
  if(scoreBar){
    var threads = (_HUB.research_threads || []).length;
    if(all > 0 || threads > 0){
      scoreBar.style.display = 'block';
      var score = Math.min(100, all * 8 + threads * 15);
      document.getElementById('rcScoreFill').style.width = score + '%';
      var parts = [];
      parts.push(all + ' note' + (all!==1?'s':''));
      parts.push(threads + ' thread' + (threads!==1?'s':''));
      if(sup) parts.push(sup + ' support' + (sup!==1?'s':''));
      if(chl) parts.push(chl + ' challenge' + (chl!==1?'s':''));
      document.getElementById('rcScoreLabel').textContent = parts.join(' \u00b7 ');
    } else {
      scoreBar.style.display = 'none';
    }
  }
}

function buildThinking(){
  var chains = _HUB.thinking_chains || [];
  var el = document.getElementById('thinkingList');
  if(!el) return;
  if(!chains.length){ el.innerHTML='<div class="th-empty">No thinking chains linked to $'+_TICKER+'.</div>'; return; }
  var html = '';
  chains.forEach(function(c){
    html += '<div class="th-list-item" onclick="window.open(\'/today?space=thinking&chain='+c.id+'\',\'_blank\')">';
    html += '<div class="emoji">'+(c.emoji||'\uD83D\uDCA1')+'</div>';
    html += '<div class="body">';
    html += '<div class="title">'+escH(c.title||'Untitled Chain')+'</div>';
    html += '<div class="sub">'+escH(c.chain_type||'chain')+' &middot; '+fmtDate(c.updated_at||c.created_at)+'</div>';
    if(c.notes) html += '<div class="detail">'+escH(String(c.notes).substring(0,200))+'</div>';
    html += '<div class="badges">';
    if(c.tags) html += '<span class="th-item-badge">'+escH(c.tags)+'</span>';
    if(c.status) html += '<span class="th-item-badge '+(c.status==='active'?'up':'')+'">'+escH(c.status)+'</span>';
    html += '</div></div></div>';
  });
  el.innerHTML = html;
}

function buildSignals(){
  var proposals = _HUB.proposals || [];
  var el = document.getElementById('signalsList');
  if(!el) return;
  if(!proposals.length){ el.innerHTML='<div class="th-empty">No AI proposals for $'+_TICKER+'.</div>'; return; }
  var html = '';
  proposals.forEach(function(p){
    var conf = p.confidence ? (Number(p.confidence)*100).toFixed(0)+'%' : '';
    var badgeCls = '';
    if(p.status==='approved') badgeCls='up';
    else if(p.status==='rejected'||p.status==='debate_rejected') badgeCls='down';
    else if(p.status==='pending') badgeCls='info';
    html += '<div class="th-list-item"><div class="emoji">\uD83D\uDCA1</div><div class="body">';
    html += '<div class="title">'+escH(p.headline||p.action_type||'Proposal')+'</div>';
    html += '<div class="sub">'+escH(p.action_type||'')+' &middot; '+fmtDate(p.created_at)+'</div>';
    if(p.reasoning) html += '<div class="detail">'+escH(String(p.reasoning).substring(0,250))+'</div>';
    html += '<div class="badges">';
    if(p.status) html += '<span class="th-item-badge '+badgeCls+'">'+escH(p.status)+'</span>';
    if(conf) html += '<span class="th-item-badge">Confidence: '+conf+'</span>';
    if(p.signal_type) html += '<span class="th-item-badge">'+escH(p.signal_type)+'</span>';
    html += '</div></div></div>';
  });
  el.innerHTML = html;
}

function _edgarIndexUrl(accession){
  if(!accession) return '';
  var nodash = accession.replace(/-/g,'');
  var cik = parseInt(nodash.substring(0,10), 10); // strip leading zeros
  return 'https://www.sec.gov/Archives/edgar/data/' + cik + '/' + nodash + '/';
}

var _filingsFilter = 'all';

function _renderFilingsList(filings, el){
  if(!filings.length){ el.innerHTML='<div class="th-empty">No filings match this filter.</div>'; return; }

  var _formLabels = {'10-K':'Annual Report','10-Q':'Quarterly Report','8-K':'Current Report','4':'Insider Transaction',
    'DEF 14A':'Proxy Statement','DEFA14A':'Additional Proxy','PRE 14A':'Preliminary Proxy',
    '6-K':'Foreign Report','20-F':'Foreign Annual','8-K/A':'Amended Current Report','10-K/A':'Amended Annual'};

  var html = '<table style="width:100%;border-collapse:separate;border-spacing:0 4px;font-size:13px;">';
  html += '<thead><tr style="font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;">';
  html += '<th style="text-align:left;padding:8px 12px;">Type</th>';
  html += '<th style="text-align:left;padding:8px 12px;">Date</th>';
  html += '<th style="text-align:left;padding:8px 12px;">Status</th>';
  html += '<th style="text-align:right;padding:8px 12px;"></th>';
  html += '</tr></thead><tbody>';

  filings.forEach(function(f){
    var edgarUrl = _edgarIndexUrl(f.accession);
    var form = f.form || 'Other';
    var label = _formLabels[form] || form;
    var formColor = form==='10-K'?'#6366f1':form==='10-Q'?'#3b82f6':form==='8-K'?'#f59e0b':form==='4'?'#8b5cf6':'#64748b';
    html += '<tr class="filing-row" style="cursor:pointer;background:var(--panel);border-radius:10px;transition:background .1s;" data-filing-ticker="'+escH(_TICKER)+'" data-filing-accession="'+escH(f.accession||'')+'">';
    html += '<td style="padding:10px 12px;border-radius:10px 0 0 10px;">';
    html += '<span style="display:inline-block;padding:3px 8px;border-radius:6px;font-size:11px;font-weight:700;background:'+formColor+'15;color:'+formColor+';">'+escH(form)+'</span>';
    html += '<div style="font-size:11px;color:var(--muted);margin-top:2px;">'+escH(label)+'</div>';
    html += '</td>';
    html += '<td style="padding:10px 12px;font-weight:600;">'+fmtDate(f.filing_date||f.processed_at)+'</td>';
    html += '<td style="padding:10px 12px;">';
    if(f.processed_at) html += '<span style="font-size:11px;font-weight:600;color:#059669;">Synced</span>';
    else html += '<span style="font-size:11px;font-weight:600;color:var(--muted);">Available</span>';
    html += '</td>';
    html += '<td style="padding:10px 12px;text-align:right;border-radius:0 10px 10px 0;">';
    if(edgarUrl) html += '<a href="'+escH(edgarUrl)+'" target="_blank" rel="noopener" onclick="event.stopPropagation();" style="font-size:11px;font-weight:600;color:var(--muted);text-decoration:none;" title="View on SEC.gov">SEC.gov ↗</a>';
    html += '</td></tr>';
  });
  html += '</tbody></table>';
  el.innerHTML = html;
}

function buildFilings(){
  var allFilings = _HUB.filings || [];
  var el = document.getElementById('filingsList');
  var filterBar = document.getElementById('filingsFilterBar');
  if(!el) return;
  if(!allFilings.length){ el.innerHTML='<div class="th-empty">No SEC filings tracked for $'+_TICKER+'.</div>'; if(filterBar) filterBar.innerHTML=''; return; }

  // Build filter buttons
  var formTypes = {};
  allFilings.forEach(function(f){ var fm = f.form||'Other'; formTypes[fm] = (formTypes[fm]||0)+1; });
  if(filterBar){
    var fhtml = '<button class="th-item-badge '+ (_filingsFilter==='all'?'info':'')+'" onclick="TickerHub._filterFilings(\'all\')" style="cursor:pointer;border:none;">All ('+allFilings.length+')</button>';
    Object.keys(formTypes).sort().forEach(function(fm){
      fhtml += '<button class="th-item-badge '+(_filingsFilter===fm?'info':'')+'" onclick="TickerHub._filterFilings(\''+escH(fm)+'\')" style="cursor:pointer;border:none;">'+escH(fm)+' ('+formTypes[fm]+')</button>';
    });
    filterBar.innerHTML = fhtml;
  }

  // Apply filter
  var filtered = _filingsFilter === 'all' ? allFilings : allFilings.filter(function(f){ return (f.form||'Other') === _filingsFilter; });
  _renderFilingsList(filtered, el);

  // Click handler: open full-screen filing viewer inside unified shell
  el.onclick = function(e){
    if(e.target.closest('a')) return;
    var item = e.target.closest('[data-filing-accession]');
    if(!item) return;
    var tk = item.getAttribute('data-filing-ticker') || '';
    var acc = item.getAttribute('data-filing-accession') || '';
    if(!acc) return;
    _openFilingViewer(tk, acc);
  };
}

function _openFilingViewer(tk, acc){
  var edgarUrl = _edgarIndexUrl(acc);

  // Remove any existing viewer
  var existing = document.getElementById('filingViewerOverlay');
  if(existing) existing.remove();

  // Backdrop
  var backdrop = document.createElement('div');
  backdrop.id = 'filingViewerOverlay';
  backdrop.style.cssText = 'position:fixed;inset:0;z-index:890;background:rgba(0,0,0,0.5);';

  // Panel — slides in from right, 65% width on desktop
  var panel = document.createElement('div');
  panel.style.cssText = 'position:fixed;top:0;right:0;bottom:0;width:min(65%,900px);z-index:900;background:#fff;display:flex;flex-direction:column;box-shadow:-8px 0 30px rgba(0,0,0,0.12);border-left:1px solid #e2e8f0;animation:fvSlideIn .2s ease-out;';

  // Add animation keyframes if not present
  if(!document.getElementById('fvAnimStyle')){
    var st = document.createElement('style');
    st.id = 'fvAnimStyle';
    st.textContent = '@keyframes fvSlideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}';
    document.head.appendChild(st);
  }

  // Header
  var hdr = document.createElement('div');
  hdr.style.cssText = 'display:flex;align-items:center;gap:8px;padding:12px 20px;background:#f8fafc;border-bottom:1px solid #e2e8f0;flex-shrink:0;';
  hdr.innerHTML = '<button id="fvClose" style="background:none;border:none;color:#64748b;cursor:pointer;font-size:18px;padding:4px 8px;border-radius:6px;" title="Close">\u2715</button>'
    +'<div style="flex:1;min-width:0;">'
    +'<div style="font-weight:700;color:#1e293b;font-size:14px;">SEC Filing</div>'
    +'<div style="font-size:11px;color:#94a3b8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'+escH(tk)+' \u00b7 '+escH(acc)+'</div>'
    +'</div>'
    +'<div style="display:flex;gap:4px;align-items:center;">'
    +'<button id="fvReader" style="background:#fff;border:1px solid #e2e8f0;color:#475569;border-radius:8px;padding:6px 14px;cursor:pointer;font-size:11px;font-weight:700;">Reader</button>'
    +'<button id="fvOriginal" style="background:#6366f1;color:#fff;border:none;border-radius:8px;padding:6px 14px;cursor:pointer;font-size:11px;font-weight:700;">Original</button>'
    +'</div>'
    +'<div style="width:1px;height:20px;background:#e2e8f0;margin:0 4px;"></div>'
    +'<button id="fvExpand" style="background:#fff;border:1px solid #e2e8f0;color:#475569;border-radius:8px;padding:6px 14px;cursor:pointer;font-size:11px;font-weight:700;" title="Open full-screen view">\u26f6 Full View</button>'
    +(edgarUrl ? '<a href="'+escH(edgarUrl)+'" target="_blank" rel="noopener" style="background:#fff;border:1px solid #e2e8f0;color:#475569;border-radius:8px;padding:6px 14px;cursor:pointer;font-size:11px;font-weight:700;text-decoration:none;" title="View on SEC.gov">SEC.gov \u2192</a>' : '');

  var body = document.createElement('div');
  body.id = 'fvBody';
  body.style.cssText = 'flex:1;overflow-y:auto;padding:0;';
  body.innerHTML = '<div style="padding:40px;text-align:center;color:var(--muted,#8b949e);font-size:13px;">Loading filing\u2026</div>';

  panel.appendChild(hdr);
  panel.appendChild(body);
  document.body.appendChild(backdrop);
  document.body.appendChild(panel);

  // Close handlers
  var closeViewer = function(){
    panel.remove();
    backdrop.remove();
    document.removeEventListener('keydown', escHandler);
  };
  document.getElementById('fvClose').onclick = closeViewer;
  backdrop.onclick = closeViewer;
  var escHandler = function(ev){ if(ev.key==='Escape') closeViewer(); };
  document.addEventListener('keydown', escHandler);

  // Full View — expand to full-screen
  document.getElementById('fvExpand').onclick = function(){
    panel.style.width = '100%';
    panel.style.borderLeft = 'none';
    this.textContent = '\u25a3 Exit Full';
    var isExpanded = true;
    this.onclick = function(){
      if(isExpanded){
        panel.style.width = 'min(65%,900px)';
        panel.style.borderLeft = '1px solid var(--border,#30363d)';
        this.textContent = '\u26f6 Full View';
        isExpanded = false;
      } else {
        panel.style.width = '100%';
        panel.style.borderLeft = 'none';
        this.textContent = '\u25a3 Exit Full';
        isExpanded = true;
      }
    };
  };

  // Build EDGAR URL so user can always jump to SEC.gov instantly
  var acc_nodash = acc.replace(/-/g,'');
  var cik = String(parseInt(acc_nodash.substring(0,10)));
  var secUrl = 'https://www.sec.gov/Archives/edgar/data/'+cik+'/'+acc_nodash+'/';

  // Fetch filing content from API
  fetch('/api/filing/content?ticker='+encodeURIComponent(tk)+'&accession='+encodeURIComponent(acc))
    .then(function(r){ return r.json(); })
    .then(function(d){
      d._ticker = tk;
      d._accession = acc;
      // Default: Original mode
      _renderOriginalMode(d, body, edgarUrl);

      // Mode toggle
      var _curMode = 'original';
      document.getElementById('fvReader').onclick = function(){
        if(_curMode==='reader') return;
        _curMode='reader';
        this.style.background='#6366f1'; this.style.color='#fff'; this.style.border='none';
        var ob = document.getElementById('fvOriginal'); ob.style.background='#fff'; ob.style.color='#475569'; ob.style.border='1px solid #e2e8f0';
        _renderReaderMode(d, body);
      };
      document.getElementById('fvOriginal').onclick = function(){
        if(_curMode==='original') return;
        _curMode='original';
        this.style.background='#6366f1'; this.style.color='#fff'; this.style.border='none';
        var rb = document.getElementById('fvReader'); rb.style.background='#fff'; rb.style.color='#475569'; rb.style.border='1px solid #e2e8f0';
        _renderOriginalMode(d, body, edgarUrl);
      };
    })
    .catch(function(){
      body.innerHTML = '<div style="padding:40px;text-align:center;color:#ef4444;">Failed to load filing content.</div>';
    });
}

function _renderReaderMode(d, body){
  var forceStyle = '<style>.fv-reader-content,.fv-reader-content *{color:#1e293b !important;border-color:#e2e8f0 !important;background-color:transparent !important;}'
    +'.fv-reader-content a{color:#6366f1 !important;}'
    +'.fv-reader-content table{border-collapse:collapse;width:100%;margin:12px 0;border-radius:8px;overflow:hidden;}'
    +'.fv-reader-content td,.fv-reader-content th{padding:8px 12px;border:1px solid #e2e8f0 !important;font-size:12px;}'
    +'.fv-reader-content th{background:#f8fafc !important;font-weight:700;}'
    +'.fv-reader-content h1,.fv-reader-content h2,.fv-reader-content h3,.fv-reader-content h4{color:#0f172a !important;margin:20px 0 10px;font-weight:800;}'
    +'.fv-reader-content p{margin:8px 0;line-height:1.7;}'
    +'.fv-reader-content img{max-width:100%;height:auto;}'
    +'.fv-reader-content hr{border:none;border-top:1px solid #e2e8f0;margin:16px 0;}'
    +'</style>';
  var content = d.content || '';
  if(d.is_html){
    // Clean HTML: remove scripts, then render as styled HTML
    var cleaned = content.replace(/<script[^>]*>[\s\S]*?<\/script>/gi, '');
    body.innerHTML = '<div style="padding:24px;min-height:100%;background:#fff;">'+forceStyle
      +'<div class="fv-reader-content" style="max-width:800px;margin:0 auto;font-size:13px;line-height:1.7;">'+cleaned+'</div></div>';
  } else {
    var rendered = _simpleMarkdown(content);
    body.innerHTML = '<div style="padding:24px;min-height:100%;background:#fff;">'+forceStyle
      +'<div class="fv-reader-content" style="max-width:800px;margin:0 auto;font-size:13px;line-height:1.7;">'+rendered+'</div></div>';
  }
}

function _simpleMarkdown(txt){
  // Basic markdown to HTML for filing reader mode
  var lines = escH(txt).split('\n');
  var out = [];
  var inTable = false;
  for(var i=0; i<lines.length; i++){
    var line = lines[i];
    // Headers
    if(line.match(/^#{4}\s/)) { out.push('<h4>'+line.replace(/^#{4}\s+/,'')+'</h4>'); continue; }
    if(line.match(/^#{3}\s/)) { out.push('<h3>'+line.replace(/^#{3}\s+/,'')+'</h3>'); continue; }
    if(line.match(/^#{2}\s/)) { out.push('<h2>'+line.replace(/^#{2}\s+/,'')+'</h2>'); continue; }
    if(line.match(/^#{1}\s/)) { out.push('<h1>'+line.replace(/^#{1}\s+/,'')+'</h1>'); continue; }
    // Horizontal rule
    if(line.match(/^-{3,}$/)||line.match(/^\*{3,}$/)) { out.push('<hr style="border:none;border-top:1px solid #30363d;margin:12px 0;">'); continue; }
    // Table rows (pipe-delimited)
    if(line.indexOf('|') !== -1 && line.trim().charAt(0)==='|'){
      var cells = line.split('|').filter(function(c,idx,arr){ return idx>0 && idx<arr.length-1; });
      // Skip separator rows (|---|---|)
      if(cells.every(function(c){ return c.trim().match(/^[-:]+$/); })){ continue; }
      if(!inTable){ out.push('<table>'); inTable=true; }
      out.push('<tr>'+cells.map(function(c){ return '<td>'+c.trim()+'</td>'; }).join('')+'</tr>');
      continue;
    } else if(inTable){ out.push('</table>'); inTable=false; }
    // Bold
    line = line.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // Empty line = paragraph break
    if(line.trim()==='') { out.push('<br>'); continue; }
    out.push('<div>'+line+'</div>');
  }
  if(inTable) out.push('</table>');
  return out.join('\n');
}

function _renderOriginalMode(d, body, edgarUrl){
  // Original = fetch the actual SEC filing HTML from EDGAR (not stored markdown)
  var tk = d._ticker || _TICKER || '';
  var acc = d._accession || '';

  // Show loading while fetching from SEC
  body.innerHTML = '<div style="padding:40px;text-align:center;color:var(--muted,#8b949e);font-size:13px;">Fetching original SEC filing\u2026</div>';

  fetch('/api/filing/original-html?ticker='+encodeURIComponent(tk)+'&accession='+encodeURIComponent(acc))
    .then(function(r){ return r.json(); })
    .then(function(result){
      if(!result.ok){
        // Fallback: use stored content if available
        _renderOriginalFallback(d, body, edgarUrl, result.error);
        return;
      }
      var filingHtml = result.html || '';
      // Strip any <script> tags to prevent XBRL viewer alerts
      filingHtml = filingHtml.replace(/<script[^>]*>[\s\S]*?<\/script>/gi, '');
      var iframe = document.createElement('iframe');
      iframe.style.cssText = 'width:100%;height:100%;border:none;background:#fff;border-radius:4px;';
      iframe.sandbox = 'allow-same-origin';
      body.innerHTML = '';
      body.appendChild(iframe);
      iframe.srcdoc = filingHtml;
    })
    .catch(function(){
      _renderOriginalFallback(d, body, edgarUrl, 'Network error');
    });
}

function _renderOriginalFallback(d, body, edgarUrl, errMsg){
  var content = d.content || '';
  if(d.is_html && content){
    var cleaned = content.replace(/<script[^>]*>[\s\S]*?<\/script>/gi, '');
    var doc = '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
      +'body{font-family:Times New Roman,serif;font-size:14px;line-height:1.5;padding:30px 40px;margin:0;background:#fff;color:#000;}'
      +'table{border-collapse:collapse;margin:10px 0;width:100%;}td,th{padding:4px 8px;border:1px solid #ccc;vertical-align:top;}'
      +'a{color:#0056b3;}img{max-width:100%;}'
      +'</style></head><body>'+cleaned+'</body></html>';
    var iframe = document.createElement('iframe');
    iframe.style.cssText = 'width:100%;height:100%;border:none;background:#fff;border-radius:4px;';
    iframe.sandbox = 'allow-same-origin';
    body.innerHTML = '';
    body.appendChild(iframe);
    iframe.srcdoc = doc;
  } else if(content){
    var doc = '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
      +'body{font-family:Courier New,monospace;font-size:13px;line-height:1.6;padding:40px 50px;margin:0;background:#fff;color:#000;white-space:pre-wrap;word-break:break-word;}'
      +'</style></head><body>'+escH(content)+'</body></html>';
    var iframe = document.createElement('iframe');
    iframe.style.cssText = 'width:100%;height:100%;border:none;background:#fff;border-radius:4px;';
    iframe.sandbox = 'allow-same-origin';
    body.innerHTML = '';
    body.appendChild(iframe);
    iframe.srcdoc = doc;
  } else {
    var eu = edgarUrl || d.edgar_url || '';
    body.innerHTML = '<div style="padding:60px 40px;text-align:center;">'
      +'<div style="font-size:40px;margin-bottom:16px;opacity:0.3;">&#128196;</div>'
      +'<div style="color:var(--text,#c9d1d9);font-size:15px;font-weight:600;margin-bottom:8px;">Could not load original filing</div>'
      +'<div style="color:var(--muted,#8b949e);font-size:13px;margin-bottom:20px;">'+(errMsg||'View it directly on SEC.gov.')+'</div>'
      +(eu ? '<a href="'+escH(eu)+'" target="_blank" rel="noopener" style="display:inline-block;background:var(--ws-accent,#5b7ff5);color:#fff;font-size:14px;font-weight:600;padding:10px 24px;border-radius:8px;text-decoration:none;">View on SEC.gov \u2192</a>' : '')
      +'</div>';
  }
}

function _filterFilings(form){
  _filingsFilter = form;
  buildFilings();
}

function buildEarnings(){
  var h = _HUB;
  var brief = h.earnings_sec_brief || {};
  var briefEl = document.getElementById('earningsBriefBox');
  if(briefEl && (brief.next_announced_date || brief.last_release_date || brief.last_result)){
    var bhtml = '<div class="th-card full" style="border-left:4px solid var(--info);">';
    bhtml += '<div class="th-card-label">Earnings Brief</div>';
    bhtml += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px;">';
    bhtml += '<span class="th-item-badge info">Next: '+ escH(brief.next_announced_date||'Not found')+'</span>';
    bhtml += '<span class="th-item-badge">Last: '+ escH(brief.last_release_date||'-')+'</span>';
    bhtml += '<span class="th-item-badge">Result: '+ escH(brief.last_result||'Unknown')+'</span></div>';
    if(brief.brief) bhtml += '<div style="font-size:12px;color:var(--muted);">'+escH(brief.brief)+'</div>';
    bhtml += '</div>';
    briefEl.innerHTML = bhtml;
  }

  var earnings = h.earnings || [];
  var el = document.getElementById('earningsList');
  if(el){
    if(!earnings.length){ el.innerHTML='<div class="th-empty">No AI earnings analysis for $'+_TICKER+'.</div>'; }
    else {
      var html = '';
      earnings.forEach(function(e){
        var dirCls = '';
        if(e.guidance_direction==='raised') dirCls='up';
        else if(e.guidance_direction==='lowered') dirCls='down';
        else if(e.guidance_direction==='maintained') dirCls='info';
        html += '<div class="th-list-item"><div class="emoji">\uD83D\uDCCA</div><div class="body">';
        html += '<div class="title">'+escH(e.quarter||'')+' '+escH(e.year||'')+' Earnings</div>';
        html += '<div class="sub">'+fmtDate(e.created_at)+'</div>';
        if(e.summary) html += '<div class="detail">'+escH(String(e.summary).substring(0,300))+'</div>';
        html += '<div class="badges">';
        if(e.guidance_direction) html += '<span class="th-item-badge '+dirCls+'">Guidance: '+escH(e.guidance_direction)+'</span>';
        if(e.sentiment) html += '<span class="th-item-badge">Sentiment: '+escH(e.sentiment)+'</span>';
        html += '</div></div></div>';
      });
      el.innerHTML = html;
    }
  }

  // Earnings Calls
  var calls = h.earnings_calls || [];
  var callsEl = document.getElementById('earningsCallsList');
  if(callsEl){
    if(!calls.length){ callsEl.innerHTML='<div class="th-empty">No earnings call transcripts indexed.</div>'; }
    else {
      var html = '';
      calls.slice(0,12).forEach(function(tr){
        html += '<div class="th-list-item" style="cursor:default;"><div class="emoji">\uD83C\uDFA7</div><div class="body">';
        html += '<div class="title">'+escH(tr.title||'Earnings Call')+'</div>';
        html += '<div class="sub">'+escH(tr.call_date||'-');
        if(tr.fiscal_year && tr.fiscal_quarter) html += ' \u00b7 '+escH(tr.fiscal_year)+' '+escH(tr.fiscal_quarter);
        if(tr.char_count) html += ' \u00b7 '+escH(tr.char_count)+' chars';
        html += '</div>';
        if(tr.excerpt){
          html += '<details class="detail" style="margin-top:4px;"><summary style="cursor:pointer;">'+escH(String(tr.excerpt).substring(0,150))+'... <span style="font-size:11px;font-weight:700;">Read More</span></summary>';
          html += '<div style="margin-top:6px;white-space:pre-wrap;">'+escH(tr.excerpt)+'</div></details>';
        }
        if(tr.source_url) html += '<div class="badges"><a class="th-item-badge info" href="'+escH(tr.source_url)+'" target="_blank" style="text-decoration:none;">Source</a></div>';
        html += '</div></div>';
      });
      callsEl.innerHTML = html;
    }
  }

  // Earnings Releases
  var rels = h.earnings_releases || [];
  var relsEl = document.getElementById('earningsReleasesList');
  if(relsEl){
    if(!rels.length){ relsEl.innerHTML='<div class="th-empty">No SEC earnings releases extracted.</div>'; }
    else {
      var html = '';
      rels.slice(0,12).forEach(function(tr){
        html += '<div class="th-list-item" style="cursor:default;"><div class="emoji">\uD83E\uDDFE</div><div class="body">';
        html += '<div class="title">'+escH(tr.title||'Earnings Release')+'</div>';
        html += '<div class="sub">'+escH(tr.call_date||'-');
        if(tr.form) html += ' \u00b7 '+escH(tr.form);
        if(tr.char_count) html += ' \u00b7 '+escH(tr.char_count)+' chars';
        html += '</div>';
        if(tr.excerpt){
          html += '<details class="detail" style="margin-top:4px;"><summary style="cursor:pointer;">'+escH(String(tr.excerpt).substring(0,150))+'... <span style="font-size:11px;font-weight:700;">Read More</span></summary>';
          html += '<div style="margin-top:6px;white-space:pre-wrap;">'+escH(tr.excerpt)+'</div></details>';
        }
        if(tr.source_url) html += '<div class="badges"><a class="th-item-badge info" href="'+escH(tr.source_url)+'" target="_blank" style="text-decoration:none;">Source</a></div>';
        html += '</div></div>';
      });
      relsEl.innerHTML = html;
    }
  }

  // Quarterly signals
  var qsig = h.quarterly_signals || [];
  var qsigEl = document.getElementById('quarterlySignalsList');
  if(qsigEl){
    if(!qsig.length){ qsigEl.innerHTML='<div class="th-empty">No quarterly result signals yet.</div>'; }
    else {
      var html = '';
      qsig.slice(0,8).forEach(function(s){
        html += '<div class="th-list-item" style="cursor:default;"><div class="emoji">\uD83D\uDCCC</div><div class="body">';
        html += '<div class="title">'+escH(String(s.text||'').substring(0,150))+'</div>';
        html += '<div class="sub">'+escH(s.date||'-');
        if(s.source) html += ' \u00b7 '+escH(String(s.source).substring(0,80));
        html += '</div></div></div>';
      });
      qsigEl.innerHTML = html;
    }
  }
}

function buildHistory(){
  var records = _HUB.records || [];
  var el = document.getElementById('historyList');
  if(!el) return;
  if(!records.length){ el.innerHTML='<div class="th-empty">No records for $'+_TICKER+'. Add your first note above.</div>'; return; }
  var html = '';
  records.forEach(function(r){
    var kind = r.kind||'note';
    var icon = KIND_ICON[kind]||'\uD83D\uDCDD';
    html += '<div class="th-list-item" id="threc-'+r.id+'"><div class="emoji">'+icon+'</div><div class="body">';
    html += '<div class="title">'+escH(r.title||'(untitled)')+'</div>';
    html += '<div class="sub">'+escH(kind)+' &middot; '+fmtDate(r.created_at)+'</div>';
    if(r.body||r.content) html += '<div class="detail">'+escH(String(r.body||r.content||'').substring(0,200))+'</div>';
    html += '<div class="badges">';
    var sCls = r.status==='done'?'up':(r.status==='open'?'info':'');
    html += '<span class="th-item-badge '+sCls+'">'+escH(r.status||'open')+'</span>';
    html += ' <button class="th-item-badge" style="cursor:pointer;border-color:var(--up);" onclick="event.stopPropagation();TickerHub.thRecAction('+r.id+',\'done\',this)">Done</button>';
    html += ' <button class="th-item-badge" style="cursor:pointer;color:var(--down-ink);" onclick="event.stopPropagation();TickerHub.thRecAction('+r.id+',\'delete\',this)">&times;</button>';
    html += '</div></div></div>';
  });
  el.innerHTML = html;
}

function buildConnections(){
  var graph = _HUB.work_graph || [];
  var el = document.getElementById('connectionsList');
  if(!el) return;
  if(!graph.length){ el.innerHTML='<div class="th-empty">No connections in work graph for $'+_TICKER+'.</div>'; return; }
  var html = '';
  var TYPE_URL = {
    thread: function(id){ return '/today?space=research&thread='+id; },
    chain: function(id){ return '/today?space=thinking&chain='+id; },
    record: function(){ return '#'; },
    proposal: function(){ return '#'; },
    playbook: function(){ return '/today?space=projects'; },
    playbook_run: function(){ return '/today?space=projects'; },
    ticker: function(id){ return 'javascript:void(0)'; },
  };
  graph.forEach(function(g){
    var ot = g.other_type||'';
    var oid = g.other_id||'';
    var title = g.other_title || (ot==='ticker' ? '$'+oid : ot+' #'+oid);
    var emoji = g.other_emoji || '';
    var rel = g.relation || 'related';
    var url = (TYPE_URL[ot]||function(){return '#';})(oid);
    var onclick = ot==='ticker' ? ' onclick="event.preventDefault();if(typeof openTicker===\'function\')openTicker(\''+escH(oid)+'\');else window.location.href=\'/ticker/'+escH(oid)+'\';"' : '';
    html += '<a class="th-conn-node" href="'+url+'"'+onclick+'>';
    html += '<span class="conn-emoji">'+escH(emoji)+'</span>';
    html += '<span>'+escH(title)+'</span>';
    html += '<span class="conn-type">'+escH(ot)+'</span>';
    if(rel!=='related') html += '<span class="conn-type" style="background:var(--info-soft);">'+escH(rel)+'</span>';
    html += '</a>';
  });
  el.innerHTML = html;
}

function buildSparkSVG(vals){
  var nums = vals.filter(function(v){ return v !== null && v !== undefined && !isNaN(Number(v)); }).map(Number);
  if(nums.length < 2) return '<svg width="82" height="20"></svg>';
  var min = Math.min.apply(null,nums), max = Math.max.apply(null,nums);
  var span = Math.max(1e-9, max-min), w=82, h=20;
  var pts = nums.map(function(v,i){
    var x = (i*(w-2))/Math.max(1,nums.length-1)+1;
    var y = (h-2)-((v-min)/span)*(h-4)+1;
    return x.toFixed(1)+' '+y.toFixed(1);
  });
  var color = nums[nums.length-1] >= nums[0] ? '#2563eb' : 'var(--down, #ef4444)';
  return '<svg width="82" height="20" viewBox="0 0 82 20" preserveAspectRatio="none"><path d="M '+pts.join(' L ')+'" fill="none" stroke="'+color+'" stroke-width="1.8" vector-effect="non-scaling-stroke"/></svg>';
}

function buildDonutCSS(rows){
  var palette = ['#1e3a8a','#334155','#6366f1','#0f766e','#475569','#4338ca'];
  var pcts = (rows||[]).map(function(r){ var v=parseFloat(String((r&&r.pct)||0)); return isNaN(v)?0:Math.max(0,v); });
  var total = pcts.reduce(function(a,b){ return a+b; },0);
  if(total <= 0) return 'background:var(--border)';
  var cum=0, parts=[];
  pcts.slice(0,6).forEach(function(v,i){ var s=cum; cum+=v; parts.push(palette[i%palette.length]+' '+s+'% '+cum+'%'); });
  if(cum<100) parts.push('#cbd5e1 '+cum+'% 100%');
  return 'background:conic-gradient('+parts.join(',')+')';
}

function buildFundamentals(){
  var h = _HUB;
  var pm = h.price_metrics || {};
  var pmEl = document.getElementById('priceMetricsGrid');
  if(pmEl){
    if(pm && Object.keys(pm).length > 0){
      var html = '';
      var items = [
        {l:'Market Cap', v:pm.market_cap},
        {l:'Current Price', v:pm.current_price ? '$'+Number(pm.current_price).toFixed(2) : ''},
        {l:'YTD Return', v:pm.ytd_return ? Number(pm.ytd_return).toFixed(1)+'%' : ''},
        {l:'12M Return', v:pm.m12_return ? Number(pm.m12_return).toFixed(1)+'%' : ''},
        {l:'5Y Return', v:pm.y5_return ? Number(pm.y5_return).toFixed(1)+'%' : ''},
        {l:'52W High', v:pm.high_52w ? '$'+Number(pm.high_52w).toFixed(2) : ''},
        {l:'52W Low', v:pm.low_52w ? '$'+Number(pm.low_52w).toFixed(2) : ''},
        {l:'P/E Ratio', v:pm.pe_ratio},
        {l:'Div Yield', v:pm.div_yield ? Number(pm.div_yield).toFixed(2)+'%' : ''},
      ];
      items.forEach(function(i){
        if(!i.v) return;
        html += '<div class="th-card"><div class="th-card-label">'+i.l+'</div><div class="th-card-body" style="font-size:16px;font-weight:700;">'+escH(i.v)+'</div></div>';
      });
      pmEl.innerHTML = html || '<div class="th-empty">No price metrics available.</div>';
    } else {
      pmEl.innerHTML = '<div class="th-empty">No price metrics available.</div>';
    }
  }

  var fd = h.financial_deltas || {};
  var deltaEl = document.getElementById('miniStatsDeltaSummary');
  if(deltaEl && fd && fd.ok && fd.metrics){
    var m = fd.metrics;
    var parts = [];
    if(m.revenue && m.revenue.delta && m.revenue.delta.pct != null) parts.push('Revenue '+fmtDelta(m.revenue.delta.pct));
    if(m.free_cash_flow && m.free_cash_flow.delta && m.free_cash_flow.delta.pct != null) parts.push('FCF '+fmtDelta(m.free_cash_flow.delta.pct));
    if(m.total_debt && m.total_debt.delta && m.total_debt.delta.pct != null) parts.push('Debt '+fmtDelta(m.total_debt.delta.pct));
    if(parts.length){
      var yr = '';
      if(m.revenue && m.revenue.prev_year && m.revenue.cur_year) yr = 'YoY ('+m.revenue.prev_year+'\u2192'+m.revenue.cur_year+'): ';
      deltaEl.textContent = yr + parts.join(', ');
    }
  }

  var ms = h.mini_statements || {};
  var msEl = document.getElementById('miniStatementsBox');
  if(msEl){
    if(ms && ms.years && ms.years.length > 0){
      var tbl = '<div style="overflow-x:auto;"><table style="width:100%;font-size:12px;border-collapse:collapse;">';
      tbl += '<tr style="border-bottom:2px solid var(--border);"><th style="text-align:left;padding:4px 8px;">Metric</th>';
      ms.years.forEach(function(y){ tbl += '<th style="text-align:right;padding:4px 8px;">'+escH(y)+'</th>'; });
      tbl += '<th style="text-align:right;padding:4px 8px;width:88px;">Trend</th></tr>';
      var rows = [
        {k:'revenue', l:'Revenue'}, {k:'gross_profit', l:'Gross Profit'},
        {k:'operating_cash_flow', l:'Op. Cash Flow'}, {k:'capex', l:'CapEx'},
        {k:'free_cash_flow', l:'Free Cash Flow'}, {k:'total_cash', l:'Cash'},
        {k:'total_debt', l:'Total Debt'}, {k:'total_equity', l:'Total Equity'},
      ];
      rows.forEach(function(r){
        var vals = ms[r.k] || [];
        if(!vals.length) return;
        tbl += '<tr style="border-bottom:1px solid var(--border);"><td style="padding:4px 8px;font-weight:600;">'+r.l+'</td>';
        vals.forEach(function(v){
          var fv = v !== null && v !== undefined ? fmtMoney(v) : '-';
          tbl += '<td style="text-align:right;padding:4px 8px;">'+fv+'</td>';
        });
        tbl += '<td style="text-align:right;padding:4px 8px;">'+buildSparkSVG(vals)+'</td></tr>';
      });
      tbl += '</table></div>';
      if(ms.source || ms.asof) tbl += '<div style="font-size:11px;color:var(--muted);margin-top:6px;">Provenance: '+(ms.source||'-')+' \u00b7 asof '+(ms.asof||'-')+'</div>';
      msEl.innerHTML = tbl;
    } else {
      msEl.innerHTML = '<div class="th-empty">No financial statements available. Click Refresh Financials above.</div>';
    }
  }

  // Revenue segments
  var rs = h.revenue_segments || {};
  var rsEl = document.getElementById('revenueSegmentsBox');
  if(rsEl){
    var product = rs.product || []; var geo = rs.geography || [];
    if(product.length || geo.length){
      var html = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;">';
      html += '<div><div style="font-size:12px;font-weight:700;color:var(--text);margin-bottom:6px;">Product Mix</div>';
      if(product.length){
        html += '<div style="width:132px;height:132px;border-radius:999px;margin:2px auto 8px auto;border:none;box-shadow:inset 0 0 0 16px var(--panel);'+buildDonutCSS(product)+'"></div>';
        product.slice(0,6).forEach(function(r){
          html += '<div style="display:flex;justify-content:space-between;gap:8px;font-size:12px;margin-bottom:4px;"><span style="color:var(--text);max-width:72%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'+escH(r.label||'-')+'</span><span style="font-weight:700;color:var(--text);">'+(Number(r.pct||0)).toFixed(1)+'%</span></div>';
        });
      } else { html += '<div class="th-empty" style="padding:8px 0;">No product segmentation.</div>'; }
      html += '</div><div><div style="font-size:12px;font-weight:700;color:var(--text);margin-bottom:6px;">Geographic Mix</div>';
      if(geo.length){
        html += '<div style="width:132px;height:132px;border-radius:999px;margin:2px auto 8px auto;border:none;box-shadow:inset 0 0 0 16px var(--panel);'+buildDonutCSS(geo)+'"></div>';
        geo.slice(0,6).forEach(function(r){
          html += '<div style="display:flex;justify-content:space-between;gap:8px;font-size:12px;margin-bottom:4px;"><span style="color:var(--text);max-width:72%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'+escH(r.label||'-')+'</span><span style="font-weight:700;color:var(--text);">'+(Number(r.pct||0)).toFixed(1)+'%</span></div>';
        });
      } else { html += '<div class="th-empty" style="padding:8px 0;">No geographic segmentation.</div>'; }
      html += '</div></div>';
      rsEl.innerHTML = html;
    } else {
      rsEl.innerHTML = '<div class="th-empty">No segment data available. Click Refresh Intel above.</div>';
    }
  }
}

function buildMoats(){
  var h = _HUB;
  var comps = h.competitors || [];
  var compEl = document.getElementById('competitorsList');
  if(compEl){
    if(comps.length){
      var html = '';
      comps.forEach(function(c){
        html += '<div class="th-list-item" onclick="if(typeof openTicker===\'function\')openTicker(\''+escH(c.ticker||'')+'\');else window.location.href=\'/ticker/'+escH(c.ticker||'')+'\';">';
        html += '<div class="emoji">\u2694\uFE0F</div><div class="body">';
        html += '<div class="title">'+escH(c.name||c.ticker||'Unknown')+'</div>';
        html += '<div class="sub">$'+escH(c.ticker||'')+' &middot; '+escH(c.date||'')+'</div>';
        if(c.evidence) html += '<div class="detail">'+escH(String(c.evidence).substring(0,200))+'</div>';
        html += '</div></div>';
      });
      compEl.innerHTML = html;
    } else {
      compEl.innerHTML = '<div class="th-empty">No competitors identified.</div>';
    }
  }

  var sims = h.similar_companies || [];
  var simEl = document.getElementById('similarList');
  if(simEl){
    if(sims.length){
      var html = '';
      sims.forEach(function(s){
        html += '<div style="display:flex;align-items:center;gap:8px;border:none;border-radius:14px;padding:10px 12px;background:var(--panel);margin-bottom:6px;box-shadow:0 1px 3px rgba(0,0,0,.04);">';
        html += '<a href="/ticker/'+escH(s.ticker||'')+'" style="text-decoration:none;color:var(--text);font-weight:700;font-size:13px;">'+escH(s.ticker||'-')+'</a>';
        html += '<span style="color:var(--muted);font-size:12px;">'+escH(s.name||'')+' \u00b7 '+escH(s.industry||'')+' \u00b7 '+escH(s.market_cap||'')+'</span>';
        html += '<button class="btn" style="margin-left:auto;font-size:11px;padding:3px 8px;" onclick="TickerHub.addToWatchlist(\''+escH(s.ticker||'')+'\',this)">+ Watchlist</button></div>';
      });
      simEl.innerHTML = html;
    } else {
      simEl.innerHTML = '<div class="th-empty">No similar companies found.</div>';
    }
  }
}

function buildSupplyChain(){
  var sc = _HUB.supply_chain || {};
  var el = document.getElementById('supplyChainBox');
  if(!el) return;
  var suppliers = sc.suppliers || [];
  var customers = sc.customers || [];
  var partners = sc.partners || [];
  if(!suppliers.length && !customers.length && !partners.length){
    el.innerHTML = '<div class="th-empty">No supply chain data for $'+_TICKER+'.</div>';
    return;
  }
  function renderGroup(label, items, emoji){
    if(!items.length) return '';
    var h = '<div class="th-section-head" style="margin-top:10px;">'+label+' <span class="count">'+items.length+'</span></div>';
    items.forEach(function(s){
      h += '<div class="th-list-item" style="cursor:default;"><div class="emoji">'+emoji+'</div><div class="body" style="flex:1;">';
      h += '<div class="title"><a href="/ticker/'+escH(s.ticker||'')+'" style="text-decoration:none;color:inherit;">'+escH(s.ticker||'-')+'</a> <span style="color:var(--muted);font-weight:400;">\u00b7 '+escH(s.name||'-')+'</span></div>';
      h += '<div class="sub">'+escH(s.industry||'-')+' \u00b7 '+escH(s.market_cap||'-')+'</div>';
      if(s.evidence) h += '<div class="detail">'+escH(String(s.evidence).substring(0,200))+'</div>';
      h += '<div class="badges"><span class="th-item-badge info">Conf '+(Number(s.confidence||0)*100).toFixed(0)+'%</span>';
      if(s.source) h += '<span class="th-item-badge">'+escH(s.source)+'</span>';
      if(String(s.source)==='manual' && s.id) h += ' <button class="th-item-badge" style="cursor:pointer;color:var(--down-ink);" onclick="event.stopPropagation();TickerHub.removeSupplyLink('+s.id+')">\u00d7 Remove</button>';
      h += '</div></div></div>';
    });
    return h;
  }
  var html = '';
  html += renderGroup('Suppliers', suppliers, '\uD83D\uDCE6');
  html += renderGroup('Customers', customers, '\uD83C\uDFE2');
  html += renderGroup('Partners', partners, '\uD83E\uDD1D');
  el.innerHTML = html;
}

function buildNotes(){
  var h = _HUB;
  var notes = h.notes || [];
  var notesEl = document.getElementById('notesList');
  setText('cntNotes', notes.length + (h.tasks||[]).length + (h.reminders||[]).length);
  if(notesEl){
    if(notes.length){
      var html = '';
      notes.forEach(function(n){
        html += '<div class="th-list-item"><div class="emoji">\uD83D\uDCDD</div><div class="body">';
        html += '<div class="title">'+escH(n.action||'Note')+'</div>';
        html += '<div class="sub">'+fmtDate(n.created_at)+'</div>';
        html += '<div class="detail">'+escH(String(n.note||'').substring(0,300))+'</div>';
        html += '</div></div>';
      });
      notesEl.innerHTML = html;
    } else {
      notesEl.innerHTML = '<div class="th-empty">No notes.</div>';
    }
  }
  var tasks = h.tasks || [];
  var tasksEl = document.getElementById('tasksList');
  if(tasksEl){
    if(tasks.length){
      var html = '';
      tasks.forEach(function(t){
        var sCls = t.status==='done'?'up':(t.status==='open'?'info':'');
        html += '<div class="th-list-item"><div class="emoji">\u2705</div><div class="body">';
        html += '<div class="title">'+escH(t.task)+'</div>';
        html += '<div class="sub">'+escH(t.priority||'')+' &middot; '+fmtDate(t.created_at)+'</div>';
        html += '<div class="badges"><span class="th-item-badge '+sCls+'">'+escH(t.status||'open')+'</span></div>';
        html += '</div></div>';
      });
      tasksEl.innerHTML = html;
    } else {
      tasksEl.innerHTML = '<div class="th-empty">No tasks.</div>';
    }
  }
  var rems = h.reminders || [];
  var remsEl = document.getElementById('remindersList');
  if(remsEl){
    if(rems.length){
      var html = '';
      rems.forEach(function(r){
        html += '<div class="th-list-item"><div class="emoji">\u23F0</div><div class="body">';
        html += '<div class="title">'+escH(r.note||'Reminder')+'</div>';
        html += '<div class="sub">'+escH(r.remind_at||'')+'</div>';
        html += '<div class="badges"><span class="th-item-badge '+(r.status==='open'?'info':'')+'">'+escH(r.status||'open')+'</span></div>';
        html += '</div></div>';
      });
      remsEl.innerHTML = html;
    } else {
      remsEl.innerHTML = '<div class="th-empty">No reminders.</div>';
    }
  }
}

function buildTransactions(){
  var h = _HUB;
  var txns = h.transactions || [];
  var txEl = document.getElementById('transactionsList');
  setText('cntTransactions', txns.length);
  if(txEl){
    if(txns.length){
      var html = '';
      txns.forEach(function(t){
        var isBuy = String(t.action||t.side||'').toLowerCase().indexOf('buy') >= 0;
        html += '<div class="th-list-item"><div class="emoji">'+(isBuy?'\uD83D\uDFE2':'\uD83D\uDD34')+'</div><div class="body">';
        html += '<div class="title">'+escH(t.action||t.side||'Trade')+' &middot; '+escH(t.shares||t.quantity||'')+' shares</div>';
        html += '<div class="sub">$'+escH(Number(t.price||0).toFixed(2))+' &middot; '+fmtDate(t.trade_date||t.created_at)+'</div>';
        if(t.notes||t.reason) html += '<div class="detail">'+escH(String(t.notes||t.reason||'').substring(0,200))+'</div>';
        html += '</div></div>';
      });
      txEl.innerHTML = html;
    } else {
      txEl.innerHTML = '<div class="th-empty">No transactions recorded.</div>';
    }
  }
  var decs = h.decisions || [];
  var decEl = document.getElementById('decisionsList');
  if(decEl){
    if(decs.length){
      var html = '';
      decs.forEach(function(d){
        html += '<div class="th-list-item"><div class="emoji">\uD83C\uDFAF</div><div class="body">';
        html += '<div class="title">'+escH(d.decision_type||d.action||'Decision')+'</div>';
        html += '<div class="sub">'+fmtDate(d.created_at)+'</div>';
        if(d.reasoning||d.notes) html += '<div class="detail">'+escH(String(d.reasoning||d.notes||'').substring(0,300))+'</div>';
        html += '</div></div>';
      });
      decEl.innerHTML = html;
    } else {
      decEl.innerHTML = '<div class="th-empty">No decision log entries.</div>';
    }
  }
}

function buildLab(){
  var cases = _HUB.case_studies || [];
  var el = document.getElementById('caseStudiesList');
  if(!el) return;
  setText('cntLab', cases.length);
  if(cases.length){
    var html = '';
    cases.forEach(function(c){
      html += '<div class="th-list-item" onclick="window.open(\'/lab/'+_TICKER+'\',\'_blank\')">';
      html += '<div class="emoji">\uD83E\uDDEA</div><div class="body">';
      html += '<div class="title">'+escH(c.title||c.case_type||'Case Study')+'</div>';
      html += '<div class="sub">'+escH(c.case_type||'')+' &middot; '+fmtDate(c.created_at)+'</div>';
      if(c.summary||c.notes) html += '<div class="detail">'+escH(String(c.summary||c.notes||'').substring(0,250))+'</div>';
      html += '</div></div>';
    });
    el.innerHTML = html;
  } else {
    el.innerHTML = '<div class="th-empty">No case studies yet. <a href="/lab/'+_TICKER+'" target="_blank">Open Financial Lab</a> to create one.</div>';
  }
}

function buildBuybacks(){
  var bb = _HUB.buyback || {};
  var el = document.getElementById('buybackBox');
  if(!el) return;
  var quarters = bb.quarters || [];
  var ttm = bb.ttm_value;
  if(ttm == null && !quarters.length){ el.innerHTML = '<div class="th-empty">No buyback data available.</div>'; return; }
  var html = '<div class="th-card"><div style="font-size:24px;font-weight:800;color:var(--text);">' + fmtMoney(ttm) + '</div>';
  html += '<div style="font-size:11px;color:var(--muted);margin-bottom:8px;">Treasury stock acquired (TTM)</div>';
  if(quarters.length){
    var max = 0;
    quarters.forEach(function(q){ var v = Math.abs(q.value||0); if(v > max) max = v; });
    if(max > 0){
      html += '<div class="th-buyback-bars">';
      quarters.forEach(function(q){ var v = Math.abs(q.value||0); var h = Math.max(6, Math.round((v/max)*44)); html += '<div class="th-buyback-bar" style="height:'+h+'px;" title="'+fmtMoney(q.value)+'"></div>'; });
      html += '</div>';
    }
  }
  html += '</div>';
  el.innerHTML = html;
}

function buildInsiderTrades(){
  var trades = _HUB.insider_trades || [];
  var el = document.getElementById('insiderTradesBox');
  if(!el) return;
  if(!trades.length){ el.innerHTML = '<div class="th-empty">No recent Form 4 insider trades found.</div>'; return; }
  var html = '';
  trades.slice(0,12).forEach(function(it){
    var isBuy = (it.tx_type||'') === 'BUY';
    var isSell = (it.tx_type||'') === 'SELL';
    var color = isBuy ? 'var(--up-ink)' : (isSell ? 'var(--down-ink)' : 'var(--text)');
    var sign = isBuy ? '+' : (isSell ? '-' : '');
    var shares = Math.abs(Number(it.net_shares||0));
    var val = Number(it.est_value||0);
    html += '<div class="th-insider-row"><div style="min-width:0;flex:1;">';
    html += '<div style="font-size:12px;font-weight:700;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">';
    html += escH(it.owner||it.title||'-');
    if(it.title && it.owner && it.title !== it.owner) html += ' &middot; ' + escH(it.title);
    html += '</div>';
    if(it.signal_summary) html += '<div style="font-size:11px;color:var(--muted);">' + escH(it.signal_summary) + '</div>';
    html += '<div style="font-size:11px;color:var(--muted);">' + escH(it.date||'-') + '</div></div>';
    html += '<div style="text-align:right;"><div style="font-size:12px;font-weight:700;color:'+color+';">';
    if(shares > 0) html += sign + shares.toLocaleString() + ' shares';
    html += ' ' + escH(it.tx_type||'') + '</div>';
    if(val > 0) html += '<div style="font-size:11px;color:var(--muted);">~' + fmtMoney(val) + '</div>';
    html += '</div></div>';
  });
  el.innerHTML = html;
}

function buildMoatEditable(){
  var moatOptions = _HUB.moat_options || [];
  var activeMoats = (_HUB.moats || []).map(function(m){ return m.key || m; });
  var el = document.getElementById('moatTagsBox');
  if(!el) return;
  if(!moatOptions.length){
    var moats = _HUB.moats || [];
    if(moats.length){
      var html = '';
      moats.forEach(function(m){ html += '<span class="th-item-badge up" style="font-size:12px;padding:4px 10px;">'+escH(m.label||m.key||m)+'</span>'; });
      el.innerHTML = html;
    } else {
      el.innerHTML = '<div class="th-empty" style="padding:8px 0;">No moat tags assigned.</div>';
    }
    var saveBtn = document.getElementById('moatSaveBtn');
    if(saveBtn) saveBtn.style.display = 'none';
    return;
  }
  var html = '';
  moatOptions.forEach(function(m){
    var checked = activeMoats.indexOf(m.key) >= 0 ? ' checked' : '';
    html += '<label class="th-moat-pill"><input type="checkbox" name="moat_keys" value="'+escH(m.key)+'"'+checked+'><span>'+escH(m.label)+'</span></label>';
  });
  el.innerHTML = html;
}

function buildActiveProposal(){
  var ap = _HUB.active_proposal;
  if(!ap || !ap.title) return;
  var box = document.getElementById('activeProposalBox');
  if(!box) return;
  var html = '<div class="th-card full accent-left"><div class="th-card-label">Latest Fundamental Insight</div>';
  html += '<div style="font-size:11px;color:var(--muted);margin-bottom:6px;">'+escH((ap.status||'').toUpperCase())+' &middot; '+escH(ap.kind||'');
  if(ap.confidence) html += ' &middot; Conf ' + (Number(ap.confidence)*100).toFixed(0) + '%';
  html += '</div>';
  html += '<div style="font-weight:800;color:var(--text);margin-bottom:8px;">' + escH(ap.title) + '</div>';
  if(ap.insights && ap.insights.length){
    ap.insights.forEach(function(i){
      html += '<div style="border:none;border-radius:14px;padding:10px 12px;margin-bottom:6px;background:var(--panel2);">';
      html += '<div style="font-size:11px;color:var(--muted);font-weight:700;margin-bottom:3px;">' + escH(i.label||'Insight') + '</div>';
      html += '<div style="font-size:13px;color:var(--text);line-height:1.35;">' + escH(i.text||'') + '</div></div>';
    });
  }
  var rz = ap.reasoning || {};
  if(typeof rz === 'object' && rz.actionable_proposal){
    html += '<div style="border:none;border-radius:14px;padding:10px 12px;margin-bottom:6px;background:var(--panel2);">';
    html += '<div style="font-size:11px;color:var(--muted);font-weight:700;margin-bottom:3px;">Key Takeaway</div>';
    html += '<div style="font-size:13px;color:var(--text);line-height:1.35;">' + escH(rz.actionable_proposal) + '</div></div>';
  }
  if(ap.citations && ap.citations.length){
    html += '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;">';
    ap.citations.forEach(function(c){
      if(!c.url) return;
      // Detect filings/sec links — switch to filings tab inline instead of navigating
      var isFilingsLink = (c.tab === 'filings') || /company_file\/sec|\/sec\b/.test(c.url) || (c.label||'').toLowerCase().indexOf('sec filing') >= 0;
      if(isFilingsLink){
        html += '<a class="th-item-badge info" href="#" onclick="event.preventDefault();if(typeof TickerHub!==\'undefined\')TickerHub.switchTab(\'filings\');return false;" style="text-decoration:none;cursor:pointer;">' + escH(c.label||'Source') + '</a>';
      } else {
        // Internal links — no target="_blank", let workspace interceptor handle
        html += '<a class="th-item-badge info" href="'+escH(c.url)+'" style="text-decoration:none;">' + escH(c.label||'Source') + '</a>';
      }
    });
    html += '</div>';
  }
  html += '</div>';
  box.innerHTML = html;
  box.style.display = '';
}

function buildOverviewPreviews(){
  var ms = _HUB.mini_statements || {};
  var mfEl = document.getElementById('overviewMiniFinancials');
  if(mfEl && ms.years && ms.years.length > 0){
    var html = '<div class="th-card full" style="cursor:pointer;" onclick="TickerHub.switchTab(\'fundamentals\',document.querySelector(\'[data-pane=fundamentals]\'))">';
    html += '<div class="th-card-label">Financials Snapshot <span style="font-size:10px;color:var(--info-ink);font-weight:700;margin-left:6px;">View all &rarr;</span></div>';
    html += '<div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center;">';
    var rev = ms.revenue || []; var fcf = ms.free_cash_flow || [];
    if(rev.length){
      var last = rev[rev.length-1];
      html += '<div><div style="font-size:11px;color:var(--muted);">Revenue</div><div style="font-size:16px;font-weight:800;">'+fmtMoney(last)+'</div></div>';
      html += '<div>'+buildSparkSVG(rev)+'</div>';
    }
    if(fcf.length){
      var lastF = fcf[fcf.length-1];
      html += '<div style="margin-left:12px;"><div style="font-size:11px;color:var(--muted);">FCF</div><div style="font-size:16px;font-weight:800;">'+fmtMoney(lastF)+'</div></div>';
      html += '<div>'+buildSparkSVG(fcf)+'</div>';
    }
    html += '</div></div>';
    mfEl.innerHTML = html;
  }
  var proposals = _HUB.proposals || [];
  var spEl = document.getElementById('overviewSignalsPreview');
  if(spEl && proposals.length){
    var html = '<div class="th-section-head">Latest AI Signals <span class="count">'+proposals.length+'</span></div>';
    proposals.slice(0,3).forEach(function(p){
      var conf = p.confidence ? (Number(p.confidence)*100).toFixed(0)+'%' : '';
      html += '<div class="th-list-item" style="cursor:pointer;" onclick="TickerHub.switchTab(\'signals\',document.querySelector(\'[data-pane=signals]\'))">';
      html += '<div class="emoji">\uD83D\uDCA1</div><div class="body">';
      html += '<div class="title">'+escH(p.headline||p.action_type||'Proposal')+'</div>';
      html += '<div class="sub">'+fmtDate(p.created_at);
      if(conf) html += ' \u00b7 Conf '+conf;
      html += '</div></div></div>';
    });
    spEl.innerHTML = html;
  }
}

function populateHeaderPrice(){
  var pm = _HUB.price_metrics || _HUB.profile || {};
  var price = pm.current_price;
  var priceEl = document.getElementById('thHeaderPrice');
  if(priceEl && price) priceEl.textContent = '$' + Number(price).toFixed(2);
  var changeEl = document.getElementById('thHeaderChange');
  if(changeEl && pm.day_change != null){
    var d = Number(pm.day_change);
    var pct = pm.day_change_pct != null ? Number(pm.day_change_pct).toFixed(2) + '%' : '';
    var sign = d >= 0 ? '+' : '';
    changeEl.textContent = sign + d.toFixed(2) + (pct ? ' (' + sign + pct + ')' : '');
    changeEl.style.color = d >= 0 ? 'var(--up)' : 'var(--down, #ef4444)';
  }
}

function populateSidebar(){
  var brief = _HUB.earnings_sec_brief || {};
  if(brief.next_announced_date){
    var el = document.getElementById('sidebarEarnings');
    var dt = document.getElementById('sidebarEarningsDate');
    if(el && dt){ dt.textContent = brief.next_announced_date; el.style.display=''; }
  }
}

function initConvictionBox(){
  var key = 'dealroom:' + _TICKER;
  function save(){
    var data = {
      bull: (document.getElementById('thBullCase')||{}).value || '',
      bear: (document.getElementById('thBearCase')||{}).value || '',
      buy:  (document.getElementById('thBuyBelow')||{}).value || ''
    };
    try { localStorage.setItem(key, JSON.stringify(data)); } catch(_){}
    refreshGauge();
  }
  function load(){
    try {
      var raw = localStorage.getItem(key);
      if(!raw) return;
      var d = JSON.parse(raw);
      if(!d || typeof d !== 'object') return;
      var bull = document.getElementById('thBullCase');
      var bear = document.getElementById('thBearCase');
      var buy  = document.getElementById('thBuyBelow');
      if(bull && d.bull != null) bull.value = d.bull;
      if(bear && d.bear != null) bear.value = d.bear;
      if(buy  && d.buy != null)  buy.value = d.buy;
    } catch(_){}
  }
  ['thBullCase','thBearCase','thBuyBelow'].forEach(function(id){
    var el = document.getElementById(id);
    if(el) el.addEventListener('input', save);
  });
  load();
  refreshGauge();
}

function refreshGauge(){
  var buyEl = document.getElementById('thBuyBelow');
  var curEl = document.getElementById('thCurPxLabel');
  var pin = document.getElementById('thGaugePin');
  var st = document.getElementById('thGaugeStatus');
  if(!pin || !st || !buyEl || !curEl) return;
  var buy = parseFloat(String(buyEl.value||'').replace(/,/g,''));
  var cur = parseFloat(String(curEl.textContent||'').replace(/[$,]/g,''));
  if(isNaN(buy) || isNaN(cur) || buy <= 0){
    pin.style.left = '50%';
    st.className = 'th-gauge-status above';
    st.textContent = 'Set a buy target to evaluate zone.';
    return;
  }
  var pct = Math.min(100, Math.max(0, (cur / buy) * 100));
  pin.style.left = pct + '%';
  if(cur <= buy){ st.className = 'th-gauge-status buy'; st.textContent = 'BUY ZONE'; }
  else { st.className = 'th-gauge-status above'; st.textContent = 'ABOVE BUY ZONE'; }
}

/* ─── API interaction functions ───────────────────────────────────── */
function thRecAction(id, action, btn){
  if(action==='delete' && !confirm('Delete this record?')) return;
  var fd = new FormData();
  fd.append('source','record'); fd.append('id',id); fd.append('action',action);
  fetch('/workspace/item-action',{method:'POST',body:fd})
    .then(function(r){ return r.json(); })
    .then(function(){
      var row = document.getElementById('threc-'+id);
      if(!row) return;
      if(action==='delete') row.remove();
      else { if(btn){ btn.disabled=true; btn.textContent='Done!'; } }
    });
}

function thAddRecord(){
  var inp = document.getElementById('thQaInput');
  if(!inp) return;
  var title = inp.value.trim();
  if(!title) return;
  var kind = (document.getElementById('thQaKind')||{}).value || 'thought';
  var fd = new FormData();
  fd.append('kind',kind); fd.append('domain','work'); fd.append('title',title);
  fd.append('ticker',_TICKER); fd.append('source','manual'); fd.append('created_by','user');
  fetch('/workspace/record',{method:'POST',body:fd})
    .then(function(r){ return r.json(); })
    .then(function(data){
      if(data.ok){
        inp.value='';
        var r={id:data.id, kind:kind, title:title, ticker:_TICKER, status:'open', body:'', created_at:new Date().toISOString()};
        (_HUB.records||[]).unshift(r);
        buildHistory();
        setText('cntHistory', (_HUB.records||[]).length);
        setText('cntRecords', (_HUB.records||[]).length);
      }
    });
}

function doRefresh(url, ticker, btn){
  var orig = btn.textContent;
  btn.disabled = true; btn.textContent = 'Refreshing\u2026';
  var fd = new FormData();
  fd.append('ticker', ticker);
  fetch(url,{method:'POST',body:fd})
    .then(function(){ btn.textContent = 'Done!'; setTimeout(function(){ btn.textContent=orig; btn.disabled=false; },2000); })
    .catch(function(){ btn.textContent = 'Error'; setTimeout(function(){ btn.textContent=orig; btn.disabled=false; },2000); });
}

function triggerAgentAnalysis(){
  var btn = document.getElementById('analyzeNowBtn');
  if(!btn) return;
  btn.disabled = true; btn.textContent = 'Running\u2026';
  fetch('/api/agent/analyze',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ticker:_TICKER})
  })
  .then(function(r){ return r.json(); })
  .then(function(d){
    btn.textContent = d.ok ? 'Started \u2713' : 'Error';
    setTimeout(function(){ btn.textContent='Analyze Now'; btn.disabled=false; }, 4000);
  })
  .catch(function(){ btn.textContent='Error'; setTimeout(function(){ btn.textContent='Analyze Now'; btn.disabled=false; },3000); });
}

function loadAssumptions(){
  fetch('/api/assumptions?ticker='+_TICKER)
    .then(function(r){ return r.json(); })
    .then(function(data){
      var el = document.getElementById('assumptionsList');
      if(!el) return;
      setText('cntAssumptions', data.length);
      if(!data.length){ el.innerHTML='<div class="th-empty">No assumptions defined for $'+_TICKER+'. Add your first above.</div>'; return; }
      var html = '';
      data.forEach(function(a){
        var statusCls = a.status==='breached'?'down':(a.status==='active'?'up':'');
        html += '<div class="th-list-item" id="asm-'+a.id+'"><div class="emoji">'+(a.status==='breached'?'\u26A0\uFE0F':'\u2705')+'</div><div class="body">';
        html += '<div class="title">'+escH(a.assumption_text)+'</div><div class="sub">';
        if(a.measurable_metric) html += escH(a.measurable_metric)+' '+escH(a.threshold_operator)+' '+escH(a.threshold_value||'');
        if(a.current_value!==null && a.current_value!==undefined) html += ' (current: '+Number(a.current_value).toFixed(2)+')';
        html += '</div><div class="badges">';
        html += '<span class="th-item-badge '+statusCls+'">'+escH(a.status)+'</span>';
        html += '<span class="th-item-badge">'+escH(a.importance)+'</span>';
        html += '<span class="th-item-badge">'+escH(a.condition_type)+'</span>';
        html += ' <button class="th-item-badge" style="cursor:pointer;color:var(--down-ink);" onclick="event.stopPropagation();TickerHub.deleteAssumption('+a.id+')">&times;</button>';
        html += '</div></div></div>';
      });
      el.innerHTML = html;
    });
}

function addAssumption(){
  var text = (document.getElementById('asmText')||{}).value;
  if(!text || !text.trim()) return;
  var fd = new FormData();
  fd.append('ticker', _TICKER);
  fd.append('assumption_text', text.trim());
  fd.append('condition_type', (document.getElementById('asmType')||{}).value||'qualitative');
  fd.append('measurable_metric', (document.getElementById('asmMetric')||{}).value||'');
  fd.append('threshold_operator', (document.getElementById('asmOp')||{}).value||'>=');
  fd.append('threshold_value', (document.getElementById('asmThreshold')||{}).value||'');
  fd.append('importance', (document.getElementById('asmImportance')||{}).value||'core');
  fetch('/api/assumption',{method:'POST',body:fd})
    .then(function(r){ return r.json(); })
    .then(function(d){ if(d.ok){ document.getElementById('asmText').value=''; loadAssumptions(); } });
}

function deleteAssumption(id){
  if(!confirm('Delete this assumption?')) return;
  fetch('/api/assumption/'+id+'/delete',{method:'POST'}).then(function(){ loadAssumptions(); });
}

function loadConviction(){
  fetch('/api/conviction/'+_TICKER)
    .then(function(r){ return r.json(); })
    .then(function(data){
      var grid = document.getElementById('convictionGrid');
      var compGrid = document.getElementById('convictionComponents');
      var histEl = document.getElementById('convictionHistory');
      if(!grid) return;
      if(data.current){
        var c = data.current;
        var score = Number(c.score||50);
        var color = score >= 70 ? 'var(--up)' : (score >= 40 ? 'var(--info)' : 'var(--down)');
        grid.innerHTML = '<div class="th-card full" style="border-left:4px solid '+color+';text-align:center;"><div style="font-size:48px;font-weight:900;color:'+color+';">'+score.toFixed(0)+'</div><div style="font-size:13px;color:var(--muted);">Conviction Score (0-100)</div><div style="font-size:12px;color:var(--muted);margin-top:4px;">Previous: '+(Number(c.previous_score||50)).toFixed(0)+'</div></div>';
        var comp = c.components;
        if(comp && typeof comp === 'object' && compGrid){
          var compLabels = {assumption_health:'Assumption Health',debate_pass_rate:'Debate Pass Rate',earnings_trajectory:'Earnings Trajectory',research_freshness:'Research Freshness',signal_engagement:'Signal Engagement'};
          var html = '';
          Object.keys(comp).forEach(function(k){
            var v = Number(comp[k]||50);
            var cls = v >= 70 ? 'up-left' : (v >= 40 ? 'info-left' : 'warn-left');
            html += '<div class="th-card '+cls+'"><div class="th-card-label">'+(compLabels[k]||k)+'</div><div class="th-card-body" style="font-size:18px;font-weight:700;">'+v.toFixed(0)+'%</div></div>';
          });
          compGrid.innerHTML = html;
        }
      } else {
        grid.innerHTML = '<div class="th-empty">No conviction data yet. Click Recalculate below.</div>';
        if(compGrid) compGrid.innerHTML = '';
      }
      if(histEl){
        var hist = data.history || [];
        if(hist.length){
          var html = '';
          hist.forEach(function(h){
            var d = Number(h.delta||0);
            html += '<div class="th-list-item"><div class="emoji">'+(d >= 0 ? '\uD83D\uDCC8' : '\uD83D\uDCC9')+'</div><div class="body">';
            html += '<div class="title">Score: '+Number(h.score||0).toFixed(0)+' ('+(d >= 0 ? '+' : '')+d.toFixed(1)+')</div>';
            html += '<div class="sub">'+escH(h.trigger_type||'')+' &middot; '+fmtDate(h.created_at)+'</div>';
            if(h.trigger_event) html += '<div class="detail">'+escH(h.trigger_event)+'</div>';
            html += '</div></div>';
          });
          histEl.innerHTML = html;
        } else {
          histEl.innerHTML = '<div class="th-empty">No conviction history yet.</div>';
        }
      }
    });
}

function refreshConviction(){
  fetch('/api/conviction/'+_TICKER+'/refresh',{method:'POST'})
    .then(function(){ loadConviction(); });
}

function loadTriggers(){
  fetch('/api/triggers?ticker='+_TICKER)
    .then(function(r){ return r.json(); })
    .then(function(data){
      var el = document.getElementById('triggersList');
      if(!el) return;
      setText('cntTriggers', data.length);
      if(!data.length){ el.innerHTML='<div class="th-empty">No triggers for $'+_TICKER+'. Add your first above.</div>'; return; }
      var html = '';
      data.forEach(function(t){
        var sCls = t.status==='fired'?'down':(t.status==='active'?'up':'');
        html += '<div class="th-list-item" id="trg-'+t.id+'"><div class="emoji">'+(t.status==='fired'?'\uD83D\uDD14':'\u23F0')+'</div><div class="body">';
        html += '<div class="title">'+escH(t.trigger_name)+'</div>';
        html += '<div class="sub">'+escH(t.trigger_type)+': '+escH(t.condition_text||'')+' '+escH(t.comparison_op||'')+' '+escH(t.threshold_value||'')+'</div>';
        if(t.action_text) html += '<div class="detail">Action: '+escH(t.action_text)+'</div>';
        html += '<div class="badges"><span class="th-item-badge '+sCls+'">'+escH(t.status)+'</span>';
        if(t.fired_at) html += '<span class="th-item-badge down">Fired: '+fmtDate(t.fired_at)+'</span>';
        html += ' <button class="th-item-badge" style="cursor:pointer;color:var(--down-ink);" onclick="event.stopPropagation();TickerHub.deleteTrigger('+t.id+')">&times;</button>';
        html += '</div></div></div>';
      });
      el.innerHTML = html;
    });
}

function addTrigger(){
  var name = (document.getElementById('trgName')||{}).value;
  if(!name || !name.trim()) return;
  var ttype = (document.getElementById('trgType')||{}).value||'price_below';
  var fd = new FormData();
  fd.append('ticker', _TICKER);
  fd.append('trigger_name', name.trim());
  fd.append('trigger_type', ttype);
  fd.append('threshold_value', (document.getElementById('trgThreshold')||{}).value||'');
  fd.append('action_text', (document.getElementById('trgAction')||{}).value||'');
  fd.append('comparison_op', ttype.indexOf('below') >= 0 ? '<' : '>');
  fd.append('condition_text', ttype === 'metric_check' ? (document.getElementById('trgThreshold')||{}).value||'' : '');
  fetch('/api/trigger',{method:'POST',body:fd})
    .then(function(r){ return r.json(); })
    .then(function(d){ if(d.ok){ document.getElementById('trgName').value=''; loadTriggers(); } });
}

function deleteTrigger(id){
  if(!confirm('Delete this trigger?')) return;
  fetch('/api/trigger/'+id+'/delete',{method:'POST'}).then(function(){ loadTriggers(); });
}

function loadSynthesis(){
  fetch('/api/synthesis/'+_TICKER)
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(!d.synthesis_text) return;
      var el = document.getElementById('overviewGrid');
      if(!el) return;
      var card = document.createElement('div');
      card.className = 'th-card full accent-left';
      card.innerHTML = '<div class="th-card-label">Living Synthesis</div>'
        +'<div class="th-card-body" style="white-space:pre-wrap;font-size:12px;line-height:1.55;">'
        +escH(d.synthesis_text).replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>')
        +'</div>'
        +'<div class="meta" style="margin-top:6px;">Generated: '+fmtDate(d.generated_at)+'</div>';
      el.insertBefore(card, el.firstChild);
    })
    .catch(function(){});
}

function removeSupplyLink(rowId){
  if(!confirm('Remove this relationship?')) return;
  var fd = new FormData();
  fd.append('ticker',_TICKER); fd.append('row_id',rowId);
  fetch('/company_file/supply-chain-remove',{method:'POST',body:fd})
    .then(function(){ TickerHub.refresh(_TICKER); });
}

function addToWatchlist(ticker, btn){
  var fd = new FormData();
  fd.append('ticker', ticker);
  fd.append('reason', 'Added from Similar Companies');
  btn.disabled = true; btn.textContent = 'Adding\u2026';
  fetch('/my_universe/watchlist/add',{method:'POST',body:fd})
    .then(function(){ btn.textContent = 'Added!'; })
    .catch(function(){ btn.textContent = 'Error'; setTimeout(function(){ btn.textContent='+ Watchlist'; btn.disabled=false; },2000); });
}

function saveMoats(){
  var checkboxes = document.querySelectorAll('#moatTagsBox input[name="moat_keys"]:checked');
  var fd = new FormData();
  fd.append('ticker', _TICKER);
  checkboxes.forEach(function(cb){ fd.append('moat_keys', cb.value); });
  fetch('/company_file/moat-save',{method:'POST',body:fd})
    .then(function(){
      var btn = document.getElementById('moatSaveBtn');
      if(btn){ btn.textContent = 'Saved!'; setTimeout(function(){ btn.textContent = 'Save Thesis Tags'; }, 2000); }
    });
}

function initSupplyChainForm(){
  var form = document.getElementById('scAddForm');
  if(!form) return;
  form.addEventListener('submit', function(e){
    e.preventDefault();
    var fd = new FormData(form);
    fetch('/company_file/supply-chain-add',{method:'POST',body:fd})
      .then(function(){ TickerHub.refresh(_TICKER); });
  });
}

/* ─── Thesis modal ──────────────────────────────────────────────── */
function openThesisModal(){
  document.getElementById('thesisModalBg').style.display = 'flex';
  var th = _HUB.thesis || {};
  var el;
  el = document.getElementById('tmThesisText'); if(el) el.value = th.thesis || '';
  el = document.getElementById('tmTarget'); if(el) el.value = th.target_price || '';
  el = document.getElementById('tmHorizon'); if(el) el.value = th.time_horizon || '';
  el = document.getElementById('tmInvalidation'); if(el) el.value = th.invalidation_criteria || '';
}
function closeThesisModal(){ document.getElementById('thesisModalBg').style.display = 'none'; }
function saveThesis(){
  var data = {
    thesis: (document.getElementById('tmThesisText')||{}).value||'',
    target_price: (document.getElementById('tmTarget')||{}).value || null,
    time_horizon: (document.getElementById('tmHorizon')||{}).value||'',
    invalidation_criteria: (document.getElementById('tmInvalidation')||{}).value||'',
    phase: 'holding'
  };
  fetch('/api/thesis/'+encodeURIComponent(_TICKER), {
    method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)
  }).then(function(r){ return r.json(); }).then(function(d){
    if(d.ok){ TickerHub.refresh(_TICKER); closeThesisModal(); }
    else { alert('Error: '+(d.error||'unknown')); }
  });
}

function createResearchThread(){
  document.getElementById('newThreadModalBg').style.display='flex';
  var inp = document.getElementById('ntThreadTitle');
  if(inp){ inp.value = _TICKER + ' Deep Dive'; setTimeout(function(){ inp.focus(); inp.select(); }, 80); }
}
function submitNewThread(){
  var title = (document.getElementById('ntThreadTitle')||{}).value;
  if(!title || !title.trim()) return;
  document.getElementById('newThreadModalBg').style.display='none';
  fetch('/workspace/thread', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:title.trim(), ticker:_TICKER, space:'research', thread_type:'research'})
  }).then(function(r){ return r.json(); }).then(function(d){
    if(d.ok && d.id){ window.location.href='/today?space=research&thread='+d.id; }
    else { alert('Error creating thread'); }
  });
}

function createThinkingChain(){
  document.getElementById('newChainModalBg').style.display='flex';
  var inp = document.getElementById('ntChainTitle');
  if(inp){ inp.value = _TICKER + ' \u2014 Investment thesis analysis'; setTimeout(function(){ inp.focus(); inp.select(); }, 80); }
}
function submitNewChain(){
  var title = (document.getElementById('ntChainTitle')||{}).value;
  if(!title || !title.trim()) return;
  document.getElementById('newChainModalBg').style.display='none';
  fetch('/workspace/thinking', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:title.trim(), ticker:_TICKER, chain_type:'thinking'})
  }).then(function(r){ return r.json(); }).then(function(d){
    if(d.ok && d.id){ window.location.href='/today?space=thinking&chain='+d.id; }
    else { alert('Error creating chain'); }
  });
}

/* ─── Init: populate all panes from hub data ──────────────────────── */
function init(hubData, ticker, researchNotes){
  _TICKER = ticker;
  _HUB = hubData || {};
  _RESEARCH_NOTES = researchNotes || [];
  _TIMELINE_DATA = _HUB.timeline || [];
  _rcSentiment = 'neutral';
  _rcFilter = 'all';
  _rcExpandedThread = null;
  _labLoaded = false;

  var _fns = [buildOverview, populateHeaderPrice, buildActiveProposal, initConvictionBox,
    buildOverviewPreviews, populateSidebar, buildResearch, buildThinking, buildSignals,
    buildFilings, buildEarnings, buildHistory, buildConnections, buildFundamentals,
    buildBuybacks, buildInsiderTrades, buildMoatEditable, buildMoats, buildSupplyChain,
    buildNotes, buildTransactions, buildLab, initSupplyChainForm, loadAssumptions, loadSynthesis];
  _fns.forEach(function(fn){ try{ fn(); }catch(e){ console.error('TickerHub init error in '+fn.name+':', e); } });

  // Sentiment bar show on focus
  var rcInp = document.getElementById('rcNoteInput');
  if(rcInp){ rcInp.addEventListener('focus', function(){ var bar = document.getElementById('rcSentimentBar'); if(bar) bar.style.display = 'flex'; }); }

  // Quick-add Enter key
  var qaInp = document.getElementById('thQaInput');
  if(qaInp) qaInp.addEventListener('keydown', function(e){ if(e.key==='Enter'){e.preventDefault();thAddRecord();} });

  // Restore last tab
  try{
    var last = localStorage.getItem('thub_tab_'+_TICKER);
    if(last && last!=='overview'){
      var group = _paneToGroup(last);
      var grpBtn = document.querySelector('.th-group-btn[data-group="'+group+'"]');
      if(grpBtn) switchGroup(group, grpBtn);
      var subBtn = document.querySelector('.th-sub-btn[data-pane="'+last+'"]');
      if(subBtn) switchTab(last, subBtn);
    }
  }catch(_){}

  // AI insight
  fetch('/workspace/ticker/'+_TICKER+'/ai-context')
    .then(function(r){ return r.json(); })
    .then(function(d){
      var el = document.getElementById('aiInsightText');
      if(el){ el.style.color=''; el.textContent=d.insight||'No insight available.'; }
    })
    .catch(function(){
      var el = document.getElementById('aiInsightText');
      if(el){ el.style.color=''; el.textContent='Insight unavailable.'; }
    });

}

/* ─── Refresh: re-fetch and re-render ─────────────────────────────── */
function refresh(ticker){
  fetch('/api/ticker-hub/'+encodeURIComponent(ticker))
    .then(function(r){ return r.json(); })
    .then(function(data){
      // Find container — could be workspace or standalone
      var container = document.getElementById('tickerContent');
      if(container){
        container.innerHTML = buildShell(data.ticker, data.company_name, data.hub, data.thesis);
        init(data.hub, data.ticker, data.research_notes);
        // Update workspace state if available
        if(window.WS && window.WS.openTickers && window.WS.openTickers[data.ticker]){
          window.WS.openTickers[data.ticker].hub = data.hub;
          window.WS.openTickers[data.ticker].thesis = data.thesis;
          window.WS.openTickers[data.ticker].company_name = data.company_name;
          window.WS.openTickers[data.ticker].research_notes = data.research_notes;
        }
      }
    });
}

/* ─── Expose public API ───────────────────────────────────────────── */
window.TickerHub = {
  buildShell: buildShell,
  init: init,
  refresh: refresh,
  switchGroup: switchGroup,
  switchTab: switchTab,
  // Research
  pickSentiment: pickSentiment,
  filterNotes: filterNotes,
  saveQuickNote: saveQuickNote,
  togglePin: togglePin,
  changeSentiment: changeSentiment,
  deleteNote: deleteNote,
  toggleThread: toggleThread,
  addThreadEntry: addThreadEntry,
  createResearchThread: createResearchThread,
  submitNewThread: submitNewThread,
  createThinkingChain: createThinkingChain,
  submitNewChain: submitNewChain,
  // Records
  thRecAction: thRecAction,
  thAddRecord: thAddRecord,
  // Filings
  _filterFilings: _filterFilings,
  // Actions
  doRefresh: doRefresh,
  triggerAgentAnalysis: triggerAgentAnalysis,
  addToWatchlist: addToWatchlist,
  removeSupplyLink: removeSupplyLink,
  saveMoats: saveMoats,
  // Thesis
  openThesisModal: openThesisModal,
  closeThesisModal: closeThesisModal,
  saveThesis: saveThesis,
  // Phase 4
  addAssumption: addAssumption,
  deleteAssumption: deleteAssumption,
  refreshConviction: refreshConviction,
  addTrigger: addTrigger,
  deleteTrigger: deleteTrigger,
  // Filings viewer
  openFilingViewer: _openFilingViewer,
  // Getters
  getTicker: function(){ return _TICKER; },
  getHub: function(){ return _HUB; },
};

})();
