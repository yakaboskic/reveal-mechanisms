// Design-only workspace continuity. These IDs and provider choices are not credentials.
const workspaceSessionKey='reveal.design.workspace.v1';
const workspaceProfileKey=provider=>'reveal.design.profile.v1.'+provider;
let workspaceStorageAvailable=true;
function readWorkspaceStorage(storage,key){
  try{return JSON.parse(storage.getItem(key)||'null')}catch{return null}
}
function freshWorkspace(){return {id:'preview:'+crypto.randomUUID(),provider:'anonymous',confirmed:false,gaps:[],accounts:[]}}
function validWorkspace(value){return value&&typeof value.id==='string'&&['anonymous','google','orcid'].includes(value.provider)&&Array.isArray(value.gaps)&&Array.isArray(value.accounts)}
let workspace=readWorkspaceStorage(sessionStorage,workspaceSessionKey);
if(!validWorkspace(workspace))workspace=freshWorkspace();
state.session=workspace.confirmed?workspace.provider:null;
state.dashboardTab='gaps';
function persistWorkspace(){
  try{
    sessionStorage.setItem(workspaceSessionKey,JSON.stringify(workspace));
    if(workspace.provider!=='anonymous')localStorage.setItem(workspaceProfileKey(workspace.provider),JSON.stringify(workspace));
  }catch{workspaceStorageAvailable=false}
}
persistWorkspace();
function workspaceSignedIn(){return workspace.provider!=='anonymous'}
function workspaceName(){return workspaceSignedIn()?'Researcher':'Anonymous researcher'}
function historyUpsert(collection,entry){
  const prior=collection.find(row=>row.id===entry.id);
  return [{...prior,...entry},...collection.filter(row=>row.id!==entry.id)].slice(0,100);
}
function rememberGap(gap){
  if(!gap)return;
  workspace.gaps=historyUpsert(workspace.gaps,{id:gap.object.id,updatedAt:new Date().toISOString(),anchors:[...state.anchors],manual:[...state.manual],dismissed:[...state.dismissed],kgs:[...state.kgs]});
  persistWorkspace();
}
function rememberAccount(completed=false){
  const prior=workspace.accounts.find(row=>row.id===account.id);
  workspace.accounts=historyUpsert(workspace.accounts,{id:account.id,updatedAt:new Date().toISOString(),completed:completed||!!prior?.completed});
  // Keep the account's exact gap relationship; never attach this example to an unrelated job gap.
  if(!workspace.gaps.some(row=>row.id===account.question)){
    workspace.gaps=historyUpsert(workspace.gaps,{id:account.question,updatedAt:new Date().toISOString(),anchors:[DATA.factors[0].source_id],manual:[DATA.factors[0].source_id],dismissed:[],kgs:['biomarkerkg','prokn']});
  }
  persistWorkspace();
}
function mergeHistory(a,b){return [...new Map([...a,...b].map(row=>[row.id,row])).values()].sort((x,y)=>y.updatedAt.localeCompare(x.updatedAt)).slice(0,100)}
function chooseWorkspaceIdentity(provider){
  if(provider!=='anonymous'){
    const saved=readWorkspaceStorage(localStorage,workspaceProfileKey(provider));
    if(validWorkspace(saved)&&saved.provider===provider){
      workspace={...saved,gaps:mergeHistory(saved.gaps,workspace.gaps),accounts:mergeHistory(saved.accounts,workspace.accounts)};
    }
  }
  workspace.provider=provider;workspace.confirmed=true;state.session=provider;persistWorkspace();renderWorkspaceChrome();
}
const workspaceAvatarIcon='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="12" cy="8" r="3.5"/><path d="M5 21v-2a7 7 0 0 1 14 0v2"/></svg>';
function renderWorkspaceChrome(){
  const signed=workspaceSignedIn();
  $('#workspace-nav').innerHTML=`<div class="workspace-avatar-wrap"><button class="workspace-avatar ${signed?'is-signed-in':''}" id="workspace-avatar" data-act="workspace-menu" aria-label="Your workspace, ${esc(workspaceName())}" aria-expanded="false" aria-controls="workspace-menu" title="Your workspace">${signed?'R':workspaceAvatarIcon}</button><div id="workspace-menu" class="workspace-menu" hidden><div class="workspace-menu-identity"><strong>${workspaceName()}</strong><span>${signed?'Signed-in preview · '+(workspace.provider==='orcid'?'ORCID':'Google'):'Temporary workspace'}</span></div><nav aria-label="Your workspace"><a href="#dashboard/gaps" data-act="workspace-route" data-key="gaps">Your knowledge gaps <span>${workspace.gaps.length}</span></a><a href="#dashboard/accounts" data-act="workspace-route" data-key="accounts">Your scientific accounts <span>${workspace.accounts.length}</span></a></nav><div class="workspace-menu-footer">${signed?'<button data-act="workspace-signout">Sign out of preview</button>':'<p>Sign in to keep your work.</p><button data-act="session">Sign in with ORCID or Google</button>'}</div></div></div>`;
}
function closeWorkspaceMenu(focus=false){$('#workspace-menu').hidden=true;$('#workspace-avatar').setAttribute('aria-expanded','false');if(focus)$('#workspace-avatar').focus()}
function workspaceDate(value){const date=new Date(value);return Number.isNaN(date.getTime())?'':date.toLocaleDateString(undefined,{month:'short',day:'numeric'})}
function workspaceRows(){
  if(state.dashboardTab==='gaps'){
    const rows=workspace.gaps.map(row=>({row,gap:DATA.gaps.find(g=>g.object.id===row.id)})).filter(item=>item.gap);
    if(!rows.length)return '<div class="workspace-empty"><h2>Your next question starts here.</h2><p>Knowledge gaps you explore will appear here.</p><button data-act="workspace-search">Explore knowledge gaps</button></div>';
    return `<div class="workspace-list">${rows.map(({row,gap})=>{const count=workspace.accounts.filter(a=>a.id===account.id&&account.question===row.id).length;return `<article class="workspace-gap-row"><a class="workspace-item-title" href="#home" data-act="workspace-gap" data-key="${esc(row.id)}">${esc(gap.object.text)}</a><div class="workspace-item-meta"><span>${esc(gap.object.scope)}</span><span>Explored ${esc(workspaceDate(row.updatedAt))}</span>${count?`<a href="#dashboard/accounts" data-act="workspace-route" data-key="accounts">${count} example scientific account</a>`:''}</div></article>`}).join('')}</div>`;
  }
  const rows=workspace.accounts.filter(row=>row.id===account.id);
  if(!rows.length)return '<div class="workspace-empty"><h2>Room for your findings.</h2><p>Scientific accounts will appear here as your analyses finish.</p><button data-act="workspace-example">Explore the example account</button></div>';
  return `<div class="workspace-list">${rows.map(row=>`<article class="workspace-account-row"><div class="workspace-item-meta"><span class="workspace-example-label">Example</span><span>${row.completed?'Analysis preview completed':'Viewed'} ${esc(workspaceDate(row.updatedAt))}</span></div><a class="workspace-item-title" href="#account" data-act="workspace-example">${esc(account.name)}</a><p class="workspace-account-conclusion">${esc(account.closing_remarks)}</p><div class="workspace-item-meta"><span>${account.component_claims.length} associated claims</span><a href="#home" data-act="workspace-gap" data-key="${esc(account.question)}">View knowledge gap</a></div></article>`).join('')}</div>`;
}
function workspaceDashboard(){
  const signed=workspaceSignedIn();
  $('#main').innerHTML=`<div class="account-study workspace-dashboard"><nav class="account-topbar"><button data-act="workspace-search">← Explore knowledge gaps</button></nav><div class="workspace-heading"><h1>Your workspace</h1><p>${signed?'Your knowledge gaps and scientific accounts, together.':'A place for the questions you explore and the accounts you build.'}</p></div><div class="workspace-continuity ${signed?'is-signed-in':''}"><div><strong>${signed?'Signed-in preview':'Anonymous workspace'}</strong><p>${!workspaceStorageAvailable?'Browser storage is unavailable. This preview’s history will be lost on reload.':signed?'Your history is kept in this browser for the design preview. Sign-in is simulated.':'History stays in this tab for now. Closing the session or clearing its data may lose access.'}</p></div>${signed?'':'<button data-act="session">Sign in to keep your work</button>'}</div><div class="account-reading-switch workspace-reading-switch" role="tablist" aria-label="Workspace view">${[['gaps','Knowledge gaps',workspace.gaps.length],['accounts','Scientific accounts',workspace.accounts.length]].map(([key,label,count])=>`<button id="workspace-tab-${key}" role="tab" aria-selected="${state.dashboardTab===key}" aria-controls="workspace-results" tabindex="${state.dashboardTab===key?0:-1}" data-act="workspace-tab" data-key="${key}">${label}<span>${count}</span></button>`).join('')}</div><div id="workspace-results" role="tabpanel" aria-labelledby="workspace-tab-${state.dashboardTab}">${workspaceRows()}</div></div>`;
}
function openWorkspace(view='gaps'){
  state.dashboardTab=view==='accounts'?'accounts':'gaps';closeWorkspaceMenu();navigate('dashboard');
  history.replaceState(null,'','#dashboard/'+state.dashboardTab);
}
function readWorkspaceRoute(){
  const match=location.hash.match(/^#dashboard(?:\/(gaps|accounts))?$/);if(!match)return false;
  const view=match[1]||'gaps';
  if(state.page==='dashboard'&&state.dashboardTab===view)return true;
  state.dashboardTab=view;state.page='dashboard';stopPlaceholder();clearTimeout(timer);state.playing=false;render();return true;
}
function restoreWorkspaceGap(id){
  const row=workspace.gaps.find(entry=>entry.id===id),gap=DATA.gaps.find(g=>g.object.id===id);if(!gap)return;
  closeWorkspaceMenu();state.gap=gap;state.text=gap.object.text;state.selected=true;state.accountDemo=false;state.referenceExample=false;state.derived=false;state.query='';
  state.contexts=gap.attachments.filter(a=>a.resolution==='resolved'&&a.target_kind==='pathophysiology').slice(0,10).map(a=>a.target_id);
  state.anchors=(row?.anchors||[]).filter(id=>DATA.factors.some(f=>f.source_id===id));state.manual=row?.manual||[];state.dismissed=row?.dismissed||[];state.kgs=row?.kgs||['biomarkerkg','prokn'];state.mechanismsOpen=false;navigate('home');
}
document.addEventListener('click',e=>{
  const t=e.target.closest('[data-act]'),action=t?.dataset.act,key=t?.dataset.key;
  if(action==='workspace-menu'){
    const open=$('#workspace-menu').hidden;$('#workspace-menu').hidden=!open;$('#workspace-avatar').setAttribute('aria-expanded',String(open));return;
  }
  if(!e.target.closest('.workspace-avatar-wrap'))closeWorkspaceMenu();
  if(action==='session')closeWorkspaceMenu();
  if(action==='workspace-route'){e.preventDefault();openWorkspace(key)}
  if(action==='workspace-tab'){openWorkspace(key);$('#workspace-tab-'+key).focus()}
  if(action==='workspace-gap'){e.preventDefault();restoreWorkspaceGap(key)}
  if(action==='workspace-example'){e.preventDefault();state.accountReading='conclusions';state.claimsOpen=false;state.claimId=null;demo()}
  if(action==='workspace-search'){state.selected=false;state.gap=null;state.query='';navigate('home')}
  if(action==='workspace-signout'){
    workspace=freshWorkspace();state.session=null;persistWorkspace();state.dashboardTab='gaps';openWorkspace('gaps');toast('Signed out of the preview. A new anonymous workspace is ready.');
  }
});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&!$('#workspace-menu').hidden){e.preventDefault();closeWorkspaceMenu(true)}
  if(e.target.matches('[data-act="workspace-tab"]')&&['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){
    e.preventDefault();const key=e.key==='Home'?'gaps':e.key==='End'?'accounts':state.dashboardTab==='gaps'?'accounts':'gaps';openWorkspace(key);$('#workspace-tab-'+key).focus();
  }
});
