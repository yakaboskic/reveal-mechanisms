"""The portal's single-page UI (one HTML document, no external assets).

`serve` returns it at `/` with an empty API base; `html` writes it with the URL of a running `serve`
instance baked in, so the page can be opened from disk or any static host.
"""

import html
import json

CSS = r"""
:root { --ink:#1f2933; --muted:#65758b; --line:#d9e1ea; --soft:#f4f7f8; --bg:#f7f9fa; --panel:#fff;
  --accent:#0f766e; --accent-soft:#dff4f1; --warn:#9a5b1f; --warn-soft:#fbefe1; --hl:#d61ac7; --hl-soft:#fbe3f8;
  --bar:#5eb8ad; --bar2:#9fb4cf; }
* { box-sizing:border-box; }
body { margin:0; color:var(--ink); background:var(--bg); font:14px/1.45 ui-sans-serif,-apple-system,"Segoe UI",sans-serif; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
code { background:var(--soft); padding:1px 5px; border-radius:5px; font-size:12px; word-break:break-all; }
h1 { margin:0 0 4px; font-size:24px; letter-spacing:-0.02em; } h2 { margin:0 0 10px; font-size:16px; } h3 { margin:14px 0 6px; font-size:13.5px; }
.muted { color:var(--muted); font-size:12px; }
.topbar { position:sticky; top:0; z-index:15; background:var(--panel); border-bottom:1px solid var(--line); }
.topbar .inner { max-width:1600px; margin:0 auto; padding:10px 24px; display:flex; gap:18px; align-items:center; flex-wrap:wrap; }
.brand { font-weight:700; font-size:16px; color:var(--ink); } .brand small { display:block; font-weight:400; color:var(--muted); font-size:11px; }
nav a { margin-right:14px; font-weight:600; color:var(--muted); } nav a.on { color:var(--accent); }
.search { position:relative; flex:1; min-width:280px; max-width:640px; margin-left:auto; }
.search input { width:100%; border:1px solid var(--line); border-radius:10px; padding:9px 12px; font-size:14px; background:var(--soft); }
.search input:focus { outline:2px solid var(--accent-soft); border-color:var(--accent); background:#fff; }
.dropdown { position:absolute; left:0; right:0; top:calc(100% + 4px); background:#fff; border:1px solid var(--line); border-radius:12px; box-shadow:0 14px 34px rgba(31,41,51,.14); max-height:70vh; overflow:auto; z-index:30; }
.dropdown[hidden] { display:none; }
.dropdown .grp { padding:6px 12px 2px; font-size:10.5px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); }
.dropdown .it { padding:6px 12px; cursor:pointer; display:flex; gap:10px; align-items:baseline; }
.dropdown .it b { font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:60%; }
.dropdown .it span { color:var(--muted); font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.dropdown .it.on, .dropdown .it:hover { background:var(--accent-soft); }
.dropdown .none { padding:10px 12px; color:var(--muted); }
main { max-width:1600px; margin:0 auto; padding:18px 24px 60px; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:16px; min-width:0; margin-bottom:16px; }
.grid2 { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(0,1fr); gap:16px; }
.grid2 > .panel { margin-bottom:0; }
@media (max-width:1150px) { .grid2 { grid-template-columns:1fr; } }
.stats { display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); gap:10px; margin:10px 0 16px; }
.stat { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:8px 12px; }
.stat strong { display:block; font-size:18px; } .stat span { color:var(--muted); font-size:12px; }
.chips { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin:6px 0; }
.chip { display:inline-block; border:1px solid var(--line); background:#fff; border-radius:999px; padding:1px 9px; font-size:12px; white-space:nowrap; }
.chip.k { background:var(--soft); } .chip b { font-weight:600; }
.flag { display:inline-block; background:var(--warn-soft); color:var(--warn); border-radius:999px; padding:1px 8px; font-size:11px; font-weight:600; margin:1px 4px 1px 0; cursor:help; white-space:nowrap; }
.ok { color:var(--accent); font-weight:600; } .bad { color:var(--warn); font-weight:600; }
.dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:5px; vertical-align:0; }
.controls { display:flex; flex-wrap:wrap; gap:12px; align-items:end; margin-bottom:10px; }
.controls label { display:block; color:var(--muted); font-size:10.5px; text-transform:uppercase; letter-spacing:.08em; margin-bottom:3px; }
.controls .inline { display:flex; gap:6px; align-items:center; color:var(--ink); font-size:13px; text-transform:none; letter-spacing:0; margin:0; }
select, input[type=text] { border:1px solid var(--line); border-radius:8px; padding:6px 8px; font-size:13px; background:#fff; color:var(--ink); }
button { border:1px solid var(--line); background:#fff; border-radius:8px; padding:5px 10px; cursor:pointer; font-weight:600; color:var(--ink); }
button:hover { border-color:var(--accent); background:var(--accent-soft); } button.on { background:var(--accent); color:#fff; border-color:var(--accent); }
.seg button { border-radius:0; margin-left:-1px; } .seg button:first-child { border-radius:8px 0 0 8px; } .seg button:last-child { border-radius:0 8px 8px 0; }
table { width:100%; border-collapse:collapse; font-size:12.5px; }
th, td { padding:5px 7px; border-bottom:1px solid var(--line); text-align:left; vertical-align:middle; }
th { position:sticky; top:0; background:#fff; z-index:1; font-weight:600; white-space:nowrap; }
th.s { cursor:pointer; user-select:none; } th.s:hover { color:var(--accent); }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
td.wrap { min-width:16ch; max-width:46ch; word-break:break-word; }
td.nw { white-space:nowrap; }
tr.click { cursor:pointer; } tr.click:hover td { background:var(--accent-soft); } tr.cur td { background:var(--hl-soft); }
.scroll { overflow:auto; max-height:640px; border:1px solid var(--line); border-radius:9px; }
.pager { display:flex; gap:6px; align-items:center; justify-content:flex-end; margin:4px 0; font-size:12px; color:var(--muted); }
.pager button { padding:2px 8px; font-weight:500; } .pager button:disabled { opacity:.35; cursor:default; }
.bar { position:relative; min-width:86px; height:18px; background:var(--soft); border-radius:4px; overflow:hidden; }
.bar i { position:absolute; left:0; top:0; bottom:0; background:var(--bar); opacity:.75; }
.bar span { position:relative; padding:0 5px; font-size:11.5px; line-height:18px; font-variant-numeric:tabular-nums; }
.genes { font-size:11.5px; color:var(--muted); }
.kv { display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); gap:6px 14px; font-size:12.5px; margin:8px 0; }
.kv span { display:block; color:var(--muted); font-size:11px; }
details summary { cursor:pointer; color:var(--muted); font-size:12.5px; user-select:none; margin:6px 0; }
.tabs { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0 12px; }
.tabs a { border:1px solid var(--line); background:#fff; border-radius:999px; padding:3px 10px; font-size:12px; color:var(--ink); max-width:26ch; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.tabs a.on { background:var(--accent); border-color:var(--accent); color:#fff; } .tabs a.flagged:not(.on) { border-color:#e7c79f; }
.crumb { font-size:12.5px; color:var(--muted); margin-bottom:4px; }
.error { background:var(--warn-soft); color:var(--warn); border:1px solid #e7c79f; padding:10px 14px; border-radius:10px; }
.note { background:var(--soft); border-radius:10px; padding:8px 12px; font-size:12.5px; color:var(--muted); margin:8px 0; }
.heat td.cell { text-align:center; font-size:11px; font-variant-numeric:tabular-nums; min-width:62px; border-left:1px solid #fff; }
.heat th.fac { writing-mode:vertical-rl; transform:rotate(180deg); white-space:nowrap; max-height:180px; font-size:11.5px; vertical-align:bottom; }
svg text { fill:var(--muted); font-size:10.5px; }
#sheet { position:fixed; top:0; right:0; height:100vh; width:820px; max-width:94vw; background:#fff; border-left:1px solid var(--line); box-shadow:-14px 0 34px rgba(31,41,51,.1); transform:translateX(105%); transition:transform .18s; overflow:auto; padding:18px 22px 40px; z-index:25; }
body.sheet-open #sheet { transform:none; }
#backdrop { position:fixed; inset:0; background:rgba(31,41,51,.18); z-index:24; display:none; } body.sheet-open #backdrop { display:block; }
#sheet .head { display:flex; gap:10px; align-items:start; } #sheet .head h2 { flex:1; font-size:17px; word-break:break-word; margin:2px 0 6px; }
#sheet .kind { color:var(--muted); font-size:10.5px; text-transform:uppercase; letter-spacing:.08em; }
#sheet .scroll { max-height:none; }
"""

SCRIPT = r"""
const API_BASE = (window.FACTOR_PORTAL_API_BASE || '').replace(/\/+$/, '');
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => [...(root || document).querySelectorAll(sel)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const enc = encodeURIComponent;
const fmt = (v, d = 3) => v === null || v === undefined ? '–' : v === 0 ? '0' : Math.abs(v) < 0.001 ? (+v).toExponential(1) : (+v).toFixed(d);
const pct = v => v === null || v === undefined ? '–' : Math.round(100 * v) + '%';
const int = v => v === null || v === undefined ? '–' : (+v).toLocaleString();
const href = {
  trait: t => '#/trait/' + enc(t), factor: (id, gs) => '#/factor/' + enc(id) + (gs ? '?gs=' + enc(gs) : ''),
  geneSet: id => '#/gene-set/' + enc(id), gene: g => '#/gene/' + enc(g), factors: flag => '#/factors' + (flag ? '?flag=' + enc(flag) : ''),
};
let SUMMARY = null;
const PALETTE = ['#0f766e', '#b45309', '#7c3aed', '#db2777', '#2563eb', '#4d7c0f', '#be123c', '#0369a1'];
const libColor = lib => { const libs = (SUMMARY ? SUMMARY.libraries.map(l => l.library) : []); const i = libs.indexOf(lib); return i < 0 ? '#94a3b8' : PALETTE[i % PALETTE.length]; };
const libTag = lib => `<span class="nw"><span class="dot" style="background:${libColor(lib)}"></span>${esc(lib)}</span>`;
const flagText = f => ((SUMMARY && SUMMARY.meta.flags) || []).find(x => x.flag === f)?.text || f;
const flagChips = flags => (flags || '').split(',').filter(Boolean).map(f => `<span class="flag" title="${esc(flagText(f))}">${esc(f)}</span>`).join('');
const bar = (v, max, color, label) => v === null || v === undefined ? '–' : `<div class="bar"><i style="width:${Math.max(0, Math.min(100, 100 * v / (max || 1)))}%;${color ? 'background:' + color : ''}"></i><span>${label ?? fmt(v)}</span></div>`;

async function api(path, params) {
  const url = new URL(API_BASE + path, location.href);
  Object.entries(params || {}).forEach(([k, v]) => { if (v !== '' && v !== null && v !== undefined) url.searchParams.set(k, v); });
  let res;
  try { res = await fetch(url); } catch (err) { throw new Error(`cannot reach the portal server at ${API_BASE || location.origin} (${err.message}). Start it with: python -m factor_portal serve --db <portal.sqlite>`); }
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}

// Sortable, paginated table. cols: [{key, label, num, nosort, value(row), html(row), cls}]
function dataTable(host, opts) {
  const st = { col: opts.sort ? opts.sort.col : null, desc: opts.sort ? opts.sort.desc !== false : true, page: 0 };
  let rows = opts.rows; const size = opts.pageSize || 25;
  const val = (c, r) => c.value ? c.value(r) : r[c.key];
  const cmp = (a, b) => { const na = a === null || a === undefined || a === '', nb = b === null || b === undefined || b === '';
    if (na || nb) return na === nb ? 0 : (na ? 1 : -1) * (st.desc ? -1 : 1);
    return typeof a === 'string' ? a.localeCompare(b, undefined, { sensitivity: 'base' }) : a - b; };
  function draw() {
    let data = rows;
    const c = opts.cols.find(c => c.key === st.col);
    if (c) data = rows.slice().sort((a, b) => cmp(val(c, a), val(c, b)) * (st.desc ? -1 : 1));
    const n = Math.max(1, Math.ceil(data.length / size)); st.page = Math.min(st.page, n - 1);
    const slice = data.slice(st.page * size, (st.page + 1) * size);
    const pager = data.length > size ? `<div class="pager"><span>${int(st.page * size + 1)}–${int(Math.min(data.length, (st.page + 1) * size))} of ${int(data.length)}</span>
      <button data-p="0" ${st.page === 0 ? 'disabled' : ''}>«</button><button data-p="${st.page - 1}" ${st.page === 0 ? 'disabled' : ''}>‹</button>
      <button data-p="${st.page + 1}" ${st.page >= n - 1 ? 'disabled' : ''}>›</button><button data-p="${n - 1}" ${st.page >= n - 1 ? 'disabled' : ''}>»</button></div>`
      : `<div class="pager"><span>${int(data.length)} row${data.length === 1 ? '' : 's'}</span></div>`;
    const head = opts.cols.map(c => `<th class="${c.num ? 'num' : ''} ${c.nosort ? '' : 's'}" data-k="${c.key}" title="${esc(c.title || '')}">${esc(c.label)}${st.col === c.key ? (st.desc ? ' ▼' : ' ▲') : ''}</th>`).join('');
    const body = slice.map((r, i) => `<tr class="${opts.onRow ? 'click' : ''} ${opts.rowClass ? opts.rowClass(r) : ''}" data-i="${st.page * size + i}">` +
      opts.cols.map(c => `<td class="${c.num ? 'num' : ''} ${c.cls || ''}">${c.html ? c.html(r) : esc(val(c, r) ?? '–')}</td>`).join('') + '</tr>').join('');
    host.innerHTML = pager + `<div class="scroll"><table><thead><tr>${head}</tr></thead><tbody>${body || `<tr><td colspan="${opts.cols.length}" class="muted">Nothing to show.</td></tr>`}</tbody></table></div>`;
    $$('th.s', host).forEach(th => th.onclick = () => { const k = th.dataset.k; if (st.col === k) st.desc = !st.desc; else { st.col = k; const c = opts.cols.find(c => c.key === k); st.desc = !!c.num; } draw(); });
    $$('.pager button[data-p]', host).forEach(b => b.onclick = () => { st.page = +b.dataset.p; draw(); });
    if (opts.onRow) $$('tbody tr[data-i]', host).forEach(tr => tr.onclick = e => { const a = e.target.closest('a'); if (a && !a.hasAttribute('data-row')) return; if (a) e.preventDefault(); opts.onRow(data[+tr.dataset.i], tr); });
  }
  draw();
  return { update(newRows) { rows = newRows; st.page = 0; draw(); } };
}

function ontologyUrl(curie) {
  const [prefix, local] = (curie || '').split(':'); const p = (prefix || '').toUpperCase();
  if (!local) return 'https://bioregistry.io/' + enc(curie || '');
  if (p === 'MESH') return 'https://meshb.nlm.nih.gov/record/ui?ui=' + enc(local);
  if (p === 'ORPHANET' || p === 'ORPHA') return 'https://www.orpha.net/en/disease/detail/' + enc(local);
  if (['EFO', 'MONDO', 'DOID', 'HP', 'OBA', 'CMO', 'NCIT', 'UBERON', 'GO'].includes(p)) return 'https://www.ebi.ac.uk/ols4/search?q=' + enc(curie);
  return 'https://bioregistry.io/' + enc(curie);
}

// ---------------------------------------------------------------------------------------------- search
function setupSearch() {
  const input = $('#q'), box = $('#dd'); let items = [], active = -1, seq = 0, timer = null;
  const kinds = [['traits', 'Traits', r => ({ go: href.trait(r.trait), main: r.phenotype_name || r.trait, sub: `${r.trait} · ${r.kpn_trait_id} · ${r.n_factors} factors` })],
    ['factors', 'Factors', r => ({ go: href.factor(r.factor_id), main: r.label || r.factor_id, sub: `${r.factor_id} · ${r.phenotype_name || ''}` })],
    ['gene_sets', 'Gene sets', r => ({ go: href.geneSet(r.gene_set_id), main: r.gene_set_name, sub: `${r.library} · ${r.n_universe} genes · top in ${r.n_top_joint_factors} factors` })],
    ['genes', 'Genes', r => ({ go: href.gene(r.gene), main: r.gene, sub: r.in_universe ? `loaded on ${r.n_factors} factors` : 'not in the EAGGL gene universe' })]];
  const render = res => {
    items = []; let html = '';
    kinds.forEach(([k, label, f]) => { if (!res[k].length) return; html += `<div class="grp">${label}</div>`;
      res[k].forEach(r => { const it = f(r); html += `<div class="it" data-i="${items.length}"><b>${esc(it.main)}</b><span>${esc(it.sub)}</span></div>`; items.push(it); }); });
    box.innerHTML = html || `<div class="none">No trait, factor, gene set or gene matches “${esc(res.query)}”.</div>`;
    active = items.length ? 0 : -1; mark(); box.hidden = false;
    $$('.it', box).forEach(d => d.onmousedown = e => { e.preventDefault(); choose(+d.dataset.i); });
  };
  const mark = () => $$('.it', box).forEach((d, i) => d.classList.toggle('on', i === active));
  const choose = i => { if (i < 0 || i >= items.length) return; location.hash = items[i].go; box.hidden = true; input.blur(); };
  input.addEventListener('input', () => { clearTimeout(timer); const q = input.value.trim(); if (!q) { box.hidden = true; return; }
    timer = setTimeout(async () => { const my = ++seq; try { const res = await api('/api/search', { q, limit: 8 }); if (my === seq) render(res); } catch (e) { box.innerHTML = `<div class="none">${esc(e.message)}</div>`; box.hidden = false; } }, 140); });
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') { active = Math.min(active + 1, items.length - 1); mark(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { active = Math.max(active - 1, 0); mark(); e.preventDefault(); }
    else if (e.key === 'Enter') { choose(active); e.preventDefault(); }
    else if (e.key === 'Escape') { box.hidden = true; }
  });
  input.addEventListener('blur', () => setTimeout(() => { box.hidden = true; }, 150));
  input.addEventListener('focus', () => { if (items.length && input.value.trim()) box.hidden = false; });
  document.addEventListener('keydown', e => { if (e.key === '/' && document.activeElement !== input && !e.target.closest('input,select,textarea')) { e.preventDefault(); input.focus(); input.select(); } });
}

// ---------------------------------------------------------------------------------------------- router
function parseHash() {
  const h = location.hash.replace(/^#\/?/, ''); const i = h.indexOf('?');
  const path = i < 0 ? h : h.slice(0, i), query = i < 0 ? '' : h.slice(i + 1);
  return { parts: path.split('/').filter(Boolean).map(decodeURIComponent), params: new URLSearchParams(query) };
}
async function route() {
  const { parts, params } = parseHash(); const view = $('#view');
  document.body.classList.remove('sheet-open');
  $$('nav a').forEach(a => a.classList.toggle('on', (a.dataset.nav || '') === (parts[0] || '')));
  view.innerHTML = '<p class="muted">Loading…</p>';
  const id = parts.slice(1).join('/');
  try {
    if (!parts.length) await renderOverview(view);
    else if (parts[0] === 'traits') await renderTraits(view);
    else if (parts[0] === 'factors') await renderFactors(view, params);
    else if (parts[0] === 'hubs') await renderHubs(view);
    else if (parts[0] === 'trait') await renderTrait(view, id);
    else if (parts[0] === 'factor') await renderFactor(view, id, params);
    else if (parts[0] === 'gene-set') await renderGeneSet(view, id);
    else if (parts[0] === 'gene') await renderGene(view, id);
    else view.innerHTML = '<div class="error">Unknown page.</div>';
  } catch (e) { view.innerHTML = `<div class="error">${esc(e.message)}</div>`; }
  window.scrollTo(0, 0);
}
function setParam(key, value) {
  const { parts, params } = parseHash(); if (value) params.set(key, value); else params.delete(key);
  const q = params.toString(); history.replaceState(null, '', '#/' + parts.map(enc).join('/') + (q ? '?' + q : ''));
}

// ---------------------------------------------------------------------------------------------- overview
async function renderOverview(view) {
  const m = SUMMARY.meta, th = m.thresholds;
  const ok = m.marginal_check_max_absdiff <= m.marginal_check_tolerance;
  view.innerHTML = `<h1>Projection audit</h1>
    <p class="muted">${esc(m.project)} · CFDE snapshot ${esc(m.cfde_snapshot)} · EAGGL ${esc(m.loading_variant)} loadings · KPN ${esc(m.kpn_release)} · eaggl ${esc((m.pigean_commit || '').slice(0, 7))} · top ${m.top_n} per factor</p>
    <div class="stats">
      <div class="stat"><strong>${int(m.n_traits)}</strong><span>traits (${int(m.n_traits_qc_pass)} passed projection QC)</span></div>
      <div class="stat"><strong>${int(m.n_factors)}</strong><span>factors · <a href="${href.factors()}">${int(m.n_flagged_factors)} flagged</a></span></div>
      <div class="stat"><strong>${int(m.n_gene_sets)}</strong><span>gene sets in ${int(m.n_collections)} collections</span></div>
      <div class="stat"><strong>${int(m.n_factor_gene_loadings)}</strong><span>nonzero gene loadings (${int(m.universe_size)} genes)</span></div>
      <div class="stat"><strong>${int(m.n_top_rows)}</strong><span>top (factor, gene set) rows</span></div>
      <div class="stat"><strong>${int(m.n_hub_gene_sets)}</strong><span><a href="#/hubs">hub gene sets</a> (joint top-${m.top_n} of ≥ ${th.hub_min_factors} factors)</span></div>
      <div class="stat"><strong class="${ok ? 'ok' : 'bad'}">${fmt(m.marginal_check_max_absdiff, 6)}</strong><span>max |marginal − Σ w<sub>g</sub>/wᵀw| over ${int(m.marginal_check_rows)} rows (tolerance ${m.marginal_check_tolerance})</span></div>
      <div class="stat"><strong>${m.has_sibling_loadings ? int(m.n_sibling_rows) : 'no'}</strong><span>sibling-factor loadings ${m.has_sibling_loadings ? '' : '(built without --long-file)'}</span></div>
    </div>
    <div class="grid2"><div class="panel"><h2>Audit flags</h2><p class="muted">Heuristics that mark the tail of each distribution for review; a flag is a prompt to look, not a verdict.</p><div id="flags"></div></div>
      <div class="panel"><h2>Libraries in the joint top-${m.top_n}</h2><p class="muted">Representation = share of all joint top-${m.top_n} slots ÷ share of all gene sets. Loadings are not normalised for gene-set size.</p><div id="libs"></div></div></div>
    <div class="grid2" style="margin-top:16px"><div class="panel"><h2>Hub gene sets</h2><p class="muted">Gene sets in the joint top-${m.top_n} of the most factors. A hub tends to reflect generic signal (large or ubiquitous signatures) rather than a factor's biology. <a href="#/hubs">All hubs</a></p><div id="hubs"></div></div>
      <div class="panel"><h2>Trait groups</h2><div id="groups"></div></div></div>`;
  dataTable($('#flags'), { rows: m.flags.map(f => Object.assign({ n: m.flag_counts[f.flag] || 0 }, f)), cols: [
    { key: 'flag', label: 'Flag', html: r => `<a href="${href.factors(r.flag)}">${flagChips(r.flag)}</a>` },
    { key: 'text', label: 'Definition', cls: 'wrap' }, { key: 'n', label: 'Factors', num: true, html: r => `<a href="${href.factors(r.flag)}">${int(r.n)}</a>` }] });
  dataTable($('#libs'), { rows: SUMMARY.libraries, sort: { col: 'n_top_joint_slots', desc: true }, cols: [
    { key: 'library', label: 'Library', html: r => libTag(r.library) }, { key: 'n_gene_sets', label: 'Gene sets', num: true, html: r => int(r.n_gene_sets) },
    { key: 'set_share', label: 'Share of sets', num: true, html: r => pct(r.set_share) }, { key: 'top_joint_share', label: 'Share of top slots', num: true, html: r => pct(r.top_joint_share) },
    { key: 'representation', label: 'Representation', num: true, html: r => fmt(r.representation, 2) + '×' },
    { key: 'n_factors_with_top', label: 'Factors using it', num: true, html: r => int(r.n_factors_with_top) },
    { key: 'n_factors_dominant', label: 'Factors dominated', num: true, html: r => int(r.n_factors_dominant), title: 'Factors whose joint top-N has this as its most frequent library' }] });
  const hubs = (await api('/api/hubs', { limit: 12 })).gene_sets;
  hubTable($('#hubs'), hubs, 12);
  dataTable($('#groups'), { rows: SUMMARY.trait_groups, cols: [
    { key: 'trait_group', label: 'Trait group' }, { key: 'n_traits', label: 'Traits', num: true }, { key: 'n_factors', label: 'Factors', num: true },
    { key: 'n_flagged', label: 'Flagged factors', num: true }] });
}
function hubTable(host, rows, pageSize) {
  const max = Math.max(1, ...rows.map(r => r.n_top_joint_factors));
  dataTable(host, { rows, pageSize, sort: { col: 'n_top_joint_factors', desc: true }, onRow: r => location.hash = href.geneSet(r.gene_set_id), cols: [
    { key: 'gene_set_name', label: 'Gene set', cls: 'wrap', html: r => `<a href="${href.geneSet(r.gene_set_id)}">${esc(r.gene_set_name)}</a>` },
    { key: 'library', label: 'Library', html: r => libTag(r.library) }, { key: 'n_universe', label: 'Genes', num: true, title: 'Member genes in the EAGGL universe' },
    { key: 'n_top_joint_factors', label: 'Joint top of', num: true, html: r => bar(r.n_top_joint_factors, max, '#e0a96d', int(r.n_top_joint_factors)) },
    { key: 'n_top_joint_traits', label: 'Traits', num: true }] });
}
async function renderHubs(view) {
  const m = SUMMARY.meta;
  view.innerHTML = `<h1>Hub gene sets</h1><p class="muted">${int(m.n_gene_sets_in_any_joint_top)} gene sets are in the joint top-${m.top_n} of at least one factor; these are the 5,000 in the most. Hubs (≥ ${m.thresholds.hub_min_factors} factors): ${int(m.n_hub_gene_sets)}. The full list is the portal's gene-set audit TSV.</p><div class="panel"><div id="t"></div></div>`;
  hubTable($('#t'), (await api('/api/hubs', { limit: 5000 })).gene_sets, 50);
}

// ---------------------------------------------------------------------------------------------- traits
async function renderTraits(view) {
  const traits = (await api('/api/traits')).traits;
  const groups = [...new Set(traits.map(t => t.trait_group))].sort();
  view.innerHTML = `<h1>Traits</h1><div class="panel"><div class="controls"><div><label>Filter</label><input type="text" id="tf" placeholder="name, trait id or KPN id"></div>
    <div><label>Trait group</label><select id="tg"><option value="">All</option>${groups.map(g => `<option>${esc(g)}</option>`).join('')}</select></div></div><div id="t"></div></div>`;
  const table = dataTable($('#t'), { rows: traits, pageSize: 50, onRow: r => location.hash = href.trait(r.trait), cols: [
    { key: 'phenotype_name', label: 'Trait', cls: 'wrap', html: r => `<a href="${href.trait(r.trait)}">${esc(r.phenotype_name || r.trait)}</a>` },
    { key: 'trait', label: 'EAGGL trait', cls: 'nw' }, { key: 'kpn_trait_id', label: 'KPN id', cls: 'nw' }, { key: 'trait_group', label: 'Group' },
    { key: 'trait_type', label: 'Type' }, { key: 'gwas_source_category', label: 'Source' }, { key: 'n_factors', label: 'Factors', num: true },
    { key: 'n_flagged_factors', label: 'Flagged', num: true }, { key: 'qc_pass', label: 'QC', html: r => r.qc_pass ? '<span class="ok">pass</span>' : '<span class="bad">fail</span>' }] });
  const apply = () => { const q = $('#tf').value.trim().toLowerCase(), g = $('#tg').value;
    table.update(traits.filter(t => (!g || t.trait_group === g) && (!q || [t.trait, t.kpn_trait_id, t.phenotype_name].some(x => (x || '').toLowerCase().includes(q))))); };
  $('#tf').oninput = apply; $('#tg').onchange = apply;
}

async function renderTrait(view, trait) {
  const d = await api('/api/trait', { id: trait }); const t = d.trait;
  view.innerHTML = `<div class="crumb"><a href="#/traits">Traits</a></div><h1>${esc(t.phenotype_name || t.trait)}</h1>
    <div class="chips"><span class="chip k">EAGGL <b>${esc(t.trait)}</b></span><span class="chip k">${esc(t.kpn_trait_id)}</span><span class="chip">${esc(t.trait_group)}</span>
      <span class="chip">${esc(t.trait_type)}</span><span class="chip">${esc(t.gwas_source_category)}</span><span class="chip">${t.n_factors} factors</span>
      <span class="chip">projection QC ${t.qc_pass ? '<span class="ok">pass</span>' : '<span class="bad">fail</span>'}</span></div>
    ${t.description && t.description !== t.phenotype_name ? `<p>${esc(t.description)}</p>` : ''}
    ${d.mappings.length ? `<details><summary>Ontology mappings (${d.mappings.length})</summary><div class="chips">${d.mappings.map(m => `<a class="chip" target="_blank" rel="noopener" href="${ontologyUrl(m.target_id)}" title="${esc([m.predicate, m.confidence && 'confidence ' + m.confidence, m.source].filter(Boolean).join(' · '))}"><b>${esc(m.target_id)}</b> ${esc(m.target_label || '')}</a>`).join('')}</div></details>` : ''}
    <details><summary>Projection QC</summary><div class="kv">${Object.entries(t.qc).map(([k, v]) => `<div><span>${esc(k)}</span>${esc(v)}</div>`).join('')}</div></details>
    <div class="panel"><h2>Factors</h2><div id="f"></div></div>
    <div class="panel"><div class="controls" style="justify-content:space-between"><div><h2 style="margin:0">Top gene sets across this trait's factors</h2>
      <p class="muted" id="mnote" style="margin:4px 0 0"></p></div>
      <div style="display:flex;gap:12px;align-items:end"><div><label>Per factor</label><select id="mn"><option>3</option><option selected>5</option><option>10</option></select></div>
      <div><label>Cell</label><span class="seg" id="mv"><button class="on" data-v="joint">joint</button><button data-v="marginal">marginal</button></span></div></div></div><div id="m"></div></div>`;
  dataTable($('#f'), { rows: d.factors, pageSize: 50, sort: { col: 'factor_number', desc: false }, onRow: r => location.hash = href.factor(r.factor_id), cols: [
    { key: 'factor_number', label: 'Factor', num: true, html: r => `<a href="${href.factor(r.factor_id)}">${esc(r.factor)}</a>` },
    { key: 'label', label: 'EAGGL label', cls: 'wrap' }, { key: 'n_nonzero', label: 'Genes', num: true },
    { key: 'top_genes', label: 'Top genes', nosort: true, cls: 'wrap genes', html: r => esc(r.top_genes.join(' ')) },
    { key: 'top_joint_gene_set', label: 'Top joint gene set', cls: 'wrap', html: r => esc(r.top_joint_gene_set) },
    { key: 'top_joint_loading', label: 'Joint', num: true, html: r => bar(r.top_joint_loading, 1) },
    { key: 'jm_top_overlap', label: 'J∩M', num: true, html: r => pct(r.jm_top_overlap), title: 'Share of the joint top-N also in the marginal top-N' },
    { key: 'own_top_frac', label: 'Own', num: true, html: r => pct(r.own_top_frac), title: "Share of the joint top-N that load highest on this factor among the trait's factors" },
    { key: 'flags', label: 'Flags', html: r => flagChips(r.flags) }] });
  let mode = 'joint', matrix = null;
  const drawMatrix = () => {
    $('#mnote').textContent = matrix.complete ? 'Each row is in the joint top of at least one factor; cells show its loading on every factor of the trait (rank in brackets).'
      : 'Built without the per-trait long files: cells outside a factor’s top list are empty.';
    $('#m').innerHTML = `<div class="scroll heat"><table><thead><tr><th>Gene set</th><th>Library</th>${matrix.factors.map(f => `<th class="fac" title="${esc(f.label)}"><a href="${href.factor(f.factor_id)}">${esc(f.factor)} · ${esc((f.label || '').slice(0, 28))}</a></th>`).join('')}</tr></thead><tbody>` +
      matrix.rows.map(r => `<tr><td class="wrap"><a href="${href.geneSet(r.gene_set_id)}">${esc(r.gene_set_name)}</a>${r.n_top_joint_factors >= SUMMARY.meta.thresholds.hub_min_factors ? ' <span class="flag" title="hub: joint top of ' + r.n_top_joint_factors + ' factors">hub</span>' : ''}</td><td>${libTag(r.library)}</td>` +
        r.cells.map((c, i) => { if (!c) return '<td class="cell muted">·</td>'; const v = c[mode], rk = c[mode + '_rank'];
          return `<td class="cell" style="background:rgba(15,118,110,${(0.08 + 0.8 * Math.min(1, v)).toFixed(2)});color:${v > 0.55 ? '#fff' : 'inherit'}"><a style="color:inherit" href="${href.factor(matrix.factors[i].factor_id, r.gene_set_id)}">${fmt(v, 2)}<br><small>#${rk}</small></a></td>`; }).join('') + '</tr>').join('') + '</tbody></table></div>';
  };
  const loadMatrix = async () => { matrix = await api('/api/trait_matrix', { id: trait, per_factor: $('#mn').value }); drawMatrix(); };
  $('#mn').onchange = loadMatrix;
  $$('#mv button').forEach(b => b.onclick = () => { mode = b.dataset.v; $$('#mv button').forEach(x => x.classList.toggle('on', x === b)); drawMatrix(); });
  await loadMatrix();
}

// ---------------------------------------------------------------------------------------------- factors table
async function renderFactors(view, params) {
  const flag = params.get('flag') || '';
  const factors = (await api('/api/factors')).factors;
  const m = SUMMARY.meta;
  view.innerHTML = `<h1>Factors</h1><p class="muted">Every factor with its audit columns. Click a column to sort, a row to open the factor.</p>
    <div class="panel"><div class="controls"><div><label>Filter</label><input type="text" id="ff" placeholder="label, factor id or trait"></div>
      <div><label>Flag</label><select id="fl"><option value="">Any</option><option value="*" ${flag === '*' ? 'selected' : ''}>Flagged (any flag)</option>${m.flags.map(f => `<option value="${esc(f.flag)}" ${f.flag === flag ? 'selected' : ''}>${esc(f.flag)} (${m.flag_counts[f.flag] || 0})</option>`).join('')}</select></div>
      <div><label>Library dominating the joint top-${m.top_n}</label><select id="fd"><option value="">Any</option>${SUMMARY.libraries.map(l => `<option>${esc(l.library)}</option>`).join('')}</select></div></div>
      <p class="muted" id="fdef"></p><div id="t"></div></div>`;
  const table = dataTable($('#t'), { rows: factors, pageSize: 50, sort: { col: 'flags_n', desc: true }, onRow: r => location.hash = href.factor(r.factor_id), cols: [
    { key: 'label', label: 'Factor', cls: 'wrap', html: r => `<a href="${href.factor(r.factor_id)}">${esc(r.label || r.factor)}</a><div class="muted">${esc(r.factor_id)}</div>` },
    { key: 'phenotype_name', label: 'Trait', cls: 'wrap', html: r => `<a href="${href.trait(r.trait)}">${esc(r.phenotype_name || r.trait)}</a>` },
    { key: 'n_nonzero', label: 'Genes', num: true },
    { key: 'top_joint_loading', label: 'Top joint', num: true, html: r => bar(r.top_joint_loading, 1) },
    { key: 'top_joint_gene_set', label: 'Top joint gene set', cls: 'wrap', html: r => esc(r.top_joint_gene_set) },
    { key: 'jm_top_overlap', label: 'J∩M', num: true, html: r => pct(r.jm_top_overlap), title: 'Share of the joint top-N also in the marginal top-N' },
    { key: 'own_top_frac', label: 'Own', num: true, html: r => pct(r.own_top_frac), title: "Share of the joint top-N that load highest on this factor among the trait's factors" },
    { key: 'hub_frac', label: 'Hub', num: true, html: r => pct(r.hub_frac), title: 'Share of the joint top-N that are hub gene sets' },
    { key: 'sig_overlap_frac', label: 'Sig. overlap', num: true, html: r => pct(r.sig_overlap_frac), title: 'Share of the joint top-N whose overlap with the loaded genes has hypergeometric p < 1e-6' },
    { key: 'median_fold_enrichment', label: 'Median fold', num: true, html: r => fmt(r.median_fold_enrichment, 1) },
    { key: 'dominant_library', label: 'Main library', html: r => `${libTag(r.dominant_library)} <span class="muted">${pct(r.dominant_library_frac)}</span>` },
    { key: 'flags_n', label: 'Flags', num: true, value: r => (r.flags ? r.flags.split(',').length : 0), html: r => flagChips(r.flags) || '<span class="muted">–</span>' }] });
  const apply = () => {
    const q = $('#ff').value.trim().toLowerCase(), fl = $('#fl').value, fd = $('#fd').value;
    $('#fdef').textContent = fl && fl !== '*' ? flagText(fl) : '';
    setParam('flag', fl);
    table.update(factors.filter(f => (!fl || (fl === '*' ? !!f.flags : (',' + f.flags + ',').includes(',' + fl + ','))) && (!fd || f.dominant_library === fd)
      && (!q || [f.label, f.factor_id, f.trait, f.phenotype_name].some(x => (x || '').toLowerCase().includes(q)))));
  };
  $('#ff').oninput = apply; $('#fl').onchange = apply; $('#fd').onchange = apply; apply();
}

// ---------------------------------------------------------------------------------------------- factor
async function renderFactor(view, factorId, params) {
  const [d, gsets, genes] = await Promise.all([api('/api/factor', { id: factorId }), api('/api/factor_gene_sets', { id: factorId }), api('/api/factor_genes', { id: factorId })]);
  const f = d.factor, t = d.trait, m = SUMMARY.meta, topN = m.top_n;
  const hubMin = m.thresholds.hub_min_factors;
  const eagglSets = (f.eaggl_top_gene_sets || '').split(',').filter(Boolean), eagglGenes = (f.eaggl_top_genes || '').split(',').filter(Boolean);
  const top10 = genes.genes.slice(0, 10).reduce((s, g) => s + (g.mass_share || 0), 0);
  view.innerHTML = `<div class="crumb"><a href="#/traits">Traits</a> › <a href="${href.trait(t.trait)}">${esc(t.phenotype_name || t.trait)}</a> <span class="muted">(${esc(t.trait)} · ${esc(t.kpn_trait_id)})</span></div>
    <div class="tabs">${d.siblings.map(s => `<a href="${href.factor(s.factor_id)}" class="${s.factor_id === f.factor_id ? 'on' : ''} ${s.flags ? 'flagged' : ''}" title="${esc(s.label + (s.flags ? ' — flags: ' + s.flags : ''))}">${esc(s.factor)} · ${esc(s.label || '')}</a>`).join('')}</div>
    <h1>${esc(f.label || f.factor)}</h1><div class="chips"><code>${esc(f.factor_id)}</code>${flagChips(f.flags)}</div>
    <div class="stats">
      <div class="stat"><strong>${int(f.n_nonzero)}</strong><span>genes with a loading (top 10 carry ${pct(top10)} of the mass)</span></div>
      <div class="stat"><strong>${fmt(f.top_joint_loading)}</strong><span>best joint loading</span></div>
      <div class="stat"><strong>${fmt(f.top_marginal_loading)}</strong><span>best marginal loading</span></div>
      <div class="stat"><strong>${pct(f.jm_top_overlap)}</strong><span>of the joint top-${topN} also in the marginal top-${topN}</span></div>
      <div class="stat"><strong>${pct(f.own_top_frac)}</strong><span>of the joint top-${topN} load highest here among the trait's ${t.n_factors} factors</span></div>
      <div class="stat"><strong>${pct(f.sig_overlap_frac)}</strong><span>of the joint top-${topN} overlap the loaded genes beyond chance</span></div>
      <div class="stat"><strong>${pct(f.hub_frac)}</strong><span>of the joint top-${topN} are hub gene sets</span></div>
    </div>
    <details><summary>EAGGL factorization: the factor's own top genes and gene sets</summary>
      <div class="kv"><div><span>gene_set_score</span>${fmt(f.gene_set_score)}</div><div><span>gene_score</span>${fmt(f.gene_score)}</div><div><span>loading variant</span>${esc(f.loading_variant)}</div><div><span>L1 / L2</span>${fmt(f.loading_l1)} / ${fmt(f.loading_l2)}</div></div>
      <div class="chips"><span class="muted">Top genes</span>${eagglGenes.map(g => `<a class="chip" href="${href.gene(g)}">${esc(g)}</a>`).join('')}</div>
      <div class="chips"><span class="muted">Top gene sets (EAGGL library)</span>${eagglSets.map(s => `<span class="chip">${esc(s)}</span>`).join('')}</div>
      <div class="kv">${Object.entries(f.metadata).filter(([k]) => !['top_genes', 'top_gene_sets', 'label', 'factor_id', 'trait', 'factor'].includes(k)).map(([k, v]) => `<div><span>${esc(k)}</span>${esc(v)}</div>`).join('')}</div></details>
    <div class="grid2">
      <div class="panel"><h2>CFDE gene sets projected onto this factor</h2>
        <div class="controls"><div><label>Rank by</label><span class="seg" id="rk"><button class="on" data-v="joint">joint</button><button data-v="marginal">marginal</button><button data-v="all">union</button></span></div>
          <div><label>Library</label><select id="lb"><option value="">All</option>${Object.keys(f.library_counts).concat(SUMMARY.libraries.map(l => l.library)).filter((x, i, a) => a.indexOf(x) === i).map(l => `<option>${esc(l)}</option>`).join('')}</select></div>
          <div><label>Filter</label><input type="text" id="gf" placeholder="name or gene"></div>
          <div><label>&nbsp;</label><span class="inline"><input type="checkbox" id="nh"> hide hubs (≥ ${hubMin} factors)</span></div></div>
        <div id="gs"></div>
        <h3>Joint vs marginal loading</h3><p class="muted" style="margin-top:0">Each point is a gene set in either top-${topN}; hollow points are hubs. Points below the diagonal lost part of their joint loading to another factor of the trait. Click to inspect.</p><div id="sc"></div></div>
      <div class="panel"><h2>Gene loadings</h2><p class="muted" style="margin-top:0">Nonzero EAGGL loadings w<sub>g</sub> as projected. “In top sets” counts the joint top-${topN} gene sets containing the gene; “Factors” counts all factors loading it.</p>
        <div class="controls"><div><label>Filter</label><input type="text" id="gn" placeholder="gene"></div></div><div id="gl"></div></div>
    </div>`;

  const rows = gsets.gene_sets; const maxJ = Math.max(...rows.map(r => r.joint), 0.01), maxM = Math.max(...rows.map(r => r.marginal), 0.01);
  const open = r => openGeneSetSheet(f, t, r.gene_set_id);
  const table = dataTable($('#gs'), { rows, pageSize: 25, sort: { col: 'joint_rank', desc: false }, onRow: open,
    cols: [
    { key: 'joint_rank', label: 'J#', num: true, title: 'Rank by joint loading among all gene sets' },
    { key: 'marginal_rank', label: 'M#', num: true, title: 'Rank by marginal loading among all gene sets' },
    { key: 'gene_set_name', label: 'Gene set', cls: 'wrap', html: r => `<a data-row href="${href.factor(f.factor_id, r.gene_set_id)}">${esc(r.gene_set_name)}</a>${r.n_top_joint_factors >= hubMin ? ` <span class="flag" title="hub: in the joint top-${topN} of ${r.n_top_joint_factors} factors">hub ${r.n_top_joint_factors}</span>` : ''}<div class="genes">${esc(r.top_overlap_genes.slice(0, 6).join(' '))}</div>` },
    { key: 'library', label: 'Library', html: r => libTag(r.library) },
    { key: 'joint', label: 'Joint', num: true, html: r => bar(r.joint, maxJ) },
    { key: 'marginal', label: 'Marginal', num: true, html: r => bar(r.marginal, maxM, 'var(--bar2)') },
    { key: 'is_joint_top_factor', label: '★', num: true, html: r => r.is_joint_top_factor ? '★' : '', title: "This factor has the gene set's highest joint loading among the trait's factors" },
    { key: 'n_universe', label: 'Size', num: true, title: 'Member genes in the EAGGL universe' },
    { key: 'n_overlap', label: 'Overlap', num: true, title: 'Member genes with a nonzero loading on this factor' },
    { key: 'overlap_mass_frac', label: 'Mass', num: true, html: r => pct(r.overlap_mass_frac), title: "Share of the factor's loading mass (Σw) on the member genes" },
    { key: 'fold_enrichment', label: 'Fold', num: true, html: r => fmt(r.fold_enrichment, 1), title: 'Overlap ÷ expected overlap of a random set of the same size' },
    { key: 'neg_log10_p', label: '−log₁₀p', num: true, html: r => fmt(r.neg_log10_p, 1), title: 'Hypergeometric overlap with the loaded genes (one-sided; 0 when at or below expectation)' }] });
  let rankBy = 'joint';
  const applyG = () => {
    const lib = $('#lb').value, q = $('#gf').value.trim().toLowerCase(), nh = $('#nh').checked;
    table.update(rows.filter(r => (rankBy === 'all' || r[rankBy + '_rank'] <= topN) && (!lib || r.library === lib) && (!nh || r.n_top_joint_factors < hubMin)
      && (!q || r.gene_set_name.toLowerCase().includes(q) || r.top_overlap_genes.some(g => g.toLowerCase() === q))));
  };
  $$('#rk button').forEach(b => b.onclick = () => { rankBy = b.dataset.v; $$('#rk button').forEach(x => x.classList.toggle('on', x === b)); applyG(); });
  $('#lb').onchange = applyG; $('#gf').oninput = applyG; $('#nh').onchange = applyG; applyG();
  scatter($('#sc'), rows, hubMin, open);

  const maxW = genes.genes.length ? genes.genes[0].loading : 1;
  const gtable = dataTable($('#gl'), { rows: genes.genes, pageSize: 25, sort: { col: 'rank', desc: false }, onRow: r => location.hash = href.gene(r.gene), cols: [
    { key: 'rank', label: '#', num: true }, { key: 'gene', label: 'Gene', html: r => `<a href="${href.gene(r.gene)}">${esc(r.gene)}</a>` },
    { key: 'loading', label: 'Loading', num: true, html: r => bar(r.loading, maxW) },
    { key: 'mass_share', label: 'Mass', num: true, html: r => pct(r.mass_share), title: "Share of the factor's loading mass Σw" },
    { key: 'n_top_sets', label: 'In top sets', num: true }, { key: 'n_factors', label: 'Factors', num: true }] });
  $('#gn').oninput = () => { const q = $('#gn').value.trim().toLowerCase(); gtable.update(genes.genes.filter(g => !q || g.gene.toLowerCase().includes(q))); };
  if (params.get('gs')) openGeneSetSheet(f, t, params.get('gs'));
}

function scatter(host, rows, hubMin, onClick) {
  const W = 520, H = 340, m = { l: 44, r: 12, t: 10, b: 36 };
  const max = Math.max(0.05, ...rows.map(r => Math.max(r.joint, r.marginal)));
  const sx = v => m.l + v / max * (W - m.l - m.r), sy = v => H - m.b - v / max * (H - m.t - m.b);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(x => x * max);
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px" role="img" aria-label="joint vs marginal loading">`;
  ticks.forEach(v => { svg += `<line x1="${sx(v)}" x2="${sx(v)}" y1="${m.t}" y2="${H - m.b}" stroke="#eef2f5"/><line x1="${m.l}" x2="${W - m.r}" y1="${sy(v)}" y2="${sy(v)}" stroke="#eef2f5"/>
    <text x="${sx(v)}" y="${H - m.b + 14}" text-anchor="middle">${fmt(v, 2)}</text><text x="${m.l - 6}" y="${sy(v) + 3}" text-anchor="end">${fmt(v, 2)}</text>`; });
  svg += `<line x1="${sx(0)}" y1="${sy(0)}" x2="${sx(max)}" y2="${sy(max)}" stroke="#c7d2dd" stroke-dasharray="4 3"/>
    <text x="${(W + m.l) / 2}" y="${H - 4}" text-anchor="middle">marginal loading</text><text transform="translate(12 ${(H - m.b) / 2}) rotate(-90)" text-anchor="middle">joint loading</text>`;
  rows.slice().sort((a, b) => a.joint - b.joint).forEach((r, i) => {
    const c = libColor(r.library), hub = r.n_top_joint_factors >= hubMin;
    svg += `<circle data-id="${esc(r.gene_set_id)}" cx="${sx(r.marginal).toFixed(1)}" cy="${sy(r.joint).toFixed(1)}" r="4.2" fill="${hub ? 'none' : c}" stroke="${c}" stroke-width="${hub ? 1.6 : 0.6}" fill-opacity=".7" style="cursor:pointer"><title>${esc(r.gene_set_name)} (${esc(r.library)})\njoint ${fmt(r.joint)} (#${r.joint_rank}) · marginal ${fmt(r.marginal)} (#${r.marginal_rank})${hub ? '\nhub: top of ' + r.n_top_joint_factors + ' factors' : ''}</title></circle>`;
  });
  host.innerHTML = svg + '</svg>' + `<div class="chips">${[...new Set(rows.map(r => r.library))].map(l => libTag(l)).join(' ')}</div>`;
  $$('circle[data-id]', host).forEach(cn => cn.onclick = () => onClick(rows.find(r => r.gene_set_id === cn.dataset.id)));
}

async function openGeneSetSheet(f, t, gsId) {
  const sheet = $('#sheet'); document.body.classList.add('sheet-open'); setParam('gs', gsId);
  sheet.innerHTML = '<p class="muted">Loading…</p>';
  try {
    const d = await api('/api/factor_gene_set', { factor: f.factor_id, id: gsId }); const g = d.gene_set, p = d.projection, m = SUMMARY.meta;
    const maxC = d.contributions.length ? d.contributions[0].contribution : 1;
    sheet.innerHTML = `<div class="head"><div style="flex:1"><div class="kind">Gene set on ${esc(f.factor)} · ${esc(f.label || '')}</div><h2>${esc(g.gene_set_name)}</h2></div><button id="x" title="Close (Esc)">✕</button></div>
      <div class="chips"><code>${esc(g.gene_set_id)}</code>${libTag(g.library)}<span class="chip">${esc(g.collection_label)}</span>
        <span class="chip">${int(g.n_genes)} genes · ${int(g.n_universe)} in the EAGGL universe</span>
        <a class="chip" href="${href.geneSet(g.gene_set_id)}">joint top-${m.top_n} of ${int(g.n_top_joint_factors)} factors in ${int(g.n_top_joint_traits)} traits</a></div>
      ${p ? `<div class="kv"><div><span>joint loading</span>${fmt(p.joint, 4)} (#${p.joint_rank})</div><div><span>marginal loading</span>${fmt(p.marginal, 4)} (#${p.marginal_rank})</div>
        <div><span>Σ w<sub>g</sub> / wᵀw (recomputed)</span>${fmt(p.marginal_recomputed, 4)}</div><div><span>highest joint among siblings</span>${p.is_joint_top_factor ? '★ yes' : 'no'}</div>
        <div><span>overlap</span>${p.n_overlap} of ${g.n_universe} members</div><div><span>fold enrichment</span>${fmt(p.fold_enrichment, 2)}</div>
        <div><span>−log₁₀ p (hypergeometric)</span>${fmt(p.neg_log10_p, 1)}</div><div><span>share of factor loading mass</span>${pct(p.overlap_mass_frac)}</div></div>`
        : '<div class="note">This gene set is not in either top list of this factor.</div>'}
      <h3>Member genes driving the marginal loading (${d.contributions.length})</h3>
      <p class="muted" style="margin-top:0">The marginal loading is clip(Σ<sub>g∈set</sub> w<sub>g</sub> / wᵀw, 0, 1), with wᵀw = ${fmt(d.loading_l2_squared, 4)}; each member gene adds w<sub>g</sub>/wᵀw. The joint loading has no per-gene split: the trait's factors compete for the set.</p>
      <div id="ct"></div>
      <h3>On the trait's ${d.siblings.length} factors</h3>
      ${d.siblings_complete ? '' : '<p class="muted">Joint loadings outside a factor’s top list need the per-trait long files (--long-file).</p>'}
      <div id="sb"></div>
      <details><summary>${d.unloaded.length} member genes with no loading on this factor</summary><div class="chips">${d.unloaded.map(u => `<a class="chip" href="${href.gene(u.gene)}" title="loaded on ${u.n_factors} factors">${esc(u.gene)}</a>`).join('')}</div></details>
      <details><summary>${d.outside.length} member genes outside the EAGGL gene universe</summary><div class="chips">${d.outside.map(x => `<span class="chip">${esc(x)}</span>`).join('')}</div></details>`;
    $('#x').onclick = closeSheet;
    dataTable($('#ct'), { rows: d.contributions, pageSize: 15, sort: { col: 'contribution', desc: true }, onRow: r => location.hash = href.gene(r.gene), cols: [
      { key: 'gene', label: 'Gene', html: r => `<a href="${href.gene(r.gene)}">${esc(r.gene)}</a>` }, { key: 'rank', label: 'Rank in factor', num: true },
      { key: 'loading', label: 'w', num: true, html: r => fmt(r.loading, 4) },
      { key: 'contribution', label: 'w / wᵀw', num: true, html: r => bar(r.contribution, maxC) },
      { key: 'cumulative', label: 'Cumulative', num: true, html: r => fmt(r.cumulative, 4) }] });
    dataTable($('#sb'), { rows: d.siblings, pageSize: 20, sort: { col: 'factor_number', desc: false }, rowClass: r => r.factor_id === f.factor_id ? 'cur' : '',
      onRow: r => { closeSheet(); location.hash = href.factor(r.factor_id, gsId); }, cols: [
      { key: 'factor_number', label: 'Factor', num: true, html: r => esc(r.factor) }, { key: 'label', label: 'Label', cls: 'wrap' },
      { key: 'joint', label: 'Joint', num: true, html: r => r.joint === null ? '–' : bar(r.joint, 1) }, { key: 'joint_rank', label: 'J#', num: true },
      { key: 'marginal_recomputed', label: 'Marginal', num: true, html: r => bar(r.marginal_recomputed, 1, 'var(--bar2)') }, { key: 'marginal_rank', label: 'M#', num: true },
      { key: 'is_joint_top_factor', label: '★', num: true, html: r => r.is_joint_top_factor ? '★' : '' }] });
  } catch (e) { sheet.innerHTML = `<div class="head"><div style="flex:1"></div><button id="x">✕</button></div><div class="error">${esc(e.message)}</div>`; $('#x').onclick = closeSheet; }
}
function closeSheet() { document.body.classList.remove('sheet-open'); setParam('gs', ''); }

// ---------------------------------------------------------------------------------------------- gene set / gene
async function renderGeneSet(view, gsId) {
  const d = await api('/api/gene_set', { id: gsId }); const g = d.gene_set, hubMin = SUMMARY.meta.thresholds.hub_min_factors;
  view.innerHTML = `<div class="crumb">Gene set</div><h1>${esc(g.gene_set_name)}</h1>
    <div class="chips"><code>${esc(g.gene_set_id)}</code>${libTag(g.library)}<span class="chip">${esc(g.collection_label)}</span><code>${esc(g.collection_id)}</code>
      ${g.n_top_joint_factors >= hubMin ? `<span class="flag">hub</span>` : ''}</div>
    <div class="kv"><div><span>genes</span>${int(g.n_genes)} (${int(g.n_universe)} in the EAGGL universe)</div><div><span>partition</span>${esc(g.partition || '–')}</div>
      <div><span>model</span>${esc(g.model || '–')}</div><div><span>comparison</span>${esc(g.comparison || '–')}</div><div><span>program</span>${esc(g.program || '–')}</div>
      <div><span>joint top-${d.top_n} of</span>${int(g.n_top_joint_factors)} factors / ${int(g.n_top_joint_traits)} traits</div><div><span>either top-${d.top_n} of</span>${int(g.n_top_any_factors)} factors</div></div>
    <div class="panel"><h2>Factors with this gene set in a top-${d.top_n} list</h2><div id="t"></div></div>
    <div class="panel"><h2>Members</h2><p class="muted">Shaded by how many factors load the gene; grey members are outside the EAGGL gene universe and cannot contribute.</p>
      <div class="chips">${d.members.map(x => `<a class="chip" href="${href.gene(x.gene)}" style="${x.in_universe ? `background:rgba(15,118,110,${(0.05 + Math.min(0.6, x.n_factors / 400)).toFixed(2)})` : 'color:#94a3b8'}" title="${x.in_universe ? 'loaded on ' + x.n_factors + ' factors' : 'outside the EAGGL universe'}">${esc(x.gene)}</a>`).join('')}</div></div>`;
  dataTable($('#t'), { rows: d.factors, pageSize: 50, sort: { col: 'joint', desc: true }, onRow: r => location.hash = href.factor(r.factor_id, g.gene_set_id), cols: [
    { key: 'label', label: 'Factor', cls: 'wrap', html: r => `<a href="${href.factor(r.factor_id, g.gene_set_id)}">${esc(r.label || r.factor)}</a><div class="muted">${esc(r.factor_id)}</div>` },
    { key: 'phenotype_name', label: 'Trait', cls: 'wrap', html: r => `<a href="${href.trait(r.trait)}">${esc(r.phenotype_name || r.trait)}</a>` },
    { key: 'joint', label: 'Joint', num: true, html: r => bar(r.joint, 1) }, { key: 'joint_rank', label: 'J#', num: true },
    { key: 'marginal', label: 'Marginal', num: true, html: r => bar(r.marginal, 1, 'var(--bar2)') }, { key: 'marginal_rank', label: 'M#', num: true },
    { key: 'is_joint_top_factor', label: '★', num: true, html: r => r.is_joint_top_factor ? '★' : '' },
    { key: 'n_overlap', label: 'Overlap', num: true }, { key: 'fold_enrichment', label: 'Fold', num: true, html: r => fmt(r.fold_enrichment, 1) },
    { key: 'neg_log10_p', label: '−log₁₀p', num: true, html: r => fmt(r.neg_log10_p, 1) }] });
}

async function renderGene(view, gene) {
  const d = await api('/api/gene', { id: gene }); const g = d.gene;
  view.innerHTML = `<div class="crumb">Gene</div><h1>${esc(g.gene)}</h1>
    <div class="chips"><span class="chip">${g.in_universe ? 'in the EAGGL gene universe' : 'outside the EAGGL gene universe'}</span>${g.cfde_symbols ? `<span class="chip">CFDE symbol ${esc(g.cfde_symbols)}</span>` : ''}
      <span class="chip">loaded on ${int(g.n_factors)} factors</span><span class="chip">max loading ${fmt(g.max_loading)}</span></div>
    <div class="panel"><h2>Factors loading ${esc(g.gene)}</h2><div id="t"></div></div>`;
  const max = d.factors.length ? d.factors[0].loading : 1;
  dataTable($('#t'), { rows: d.factors, pageSize: 50, sort: { col: 'loading', desc: true }, onRow: r => location.hash = href.factor(r.factor_id), cols: [
    { key: 'label', label: 'Factor', cls: 'wrap', html: r => `<a href="${href.factor(r.factor_id)}">${esc(r.label || r.factor)}</a><div class="muted">${esc(r.factor_id)}</div>` },
    { key: 'phenotype_name', label: 'Trait', cls: 'wrap', html: r => `<a href="${href.trait(r.trait)}">${esc(r.phenotype_name || r.trait)}</a>` },
    { key: 'loading', label: 'Loading', num: true, html: r => bar(r.loading, max) },
    { key: 'rank', label: 'Rank', num: true, html: r => `${int(r.rank)} <span class="muted">/ ${int(r.n_nonzero)}</span>` }] });
}

// ---------------------------------------------------------------------------------------------- boot
document.addEventListener('keydown', e => { if (e.key === 'Escape' && document.body.classList.contains('sheet-open')) closeSheet(); });
window.addEventListener('hashchange', route);
(async () => {
  setupSearch(); $('#backdrop').onclick = closeSheet;
  try { SUMMARY = await api('/api/summary'); } catch (e) { $('#view').innerHTML = `<div class="error">${esc(e.message)}</div>`; return; }
  $('#proj').textContent = SUMMARY.meta.project;
  route();
})();
"""

BODY = """<div class="topbar"><div class="inner">
  <a class="brand" href="#/">__TITLE__<small id="proj"></small></a>
  <nav><a href="#/" data-nav="">Overview</a><a href="#/traits" data-nav="traits">Traits</a><a href="#/factors" data-nav="factors">Factors</a><a href="#/hubs" data-nav="hubs">Hub gene sets</a></nav>
  <div class="search"><input id="q" type="text" autocomplete="off" spellcheck="false" placeholder="Search traits, KPN ids, factors, gene sets, genes  ( / )"><div class="dropdown" id="dd" hidden></div></div>
</div></div>
<main id="view"></main>
<div id="backdrop"></div><aside id="sheet" aria-label="Gene set detail"></aside>
"""


def render_page(title, api_base=""):
    """The full HTML document. `api_base` is the URL of a `serve` instance ("" = same origin)."""
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<title>%s</title>\n<style>%s</style>\n</head>\n<body>\n%s"
            "<script>window.FACTOR_PORTAL_API_BASE = %s;</script>\n<script>%s</script>\n</body>\n</html>\n"
            % (html.escape(title), CSS, BODY.replace("__TITLE__", html.escape(title)),
               json.dumps(api_base).replace("</", "<\\/"), SCRIPT))
