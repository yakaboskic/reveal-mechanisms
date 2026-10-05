#!/usr/bin/env node
/** Browser regression with intercepted reference APIs. Never starts research or writes data. */
import assert from 'node:assert/strict';
import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const origin = new URL(process.env.FACTOR_PAGES_BASE_URL || 'http://127.0.0.1:3300').origin;
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname), 'Only a local renderer may be tested');
const output = resolve(process.env.FACTOR_PAGES_AUDIT_DIR || resolve(root, '.runtime/factor-pages/browser'));
await mkdir(output, { recursive: true });
const spec = JSON.parse(await readFile(resolve(root, 'api/openapi.json'), 'utf8'));
const example = (path, name, status = '200') => structuredClone(spec.paths[path].get.responses[status].content[status === '200' ? 'application/json' : 'application/problem+json'].examples[name].value);
const detail = example('/v1/factors/{source_id}', 'factor');
const factor = detail.factor, generation = detail.generation_id;
const factorUrl = `/factors/${encodeURIComponent(factor.source_id)}?source_revision=${factor.source_revision}&from=${encodeURIComponent('/?job=152f549a-af8c-4b39-8480-986f671de0be')}`;
const setId = 'dapper:GeneSet.9oTtK8sOTbbKQ-e1-oFbxyrF28Wj_m7U';
const collectionId = 'dapper:GeneSetCollection.NFmyGDsk1okgQ1MJWjLdcFzxlzj_a1lr';
const datasetId = 'dapper:Dataset.' + 'd'.repeat(32), activityId = 'dapper:Activity.' + 'a'.repeat(32), fileId = 'dapper:File.' + 'f'.repeat(32);
// Values and the first label mirror actual EAGGL/CFDE shapes. Additional rows are
// explicitly synthetic renderer fixtures, not biological assertions or saved data.
const geneNames = ['NRXN1', 'SHANK3', 'PTEN', 'GABRA1', 'GABRB2', 'GABRG3', 'NRXN1_ALT'];
const genes = Array.from({ length: 205 }, (_, index) => ({ id: geneNames[index] || `ZZ_FIXTURE_GENE_${String(index + 1).padStart(3, '0')}`, label: geneNames[index] || `ZZ_FIXTURE_GENE_${String(index + 1).padStart(3, '0')}`, loading: index === 0 ? 0.7196999788284302 : index === 1 ? 0.6890000104904175 : index === 204 ? 0 : Number((0.67 - index * 0.003).toFixed(6)), rank: index + 1 }));
const sets = Array.from({ length: 73 }, (_, index) => ({
  id: index === 0 ? setId : `dapper:GeneSet.${String(index).padStart(32, 'b')}`,
  gene_set_id: index === 0 ? setId : `dapper:GeneSet.${String(index).padStart(32, 'b')}`,
  label: index === 0 ? 'LINCS_L1000_Chem_Pert_BRD-K18416855_up' : `ZZ Fixture synaptic response gene set ${String(index + 1).padStart(3, '0')}`,
  loading: Number((0.3013 - index * 0.001).toFixed(4)), joint_loading: Number((0.3013 - index * 0.001).toFixed(4)),
  marginal_loading: index === 1 ? 0.92 : Number((0.3683 - index * 0.001).toFixed(4)), rank: index + 1,
  library: index < 8 ? 'LINCS_L1000' : 'GO_BP', gene_count: 336 + index,
}));
detail.genes = { total: genes.length, min: 0, max: genes[0].loading, available: true, coverage: 'All stored nonzero EAGGL gene loadings. Missing observations are not zero.' };
detail.gene_sets = { total: sets.length, min: sets.at(-1).loading, max: sets[0].loading, available: true, coverage: 'Retained per-trait gene-set projections; the full gene-set universe is not represented.' };
const catalog = {
  id: setId, generation_id: generation, name: sets[0].label, library: 'LINCS_L1000', gene_count: 336, genes_in_universe: 317,
  object: { id: setId, name: sets[0].label, description: 'Renderer fixture for a chemical perturbation signature and its source records.', organism: 'human', assay: 'bulk', data_type: 'transcriptomics', members: ['HGNC.SYMBOL:GABRA1', 'GABRB2', 'HGNC.SYMBOL:NRXN1', ...Array.from({ length: 110 }, (_, index) => `HGNC.SYMBOL:FIXTURE_MEMBER_${index + 1}`)], n_genes: 336, in_gene_set_collection: [collectionId], in_gmt_file: fileId, gmt_entry: sets[0].label, was_generated_by: activityId, was_derived_from: [datasetId] },
  metadata: { model: 'HZ1', partition: 'all_signatures' },
  collection: { id: collectionId, label: 'LINCS L1000 chemical perturbations', library: 'LINCS_L1000', gene_set_count: 2000, payload_sha256: 'c'.repeat(64), object: { id: collectionId, name: 'LINCS L1000 chemical perturbations', was_generated_by: activityId, was_derived_from: [datasetId] } },
  provenance: { datasets: [{ id: datasetId, name: 'LINCS L1000 source dataset', description: 'Self-contained provenance fixture.', version: '2026-09-28', url: 'https://lincsproject.org/' }], activities: [{ id: activityId, name: 'Extract chemical perturbation signatures', software_name: 'CFDE gene-set extractor', software_version: '1.0', was_derived_from: [datasetId], repo_url: 'https://github.com/flannick/dig-gene-set-extractors' }], files: [{ id: fileId, filename: 'lincs-chemical-signatures.gmt', location: 'https://example.org/fixture-signatures.gmt', sha256: 'e'.repeat(64) }], organizations: [] },
  limitations: ['This browser fixture supplies 113 members of a reported 336-gene set.'],
};
const old = example('/v1/mechanisms/{source_id}', 'reference_generation_superseded', '410').archived_reference_factor;
old.top_genes = [{ symbol: 'ARCHIVED_NRXN1', loading: 0.8123456789 }, { symbol: 'ARCHIVED_SHANK3', loading: 0.6 }];
old.top_gene_sets = [{ rank: 1, gene_set_id: null, name: 'Archived set with unknown loading', library: 'Legacy CFDE', collection_id: null, source_key: 'legacy-set', joint_loading: null, marginal_loading: null, score: null }];

async function playwright() {
  if (process.env.PLAYWRIGHT_MODULE) return import(process.env.PLAYWRIGHT_MODULE.startsWith('/') ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : process.env.PLAYWRIGHT_MODULE);
  try { return await import('playwright'); } catch { return import(pathToFileURL(resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href); }
}
async function executable() {
  if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_EXECUTABLE_PATH;
  for (const entry of (await readdir(resolve(homedir(), 'Library/Caches/ms-playwright')).catch(() => [])).filter(value => /^chromium-\d+$/.test(value)).sort().reverse()) {
    const path = resolve(homedir(), 'Library/Caches/ms-playwright', entry, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium');
    try { await access(path); return path; } catch { /* Use the next installed browser. */ }
  }
}
const { chromium } = await playwright(), browser = await chromium.launch({ headless: true, executablePath: await executable() });
const report = { scope: 'Local renderer with intercepted GET reference APIs and self-contained illustrative fixtures. No live API calls, sessions, database writes, research jobs or external navigation.', scenarios: [], screenshots: [] };
let activePage;
async function harness(name, options = {}) {
  const context = await browser.newContext({ viewport: options.mobile ? { width: 390, height: 844 } : { width: 1300, height: 1050 }, isMobile: !!options.mobile, hasTouch: !!options.mobile, reducedMotion: 'reduce', serviceWorkers: 'block' });
  const page = await context.newPage(); activePage = page; page.setDefaultTimeout(20000);
  const calls = [], errors = [], unexpected = [], pending = new Map();
  const gates = { detail: !options.retry, genes: !options.retry };
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', async route => {
    try {
    const request = route.request(), url = new URL(request.url()), path = decodeURIComponent(url.pathname);
    if (url.origin !== origin) { unexpected.push(url.href); return route.abort(); }
    if (!path.startsWith('/api/')) return route.continue();
    calls.push(path + url.search);
    assert.equal(request.method(), 'GET', 'Reference exploration must never mutate a record');
    if (path === '/api/session/status') return route.fulfill({ json: { principal: null, canClaim: false, canAdmin: false, providers: { google: false, orcid: false } } });
    if (path.startsWith('/api/backend/v1/factors/')) {
      if (options.stale) return route.fulfill({ status: 410, json: { code: 'REFERENCE_GENERATION_SUPERSEDED', detail: 'The factor has been archived.', archived_reference_factor: old } });
      assert.equal(path, `/api/backend/v1/factors/${factor.source_id}`);
      assert.equal(url.searchParams.get('source_revision'), factor.source_revision);
      if (!gates.detail) return route.fulfill({ status: 503, json: { code: 'DEPENDENCY_UNAVAILABLE', detail: 'Fixture factor lookup interrupted. Please retry.' } });
      return route.fulfill({ json: detail });
    }
    if (path === '/api/backend/v1/factor-loadings') {
      assert.equal(url.searchParams.get('source_id'), factor.source_id);
      assert.equal(url.searchParams.get('source_revision'), factor.source_revision);
      assert.equal(url.searchParams.get('generation_id'), generation);
      const kind = url.searchParams.get('kind'), metric = url.searchParams.get('metric'), q = (url.searchParams.get('q') || '').toLowerCase(), offset = Number(url.searchParams.get('offset') || 0), limit = Number(url.searchParams.get('limit'));
      assert.ok(limit === (kind === 'gene' ? 200 : 50) || (kind === 'gene_set' && limit === 500), 'Panels use bounded pages; the picker loads its own complete gene-set options');
      const sort = url.searchParams.get('sort'); assert.ok(['alphabetical', 'loading'].includes(sort), 'Order must be explicit');
      if (kind === 'gene' && !gates.genes) return route.fulfill({ status: 503, json: { code: 'DEPENDENCY_UNAVAILABLE', detail: 'Fixture loading lookup interrupted. Please retry.' } });
      let values = structuredClone(kind === 'gene' ? genes : sets);
      if (kind === 'gene_set' && options.nullSets) values = values.map(row => ({ ...row, loading: null }));
      else if (kind === 'gene_set' && metric === 'marginal') values = values.map(row => ({ ...row, loading: row.marginal_loading })).sort((a, b) => b.loading - a.loading).map((row, index) => ({ ...row, rank: index + 1 }));
      const numeric = values.flatMap(row => row.loading === null ? [] : [row.loading]);
      const summary = { ...(kind === 'gene' ? detail.genes : detail.gene_sets), min: numeric.length ? Math.min(...numeric) : null, max: numeric.length ? Math.max(...numeric) : null, available: !!numeric.length };
      if (sort === 'alphabetical') values.sort((a, b) => a.label.toLowerCase().localeCompare(b.label.toLowerCase()) || a.id.localeCompare(b.id));
      const matches = values.filter(row => `${row.label} ${row.library || ''}`.toLowerCase().includes(q));
      const json = { source_id: factor.source_id, generation_id: generation, kind, metric, sort, items: matches.slice(offset, offset + limit), total: matches.length, offset, limit, next_offset: offset + limit < matches.length ? offset + limit : null, summary };
      if (q === 'shank' && options.race) await new Promise(resolve => pending.set('slow-search', resolve));
      try { return await route.fulfill({ json }); } catch (error) { if (!/closed|cancel|intercept/i.test(String(error))) throw error; }
      return;
    }
    if (path.startsWith('/api/backend/v1/catalog/gene-sets/')) {
      assert.equal(url.searchParams.get('generation_id'), generation, 'Membership and provenance requests stay generation pinned');
      const row = sets.find(value => path === `/api/backend/v1/catalog/gene-sets/${value.id}`); assert.ok(row, 'Overlay choices must come from the current factor');
      let result = structuredClone(catalog);
      if (row.id !== setId) result = { ...result, id: row.id, name: row.label, object: { ...result.object, id: row.id, name: row.label, members: row.id === sets[1].id ? ['HGNC.SYMBOL:SHANK3'] : ['HGNC.SYMBOL:PTEN', 'ENSEMBL:ENSG00000171862'] } };
      if (row.id === sets[3].id) result.generation_id = '0'.repeat(64);
      if (row.id === sets[1].id && options.race) await new Promise(resolve => pending.set('slow-membership', resolve));
      try { return await route.fulfill({ json: options.unknownMembers ? { ...result, gene_count: null, genes_in_universe: null, object: null, collection: null, provenance: {}, limitations: ['The source does not include the original membership or provenance records.'] } : result }); }
      catch (error) { if (!/closed|cancel|intercept/i.test(String(error))) throw error; }
      return;
    }
    if (path === `/api/backend/v1/reference-factors/${old.archive_id}`) return route.fulfill({ json: old });
    unexpected.push(path); return route.fulfill({ status: 500, json: { detail: 'Unexpected browser fixture route' } });
    } catch (error) {
      errors.push(String(error));
      return route.fulfill({ status: 500, json: { detail: String(error) } }).catch(() => {});
    }
  });
  return { page, calls, pending, gates, async open(path = factorUrl) { await page.goto(origin + path); }, async shot(filename, locator = page) { await locator.screenshot({ path: resolve(output, filename), ...(locator === page ? { fullPage: !options.mobile } : {}) }); report.screenshots.push(filename); }, async close(checks) { for (const release of pending.values()) release(); assert.deepEqual(errors, []); assert.deepEqual(unexpected, []); report.scenarios.push({ name, checks, calls }); await context.close(); activePage = null; } };
}
const tiles = (page, kind = 'gene') => page.locator(`#${kind === 'gene' ? 'gene-loadings' : 'gene-set-loadings'} .loading-heatmap-tile`);
const tab = (page, kind = 'gene') => page.getByRole('tab', { name: kind === 'gene' ? /^Gene loadings/ : /^Gene-set loadings/ });
const geneTile = (page, symbol) => panel(page).getByRole('button', { name: new RegExp(`^${symbol}; loading`) });
const labels = async (page, kind = 'gene') => tiles(page, kind).evaluateAll(elements => elements.map(element => element.getAttribute('aria-label').split(';')[0]));
const alphabetical = rows => [...rows].sort((a, b) => a.label.toLowerCase().localeCompare(b.label.toLowerCase()) || a.id.localeCompare(b.id)).map(row => row.label);
const panel = (page, kind = 'gene') => page.locator(kind === 'gene' ? '#gene-loadings' : '#gene-set-loadings');
const waitTiles = (page, count, kind = 'gene') => page.waitForFunction(({ id, count }) => document.querySelectorAll(`#${id} .loading-heatmap-tile`).length === count, { id: kind === 'gene' ? 'gene-loadings' : 'gene-set-loadings', count });

try {
  const full = await harness('factor tabs, alphabetical append, exact values, overlay races and provenance', { race: true });
  const page = full.page; await full.open(); await page.getByRole('heading', { name: factor.cfde_anchor.label, exact: true }).waitFor(); await waitTiles(page, 200);
  assert.match(await page.getByRole('link', { name: 'Back to research run', exact: false }).getAttribute('href'), /job=152f549a/);
  const metadata = page.locator('details.factor-metadata'); assert.equal(await metadata.getAttribute('open'), null);
  await metadata.locator('summary').first().focus(); await page.keyboard.press('Enter');
  const traitLink = metadata.getByRole('link', { name: new RegExp(factor.kpn_trait.id) });
  assert.equal(await traitLink.getAttribute('href'), `https://broadinstitute.github.io/kpn-data-models/kpn.trait/${factor.kpn_trait.id.split(':')[1]}/`);
  assert.equal(await traitLink.getAttribute('target'), '_blank'); await metadata.locator('summary').first().click();
  assert.equal(await tab(page).getAttribute('aria-selected'), 'true'); assert.equal(await panel(page).getByRole('combobox', { name: 'Order', exact: true }).inputValue(), 'alphabetical');
  assert.equal(full.calls.some(call => call.includes('kind=gene_set')), false, 'The second panel is loaded on first visit');
  assert.deepEqual(await labels(page), alphabetical(genes).slice(0, 200));
  await geneTile(page, 'NRXN1').hover(); assert.match(await page.getByRole('tooltip').innerText(), /NRXN1.*0[.]7196999788284302.*Rank 1/s);
  const originalColor = await geneTile(page, 'NRXN1').evaluate(element => getComputedStyle(element).backgroundColor);
  await page.mouse.move(0, 0); await tiles(page).first().focus(); await page.keyboard.press('ArrowRight');
  assert.match(await page.locator(':focus').getAttribute('aria-label'), /^GABRB2;/);
  await page.keyboard.press('Enter'); assert.match(await panel(page).locator('.loading-heatmap-selection').innerText(), /GABRB2.*0[.]658/s);
  await page.keyboard.press('Escape'); assert.equal(await panel(page).locator('.loading-heatmap-selection').count(), 0);
  assert.equal(await panel(page).locator('.loading-heatmap-tile[tabindex="0"]').count(), 1);
  const search = page.getByRole('searchbox', { name: 'Search gene loadings', exact: true });
  const before = full.calls.length; await search.fill('N'); await search.fill('NR'); await search.fill('NRXN'); await waitTiles(page, 2);
  assert.equal(await geneTile(page, 'NRXN1').evaluate(element => getComputedStyle(element).backgroundColor), originalColor, 'Filtering must not recolor a loading');
  const queries = full.calls.slice(before).filter(value => value.includes('/factor-loadings?')).map(value => new URL(value, origin).searchParams.get('q'));
  assert.deepEqual(queries, ['NRXN'], 'Typing is debounced to the final query');
  await search.fill('SHANK'); await page.waitForRequest(request => new URL(request.url()).searchParams.get('q') === 'SHANK');
  await search.fill('PTEN'); await geneTile(page, 'PTEN').waitFor();
  assert.ok(full.pending.has('slow-search')); full.pending.get('slow-search')(); full.pending.delete('slow-search');
  await page.waitForTimeout(150); assert.match(await tiles(page).first().getAttribute('aria-label'), /^PTEN;/, 'An older search response must not replace the latest result');
  await search.fill(''); await waitTiles(page, 200);
  const firstPageGenes = await labels(page); await panel(page).getByRole('button', { name: 'Load more genes', exact: true }).click(); await waitTiles(page, 205);
  assert.deepEqual((await labels(page)).slice(0, 200), firstPageGenes, 'Continuation appends instead of replacing'); assert.deepEqual(await labels(page), alphabetical(genes));
  assert.match(await panel(page).locator('.loading-result-count').innerText(), /205 of 205 genes/);
  assert.match(await tiles(page).last().getAttribute('aria-label'), /loading 0; rank 205/);
  await tab(page).focus(); await page.keyboard.press('ArrowRight'); await waitTiles(page, 50, 'gene_set'); assert.equal(await tab(page, 'gene_set').getAttribute('aria-selected'), 'true');
  const setsPanel = panel(page, 'gene_set'); assert.deepEqual(await labels(page, 'gene_set'), alphabetical(sets).slice(0, 50));
  const firstPageSets = await labels(page, 'gene_set'); await setsPanel.getByRole('button', { name: 'Load more gene sets', exact: true }).click(); await waitTiles(page, 73, 'gene_set');
  assert.deepEqual((await labels(page, 'gene_set')).slice(0, 50), firstPageSets); assert.deepEqual(await labels(page, 'gene_set'), alphabetical(sets));
  const beforeTabs = full.calls.length; await tab(page).click(); await waitTiles(page, 205); await tab(page, 'gene_set').click(); await waitTiles(page, 73, 'gene_set');
  assert.equal(full.calls.slice(beforeTabs).filter(call => call.includes('/factor-loadings?')).length, 0, 'Both tab panels retain their loaded pages');
  await setsPanel.getByRole('combobox', { name: 'Loading', exact: true }).selectOption('marginal'); await waitTiles(page, 50, 'gene_set');
  await page.waitForFunction(() => document.querySelector('#gene-set-loadings .loading-heatmap-tile')?.getAttribute('aria-label')?.includes('loading 0.3683;'));
  assert.match(await tiles(page, 'gene_set').first().getAttribute('aria-label'), /rank 2/, 'Alphabetical presentation preserves metric rank');
  await setsPanel.getByRole('combobox', { name: 'Order', exact: true }).selectOption('loading');
  await page.waitForFunction(() => document.querySelector('#gene-set-loadings .loading-heatmap-tile')?.getAttribute('aria-label')?.includes('loading 0.92;'));
  await setsPanel.getByRole('combobox', { name: 'Loading', exact: true }).selectOption('joint');
  await page.waitForFunction(() => document.querySelector('#gene-set-loadings .loading-heatmap-tile')?.getAttribute('aria-label')?.includes('loading 0.3013;'));
  await setsPanel.getByRole('combobox', { name: 'Order', exact: true }).selectOption('alphabetical');
  await tiles(page, 'gene_set').first().click(); await setsPanel.getByRole('link', { name: /View gene set and provenance/ }).waitFor();
  await setsPanel.getByRole('button', { name: 'Overlay on genes', exact: true }).click(); await waitTiles(page, 205);
  const chooser = page.getByRole('combobox', { name: 'Overlay gene set', exact: true });
  await page.waitForFunction(() => document.querySelectorAll('#gene-loadings .loading-heatmap-tile-member').length === 3);
  assert.deepEqual(await panel(page).locator('.loading-heatmap-tile-member').evaluateAll(elements => elements.map(element => element.getAttribute('aria-label').split(';')[0])), ['GABRA1', 'GABRB2', 'NRXN1']);
  assert.match(await geneTile(page, 'NRXN1_ALT').getAttribute('class'), /tile-nonmember/, 'A symbol prefix is not exact membership');
  assert.equal(await geneTile(page, 'NRXN1').evaluate(element => getComputedStyle(element).backgroundColor), originalColor, 'Membership emphasis must not change loading colors');
  assert.equal(await chooser.inputValue(), setId); await chooser.locator('option', { hasText: sets.at(-1).label }).waitFor({ state: 'attached' });
  assert.equal(await chooser.locator('option').count(), sets.length + 1, 'All gene sets are offered, including later panel pages');
  await full.shot('factor-overlay.png');
  await Promise.all([page.waitForRequest(request => decodeURIComponent(new URL(request.url()).pathname).endsWith(sets[1].id)), chooser.selectOption(sets[1].id)]);
  await chooser.selectOption(sets[2].id); await page.waitForFunction(() => document.querySelector('#gene-loadings .loading-heatmap-tile-member')?.getAttribute('aria-label')?.startsWith('PTEN;'));
  assert.ok(full.pending.has('slow-membership')); full.pending.get('slow-membership')(); full.pending.delete('slow-membership');
  await page.waitForTimeout(150); assert.match(await geneTile(page, 'SHANK3').getAttribute('class'), /membership-unknown/, 'A late membership response never replaces the latest choice');
  assert.match(await geneTile(page, 'PTEN').getAttribute('class'), /tile-member/);
  await chooser.selectOption(sets[3].id); await page.getByText('The gene set belongs to a different reference. Reload the factor page.', { exact: false }).waitFor();
  assert.equal(await panel(page).locator('.loading-heatmap-overlay').count(), 0, 'Mismatched generation membership is rejected');
  await page.getByRole('button', { name: 'Clear overlay', exact: true }).click(); assert.equal(await chooser.inputValue(), '');
  assert.equal(await panel(page).locator('.loading-heatmap-tile-member,.loading-heatmap-tile-nonmember,.loading-heatmap-tile-membership-unknown').count(), 0);
  assert.equal(await geneTile(page, 'NRXN1').evaluate(element => getComputedStyle(element).backgroundColor), originalColor);
  await full.shot('factor-desktop.png');
  await tab(page, 'gene_set').click(); await setsPanel.getByRole('button', { name: 'Table', exact: true }).click();
  await setsPanel.locator('tbody tr').first().getByRole('button', { name: 'Overlay on genes', exact: true }).click(); await waitTiles(page, 205);
  await page.waitForFunction(() => document.querySelectorAll('#gene-loadings .loading-heatmap-tile-member').length === 3);
  await page.getByRole('button', { name: 'Clear overlay', exact: true }).click(); await tab(page, 'gene_set').click();
  const link = setsPanel.getByRole('link', { name: sets[0].label, exact: true }), href = await link.getAttribute('href');
  assert.equal(new URL(href, origin).searchParams.get('generation_id'), generation);
  assert.match(new URL(href, origin).searchParams.get('from'), /source_revision=/);
  await link.click(); await page.getByRole('heading', { name: catalog.name, exact: true }).waitFor();
  await page.getByRole('heading', { name: 'LINCS L1000 source dataset', exact: true }).waitFor();
  assert.match(await page.locator('.provenance-relations').innerText(), /Extract chemical perturbation signatures/);
  await page.locator('.provenance-relations').getByRole('link', { name: 'Extract chemical perturbation signatures', exact: true }).click();
  assert.equal(await page.locator('details.provenance-entry').filter({ has: page.locator('summary', { hasText: 'Extract chemical perturbation signatures' }) }).getAttribute('open'), '', 'Created by opens the correct provenance activity');
  const members = page.getByRole('searchbox', { name: 'Search member genes', exact: true }); await members.fill('GABRA');
  assert.deepEqual(await page.locator('.member-genes li').allTextContents(), ['GABRA1']);
  await members.fill('DOES_NOT_EXIST'); await page.getByText('No member genes match “DOES_NOT_EXIST”.', { exact: true }).waitFor();
  await members.fill(''); await page.getByRole('button', { name: 'Show all 113 genes', exact: true }).click(); assert.equal(await page.locator('.member-genes li').count(), 113);
  await full.shot('gene-set-provenance.png');
  await page.getByRole('link', { name: 'Back to factor', exact: false }).click(); await waitTiles(page, 200);
  await full.close(['collapsed metadata and accessible trait link', 'alphabetical requests and global ordering with preserved ranks', 'exact float hover and roving keyboard inspection', 'search debounce and late-response protection', 'fixed scale across filtering', 'both page continuations append', 'tab panels retain loaded rows', 'joint/marginal and strongest-first switch', 'exact member overlay and unchanged colors', 'membership response races and generation pinning', 'unknown namespace membership stays unknown', 'clear overlay and heatmap/table overlay actions', 'generation-pinned provenance and searchable members', 'return to exact factor']);

  const retry = await harness('factor and loading retries, missing scores and empty search', { retry: true, nullSets: true });
  await retry.open(); await retry.page.getByText('Fixture factor lookup interrupted. Please retry.', { exact: true }).waitFor(); retry.gates.detail = true; await retry.page.getByRole('button', { name: 'Retry', exact: false }).click();
  await panel(retry.page).getByText('Fixture loading lookup interrupted. Please retry.', { exact: true }).waitFor(); retry.gates.genes = true; await panel(retry.page).getByRole('button', { name: 'Retry', exact: false }).click(); await waitTiles(retry.page, 200); await tab(retry.page, 'gene_set').click(); await waitTiles(retry.page, 50, 'gene_set');
  assert.equal(await panel(retry.page, 'gene_set').locator('.loading-heatmap-tile.loading-heatmap-tile-unknown').count(), 50);
  await tiles(retry.page, 'gene_set').first().hover(); assert.match(await retry.page.getByRole('tooltip').innerText(), /Not available/);
  await tab(retry.page).click();
  await retry.page.getByRole('searchbox', { name: 'Search gene loadings', exact: true }).fill('NO_SUCH_GENE');
  await panel(retry.page).getByText('No matching loadings', { exact: true }).waitFor();
  await panel(retry.page).getByRole('button', { name: 'Clear search', exact: true }).click(); await waitTiles(retry.page, 200);
  await retry.shot('factor-missing-scores.png'); await retry.close(['failed detail retries', 'failed loading panel retries independently', 'null scores stay unknown', 'empty search is recoverable']);

  const archived = await harness('stale factor renders its frozen snapshot without live loading requests', { stale: true });
  await archived.open(`/factors/${encodeURIComponent(old.source_id)}?source_revision=old-revision`);
  await archived.page.getByRole('heading', { name: old.label, exact: true }).waitFor();
  await archived.page.getByRole('button', { name: /^ARCHIVED_NRXN1; loading/ }).waitFor();
  assert.equal(archived.calls.some(call => call.includes('/factor-loadings')), false);
  await archived.page.getByRole('searchbox', { name: 'Search saved loadings', exact: true }).fill('SHANK');
  assert.equal(await archived.page.getByRole('button', { name: /^ARCHIVED_NRXN1; loading/ }).count(), 0);
  await archived.page.getByRole('button', { name: /^ARCHIVED_SHANK3; loading/ }).waitFor();
  await archived.shot('factor-archived.png'); await archived.close(['stale source is never replaced by active generation', 'frozen top loadings remain searchable', 'no current loading requests']);

  const explicitArchive = await harness('explicit archive link preserves captured identity');
  await explicitArchive.open(`/factors/${encodeURIComponent(old.source_id)}?archive=${old.archive_id}`);
  await explicitArchive.page.getByRole('button', { name: /^ARCHIVED_NRXN1; loading/ }).waitFor();
  assert.equal(explicitArchive.calls.some(call => call.includes('/factor-loadings') || call.includes('/v1/factors/')), false);
  await explicitArchive.close(['archive route calls only the immutable snapshot endpoint']);

  const unknown = await harness('unknown member counts are never presented as zero', { unknownMembers: true });
  await unknown.open(`/gene-sets/${encodeURIComponent(setId)}?generation_id=${generation}`);
  await unknown.page.getByText('Gene count not reported', { exact: true }).waitFor();
  await unknown.page.getByText('The source does not report a member count or an explicit membership list.', { exact: true }).waitFor();
  assert.equal(await unknown.page.getByText('0 genes', { exact: true }).count(), 0);
  await unknown.close(['missing gene count and membership remain explicit', 'missing provenance remains explicit']);

  const mobile = await harness('mobile factor grid, touch inspection and provenance without overflow', { mobile: true });
  await mobile.open(); await waitTiles(mobile.page, 200);
  assert.ok(await mobile.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'Factor page must fit narrow viewports');
  const bounds = await tiles(mobile.page).first().boundingBox(); assert.ok(bounds.width >= 43 && bounds.height >= 43, 'Touch tiles remain usable');
  await tiles(mobile.page).first().tap(); await panel(mobile.page).locator('.loading-heatmap-selection').waitFor();
  report.mobile_layout = await mobile.page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, coarse_pointer: matchMedia('(pointer: coarse)').matches, tile_width: document.querySelector('.loading-heatmap-tile').getBoundingClientRect().width }));
  await mobile.page.screenshot({ path: resolve(output, 'factor-mobile-viewport.png') }); report.screenshots.push('factor-mobile-viewport.png');
  await mobile.shot('factor-mobile.png');
  await tab(mobile.page, 'gene_set').click(); await waitTiles(mobile.page, 50, 'gene_set');
  await panel(mobile.page, 'gene_set').getByRole('button', { name: 'Table', exact: true }).click();
  await panel(mobile.page, 'gene_set').getByRole('link', { name: sets[0].label, exact: true }).click();
  await mobile.page.getByRole('heading', { name: catalog.name, exact: true }).waitFor();
  assert.ok(await mobile.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'Long source identities and names must wrap');
  await mobile.shot('gene-set-mobile.png'); await mobile.close(['no document overflow', '44px touch squares', 'tap pins exact details', 'long gene-set names and provenance fit mobile']);
  report.status = 'passed';
} catch (error) {
  report.status = 'failed'; report.error = String(error);
  if (activePage) await activePage.screenshot({ path: resolve(output, 'failure.png'), fullPage: true }).catch(() => {});
  throw error;
} finally { await writeFile(resolve(output, 'report.json'), JSON.stringify(report, null, 2) + '\n'); await browser.close(); }
console.log(JSON.stringify({ status: report.status, scenarios: report.scenarios.length, output }));
