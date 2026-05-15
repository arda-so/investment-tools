/* ══════════════════════════════════════════════════════════════════════════════
   Lab Renderer — Financial Lab inline in Ticker Hub's Lab pane
   ══════════════════════════════════════════════════════════════════════════════
   Usage:
     LabView.build(ticker, ctx, checklist) → HTML string
     LabView.init(ticker)                  → attach event listeners
══════════════════════════════════════════════════════════════════════════════ */
(function(){
"use strict";

var _ticker = '';
function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function build(ticker, ctx, checklist){
  _ticker = ticker;
  var html = '<div id="labRoot" style="padding:4px 0;">';

  // Buffett Checklist
  html += buildChecklist(checklist);

  // Scenario simulator
  html += buildScenarioSim(ticker);

  // Case studies
  html += buildCaseStudies(ticker);

  // Risk diff
  html += buildRiskDiff(ticker);

  html += '</div>';
  return html;
}

function buildChecklist(cl){
  var html = '<div class="lab-section">';
  html += '<div class="lab-section-head">Buffett Checklist</div>';
  if(!cl || !cl.items || !cl.items.length){
    html += '<div class="lab-empty">No checklist data available.</div>';
    html += '</div>';
    return html;
  }
  html += '<div class="lab-score-bar">';
  var score = cl.score || 0;
  var total = cl.total || cl.items.length;
  var pct = total > 0 ? Math.round(score/total*100) : 0;
  var cls = pct >= 70 ? 'good' : (pct >= 40 ? 'mid' : 'bad');
  html += '<div class="lab-score-num '+cls+'">'+pct+'%</div>';
  html += '<div class="lab-score-label">'+score+' / '+total+' criteria met</div>';
  html += '</div>';
  html += '<div class="lab-checklist">';
  for(var i=0; i<cl.items.length; i++){
    var item = cl.items[i];
    var pass = item.pass || item.passed;
    var icon = pass ? '✅' : '❌';
    html += '<div class="lab-check-row">';
    html += '<span class="lab-check-icon">'+icon+'</span>';
    html += '<div class="lab-check-text">';
    html += '<div class="lab-check-name">'+esc(item.name||item.label||'')+'</div>';
    if(item.detail || item.value) html += '<div class="lab-check-detail">'+esc(item.detail||item.value||'')+'</div>';
    html += '</div></div>';
  }
  html += '</div></div>';
  return html;
}

function buildScenarioSim(ticker){
  var html = '<div class="lab-section">';
  html += '<div class="lab-section-head">Scenario Simulator</div>';
  html += '<div class="lab-scenario-form">';
  html += '<textarea id="labScenarioInput" class="lab-scenario-input" rows="3" placeholder="Describe a scenario: e.g. Fed raises rates 50bps, tariffs on China increase 25%…"></textarea>';
  html += '<button class="lab-btn" id="labRunScenario">Run Scenario</button>';
  html += '</div>';
  html += '<div id="labScenarioResult" class="lab-scenario-result"></div>';
  html += '</div>';
  return html;
}

function buildCaseStudies(ticker){
  var html = '<div class="lab-section">';
  html += '<div class="lab-section-head">Case Studies <button class="lab-btn-sm" id="labRefreshCases">Refresh</button></div>';
  html += '<div id="labCasesList" class="lab-cases-list"><div class="lab-empty">Loading…</div></div>';
  html += '</div>';
  return html;
}

function buildRiskDiff(ticker){
  var html = '<div class="lab-section">';
  html += '<div class="lab-section-head">Risk Factor Changes</div>';
  html += '<div id="labRiskDiff" class="lab-risk-diff"><div class="lab-empty">Loading…</div></div>';
  html += '</div>';
  return html;
}

function loadCaseStudies(){
  fetch('/api/lab/'+encodeURIComponent(_ticker)+'/case-studies')
    .then(function(r){ return r.json(); })
    .then(function(data){
      var cases = data.cases || [];
      var el = document.getElementById('labCasesList');
      if(!el) return;
      if(!cases.length){ el.innerHTML = '<div class="lab-empty">No case studies yet.</div>'; return; }
      var html = '';
      for(var i=0; i<cases.length; i++){
        var c = cases[i];
        html += '<div class="lab-case-item">';
        html += '<div class="lab-case-title">'+esc(c.title||'Untitled')+'</div>';
        html += '<div class="lab-case-meta">'+esc(c.module||'')+ ' &middot; '+esc((c.created_at||'').substring(0,10))+'</div>';
        if(c.notes) html += '<div class="lab-case-notes">'+esc(c.notes)+'</div>';
        html += '</div>';
      }
      el.innerHTML = html;
    })
    .catch(function(){ var el = document.getElementById('labCasesList'); if(el) el.innerHTML = '<div class="lab-empty">Could not load.</div>'; });
}

function loadRiskDiff(){
  fetch('/api/lab/'+encodeURIComponent(_ticker)+'/risk-diff')
    .then(function(r){ return r.json(); })
    .then(function(data){
      var el = document.getElementById('labRiskDiff');
      if(!el) return;
      if(!data.ok || !data.diffs || !data.diffs.length){
        el.innerHTML = '<div class="lab-empty">No risk factor changes detected.</div>';
        return;
      }
      var html = '';
      for(var i=0; i<data.diffs.length; i++){
        var d = data.diffs[i];
        html += '<div class="lab-risk-item">';
        html += '<div class="lab-risk-type">'+esc(d.change_type||d.type||'changed')+'</div>';
        html += '<div class="lab-risk-text">'+esc(d.text||d.summary||'')+'</div>';
        html += '</div>';
      }
      el.innerHTML = html;
    })
    .catch(function(){ var el = document.getElementById('labRiskDiff'); if(el) el.innerHTML = '<div class="lab-empty">Could not load risk diff.</div>'; });
}

function init(ticker){
  _ticker = ticker;
  var root = document.getElementById('labRoot');
  if(!root) return;

  // Load async data
  loadCaseStudies();
  loadRiskDiff();

  // Scenario simulator
  var runBtn = document.getElementById('labRunScenario');
  if(runBtn){
    runBtn.addEventListener('click', function(){
      var input = document.getElementById('labScenarioInput');
      var result = document.getElementById('labScenarioResult');
      if(!input || !result) return;
      var scenario = input.value.trim();
      if(!scenario){ result.innerHTML = '<div class="lab-empty">Enter a scenario first.</div>'; return; }
      result.innerHTML = '<div class="lab-empty">Running scenario…</div>';
      runBtn.disabled = true;
      var formData = new FormData();
      formData.append('scenario', scenario);
      fetch('/api/lab/'+encodeURIComponent(_ticker)+'/simulate-scenario', {method:'POST', body:formData})
        .then(function(r){ return r.json(); })
        .then(function(data){
          runBtn.disabled = false;
          if(!data.ok){ result.innerHTML = '<div class="lab-empty" style="color:#ef4444;">'+esc(data.error||'Simulation failed')+'</div>'; return; }
          var html = '<div class="lab-scenario-output">';
          html += '<div class="lab-scenario-text">'+esc(data.analysis||data.result||JSON.stringify(data))+'</div>';
          html += '</div>';
          result.innerHTML = html;
        })
        .catch(function(err){
          runBtn.disabled = false;
          result.innerHTML = '<div class="lab-empty" style="color:#ef4444;">Error: '+esc(String(err))+'</div>';
        });
    });
  }

  // Refresh case studies
  var refreshBtn = document.getElementById('labRefreshCases');
  if(refreshBtn){
    refreshBtn.addEventListener('click', loadCaseStudies);
  }
}

window.LabView = { build: build, init: init };
})();
