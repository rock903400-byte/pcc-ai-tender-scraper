// Flask 輕量版前端邏輯
let currentJob = null;
let trickleJob = null;
let selectedMatched = new Set();
let selectedAll = new Set();
let selectedWatch = new Set();
let charts = {};

function qs(id){ return document.getElementById(id); }

function showTab(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active', t.dataset.tab===name));
  document.querySelectorAll('.panel').forEach(p=>{
    const isTarget = p.id==='panel-'+name;
    p.classList.toggle('active', isTarget);
    // panel-all 預設隱藏，透過 active 控制，無需 inline
  });
  // 處理「展開查看全部標案」連結點開時，確保內層 adv 顯示
  if(name==='all'){
    const adv = document.getElementById('adv-all');
    if(adv) adv.style.display='block';
    loadTable('all');
  }
  if(name==='dashboard') loadDashboard();
  if(name==='watchlist') loadTable('watchlist');
  // 紀錄已改為抽屜，無需 tab
}

document.querySelectorAll('.tab').forEach(t=> t.addEventListener('click', ()=> showTab(t.dataset.tab)));

function updateKpi(){
  fetch('/api/stats').then(r=>r.json()).then(d=>{
    qs('k-total').textContent = d.total + ' 筆';
    qs('k-qual').textContent = d.qualified + ' 筆';
    qs('k-hits').textContent = '命中 '+(d.hits||0)+' 筆';
    qs('k-pending').textContent = d.pending + ' 筆';
    qs('k-watch').textContent = d.watch + ' 筆';
    const badge = qs('badge-status');
    if(d.total) badge.textContent = '已載入 '+d.total+' 筆';
  });
}

function pushNotice(msg){
  const box = qs('notices');
  const div = document.createElement('div');
  div.style.cssText='background:#f59e0b;color:#fff;padding:8px;border-radius:6px;margin:6px 0';
  div.textContent='⚠️ '+msg;
  box.appendChild(div);
}

async function startSearch(){
  const body = {
    keywords: qs('kw-input').value.trim(),
    date_mode: qs('date-mode').value,
    days: qs('days').value,
    attr: qs('attr').value,
    award: qs('award').value,
    verify: qs('verify').checked,
    include_misses: qs('include-misses').checked,
    hide_pending: qs('hide-pending').checked,
  };
  if(!body.keywords){ alert('請至少輸入一個關鍵字'); return; }
  qs('btn-search').disabled = true;
  qs('btn-search').textContent = '🔄 搜尋中…';
  qs('btn-stop').style.display = 'block';
  qs('progress-wrap').style.display = 'block';
  qs('notices').innerHTML='';
  try{
    const res = await fetch('/api/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j = await res.json();
    if(!res.ok){ alert(j.error||'搜尋失敗'); resetSearch(); return; }
    if(j.warning) pushNotice(j.warning);
    currentJob = j.job_id;
    pollSearch();
  }catch(e){ alert(e); resetSearch(); }
}

function resetSearch(){
  qs('btn-search').disabled=false;
  qs('btn-search').textContent='🚀 開始搜尋標案';
  qs('btn-stop').style.display='none';
  // keep progress visible
}

async function pollSearch(){
  if(!currentJob) return;
  const res = await fetch('/api/search/status?job_id='+currentJob);
  const j = await res.json();
  if(j.error){ resetSearch(); return; }
  qs('progress-bar').style.width = (j.progress||0)+'%';
  qs('progress-text').textContent = j.status ? (j.status[1]||'') + ' '+(j.progress||0)+'%' : '';
  if(j.logs) j.logs.forEach(l=> console.log(l));
  if(j.done){
    if(j.failed) pushNotice('搜尋失敗: '+j.failed);
    else if(j.status) pushNotice(j.status[1]);
    resetSearch();
    currentJob=null;
    updateKpi();
    loadTable('matched'); loadTable('all');
    loadLogs();
    return;
  }
  setTimeout(pollSearch, 800);
}

async function stopSearch(){
  if(!currentJob) return;
  await fetch('/api/search/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id: currentJob})});
}

qs('btn-search').addEventListener('click', startSearch);
qs('btn-stop').addEventListener('click', stopSearch);
qs('btn-reset-kw').addEventListener('click', ()=>{
  // 預設關鍵字 (hardcode 與 config 一致，避免再打 API)
  qs('kw-input').value = "AI 人工智慧 機器學習 深度學習 大型語言模型 LLM 演算法 大數據 智慧化 資訊 軟體 網站 系統 平台 資安 資料庫 網路 雲端 數位 APP 程式 電腦 資通 機房 維護 建置 委外 人力資源 E化 E指通 無紙化";
});

async function startTrickle(){
  qs('btn-trickle').disabled=true; qs('btn-trickle').textContent='🔄 補齊中…';
  const res = await fetch('/api/trickle',{method:'POST'});
  const j = await res.json();
  trickleJob = j.job_id;
  pollTrickle();
}
async function pollTrickle(){
  if(!trickleJob) return;
  const r = await fetch('/api/trickle/status?job_id='+trickleJob);
  const j = await r.json();
  if(j.done){
    qs('btn-trickle').disabled=false; qs('btn-trickle').textContent='🔄 立即補齊 40 筆決標方式';
    trickleJob=null; updateKpi(); loadTable('matched'); loadTable('watchlist');
    return;
  }
  setTimeout(pollTrickle, 1000);
}
qs('btn-trickle').addEventListener('click', startTrickle);

// 通用表格
const COLS = ["#","公告日期","招標機關","標案名稱","預算金額","決標方式","招標方式","決標方式來源","截止投標","剩餘天數","命中關鍵字","詳細連結"];

function renderTable(tblId, rows, tab){
  const thead = document.querySelector('#'+tblId+' thead');
  const tbody = document.querySelector('#'+tblId+' tbody');
  thead.innerHTML = '<tr><th>☐</th>'+COLS.map(c=>'<th>'+c+'</th>').join('')+'</tr>';
  tbody.innerHTML = rows.map((r,i)=>{
    const chk = '<input type="checkbox" data-pk="'+(r.pk||r['標案案號'])+'" data-tab="'+tab+'">';
    const link = r['詳細連結'] ? '<a href="'+r['詳細連結']+'" target="_blank">🔗 開啟</a>' : '';
    const cells = COLS.map(c=>{
      if(c==='詳細連結') return '<td>'+link+'</td>';
      return '<td>'+(r[c]||'')+'</td>';
    }).join('');
    return '<tr>'+'<td>'+chk+'</td>'+cells+'</tr>';
  }).join('');
  // 綁勾選
  tbody.querySelectorAll('input[type=checkbox]').forEach(cb=>{
    cb.addEventListener('change', ()=>{
      const tabName = cb.dataset.tab;
      const set = tabName==='matched'? selectedMatched : tabName==='all'? selectedAll : selectedWatch;
      if(cb.checked) set.add(cb.dataset.pk); else set.delete(cb.dataset.pk);
      updateSelBtn();
    });
  });
}

function updateSelBtn(){
  qs('btn-add-matched').textContent = '➕ 加入追蹤（已選 '+selectedMatched.size+' 筆）';
  qs('btn-add-all').textContent = '➕ 加入追蹤（已選 '+selectedAll.size+' 筆）';
}

function getFilter(tab){
  const q = qs('q-'+tab)?.value || '';
  const sort = qs('sort-'+tab)?.value || '';
  const min = qs('budget-min-'+tab)?.value || 0;
  const max = qs('budget-max-'+tab)?.value || 0;
  const urgency = qs('urgency-'+tab)?.value || '全部';
  const award = qs('award-'+tab)?.value || '全部';
  const agencies = (qs('agencies-'+tab)?.value || '').split(',').map(s=>s.trim()).filter(Boolean);
  const hide = qs('hide-pending')?.checked || false;
  const include = qs('include-misses')?.checked || false;
  return {q, sort, min, max, urgency, award, agencies, hide, include};
}

let pagState = {matched:{page:1,limit:100}, all:{page:1,limit:100}, watchlist:{page:1,limit:50}};

async function loadTable(tab){
  const f = getFilter(tab);
  const p = pagState[tab] || {page:1,limit:100};
  const params = new URLSearchParams({
    tab: tab,
    query: f.q,
    min_budget: f.min,
    max_budget: f.max,
    urgency: f.urgency,
    award_status: f.award,
    sort: f.sort,
    page: p.page,
    limit: p.limit,
    hide_pending: f.hide,
    include_misses: f.include,
  });
  f.agencies.forEach(a=> params.append('agencies', a));
  const res = await fetch('/api/tenders?'+params.toString());
  const j = await res.json();
  if(j.error){ console.log(j.error); return; }
  renderTable('tbl-'+tab, j.rows, tab);
  // 分頁
  const pag = qs('pag-'+tab);
  if(pag){
    pag.innerHTML = '第 '+j.page+' / '+j.total_pages+' 頁 · 共 '+j.total+' 筆 '
      + '<button onclick="chgPage(\''+tab+'\','+(j.page-1)+')">◀ 上一頁</button>'
      + '<button onclick="chgPage(\''+tab+'\','+(j.page+1)+')">下一頁 ▶</button>'
      + ' 每頁 <select onchange="chgLimit(\''+tab+'\',this.value)"><option>50</option><option selected>100</option><option>300</option><option>全部</option></select>';
  }
}

function chgPage(tab, p){ pagState[tab].page = Math.max(1,p); loadTable(tab); }
function chgLimit(tab, v){ pagState[tab].limit = v; pagState[tab].page=1; loadTable(tab); }
function clearAdv(tab){
  if(tab==='matched'){
    qs('budget-min-matched').value=0; qs('budget-max-matched').value=2000;
    qs('urgency-matched').value='全部'; qs('award-matched').value='全部'; qs('agencies-matched').value='';
  } else {
    qs('budget-min-all').value=0; qs('budget-max-all').value=2000;
    qs('urgency-all').value='全部'; qs('award-all').value='全部'; qs('agencies-all').value='';
  }
  loadTable(tab);
}

// 綁定 filter 變化即重載
['q-matched','sort-matched','urgency-matched','award-matched','agencies-matched','budget-min-matched','budget-max-matched'].forEach(id=>{
  const el = qs(id); if(el) el.addEventListener('input', ()=>{ pagState.matched.page=1; loadTable('matched'); });
  if(el && el.tagName==='SELECT') el.addEventListener('change', ()=>{ pagState.matched.page=1; loadTable('matched'); });
});
['q-all','sort-all','urgency-all','award-all','agencies-all','budget-min-all','budget-max-all'].forEach(id=>{
  const el = qs(id); if(el) el.addEventListener('input', ()=>{ pagState.all.page=1; loadTable('all'); });
  if(el && el.tagName==='SELECT') el.addEventListener('change', ()=>{ pagState.all.page=1; loadTable('all'); });
});
qs('sort-watchlist')?.addEventListener('change', ()=> loadTable('watchlist'));

// 加入追蹤
async function addToWatch(tab){
  const set = tab==='matched'? selectedMatched : selectedAll;
  if(set.size===0){ alert('請先勾選'); return; }
  const res = await fetch('/api/watchlist/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pks:[...set]})});
  const j = await res.json();
  alert('已加入 '+j.added+' 筆');
  set.clear(); updateSelBtn(); updateKpi(); loadTable('watchlist');
}
qs('btn-add-matched').addEventListener('click', ()=> addToWatch('matched'));
qs('btn-add-all').addEventListener('click', ()=> addToWatch('all'));

qs('btn-remove-wl').addEventListener('click', async()=>{
  if(selectedWatch.size===0){ alert('請先在追蹤表勾選'); return; }
  await fetch('/api/watchlist/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keys:[...selectedWatch]})});
  selectedWatch.clear(); loadTable('watchlist'); updateKpi();
});
qs('btn-clear-wl').addEventListener('click', async()=>{
  if(!confirm('確定清空所有追蹤？')) return;
  await fetch('/api/watchlist/clear',{method:'POST'});
  loadTable('watchlist'); updateKpi();
});

// 匯出
qs('dl-xlsx-matched').addEventListener('click', ()=> window.location='/api/export?format=xlsx&tab=matched&include_misses='+qs('include-misses').checked);
qs('dl-csv-matched').addEventListener('click', ()=> window.location='/api/export?format=csv&tab=matched&include_misses='+qs('include-misses').checked);
qs('dl-csv-all').addEventListener('click', ()=> window.location='/api/export?format=csv&tab=all');
qs('dl-csv-wl').addEventListener('click', ()=> window.location='/api/export?format=csv&tab=watchlist');

// 看板
let chartTier, chartAward, chartAgency, chartKw, chartUrgency;
async function loadDashboard(){
  const res = await fetch('/api/dashboard'); const j = await res.json();
  if(j.empty){ return; }
  qs('d-total-budget').textContent = (j.kpi.total_budget/100000000>=1? (j.kpi.total_budget/100000000).toFixed(2)+' 億元' : (j.kpi.total_budget/10000).toFixed(0)+' 萬元');
  qs('d-avg').textContent = (j.kpi.avg_budget/10000).toFixed(1)+' 萬元';
  qs('d-max').textContent = (j.kpi.max_amount/10000).toFixed(0)+' 萬元';
  qs('d-conf').textContent = j.kpi.confirmed_ratio.toFixed(1)+'% ('+j.kpi.confirmed_count+'/'+j.kpi.total_count+')';

  const tierOrder = ["< 100萬 (公告金額以下)","100萬 ~ 500萬","500萬 ~ 1,000萬","1,000萬 ~ 5,000萬 (查核金額)","5,000萬 ~ 2億","≥ 2億 (巨額採購)","未公開 / 0 元"];
  const tierData = tierOrder.map(l=> (j.budget_tier.find(x=>x['級距']===l)||{['標案筆數']:0})['標案筆數']);
  if(chartTier) chartTier.destroy();
  chartTier = new Chart(document.getElementById('chart-tier'),{type:'bar',data:{labels:tierOrder,datasets:[{label:'筆數',data:tierData,backgroundColor:'#5b8def'}]},options:{responsive:true,plugins:{legend:{display:false}}}});

  if(chartAward) chartAward.destroy();
  chartAward = new Chart(document.getElementById('chart-award'),{type:'doughnut',data:{labels:j.award_composition.map(x=>x['決標方式類別']),datasets:[{data:j.award_composition.map(x=>x['筆數'])}]},options:{responsive:true}});

  if(chartAgency) chartAgency.destroy();
  chartAgency = new Chart(document.getElementById('chart-agency'),{type:'bar',data:{labels:j.top_agencies.map(x=>x['招標機關']),datasets:[{label:'筆數',data:j.top_agencies.map(x=>x['標案筆數']),backgroundColor:'#0ea5e9'}]},options:{responsive:true,indexAxis:'y'}});

  if(chartKw) chartKw.destroy();
  chartKw = new Chart(document.getElementById('chart-kw'),{type:'bar',data:{labels:j.keyword_ranking.map(x=>x['關鍵字']),datasets:[{label:'命中次數',data:j.keyword_ranking.map(x=>x['命中次數']),backgroundColor:'#a78bfa'}]},options:{responsive:true,indexAxis:'y'}});

  if(chartUrgency) chartUrgency.destroy();
  const uOrder = ["🔥 3天內即將截標","⏳ 4~7天內截標","📅 8~14天內截標","🗓️ 14天以上","⌛ 已截標 / 截止日期未定"];
  const uData = uOrder.map(l=> (j.urgency_bins.find(x=>x['急迫度分類']===l)||{['標案數量']:0})['標案數量']);
  chartUrgency = new Chart(document.getElementById('chart-urgency'),{type:'bar',data:{labels:uOrder,datasets:[{data:uData,backgroundColor:['#ef4444','#f59e0b','#3b82f6','#10b981','#64748b']} ]},options:{responsive:true,plugins:{legend:{display:false}}}});
}

async function loadLogs(){
  const r = await fetch('/api/logs'); const j = await r.json();
  qs('log-box').textContent = (j.logs||[]).join('\n') || '✅ 已就緒';
}
qs('btn-clear-log').addEventListener('click', ()=> qs('log-box').textContent='');
qs('dl-log').addEventListener('click', ()=>{
  const t = qs('log-box').textContent;
  const blob = new Blob([t],{type:'text/plain'});
  const a = document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='pcc_log_'+Date.now()+'.txt'; a.click();
});

// 初始
updateKpi(); loadTable('matched'); loadTable('all');
setInterval(updateKpi, 5000);
setInterval(loadLogs, 3000);
