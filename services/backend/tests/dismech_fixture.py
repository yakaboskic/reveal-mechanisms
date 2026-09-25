"""Small export pair with cross-document and ambiguous gap attachments."""
import gzip
import json
from pathlib import Path

from reveal_backend.dismech_import import FILES, canonical, digest


def write_fixture(root):
    source, gaps = Path(root) / 'source', Path(root) / 'gaps'
    source.mkdir(); gaps.mkdir()
    a, b = 'dismech:disorders/A', 'dismech:modules/B'
    documents = [dict(id=key, name=name, kind=key.split(':')[1].split('/')[0], source_file=file)
                 for key, name, file in [(a, "Alpha's β question 🧬", 'kb/disorders/A.yaml'), (b, 'B', 'kb/modules/B.yaml')]]
    mechanism = dict(id=b + '#/pathophysiology/0', document_id=b, source_file=documents[1]['source_file'],
                     json_pointer='/pathophysiology/0', name='Nested source mechanism', description=None,
                     raw={'name': 'Nested source mechanism', 'evidence': [{'reference': 'PMID:1'}]})
    def discussion(local_id, kind, status):
        raw = dict(discussion_id=local_id, kind=kind, prompt='Does α affect β?', evidence=[{'reference': 'PMID:2'}])
        if status is not None:
            raw['status'] = status
        return dict(id=a + '#discussion:' + local_id, document_id=a, source_file=documents[0]['source_file'],
                    source_pointer='/discussions/' + str(len(local_id)), discussion_id=local_id,
                    kind=kind, status=status, is_gap=kind in ('KNOWLEDGE_GAP', 'HUMAN_MODEL_MISMATCH'),
                    has_stable_source_id=True, raw=raw)
    gap = discussion('gap', 'KNOWLEDGE_GAP', 'OPEN')
    gap['raw']['attaches_to'] = ['B.yaml::pathophysiology#Nested source mechanism', 'pathophysiology#ambiguous', 'treatments#']
    mismatch = discussion('mismatch', 'HUMAN_MODEL_MISMATCH', None)
    note = discussion('note', 'INTERPRETATION', 'RESOLVED')
    attachments = [
        dict(gap_id=gap['id'], attachment_index=0, source_reference=gap['raw']['attaches_to'][0], resolution='resolved',
             target_document_id=b, target_source_file=documents[1]['source_file'], target_id=mechanism['id'],
             target_pointer=mechanism['json_pointer'], target_kind='pathophysiology'),
        dict(gap_id=gap['id'], attachment_index=1, source_reference=gap['raw']['attaches_to'][1], resolution='ambiguous_target',
             target_document_id=a, target_source_file=documents[0]['source_file'], target_kind='pathophysiology', match_count=2),
        dict(gap_id=gap['id'], attachment_index=2, source_reference=gap['raw']['attaches_to'][2], resolution='whole_section',
             target_document_id=a, target_source_file=documents[0]['source_file'], target_id=a + '#/treatments',
             target_pointer='/treatments', target_kind='treatments', section_has_content=False),
    ]
    rows = dict(documents=documents, mechanisms=[mechanism], causal_edges=[dict(
        id=mechanism['id'] + '/downstream/0', source_id=mechanism['id'], document_id=b,
        source_file=mechanism['source_file'], json_pointer=mechanism['json_pointer'] + '/downstream/0',
        raw={'target': 'unresolved label'}, target_resolution='unresolved_source_reference')],
        hypotheses=[dict(document_id=a, source_file=documents[0]['source_file'], json_pointer='/mechanistic_hypotheses/0', raw={'status': 'CANDIDATE'})],
        ontology_terms=[dict(id='GO:1', labels=['test'], occurrences=[dict(source_file=mechanism['source_file'], document_id=b, mechanism_id=mechanism['id'])])],
        vocabulary=[dict(id='vocab:1', kind='mechanism', label=mechanism['name'], term_id=None, occurrences=[])],
        discussions=[gap, mismatch, note], knowledge_gaps=[gap, mismatch], gap_attachments=attachments)
    inventory = [dict(path=d['source_file'], sha256=digest(d['source_file'])) for d in documents]
    for root in (source, gaps):
        (root / 'source-files.json').write_text(canonical(inventory))
        (root / 'manifest.json').write_text(canonical(dict(source_commit='a' * 40, complete=True, errors=[], yaml_files=2, knowledge_gaps=2, files={})))
    (source / 'schema-vocabularies.json').write_text(canonical({'enums': []}))
    for stage, records in rows.items():
        root = gaps if stage in ('discussions', 'knowledge_gaps', 'gap_attachments') else source
        rewrite(root, FILES[stage], records)
    return source, gaps


def rewrite(root, filename, records):
    raw = gzip.compress(('\n'.join(canonical(r) for r in records) + '\n').encode(), mtime=0)
    (root / filename).write_bytes(raw)
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest['files'][filename] = dict(rows=len(records), sha256=digest(raw))
    (root / 'manifest.json').write_text(canonical(manifest))


def records(root, filename):
    return [json.loads(line) for line in gzip.decompress((root / filename).read_bytes()).splitlines()]
