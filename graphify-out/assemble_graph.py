"""Assemble and inspect this Graphify build from completed local extraction chunks."""
from pathlib import Path
import json
from graphify.cache import save_semantic_cache
from graphify.build import build_from_json
from graphify.cluster import cluster, score_all
from graphify.analyze import god_nodes, surprising_connections, suggest_questions
from graphify.report import generate
from graphify.export import to_json
from graphify.diagnostics import diagnose_extraction, format_diagnostic_report
from enrich_extraction import build, ROOT, OUT

SPEC = '/Users/cyakaboski/.agents/skills/graphify/references/extraction-spec.md'
parts = []
for i in range(1, 4):
    path = OUT / f'.graphify_chunk_{i:02}.json'
    part = json.loads(path.read_text())
    assert isinstance(part['nodes'], list) and isinstance(part['edges'], list), path
    for edge in part['edges']:
        assert edge.get('confidence_score') is not None, (path, edge)
    parts.append(part)
new = {key: [item for part in parts for item in part.get(key, [])]
       for key in ('nodes', 'edges', 'hyperedges')}
uncached = (OUT / '.graphify_uncached.txt').read_text().splitlines()
saved = save_semantic_cache(new['nodes'], new['edges'], new['hyperedges'],
                            root=str(ROOT), allowed_source_files=uncached, prompt_file=SPEC)
print(f'Cached semantic extraction for {saved} files')
cached_path = OUT / '.graphify_cached.json'
if cached_path.exists():
    cached = json.loads(cached_path.read_text())
    for key in new:
        new[key] += cached.get(key, [])
(OUT / '.graphify_semantic.json').write_text(json.dumps(new, indent=2))
build()
extraction = json.loads((OUT / '.graphify_extract.json').read_text())
detection = json.loads((OUT / '.graphify_detect.json').read_text())
G = build_from_json(extraction, root=str(ROOT), directed=False)
assert G.number_of_nodes(), 'Refusing to export empty graph'
communities = cluster(G)
cohesion = score_all(G, communities)
gods = god_nodes(G)
surprises = surprising_connections(G, communities)
labels = {cid: 'Community ' + str(cid) for cid in communities}
questions = suggest_questions(G, communities, labels)
assert to_json(G, communities, str(OUT / 'graph.json')), 'Graph shrink guard refused export'
report = generate(G, communities, cohesion, labels, gods, surprises, detection,
                  {'input': 0, 'output': 0}, str(ROOT), suggested_questions=questions)
report = report.replace('- Token cost: 0 input · 0 output',
                        '- Token cost: unavailable (host-agent usage was not exposed; zero placeholders are not measured usage)')
(OUT / 'GRAPH_REPORT.md').write_text(report)
analysis = dict(communities=communities, cohesion=cohesion, gods=gods,
                surprises=surprises, questions=questions)
(OUT / '.graphify_analysis.json').write_text(json.dumps(analysis, indent=2))
diagnostic = diagnose_extraction(extraction, directed=False, root=str(ROOT))
(OUT / 'GRAPH_HEALTH.json').write_text(json.dumps(diagnostic, indent=2))
print(format_diagnostic_report(diagnostic))
for cid, members in communities.items():
    top = sorted(members, key=lambda n: G.degree(n), reverse=True)[:9]
    print('COMMUNITY', cid, len(members), ':', ' | '.join(G.nodes[n].get('label', n) for n in top))
print(f'Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, {len(communities)} communities')
