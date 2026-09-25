/* Exercise the actual JavaScript shipped in the generated, standalone portal.
 * Run: node --test tests/portal.test.cjs (no npm dependencies). */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const cytoscape = require('../portal/lib/cytoscape.min.js');

const html = fs.readFileSync(path.join(__dirname, '../portal/index.html'), 'utf8');
const app = html.slice(html.lastIndexOf('<script>') + '<script>'.length, html.lastIndexOf('</script>'));
const DATA = JSON.parse(app.match(/^const DATA = (.*);$/m)[1]);
const helpers = app.slice(app.indexOf('const esc ='), app.indexOf('function renderInspector'));
const uploader = app.slice(app.indexOf('function graphFromDoc'), app.indexOf('/* Structural checks'));
const trace = app.slice(app.indexOf('function applyTrace'), app.indexOf('/* ---- inspector'));
const visuals = app.slice(app.indexOf('function edgeAppearance'), app.indexOf('function styleSheet'));

function runtime(cy = null) {
  const note = { textContent: '' };
  const context = vm.createContext({ DATA, URL, cy, state: { edgeMode: 'statements' }, document: { getElementById: () => note } });
  vm.runInContext(helpers + uploader + trace + visuals, context);
  return { context, run: source => vm.runInContext(source, context), note };
}

test('scientific accounts show roles, evidence, and a saved paragraph without asserting a disputed target', () => {
  const { run } = runtime();
  const rendered = run(`(() => {
    const graph = JSON.parse(JSON.stringify(DATA.graphs.find(g => g.key === "scientific_account")));
    const account = graph.nodes.find(n => n.cls === "ScientificAccount");
    graph.nodes.find(n => n.id === account.fields.question).fields.text = '<img src=x onerror=alert(1)>';
    const conclusion = graph.nodes.find(n => n.id === account.fields.conclusion_claims[0]);
    delete conclusion.fields.statement;
    conclusion.fields.direction = 'DISPUTES';
    return accountOverviewHtml(account, graph);
  })()`);
  assert.match(rendered, /Hypothesis under investigation/);
  assert.match(rendered, /Knowledge gap/);
  assert.match(rendered, /<strong>Claim<\/strong>/);
  assert.match(rendered, /<strong>Conclusion claim<\/strong>/);
  assert.match(rendered, /Evidence and interpretation/);
  assert.match(rendered, /Results paragraph/);
  assert.match(rendered, /Cited objects/);
  assert.match(rendered, /metadata revision 1/);
  assert.match(rendered, /Entity context/);
  assert.match(rendered, /Assessment of the proposition:/);
  assert.match(rendered, /DISPUTES/);
  assert.match(rendered, /&lt;img/);
  assert.doesNotMatch(rendered, /<img/);
});

test('citation excerpts count Unicode code points and escape authored text', () => {
  const { run } = runtime();
  const rendered = run(`paragraphCitationsHtml({fields: {
    text: '🧬 <img> claim', citations: [{target_id: 'claim', citation_metadata_revision: 2, start: 2, end: 7}]
  }}, {nodes: [{id: 'claim'}]})`);
  assert.match(rendered, /data-goto="claim"/);
  assert.match(rendered, /metadata revision 2/);
  assert.match(rendered, /“&lt;img&gt;”/);
  assert.doesNotMatch(rendered, /<img>/);
});

test('uploaded scientific accounts open at the account even when a paragraph is its terminal expression', () => {
  const { run } = runtime();
  const graph = run(`graphFromDoc({
    scientific_accounts: [{id: "account", question: "question", component_claims: ["claim"]}],
    questions: [{id: "question", text: "What was found?"}],
    claims: [{id: "claim", proposition: "proposition"}],
    propositions: [{id: "proposition", statement: "An illustrative result."}],
    paragraphs: [{id: "paragraph", scientific_account: "account", text: "A rendering."}]
  }, "account.yaml")`);
  assert.equal(graph.start, 'account');
  assert.equal(graph.nodes.length, 5);
  assert.equal(graph.edges.length, 4);
});

test('CURIEs resolve with slash-bearing DOI suffixes; only known DAPPER terms get pages', () => {
  const { run } = runtime();
  assert.equal(run('curieUrl("KPN.TRAIT:0000096")'),
    'https://broadinstitute.github.io/kpn-data-models/kpn.trait/0000096/');
  assert.equal(run('curieUrl("doi:10.1000/example/path")'), 'https://doi.org/10.1000/example/path');
  assert.equal(run('curieUrl("dapper:drsRepresentation")'), 'model/reference/slots/drs_representation/');
  assert.equal(run('curieUrl("dapper:File.example")'), null);
  assert.equal(run('curieUrl("unknown:example")'), null);
  assert.equal(run('curieUrl("dapper:__proto__")'), null);
});

test('local references navigate the graph before considering an external URL', () => {
  const { run } = runtime();
  const rendered = run('valueHtml("doi:10.1000/example", new Set(["doi:10.1000/example"]))');
  assert.match(rendered, /data-goto="doi:10.1000\/example"/);
  assert.doesNotMatch(rendered, /href=/);
});

test('gene-set membership pointers navigate both ways with a consistent flow layout', () => {
  const { run } = runtime();
  const graph = run(`graphFromDoc({
    gene_sets: [{id: "set", name: "A gene set", in_gene_set_collection: ["collection"]}],
    gene_set_collections: [{id: "collection", name: "A library", members: ["set"]}]
  }, "membership.yaml")`);
  assert.equal(graph.start, 'collection');
  assert.equal(graph.edges.length, 2);
  const forward = graph.edges.find(e => e.predicate === 'prov:hadMember');
  const inverse = graph.edges.find(e => e.predicate === 'dapper:inGeneSetCollection');
  assert.equal(forward.subject, 'collection');
  assert.equal(forward.object, 'set');
  assert.equal(inverse.subject, 'set');
  assert.equal(inverse.object, 'collection');
  for (const edge of graph.edges) {
    assert.equal(edge.source, 'set');
    assert.equal(edge.target, 'collection');
  }
  assert.match(run('valueHtml(["collection"], new Set(["collection"]))'), /data-goto="collection"/);
  assert.equal(run('curieUrl("dapper:inGeneSetCollection")'), 'model/reference/slots/in_gene_set_collection/');
  const example = DATA.graphs.find(g => g.key === 'gmt_rows');
  assert.equal(example.edges.filter(e => e.predicate === 'dapper:inGeneSetCollection').length, 2);
});

test('enum values link to definitions with explicitly qualified ontology mappings', () => {
  const { run } = runtime();
  const render = value => run(`valueHtml(${JSON.stringify(value)}, new Set(), DATA.schema.Dataset.attributes.ancestry)`);
  const details = value => run(`modelDetailsHtml(DATA.schema.Dataset, {ancestry: ${JSON.stringify(value)}})`);
  assert.match(render('AA'), /href="model\/reference\/enums\/AncestryEnum\/"/);
  assert.match(render('AA'), /href="http:\/\/purl.obolibrary.org\/obo\/HANCESTRO_0016"/);
  assert.match(details('AA'), /Meaning:.*HANCESTRO_0016/);
  assert.match(details('HS'), /close:.*HANCESTRO_0014/);
  assert.doesNotMatch(details('HS'), /Meaning:/);
  assert.doesNotMatch(details('Mixed'), /HANCESTRO|Meaning:/);
});

test('nested fields and prose link references without turning HTML into markup', () => {
  const { run } = runtime();
  const rendered = run('valueHtml({citation: ["doi:10.1000/example"], note: "See (https://example.org/a). <img src=x onerror=alert(1)>"}, new Set())');
  assert.match(rendered, /href="https:\/\/doi.org\/10.1000\/example"/);
  assert.match(rendered, /href="https:\/\/example.org\/a"/);
  assert.match(rendered, /&lt;img/);
  assert.doesNotMatch(rendered, /<img/);
  for (const value of ['javascript:alert(1)', 'data:text/html,<script>alert(1)</script>', 'https://', 'https://example.org/" onclick="alert(1)']) {
    const rendered = run(`valueHtml(${JSON.stringify(value)}, new Set())`);
    assert.doesNotMatch(rendered, /href="(?:javascript:|data:)|<script|" onclick="/);
  }
});

test('S3 files and prefixes link to distinct HTTPS operations with encoded keys', () => {
  const { run } = runtime();
  assert.equal(run('referenceUrl("s3://example-bucket/AA/file name#1.tsv")'),
    'https://s3.amazonaws.com/example-bucket/AA/file%20name%231.tsv');
  assert.equal(run('referenceUrl("s3://example-bucket/AA/")'),
    'https://s3.amazonaws.com/example-bucket/?list-type=2&prefix=AA%2F');
});

test('uploaded file/activity graphs start at an output and preserve illustrative flags', () => {
  const { run } = runtime();
  const graph = run(`graphFromDoc({files: [{id: "input"}, {id: "output"}], activities: [{id: "run"}],
    used_edges: [{subject: "run", object: "input"}],
    was_generated_by_edges: [{subject: "output", object: "run"}], _illustrative: ["run"]}, "example.yaml")`);
  assert.equal(graph.start, 'output');
  assert.equal(graph.nodes.find(n => n.id === 'run').illustrative, true);
  assert.equal(graph.nodes.find(n => n.id === 'input').illustrative, false);
  const used = graph.edges.find(e => e.predicate === 'prov:used');
  const generated = graph.edges.find(e => e.predicate === 'prov:wasGeneratedBy');
  assert.equal(used.subject, 'run');
  assert.equal(used.object, 'input');
  assert.equal(used.source, 'input');
  assert.equal(used.target, 'run');
  assert.equal(generated.subject, 'output');
  assert.equal(generated.object, 'run');
  assert.equal(generated.source, 'run');
  assert.equal(generated.target, 'output');
});

test('stored arrows follow subject to object while both views retain input-to-output layout', () => {
  const { context, run } = runtime();
  for (const graph of DATA.graphs) {
    context.graph = graph;
    const original = JSON.stringify(graph);
    const stored = run('state.edgeMode = "statements"; buildElements(graph)').filter(e => e.data.predicate);
    const flow = run('state.edgeMode = "flow"; buildElements(graph)').filter(e => e.data.predicate);
    assert.equal(stored.length, flow.length);
    for (let i = 0; i < stored.length; i++) {
      const s = stored[i].data, f = flow[i].data;
      const backward = s.sourceArrow === 'triangle';
      assert.equal(backward ? s.target : s.source, s.subject);
      assert.equal(backward ? s.source : s.target, s.object);
      assert.equal(s.label, s.predicate);
      assert.equal(s.source, f.source);
      assert.equal(s.target, f.target);
      assert.equal(f.targetArrow, 'triangle');
      assert.equal(f.sourceArrow, 'none');
      if (backward && s.predicate === 'prov:wasGeneratedBy') assert.equal(f.label, 'prov:generated');
      if (backward && s.predicate === 'prov:used') assert.equal(f.label, 'was used by');
    }
    assert.equal(JSON.stringify(graph), original);
  }
});

test('upstream provenance is identical in both arrow views and excludes other outputs', () => {
  const { context, run } = runtime();
  run(`globalThis.graph = graphFromDoc({files: [{id: "input"}, {id: "output"}, {id: "other-output"}],
    activities: [{id: "run"}], used_edges: [{subject: "run", object: "input"}],
    was_generated_by_edges: [{subject: "output", object: "run"}, {subject: "other-output", object: "run"}]
  }, "branching.yaml")`);
  for (const mode of ['flow', 'statements']) {
    const elements = run(`state.edgeMode = "${mode}"; buildElements(graph)`);
    // Cytoscape checks plain-object prototypes; move VM objects into this realm.
    const cy = cytoscape({ headless: true, elements: JSON.parse(JSON.stringify(elements)) });
    cy.animate = () => {};
    context.cy = cy;
    try {
      run('applyTrace("output")');
      assert.deepEqual(cy.nodes('.lit').map(n => n.id()).sort(), ['input', 'output', 'run']);
      assert.equal(cy.$id('other-output').hasClass('dim'), true);
    } finally { cy.destroy(); }
  }
});

test('upstream trace follows deep grouped dependencies and resets old highlights', () => {
  const elements = [];
  for (let i = 0; i < 30; i++) {
    elements.push({ data: { id: `group${i}` } });
    elements.push({ data: { id: `node${i}`, parent: `group${i}` } });
    if (i > 0) elements.push({ data: { id: `edge${i}`, source: `group${i - 1}`, target: `node${i}` } });
  }
  const cy = cytoscape({ headless: true, elements });
  cy.animate = () => {}; // Layout animation is exercised in the browser.
  try {
    const { run, note } = runtime(cy);
    run('applyTrace("node29")');
    assert.equal(cy.$id('node0').hasClass('lit'), true);
    assert.match(note.textContent, /60 nodes in this trace/);
    assert.doesNotMatch(note.textContent, /raw source|C2M2|claim/);
    run('applyTrace("node0")');
    assert.equal(cy.$id('node29').hasClass('lit'), false);
    assert.equal(cy.$id('node29').hasClass('dim'), true);
    run('clearTrace()');
    assert.equal(cy.elements('.lit, .dim').length, 0);
  } finally { cy.destroy(); }
});

test('a result keeps its file and DRS access visible without tracing downstream consumers', () => {
  const { context, run } = runtime();
  run(`globalThis.graph = graphFromDoc({
    datasets: [{id: "result", has_file: ["file"]}],
    files: [{id: "input"}, {id: "file", filename: "result.tsv.gz", name: "Published endpoint", drs_representation: ["drs"]}],
    drs_objects: [{id: "drs"}], activities: [{id: "run"}, {id: "consumer"}],
    used_edges: [{subject: "run", object: "input"}, {subject: "consumer", object: "file"}],
    was_generated_by_edges: [{subject: "result", object: "run"}, {subject: "file", object: "run"}]
  }, "result.yaml")`);
  assert.equal(run('graph.nodes.find(n => n.id === "file").label'), 'result.tsv.gz');
  const elements = JSON.parse(JSON.stringify(run('buildElements(graph)')));
  const cy = cytoscape({ headless: true, elements });
  cy.animate = () => {};
  context.cy = cy;
  try {
    run('applyTrace("result")');
    assert.deepEqual(cy.nodes('.lit').map(n => n.id()).sort(), ['input', 'result', 'run']);
    assert.deepEqual(cy.nodes('.context').map(n => n.id()).sort(), ['drs', 'file']);
    assert.equal(cy.$id('consumer').hasClass('dim'), true);
    const generation = cy.edges().filter(e => e.data('subject') === 'file' && e.data('object') === 'run');
    assert.equal(generation.hasClass('context'), true);
    assert.equal(generation.hasClass('dim'), false);
    run('applyTrace("input")');
    assert.equal(cy.elements('.context').length, 0);
    assert.equal(cy.$id('file').hasClass('dim'), true);
    run('clearTrace()');
    assert.equal(cy.elements('.lit, .dim, .context').length, 0);
  } finally { cy.destroy(); }
});
