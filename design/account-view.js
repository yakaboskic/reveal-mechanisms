// All claim/evidence content comes from the DAPPER packet; presentation metadata
// holds grouping, captured/mock labels and exact artifact access only.
const accountView=DATA.account_view;
function selectedClaim(){return nodes[state.claimId]}
function claimMeta(claim){return accountView.claim_views[claim.id]}
function evidenceFor(claim){return (claim.has_evidence||[]).map(id=>nodes[id])}
function originBadge(origin){return `<span class="evidence-origin ${origin==='illustrative'?'mock':''}">${origin==='illustrative'?'Illustrative · not retrieved':'Captured CFDE'}</span>`}
function claimNumber(claim){return 'C'+(account.component_claims.indexOf(claim.id)+1)}
function sameAccountGap(){return state.gap?.object.id===account.question}
function exampleNotice(){return state.gap&&!sameAccountGap()?'Reference account for a different DisMech gap.':'Authored example with captured CFDE and illustrative KG evidence.'}
function accountPreview(){ensureParagraphPreview();return `<section class="account-preview" aria-label="Scientific account example"><div class="account-preview-heading"><h3 id="accounts-title">Scientific accounts <span>1</span></h3><span class="example-tag">Design example</span></div><p class="account-example-note">${esc(exampleNotice())}</p><article class="account-result"><h4>${esc(account.name)}</h4>${!sameAccountGap()?`<p class="account-example-note">${esc(question.text)}</p>`:''}<p class="account-result-closing">${esc(account.closing_remarks)}</p><div class="account-facts"><span><b>3</b> genes</span><span><b>2</b> gene sets</span><span><b>${account.component_claims.length}</b> claims</span></div><div class="account-result-actions"><button class="inspect-account" data-act="preview-account" data-key="claim">View account <span aria-hidden="true">↗</span></button><div class="account-parts"><button data-act="preview-account" data-key="statement">Research statement</button><span class="account-paragraph-status" data-paragraph-status>${paragraphStatus()}</span></div></div></article></section>`}
function accountClaims(){return account.component_claims.map(id=>nodes[id])}
function matchingClaims(){
  const terms=state.claimQuery.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  return accountClaims().filter(c=>{
    const m=claimMeta(c),text=[nodes[c.proposition].statement,c.statement,m.label,m.group].join(' ').toLocaleLowerCase();
    return (state.claimGroup==='All'||state.claimGroup===m.group)&&terms.every(term=>text.includes(term));
  });
}
function alignClaimSelection(claims){if(!claims.some(c=>c.id===state.claimId))state.claimId=null}
function claimResultCount(claims){return claims.length===account.component_claims.length?String(claims.length):`${claims.length} of ${account.component_claims.length}`}
function claimInlineDetails(c){
  const ev=evidenceFor(c);
  return `<div class="claim-inline-body"><p class="claim-inline-assessment">${esc(c.statement)}</p><div class="claim-inline-status"><span>${esc(c.direction.toLowerCase())}</span><span>${esc(c.status)}</span></div><div class="claim-inline-sources">${ev.map(e=>{const v=accountView.evidence_views[e.id];return `<div><span>${esc(v.graph)} <small>${e.direction==='NEUTRAL'?'Context only':esc(e.direction.toLowerCase())}</small></span>${originBadge(v.origin)}</div>`}).join('')}</div><a class="claim-page-link" href="#claim/${encodeURIComponent(c.id)}">Open claim page <span aria-hidden="true">↗</span></a></div>`;
}
function claimList(claims){
  if(!claims.length)return `<div class="claim-empty"><p>No claims found</p><span>Try a different search or relationship.</span><button data-act="reset-claim-search">Clear search and filters</button></div>`;
  return claims.map(c=>{
    const m=claimMeta(c),ev=evidenceFor(c),open=c.id===state.claimId,n=claimNumber(c);
    return `<article class="account-claim-card ${open?'is-expanded':''}" id="claim-card-${n}"><button class="account-claim-option" id="claim-toggle-${n}" data-act="select-claim" data-key="${c.id}" aria-expanded="${open}" aria-controls="claim-inline-${n}"><span class="claim-ref">${n}</span><span><span class="claim-option-text">${esc(nodes[c.proposition].statement)}</span><span class="claim-option-meta"><span>${esc(m.group.endsWith('mechanism')?'Mechanism':m.group.endsWith('trait')?'Trait':'Gene-set membership')}</span><span>${ev.length} evidence item${ev.length===1?'':'s'}</span>${m.illustrative?originBadge('illustrative'):`${ev.some(e=>accountView.evidence_views[e.id].origin==='illustrative')?'<span class="evidence-origin mock">Illustrative KG context</span>':''}`}</span></span><span class="claim-selection-caret" aria-hidden="true">⌄</span></button><div class="claim-inline-details" id="claim-inline-${n}" ${open?'':'hidden'}>${claimInlineDetails(c)}</div></article>`;
  }).join('');
}
function toggleInlineClaim(id){
  state.claimId=state.claimId===id?null:id;
  document.querySelectorAll('.account-claim-option').forEach(button=>{
    const open=button.dataset.key===state.claimId;
    button.setAttribute('aria-expanded',String(open));
    button.closest('.account-claim-card').classList.toggle('is-expanded',open);
    document.getElementById(button.getAttribute('aria-controls')).hidden=!open;
  });
}
function restoreClaimCard(){
  const claim=selectedClaim();if(!claim)return;
  requestAnimationFrame(()=>{
    const button=document.getElementById('claim-toggle-'+claimNumber(claim));
    button?.scrollIntoView({block:'center'});button?.focus({preventScroll:true});
  });
}
function readClaimRoute(){
  if(!location.hash.startsWith('#claim/'))return false;
  let id;try{id=decodeURIComponent(location.hash.slice(7))}catch{id=null}
  state.claimId=account.component_claims.includes(id)?id:null;
  state.claimsOpen=true;state.page='claim';state.tab='claim';
  stopPlaceholder();clearTimeout(timer);state.playing=false;
  render();window.scrollTo(0,0);$('#main').focus({preventScroll:true});return true;
}
function claimPage(){
  const c=selectedClaim();
  const back='<a href="#account" data-act="claim-account">← Back to scientific account</a>';
  if(!c){$('#main').innerHTML=`<div class="account-study claim-page"><nav class="account-topbar">${back}</nav><h1>Claim not found</h1><p>This claim is not included in the design example.</p></div>`;return}
  $('#main').innerHTML=`<div class="account-study claim-page"><nav class="account-topbar" aria-label="Claim navigation">${back}${rawbutton(c,'DAPPER record')}</nav><div class="claim-page-heading"><p class="claim-page-kind">Proposition</p><h1>${esc(nodes[c.proposition].statement)}</h1><p class="claim-page-account">From <a href="#account" data-act="claim-account">${esc(account.name)}</a></p></div><section class="claim-detail-panel" aria-label="Claim details">${claimDetail()}</section></div>`;
}

function claimDetail(){
  if(!state.claimId)return '';
  return `<div class="account-reading-switch claim-reading-switch" role="tablist" aria-label="Claim details">${[['claim','Assessment'],['proposition','Proposition'],['evidence','Evidence'],['lineage','Provenance']].map(([key,label])=>`<button id="claim-tab-${key}" role="tab" aria-selected="${state.tab===key}" aria-controls="claim-reading-panel" tabindex="${state.tab===key?0:-1}" data-act="claim-reading" data-key="${key}">${label}</button>`).join('')}</div><div id="claim-reading-panel" role="tabpanel" aria-labelledby="claim-tab-${state.tab}" tabindex="0">${inspector()}</div>`;
}
function setClaimReading(view){
  state.tab=view;
  document.querySelectorAll('[data-act="claim-reading"]').forEach(b=>{
    const active=b.dataset.key===view;b.setAttribute('aria-selected',String(active));b.tabIndex=active?0:-1;
  });
  $('#claim-reading-panel').innerHTML=inspector();
  $('#claim-reading-panel').setAttribute('aria-labelledby','claim-tab-'+view);
}
function updateClaimResults(){
  const claims=matchingClaims();alignClaimSelection(claims);
  $('#claim-results').innerHTML=claimList(claims);
  $('#claim-result-count').textContent=claimResultCount(claims);
  updateClaimFilters();
}
function updateClaimFilters(){
  $('#claim-filters').hidden=!state.claimFiltersOpen;
  const toggle=$('#claim-filter-toggle'),active=state.claimGroup!=='All';
  toggle.setAttribute('aria-expanded',String(state.claimFiltersOpen));
  toggle.setAttribute('aria-label',active?`Filters, ${state.claimGroup} selected`:'Filters');
  toggle.classList.toggle('has-filter',active);
  $('#claim-filter-count').hidden=!active;
  document.querySelectorAll('#claim-filters [data-act="filter-claims"]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.key===state.claimGroup)));
}
// Local playback of an account-specific paragraph job. No generation request is sent.
const paragraphJobs=new Map();
function paragraphJob(){return paragraphJobs.get(account.id)}
function paragraphStatus(){return paragraphJob()?.status==='ready'?'Cited statement ready':'Preparing cited statement…'}
function ensureParagraphPreview(){
  if(paragraphJob())return;
  const job={status:'generating',message:'Writing from the account’s claims…',timers:[]};
  paragraphJobs.set(account.id,job);
  job.timers.push(setTimeout(()=>{job.message='Checking citations and references…';updateParagraphPresentation()},1800));
  job.timers.push(setTimeout(()=>{job.status='ready';updateParagraphPresentation()},4200));
}
function resetParagraphPreview(){const job=paragraphJob();job?.timers.forEach(clearTimeout);paragraphJobs.delete(account.id)}
function updateParagraphPresentation(){
  document.querySelectorAll('[data-paragraph-status]').forEach(el=>el.textContent=paragraphStatus());
  if(state.page!=='account'||!$('#account-research'))return;
  $('#statement-progress').hidden=paragraphJob()?.status==='ready';
  $('#account-research').innerHTML=paragraphView();
  $('#account-research').setAttribute('aria-busy',String(paragraphJob()?.status!=='ready'));
}
function setAccountReading(view){
  state.accountReading=view;
  document.querySelectorAll('[data-act="account-reading"]').forEach(b=>{
    const active=b.dataset.key===view;b.setAttribute('aria-selected',String(active));b.tabIndex=active?0:-1;
  });
  $('#account-conclusions').hidden=view!=='conclusions';
  $('#account-research').hidden=view!=='statement';
}
function citationModel(){
  const p=DATA.paragraph_document.paragraphs[0],refs=[],numbers=new Map(),endings=new Map();
  for(const c of [...p.citations].sort((a,b)=>a.start-b.start||a.end-b.end)){
    const key=c.target_id+'@'+c.citation_metadata_revision;
    if(!numbers.has(key)){
      const record=DATA.citation_registry.find(r=>r.target_id===c.target_id&&r.metadata_revision===c.citation_metadata_revision);
      if(!record)throw Error('Missing pinned citation record: '+key);
      refs.push(record);numbers.set(key,refs.length);
    }
    if(!endings.has(c.end))endings.set(c.end,[]);
    endings.get(c.end).push(numbers.get(key));
  }
  return {p,refs,endings};
}
function citedParagraphText(model){
  const chars=Array.from(model.p.text);let last=0,html='';
  for(const [end,numbers] of [...model.endings].sort((a,b)=>a[0]-b[0])){
    html+=esc(chars.slice(last,end).join(''))+'<sup class="research-citations">'+[...new Set(numbers)].map(n=>`<a href="#account-reference-${n}" data-act="account-reference" data-key="${n}" aria-label="Reference ${n}" title="${esc(model.refs[n-1].title)}">${n}</a>`).join(', ')+'</sup>';
    last=end;
  }
  return html+esc(chars.slice(last).join(''));
}
function referenceCredit(r){return r.byline.map(a=>a.literal_name||[a.family_name,a.given_name].filter(Boolean).join(', ')).join('; ')}
function copyParagraphFallback(payload){
  const selection=window.getSelection(),ranges=[];
  for(let i=0;i<(selection?.rangeCount||0);i++)ranges.push(selection.getRangeAt(i).cloneRange());
  const focused=document.activeElement,holder=document.createElement('div');
  holder.contentEditable='true';holder.style.cssText='position:fixed;left:-10000px;top:0;width:700px';holder.innerHTML=payload.html;
  document.body.append(holder);holder.focus();
  const range=document.createRange();range.selectNodeContents(holder);selection.removeAllRanges();selection.addRange(range);
  const write=e=>{if(e.clipboardData){e.clipboardData.setData('text/html',payload.html);e.clipboardData.setData('text/plain',payload.text);e.preventDefault()}};
  document.addEventListener('copy',write);
  try{return document.execCommand('copy')}catch{return false}finally{
    document.removeEventListener('copy',write);holder.remove();selection.removeAllRanges();
    for(const old of ranges)selection.addRange(old);focused?.focus({preventScroll:true});
  }
}
function showManualParagraphCopy(payload){
  $('#record-title').textContent='Copy paragraph and references';
  $('#record-body').innerHTML='<p>Select the text below and press ⌘C (Mac) or Ctrl+C (Windows). In Word, paste with Keep Source Formatting.</p><div id="manual-paragraph-copy" contenteditable="true" role="textbox" aria-label="Paragraph and references to copy" aria-multiline="true">'+payload.html+'</div>';
  $('#record').showModal();const target=$('#manual-paragraph-copy');target.focus();
  const range=document.createRange();range.selectNodeContents(target);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
}
async function copyParagraphForWord(button){
  const payload=DATA.paragraph_rich_text;
  button.disabled=true;button.textContent='Copying…';
  let copied=false;
  try{
    if(navigator.clipboard?.write&&typeof ClipboardItem!=='undefined'){
      await navigator.clipboard.write([new ClipboardItem({
        'text/html':new Blob([payload.html],{type:'text/html'}),
        'text/plain':new Blob([payload.text],{type:'text/plain'})
      })]);copied=true;
    }
  }catch{/* Browser permissions may require the selection-based copy fallback. */}
  try{if(!copied)copied=copyParagraphFallback(payload)}catch{/* Offer manual rich-text copy if clipboard access is unavailable. */}
  button.disabled=false;button.textContent=copied?'Copied for Word':'Copy for Word';
  const status=$('#paragraph-copy-status');
  if(status)status.textContent=copied?'Paragraph and references copied. In Word, choose Keep Source Formatting.':'Automatic copy is unavailable. Copy the selected text in the preview.';
  if(!copied)showManualParagraphCopy(payload);
}

function paragraphView(){
  const job=paragraphJob();
  if(job?.status!=='ready')return `<div class="statement-pending" role="status"><span class="statement-spinner" aria-hidden="true"></span><div><p>Preparing your research statement</p><span>${esc(job?.message||'Writing from the account’s claims…')}</span></div></div><p class="statement-pending-note">You can read the conclusions or explore the claims while this runs.</p>`;
  const model=citationModel();
  return `<article class="research-statement"><p class="research-prose">${citedParagraphText(model)}</p><div class="research-downloads" aria-label="Copy and download research statement"><button class="copy-for-word" data-act="copy-paragraph">Copy for Word</button><a href="data/cad-account/research-statement.md" download>Markdown</a><a href="data/cad-account/research-statement.tex" download data-act="latex-reminder">LaTeX</a><a href="data/cad-account/references.bib" download>BibTeX references</a></div><p id="paragraph-copy-status" class="paragraph-export-note" role="status"></p><p class="paragraph-export-note latex-export-note">For LaTeX, also download <a href="data/cad-account/references.bib" download>references.bib</a> and save both files in the same folder.</p><section class="research-references" aria-labelledby="references-heading"><h3 id="references-heading">References <span>${model.refs.length}</span></h3><ol>${model.refs.map((r,i)=>`<li id="account-reference-${i+1}" tabindex="-1"><p>${referenceCredit(r)?`<span>${esc(referenceCredit(r))} (${esc(r.issued_date?.slice(0,4)||'n.d.')}). </span>`:''}<button class="reference-title" data-act="inspect-reference" data-key="${i}">${esc(r.title)}</button></p><div class="reference-meta">DAPPER ${esc(r.target_class)} · ${esc(r.issued_date||'Date unavailable')} · Revision ${r.metadata_revision}${r.target_class==='Claim'&&accountView.claim_views[r.target_id]?.illustrative?' · Illustrative membership':''}</div></li>`).join('')}</ol></section></article>`;
}

function accountPage(){
  ensureParagraphPreview();
  const claims=accountClaims(),groups=[...new Set(claims.map(c=>claimMeta(c).group))],matches=matchingClaims();
  alignClaimSelection(matches);
  $('#main').innerHTML=`<div class="account-study"><nav class="account-topbar" aria-label="Account navigation"><button data-act="${state.accountDemo?'back-job':'home'}">← ${state.accountDemo?'Back to activity':'Search knowledge gaps'}</button><a href="data/cad-account/scientific-account.yaml" download>Download YAML packet</a></nav><header class="account-gap"><p class="gap-source">DisMech knowledge gap · Coronary artery disease</p><h1>${esc(question.text)}</h1><button class="job-anchor" data-act="raw" data-key="${doc.mechanisms[0].id}">${esc(doc.mechanisms[0].name)}</button></header><section class="account-summary" aria-label="Account synthesis"><div class="account-reading-switch" role="tablist" aria-label="Account summary view"><button id="conclusions-tab" role="tab" data-act="account-reading" data-key="conclusions" aria-selected="${state.accountReading==='conclusions'}" aria-controls="account-conclusions" tabindex="${state.accountReading==='conclusions'?0:-1}">Conclusions</button><button id="statement-tab" role="tab" data-act="account-reading" data-key="statement" aria-selected="${state.accountReading==='statement'}" aria-controls="account-research" tabindex="${state.accountReading==='statement'?0:-1}">Research statement<span id="statement-progress" class="statement-progress" ${paragraphJob()?.status==='ready'?'hidden':''}><span aria-hidden="true">·</span><span class="sr-only"> Preparing</span></span></button></div><h2>${esc(account.name)}</h2><div id="account-conclusions" class="account-reading-panel" role="tabpanel" aria-labelledby="conclusions-tab" ${state.accountReading==='conclusions'?'':'hidden'}><p class="account-conclusions">${esc(account.closing_remarks)}</p></div><div id="account-research" class="account-reading-panel" role="tabpanel" aria-labelledby="statement-tab" aria-busy="${paragraphJob()?.status!=='ready'}" ${state.accountReading==='statement'?'':'hidden'}>${paragraphView()}</div></section><section class="account-claims-section"><button class="account-claims-toggle" data-act="toggle-account-claims" aria-expanded="${state.claimsOpen}" aria-controls="account-claims-body"><span>Associated claims <small>${claims.length}</small></span><span class="claims-toggle-caret" aria-hidden="true">⌄</span></button><div id="account-claims-body" ${state.claimsOpen?'':'hidden'}><div class="account-workspace"><section class="claim-ledger" aria-label="Associated claims"><span id="claim-result-count" class="sr-only" role="status" aria-label="Matching claims">${claimResultCount(matches)}</span><div class="claim-search-toolbar"><label class="claim-search"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/></svg><input id="claim-search" type="search" aria-label="Search associated claims" placeholder="Search claims…" value="${esc(state.claimQuery)}" autocomplete="off" aria-controls="claim-results"></label><button id="claim-filter-toggle" class="claim-filter-toggle ${state.claimGroup!=='All'?'has-filter':''}" data-act="toggle-claim-filters" aria-label="${state.claimGroup!=='All'?esc('Filters, '+state.claimGroup+' selected'):'Filters'}" aria-expanded="${state.claimFiltersOpen}" aria-controls="claim-filters"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M3 7h4m4 0h10M3 17h10m4 0h4"/><circle cx="9" cy="7" r="2"/><circle cx="15" cy="17" r="2"/></svg>Filters<span id="claim-filter-count" aria-hidden="true" ${state.claimGroup==='All'?'hidden':''}>1</span></button></div><div id="claim-filters" class="claim-filters" role="group" aria-label="Filter by relationship" ${state.claimFiltersOpen?'':'hidden'}>${['All',...groups].map(g=>`<button data-act="filter-claims" data-key="${esc(g)}" aria-pressed="${state.claimGroup===g}">${esc(g)}</button>`).join('')}</div><div id="claim-results">${claimList(matches)}</div></section></div></div></section><details class="account-example-details"><summary>About this example and its evidence</summary><p>${esc(account.context)} Membership links and KG assertions are illustrative and unverified. ${esc(accountView.coverage)}</p><p>The research statement uses an authored DAPPER Paragraph. Background generation is simulated. References use the exact Claim and KnowledgeGap identities and citation metadata revisions; these are unpublished design records.</p><div class="example-records">${rawbutton(account,'Account record')}${rawbutton(question,'KnowledgeGap record')}${rawbutton(DATA.paragraph_document.paragraphs[0].id,'Paragraph record')}${btn('Full DAPPER document','account-doc','','text small')}<a href="data/cad-account/paragraph-packet.json" download>Paragraph + citations (JSON)</a><button data-act="replay-paragraph">Replay paragraph preparation</button></div></details></div>`;
}
function artifactLink(id){const f=nodes[id],m=accountView.source_files[id];return `<button class="source-artifact" data-act="fixture-source" data-key="${id}"><span>${esc(f.filename)}<small>${m.origin==='illustrative'?'Invented design artifact':'Captured source artifact'} · SHA-256 available</small></span><span aria-hidden="true">↗</span></button>`}
function inspector(){
  const c=selectedClaim(),p=nodes[c.proposition],ev=evidenceFor(c),m=claimMeta(c);
  if(state.tab==='claim')return `<h3>The assessment</h3>${m.illustrative?`<p class="mock-context">${originBadge('illustrative')} This membership claim is included to review the interaction. It is not established by the captured data.</p>`:''}<p class="detail-proposition">${esc(c.statement)}</p><dl class="keyval"><dt>Assessment</dt><dd>${esc(c.direction.charAt(0)+c.direction.slice(1).toLowerCase())}</dd><dt>Status</dt><dd>${esc(c.status.charAt(0).toUpperCase()+c.status.slice(1).toLowerCase())}</dd><dt>Evidence</dt><dd>${ev.length} linked item${ev.length===1?'':'s'}</dd></dl><p class="small-note">One claim assesses one proposition. The observed values and their interpretations live in its evidence items.</p>${btn('Inspect the evidence','tab','evidence','text small')}${rawbutton(c)}`;
  if(state.tab==='proposition')return `<h3>What is being assessed</h3><p class="detail-proposition">${esc(p.statement)}</p><dl class="keyval"><dt>Subject</dt><dd>${esc(entityLabel(p.subject_entity))}</dd><dt>Relationship</dt><dd>${esc(p.relation.split(':').at(-1).replaceAll('-',' '))}</dd><dt>Object</dt><dd>${esc(entityLabel(p.object_entity))}</dd></dl><h3>Scope</h3><p>${esc(p.scope)}</p>${rawbutton(p)}`;
  if(state.tab==='evidence')return `<h3>Evidence for this proposition</h3>${ev.map(e=>{const v=accountView.evidence_views[e.id];return `<section class="claim-evidence"><div class="evidence-heading"><strong>${esc(v.graph)}</strong>${originBadge(v.origin)}</div><p class="evidence-assessment">${e.direction==='NEUTRAL'?'Context only — does not independently support this proposition':esc(e.direction.toLowerCase())}</p>${v.metrics.length?`<div class="evidence-metrics">${v.metrics.map(x=>`<div class="metric-observation"><span>${esc(x.meaning)}<br><span>${esc(x.metric)}</span></span><b>${esc(x.value)}</b></div>`).join('')}</div>`:''}<p>${esc(e.explanation)}</p><details><summary class="small-note">Observed source excerpt</summary><pre class="source-snippet">${esc(e.snippet)}</pre><p class="small-note">${esc(v.locator)}</p></details>${e.was_derived_from.map(artifactLink).join('')}${rawbutton(e,'EvidenceItem record')}</section>`}).join('')}`;
  const files=[...new Set(ev.flatMap(e=>e.was_derived_from||[]))],sets=doc.gene_sets.filter(s=>p.subject_entity===s.id||p.object_entity===s.id);
  return `<h3>Follow the evidence to its source</h3><p class="small-note">Claim → EvidenceItem → source File. Original values remain in the captured rows.</p>${files.map(artifactLink).join('')}${sets.length?`<div class="rule"><h3>Referenced GeneSet</h3>${sets.map(s=>`<button class="source-artifact" data-act="raw" data-key="${s.id}"><span>${esc(accountView.gene_set_labels[s.id])}<small>Original catalog identity preserved; membership not populated</small></span>↗</button>`).join('')}</div>`:''}<div class="rule"><h3>Construction provenance</h3><p>These CFDE records currently reach the catalog import. Original collection and construction files have not been connected.</p><button class="source-artifact" data-act="hubmap"><span>Explore the separate HuBMAP provenance example<small>GeneSet → collection → processing → source files</small></span>↗</button></div>${rawbutton(c,'Claim record')}`;
}
function entityLabel(id){return accountView.gene_set_labels[id]||nodes[id]?.name||(id==='urn:cfde:trait:portal:CADinT2D'?'CAD in people with type 2 diabetes':id?.split(':').at(-1))||'Not specified'}
document.addEventListener('click',e=>{const t=e.target.closest('[data-act]');if(!t)return;const k=t.dataset.key;
  if(t.dataset.act==='toggle-account-claims'){
    state.claimsOpen=!state.claimsOpen;t.setAttribute('aria-expanded',String(state.claimsOpen));$('#account-claims-body').hidden=!state.claimsOpen;
    if(state.claimsOpen){
      updateClaimResults();
      requestAnimationFrame(()=>$('.account-claims-section').scrollIntoView({block:'start',behavior:matchMedia('(prefers-reduced-motion:reduce)').matches?'instant':'smooth'}));
    }
  }
  if(t.dataset.act==='copy-paragraph')copyParagraphForWord(t);
  if(t.dataset.act==='latex-reminder')toast('Also download references.bib and save it beside research-statement.tex.');
  if(t.dataset.act==='account-reading')setAccountReading(k);
  if(t.dataset.act==='claim-reading')setClaimReading(k);
  if(t.dataset.act==='account-reference'){
    e.preventDefault();const ref=document.getElementById('account-reference-'+k);ref.scrollIntoView({block:'center',behavior:matchMedia('(prefers-reduced-motion:reduce)').matches?'instant':'smooth'});ref.focus({preventScroll:true});
  }
  if(t.dataset.act==='inspect-reference'){
    const r=citationModel().refs[Number(k)];raw('Cited '+r.target_class,{object:nodes[r.target_id],citation_metadata:r},'Unpublished design record. Citation metadata revision '+r.metadata_revision+'.');
  }
  if(t.dataset.act==='replay-paragraph'){resetParagraphPreview();ensureParagraphPreview();updateParagraphPresentation();setAccountReading('statement');$('#statement-tab').focus();$('#account-research').scrollIntoView({block:'center'})}
  if(t.dataset.act==='select-claim')toggleInlineClaim(k);
  if(t.dataset.act==='claim-account'){e.preventDefault();state.claimsOpen=true;navigate('account');restoreClaimCard()}
  if(t.dataset.act==='filter-claims'){state.claimGroup=k;updateClaimResults()}
  if(t.dataset.act==='toggle-claim-filters'){state.claimFiltersOpen=!state.claimFiltersOpen;updateClaimFilters()}
  if(t.dataset.act==='reset-claim-search'){state.claimGroup='All';state.claimQuery='';$('#claim-search').value='';updateClaimResults();$('#claim-search').focus()}
  if(t.dataset.act==='fixture-source'){const f=nodes[k],v=accountView.source_files[k];raw(f.filename,{file:f,source:v.payload},v.origin==='illustrative'?'Invented for design. Not a retrieved KG result.':'Exact retained source artifact. The File digest and checksum identify its bytes.');const link=document.createElement('a');link.href=v.path;link.download=f.filename;link.className='btn small';link.textContent='Download source JSON';$('#record-body').prepend(link)}
});

document.addEventListener('input',e=>{if(e.target.id==='claim-search'){state.claimQuery=e.target.value;updateClaimResults()}});

document.addEventListener('keydown',e=>{
  if(e.target.matches('[data-act="claim-reading"]')&&['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){
    e.preventDefault();const keys=['claim','proposition','evidence','lineage'],i=keys.indexOf(state.tab);
    const view=keys[e.key==='Home'?0:e.key==='End'?keys.length-1:(i+(e.key==='ArrowRight'?1:-1)+keys.length)%keys.length];
    setClaimReading(view);$('#claim-tab-'+view).focus();return;
  }
  if(!e.target.matches('[data-act="account-reading"]')||!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;
  e.preventDefault();const view=e.key==='Home'?'conclusions':e.key==='End'?'statement':state.accountReading==='conclusions'?'statement':'conclusions';setAccountReading(view);document.querySelector(`[data-act="account-reading"][data-key="${view}"]`).focus();
});
