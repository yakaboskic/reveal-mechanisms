#!/usr/bin/env python3
"""Build the REVEAL API handoff from pinned DAPPER schemas and verified fixtures.

Offline and deterministic. Does not create research records or run agents.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys
from urllib.parse import quote, urlencode

import yaml
from linkml.generators.jsonschemagen import JsonSchemaGenerator

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'api'
sys.path.insert(0, str(ROOT / 'services/backend/src'))
from reveal_backend.runtime_config import CURRENT_DAPPER_SNAPSHOT
PIN_DIR = CURRENT_DAPPER_SNAPSHOT
DAPPER = PIN_DIR / 'snapshot/schema'
sys.path[:0] = [str(DAPPER / 'identity'), str(DAPPER / 'lint'), str(DAPPER)]
from dapper_identity import DOC_GROUPS, assign_ids, compute_id, load_schema, verify
from lint_provenance import Vocabulary
from scientific_claims import assemble_cited_text
from import_cfde_genesets import metadata_record

PIN = json.loads((PIN_DIR / 'snapshot.json').read_text())
SV = load_schema(DAPPER / 'dapper.yaml')
SCHEMAS, PATHS, EXCHANGES = {}, {}, []
MODEL = 'cfde-inc-v2'
EMBEDDING = 'pritamdeka/BioBERT-mnli-snli-scinli-scitail-mednli-stsb'
BASE = 'https://api.reveal.example.org'
NOW = '2026-09-24T16:00:00Z'  # Explicitly fictional issuance/runtime timestamps.
LATER = '2026-09-24T16:01:00Z'
USER_ID = '11111111-1111-4111-8111-111111111111'
DRAFT_ID = '22222222-2222-4222-8222-222222222222'
REQUEST_ID = '33333333-3333-4333-8333-333333333333'
JOB_ID = '44444444-4444-4444-8444-444444444444'
PARAGRAPH_JOB_ID = '55555555-5555-4555-8555-555555555555'


def canonical(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha(x):
    return hashlib.sha256(x if isinstance(x, bytes) else canonical(x).encode()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def ref(name):
    return {'$ref': '#/components/schemas/' + name}


def string(description='', **kw):
    return {'type': 'string', **({'description': description} if description else {}), **kw}


def enum(*values, **kw):
    return {'type': 'string', 'enum': list(values), **kw}


def array(items, **kw):
    return {'type': 'array', 'items': items, **kw}


def obj(properties, required=None, **kw):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required,
            'additionalProperties': False, **kw}


def nullable(schema):
    return {'anyOf': [schema, {'type': 'null'}]}


def add(name, schema):
    assert name not in SCHEMAS, name
    SCHEMAS[name] = schema
    return ref(name)


def did(cls='[A-Za-z][A-Za-z0-9]*'):
    return string('Exact, case-sensitive, compact DAPPER-ID-1 identifier. URL-encode path values.',
                  pattern=r'^dapper:' + cls + r'\.[A-Za-z0-9_-]{32}$')


def remap_refs(value, mapping):
    if isinstance(value, list):
        return [remap_refs(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: mapping.get(v, v) if k == '$ref' else remap_refs(v, mapping) for k, v in value.items()}
    return value


def import_dapper():
    for name, digest in PIN['files'].items():
        assert hashlib.sha256((PIN_DIR / 'snapshot' / name).read_bytes()).hexdigest() == digest, name
    generated = json.loads(JsonSchemaGenerator(str(DAPPER / 'dapper.yaml'), not_closed=False).serialize())
    mapping = {'#/$defs/' + k: '#/components/schemas/Dapper' + k for k in generated['$defs']}
    for name, schema in generated['$defs'].items():
        schema = remap_refs(schema, mapping)
        schema['x-dapper-origin'] = {'class_or_enum': name, 'schema_sha256': PIN['root_schema_sha256']}
        if name in DOC_GROUPS.values():
            schema['properties']['id'] = did(name)
        add('Dapper' + name, schema)
    vocab = Vocabulary.build(SV, yaml.safe_load((DAPPER / 'lint/profiles.yaml').read_text()))
    groups = {**DOC_GROUPS, **vocab.edge_groups}
    add('DapperDocument', obj({group: array(ref('Dapper' + cls)) for group, cls in groups.items()}, [],
        minProperties=1, description='DAPPER grouped-node/edge document. Wire records use compact DAPPER IDs and absolute external entity URIs. References, digest validity, and closure are checked by the backend. Per-account validation requires exactly one account.'))
    citation = json.loads((DAPPER / 'citations/citation-record.schema.json').read_text())
    defs = citation.pop('$defs')
    citation.pop('$schema', None)
    names = {k: 'Citation' + ''.join(p.title() for p in k.split('_')) for k in defs}
    mapping = {'#/$defs/' + k: '#/components/schemas/' + v for k, v in names.items()}
    for k, schema in defs.items():
        add(names[k], remap_refs(schema, mapping))
    add('CitationMetadata', remap_refs(citation, mapping))
    for cls, fields in [('Question', ['text', 'scope', 'about_entities']),
                        ('KnowledgeGap', ['text', 'scope', 'about_entities', 'gap_description', 'gap_kind'])]:
        original = SCHEMAS['Dapper' + cls]
        add(cls + 'Input', obj({k: deepcopy(original['properties'][k]) for k in fields},
            [k for k in original['required'] if k in fields],
            description='Editable DAPPER fields before minting. IDs, provenance and attribution are assigned by the backend. This is an input projection, not a second scientific model.'))


def mint(node, cls):
    return {**node, 'id': compute_id(node, cls, SV)}


def citation(node, cls, origin='native', byline=None):
    source = {'source_ref': 'urn:example:reveal:contract-fixture'}
    return {'target_id': node['id'], 'target_class': cls,
        'dapper_schema_version': 'sha256:' + PIN['root_schema_sha256'], 'dapper_identity_profile': 'DAPPER-ID-1',
        'object_payload_ref': BASE + '/v1/objects/' + quote(node['id'], safe=''),
        'metadata_revision': 1, 'citation_profile_version': 'reveal-citation-v1',
        'title': node.get('text') or node.get('statement') or node['name'],
        'title_derivation': {'method': 'inquiry_text' if cls != 'Claim' else 'assessment_statement', 'version': '1', 'source': source},
        'language': 'en', 'byline': byline or [], 'origin': origin,
        'generated_at': None, 'first_minted_at': NOW, 'published_at': None,
        **({'original_issued_date': None, 'imported_at': NOW} if origin == 'imported' else {}),
        'issued_date': NOW[:10], 'issued_basis': 'first_minted_at',
        'date_provenance': {'first_minted_at': source, **({'imported_at': source} if origin == 'imported' else {})}, 'publisher': None,
        'repository': 'REVEAL contract fixtures — not published research',
        'canonical_url': 'https://reveal.example.org/id/' + quote(node['id'], safe=''),
        'access_level': 'private', 'publication_state': 'unpublished'}


def make_fixtures():
    capture = json.loads((ROOT / 'data/interactive/2026-09-24/cad-in-t2d-factor-gene_set.json').read_text())
    response = capture['response']
    raw = (canonical(response) + '\n').encode()
    (OUT / 'examples/cfde-response.json').write_bytes(raw)
    file = mint({'filename': 'cfde-response.json', 'sha256': sha(raw), 'size_in_bytes': len(raw), 'mime_type': 'application/json',
                 'description': 'Canonical JSON serialization of a captured CFDE response, not a new live request.',
                 'location': BASE + '/fixtures/cfde-response.json'}, 'File')
    candidate = response['candidates'][0]
    edge = candidate['edges'][0]
    encoding = json.loads((ROOT / 'data/cfde-genesets/2026-09-24/activity.json').read_text())
    geneset = mint(metadata_record(candidate['candidate']['node_key'], MODEL, encoding['id']), 'GeneSet')
    question = {'id': 'urn:example:question', 'text': 'Which gene sets connect to Factor1 for CADinT2D in the captured CFDE graph?',
                'scope': 'CFDE model cfde-inc-v2; the captured CADinT2D factor query. No biological causality is implied.'}
    person = {'id': 'urn:example:person', 'given_name': 'Example', 'family_name': 'Researcher'}
    inquiry_bytes = (canonical({k: question[k] for k in ['text', 'scope']}) + '\n').encode()
    (OUT / 'examples/question-input.json').write_bytes(inquiry_bytes)
    inquiry_file = mint({'filename': 'question-input.json', 'sha256': sha(inquiry_bytes), 'size_in_bytes': len(inquiry_bytes),
        'mime_type': 'application/json', 'description': 'Illustrative submitted inquiry projection.'}, 'File')
    inquiry_activity = {'id': 'urn:example:inquiry-activity', 'name': 'Illustrative inquiry recording',
        'description': 'Trusted-backend recording illustrated by the contract fixture; no user submission was executed.'}
    question.update(was_generated_by=inquiry_activity['id'], was_attributed_to=[person['id']], was_derived_from=[inquiry_file['id']])
    source_activity = {'id': 'urn:example:source-activity', 'name': 'Illustrative source-claim recording',
        'description': 'Contract fixture assembled from an existing CFDE capture; no agent was run.',
        'command': 'reveal-contract-fixture --capture-sha256 ' + file['sha256']}
    assembly = {'id': 'urn:example:assembly', 'name': 'Illustrative account assembly',
        'description': 'Contract fixture, not an executed Claude Code job.', 'software_name': 'REVEAL contract fixture generator',
        'software_version': '0.1.0', 'has_agentic_workspace': ['urn:example:workspace']}
    workspace = {'id': 'urn:example:workspace', 'regeneration_prompt': 'Describe the captured CFDE edge without inferring a causal mechanism. Input SHA-256: ' + file['sha256'],
        'agent_model': 'illustrative-model-not-executed', 'agent_role': 'urn:example:software:claude-code'}
    p1 = {'id': 'urn:example:p1', 'statement': 'The captured CFDE query reports a direct connection from CADinT2D Factor1 to the retained AMP_AD/GTEx gene set.',
          'proposition_kind': 'RESULT', 'scope': question['scope'], 'subject_entity': 'urn:cfde:factor:portal:CADinT2D:cfde-inc-v2:Factor1',
          'relation': 'urn:reveal:relation:factor-gene-set-direct', 'object_entity': geneset['id']}
    score = {'id': 'urn:example:score', 'score_kind': 'SCORE', 'value': edge['normalized_score'], 'metric': 'normalized_score',
        'interpretation': 'Method-specific score returned for factor_gene_set_direct. Not a calibrated probability; the upstream normalization formula is not established by this fixture.'}
    c1 = {'id': 'urn:example:source-claim', 'proposition': p1['id'], 'statement': p1['statement'],
          'was_generated_by': source_activity['id'], 'was_attributed_to': [person['id']],
          'was_derived_from': [file['id'], geneset['id']], 'source_locator': 'cfde-response.json#/candidates/0/edges/0', 'has_score': [score['id']]}
    p2 = {'id': 'urn:example:p2', 'statement': 'The retained AMP_AD/GTEx gene set is a directly connected candidate for inspecting the captured CADinT2D Factor1 result.',
          'proposition_kind': 'RESULT', 'scope': question['scope']}
    evidence = {'id': 'urn:example:evidence', 'source_claims': [c1['id']], 'target_proposition': p2['id'], 'direction': 'SUPPORTS',
        'explanation': 'The captured candidate contains a factor_gene_set_direct edge with the selected factor as its source.',
        'context': question['scope'], 'was_generated_by': assembly['id'], 'was_attributed_to': [person['id']]}
    c2 = {'id': 'urn:example:finding', 'proposition': p2['id'], 'statement': p2['statement'], 'direction': 'SUPPORTS',
        'was_generated_by': assembly['id'], 'was_attributed_to': [person['id']], 'has_evidence': [evidence['id']]}
    account = {'id': 'urn:example:account', 'question': question['id'], 'context': question['scope'], 'component_claims': [c2['id']],
        'closing_remarks': 'A stored graph connection alone does not establish a disease mechanism.',
        'was_generated_by': assembly['id'], 'was_attributed_to': [person['id']]}
    doc = {'files': [file, inquiry_file], 'gene_sets': [geneset], 'persons': [person], 'activities': [encoding, source_activity, assembly, inquiry_activity],
        'agentic_workspaces': [workspace], 'questions': [question], 'propositions': [p1, p2], 'claim_scores': [score],
        'claims': [c1, c2], 'evidence_items': [evidence], 'scientific_accounts': [account],
        'used_edges': [{'subject': a, 'predicate': 'prov:used', 'object': b} for a, b in
                       [(source_activity['id'], file['id']), (assembly['id'], c1['id']), (assembly['id'], question['id']),
                        (inquiry_activity['id'], inquiry_file['id'])]]}
    assign_ids(doc, SV)
    assert not verify(doc, SV)
    question, account, finding = doc['questions'][0], doc['scientific_accounts'][0], doc['claims'][1]
    byline = [{'agent_id': doc['persons'][0]['id'], 'kind': 'person', 'given_name': 'Example', 'family_name': 'Researcher',
               'roles': ['agent_operator'], 'source': {'source_ref': 'urn:example:reveal:contract-fixture'}}]
    citations = [citation(question, 'Question', byline=byline), *[citation(c, 'Claim', byline=byline) for c in doc['claims']]]
    inquiry_document = {'questions': [question], 'files': [doc['files'][1]], 'persons': doc['persons'],
        'activities': [doc['activities'][3]], 'used_edges': [doc['used_edges'][3]]}
    para_activity = mint({'name': 'Illustrative paragraph expression', 'description': 'Authored contract fixture; no paragraph agent ran.'}, 'Activity')
    paragraph = mint({'scientific_account': account['id'], 'language': 'en', 'was_generated_by': para_activity['id'],
        'was_attributed_to': [doc['persons'][0]['id']], **assemble_cited_text([
        {'text': question['text'], 'citations': [{'target_id': question['id'], 'citation_metadata_revision': 1}]},
        {'text': finding['statement'], 'citations': [{'target_id': finding['id'], 'citation_metadata_revision': 1}]},
        {'text': account['closing_remarks']}])}, 'Paragraph')
    paragraph_doc = deepcopy(doc)
    paragraph_doc['paragraphs'] = [paragraph]
    paragraph_doc['activities'].append(para_activity)
    paragraph_doc['used_edges'].append({'subject': para_activity['id'], 'predicate': 'prov:used', 'object': account['id']})
    assert not verify(paragraph_doc, SV)
    aip = json.loads((ROOT / 'data/dismech-gaps/2026-09-24/aip-gap-example.json').read_text())
    source = aip['gap']; gap_raw = source['raw']
    gap = mint({'text': gap_raw['prompt'], 'gap_description': gap_raw['rationale'], 'gap_kind': gap_raw['kind'],
                'scope': source['document_name'], 'about_entities': ['http://purl.obolibrary.org/obo/MONDO_1060231']}, 'KnowledgeGap')
    mechanisms = [mint({'name': a['target_label'], 'dismech_entry_key': source['document_id'],
                        'dismech_class': 'Pathophysiology'}, 'Mechanism') for a in aip['attachments'][:3]]
    factors = json.loads((ROOT / 'data/interactive/2026-09-24/catalog-factor-t2d.json').read_text())['response']['items'][:5]
    factor_records = []
    for n in factors:
        encoded = (canonical(n) + '\n').encode()
        factor_path = OUT / 'examples/factors' / (sha(encoded) + '.json')
        factor_path.parent.mkdir(exist_ok=True)
        factor_path.write_bytes(encoded)
        f = mint({'filename': n['node_key'] + '.catalog-record.json', 'sha256': sha(encoded), 'size_in_bytes': len(encoded),
                  'mime_type': 'application/json', 'description': 'Canonical CFDE catalog record; a factor source record, not a causal mechanism.'}, 'File')
        factor_records.append({'source': 'eaggl', 'source_id': n['node_id'], 'source_revision': f['sha256'],
            'object_class': 'File', 'object': f, 'cfde_anchor': {k: n[k] for k in ['node_id', 'node_type', 'label', 'subtitle']}, 'model': MODEL})
    dismech_records = [{'source': 'dismech', 'source_id': a['target_id'], 'source_revision': sha(a),
                       'object_class': 'Mechanism', 'object': m, 'disease_label': source['document_name']}
                      for a, m in zip(aip['attachments'], mechanisms)]
    fixture = {'account_document': doc, 'paragraph_document': paragraph_doc, 'inquiry_document': inquiry_document, 'question': question, 'account': account,
        'claim': finding, 'paragraph': paragraph, 'gene_set': geneset, 'citations': citations, 'gap': gap,
        'gap_citation': citation(gap, 'KnowledgeGap', origin='imported'), 'gap_source': source,
        'attachments': aip['attachments'], 'dismech_records': dismech_records, 'factor_records': factor_records,
        'capture': {'url': capture['url'], 'retrieved_at': capture['retrieved_at'], 'file': file}}
    write_json(OUT / 'examples/scientific-account.dapper.json', doc)
    write_json(OUT / 'examples/paragraph.dapper.json', paragraph_doc)
    write_json(OUT / 'examples/citation-registry.json', citations + [fixture['gap_citation']])
    write_json(OUT / 'examples/knowledge-gap.dapper.json', {'knowledge_gaps': [gap]})
    return fixture


def application_schemas():
    uuid = string(format='uuid')
    timestamp = string(format='date-time')
    digest = string(pattern='^[a-f0-9]{64}$')
    add('SourceRef', obj({'source': enum('dismech', 'eaggl'), 'source_id': string(minLength=1), 'source_revision': digest,
                         'dapper_id': did()}, description='Exact source identity/revision plus its DAPPER scientific record or source File. A source alias is not a minted DAPPER class.'))
    add('Selection', obj({'reference': ref('SourceRef'), 'origin': enum('manual', 'automatic', 'gap_attachment'),
                         'suggestion_id': nullable(uuid)}))
    add('CfdeAnchor', obj({'node_id': string(pattern='^factor:'), 'node_type': {'const': 'factor', 'type': 'string'},
                          'label': string(), 'subtitle': string()}))
    add('DismechMechanism', obj({'source': {'const': 'dismech', 'type': 'string'}, 'source_id': string(), 'source_revision': digest,
        'object_class': {'const': 'Mechanism', 'type': 'string'}, 'object': ref('DapperMechanism'), 'disease_label': string()}))
    add('EagglFactor', obj({'source': {'const': 'eaggl', 'type': 'string'}, 'source_id': string(), 'source_revision': digest,
        'object_class': {'const': 'File', 'type': 'string'}, 'object': ref('DapperFile'), 'cfde_anchor': ref('CfdeAnchor'), 'model': {'const': MODEL, 'type': 'string'}},
        description='DAPPER File identifies the exact factor catalog JSON bytes. EAGGL factors remain native source anchors; no unsupported Factor class or causal Mechanism is invented. No gene-expression GeneProgram equivalence is asserted.'))
    add('MechanismRecord', {'oneOf': [ref('DismechMechanism'), ref('EagglFactor')],
        'discriminator': {'propertyName': 'source', 'mapping': {s: '#/components/schemas/' + n for s, n in [('dismech', 'DismechMechanism'), ('eaggl', 'EagglFactor')]}}})
    add('InquiryInput', {'oneOf': [obj({'object_class': {'const': c, 'type': 'string'}, 'object': ref(c + 'Input')}) for c in ['Question', 'KnowledgeGap']],
        'discriminator': {'propertyName': 'object_class'}})
    add('Composer', obj({'inquiry': nullable(ref('InquiryInput')), 'source_gap': nullable(obj({'id': did('KnowledgeGap'), 'source_revision': digest})),
        'dismech_context': array(ref('Selection'), maxItems=10), 'eaggl_anchors': array(ref('Selection'), maxItems=10),
        'dismissed_source_ids': array(string(), uniqueItems=True, maxItems=1000), 'mechanism_subquery': string(maxLength=2000),
        'model': {'const': MODEL, 'type': 'string'}, 'selected_kgs': array(enum('biomarkerkg', 'prokn'), uniqueItems=True, maxItems=2)},
        description='Editable state. Null inquiry represents an empty editor; drafts may have zero anchors. Submitting an analysis job requires a valid inquiry and at least one resolvable EAGGL anchor. Server validates source types and suggestion provenance.'))
    draft_name = string(minLength=1, maxLength=120, pattern=r'\S')
    add('DraftCreate', obj({'composer': ref('Composer'), 'name': draft_name}, ['composer']))
    add('DraftPatch', obj({'expected_version': {'type': 'integer', 'minimum': 1}, 'composer': ref('Composer'), 'name': draft_name}, ['expected_version'],
        minProperties=2, description='Rename or replace the complete composer atomically using compare-and-swap. Omitted fields are preserved. Retry a lost acknowledgment with the same Idempotency-Key.'))
    add('Draft', obj({'id': uuid, 'owner_user_id': uuid, 'version': {'type': 'integer', 'minimum': 1}, 'composer': ref('Composer'),
                      'created_at': timestamp, 'updated_at': timestamp}))
    SCHEMAS['Draft']['properties']['name'] = draft_name
    add('DraftDelete', obj({'expected_version': {'type': 'integer', 'minimum': 1}}))
    add('DraftDeletion', obj({'id': uuid, 'deleted': {'type': 'boolean', 'const': True}}))
    add('Page', obj({'next_cursor': nullable(string()), 'has_more': {'type': 'boolean'}, 'snapshot_id': string()},
        description='Opaque cursor pins sorting, filters and an authorized collection snapshot. A null cursor means no further page. Cursors cannot be reused with different filters or callers.'))
    add('Me', obj({'user_id': uuid, 'display_name': nullable(string()), 'email': nullable(string(format='email')), 'email_verified': nullable({'type': 'boolean'}),
        'orcid': nullable(string(format='uri')), 'orcid_authenticated': {'type': 'boolean'}, 'person': nullable(ref('DapperPerson'))},
        description='Private application identity plus optional DAPPER Person snapshot. No provider access tokens, passwords or sessions.'))
    add('AttributionSnapshot', obj({'user_id': uuid, 'person_id': nullable(did('Person')), 'display_name': nullable(string()),
        'orcid': nullable(string(format='uri')), 'orcid_authenticated': {'type': 'boolean'}, 'observed_at': timestamp}))
    add('ResearchRequest', obj({'id': uuid, 'owner_user_id': uuid, 'source_draft_id': uuid, 'source_draft_version': {'type': 'integer', 'minimum': 1},
        'composer': ref('Composer'), 'question_id': did('(Question|KnowledgeGap)'), 'document': ref('DapperDocument'),
        'attribution': ref('AttributionSnapshot'), 'submitted_at': timestamp},
        description='Immutable submitted composer plus the minted DAPPER inquiry. Later draft edits do not change it.'))
    add('GapSource', obj({'source': enum('dismech', 'user'), 'source_id': string(), 'source_revision': digest,
        'status': nullable(enum('OPEN', 'RESOLVED')), 'disease_label': nullable(string()), 'description_derivation': enum('source_rationale', 'prompt_fallback', 'user_authored')}))
    add('Attachment', obj({'source_reference': string(), 'target_kind': string(),
        'resolution': enum('resolved', 'whole_section', 'whole_document', 'ambiguous_target', 'ambiguous_file',
                           'missing_target', 'missing_file', 'unsupported_kind', 'invalid_syntax'),
        'target': nullable(ref('SourceRef')), 'label': nullable(string())},
        description='Only uniquely resolved mechanism occurrences can become selected context. Phenotypes and whole sections retain their actual kinds.'))
    add('GapRecord', obj({'object': ref('DapperKnowledgeGap'), 'source': ref('GapSource'), 'attachments': array(ref('Attachment'))}))
    add('Rank', obj({'value': {'type': 'number'}, 'metric': enum('cosine_similarity', 'lexical_rank', 'fuzzy_similarity', 'reciprocal_rank_fusion', 'eligible_disease_identity'),
        'rank': {'type': 'integer', 'minimum': 1}}, description='Retrieval relevance, not biological support or probability. Compare values only within the same metric/configuration.'))
    add('SearchProvenance', obj({'query': string(), 'mode': enum('lexical', 'fuzzy', 'semantic', 'hybrid'), 'corpus_snapshot': string(),
        'embedding_model': nullable(string()), 'embedding_revision': nullable(string()), 'template_version': string(),
        'score_aggregation': nullable(string())}))
    add('GapHit', obj({'gap': ref('GapRecord'), 'ranking': ref('Rank')}))
    add('MechanismHit', obj({'record': ref('MechanismRecord'), 'ranking': ref('Rank')}))
    add('GapSearchResults', obj({'items': array(ref('GapHit')), 'page': ref('Page'), 'search': ref('SearchProvenance')}))
    add('MechanismSearchResults', obj({'items': array(ref('MechanismHit')), 'page': ref('Page'), 'search': ref('SearchProvenance')}))
    add('SuggestInput', obj({'inquiry': ref('InquiryInput'), 'dismech_context': array(ref('SourceRef'), maxItems=10),
        'manual_eaggl_anchors': array(ref('SourceRef'), maxItems=10), 'dismissed_source_ids': array(string(), uniqueItems=True, maxItems=1000),
        'subquery': string(maxLength=2000), 'mode': enum('semantic', 'hybrid'), 'model': {'const': MODEL, 'type': 'string'}}))
    add('Suggestion', obj({'factor': ref('EagglFactor'), 'ranking': ref('Rank'), 'matched_context_ids': array(string(), uniqueItems=True)}))
    SCHEMAS['Suggestion']['properties']['reason'] = string()
    add('Suggestions', obj({'suggestion_id': uuid, 'automatic_anchors': array(ref('Suggestion'), maxItems=5),
        'automatic_target_count': {'const': 5, 'type': 'integer'}, 'search': ref('SearchProvenance'), 'limitations': array(string())},
        description='At most five unique automatic EAGGL factors TOTAL across context. Eligible versioned exact disease mappings are proposed first, followed by semantic/hybrid context retrieval. Reasons establish relevance to inspect, not biological support. Excludes manual selections and dismissals. Removal does not trigger silent refill; reset suggestions is explicit.'))
    add('JobBudgets', obj({'max_accounts': {'type': 'integer', 'minimum': 1, 'maximum': 3, 'default': 3},
        'candidates_per_type': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 100},
        'max_nodes': {'type': 'integer', 'minimum': 10, 'maximum': 250, 'default': 250},
        'max_edges': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 1000},
        'evidence_tokens': {'type': 'integer', 'minimum': 1000, 'maximum': 24000, 'default': 24000},
        'mcp_calls': {'type': 'integer', 'minimum': 0, 'maximum': 20, 'default': 20}}, []))
    add('AnalysisJobInput', obj({'kind': {'const': 'analysis', 'type': 'string'}, 'draft_id': uuid,
        'draft_version': {'type': 'integer', 'minimum': 1}, 'budgets': ref('JobBudgets')}, ['kind', 'draft_id', 'draft_version'],
        description='Freezes an owned, saved draft and mints its inquiry. Requires one or more resolvable EAGGL anchors. Expansion is four target queries against the same frozen anchors, then one contextual-edge call.'))
    add('ParagraphJobInput', obj({'kind': {'const': 'paragraph', 'type': 'string'}, 'account_id': did('ScientificAccount'),
        'focus_claim_id': nullable(did('Claim')), 'audience': enum('researcher', 'general'), 'language': string(default='en'),
        'target_words': {'type': 'integer', 'minimum': 60, 'maximum': 500, 'default': 180},
        'additional_citations': array(ref('CitationTarget'), maxItems=20)}, ['kind', 'account_id'],
        description='Uses the exact saved account and authorized citation context. No fresh KG research. Focus claim must belong to the account; additional citations must already belong to its saved evidence context.'))
    add('JobCreate', {'oneOf': [ref('AnalysisJobInput'), ref('ParagraphJobInput')],
        'discriminator': {'propertyName': 'kind', 'mapping': {k: '#/components/schemas/' + n for k, n in [('analysis', 'AnalysisJobInput'), ('paragraph', 'ParagraphJobInput')]}}})
    add('CitationTarget', obj({'target_id': did('(Claim|Question|KnowledgeGap)'), 'citation_metadata_revision': {'type': 'integer', 'minimum': 1}}))
    add('Enrichment', obj({'graph': enum('biomarkerkg', 'prokn'), 'status': enum('matched', 'no_match', 'unavailable', 'skipped'),
        'source_claim_ids': array(did('Claim')), 'detail': nullable(string())}))
    add('AnalysisResult', obj({'kind': {'const': 'analysis', 'type': 'string'}, 'request_id': uuid,
        'account_ids': array(did('ScientificAccount'), minItems=1, maxItems=3), 'enrichment': array(ref('Enrichment'))}))
    add('ParagraphResult', obj({'kind': {'const': 'paragraph', 'type': 'string'}, 'account_id': did('ScientificAccount'), 'paragraph_id': did('Paragraph')}))
    add('JobFailure', obj({'code': string(), 'message': string(), 'retryable': {'type': 'boolean'}}))
    add('Job', obj({'id': uuid, 'kind': enum('analysis', 'paragraph'), 'owner_user_id': uuid,
        'status': enum('queued', 'running', 'cancel_requested', 'cancelled', 'succeeded', 'insufficient_evidence', 'failed'),
        'stage': enum('queued', 'freezing_inputs', 'retrieving_cfde', 'authoring_account', 'enriching_okn', 'authoring_paragraph', 'validating', 'persisting', 'complete'),
        'research_request_id': nullable(uuid), 'input_account_id': nullable(did('ScientificAccount')),
        'created_at': timestamp, 'updated_at': timestamp, 'completed_at': nullable(timestamp),
        'result': nullable({'oneOf': [ref('AnalysisResult'), ref('ParagraphResult')]}),
        'failure': nullable(ref('JobFailure')), 'warnings': array(string()), 'last_event_id': string(pattern='^[0-9]+$'),
        'links': obj({'self': string(format='uri-reference'), 'events': string(format='uri-reference'), 'cancel': string(format='uri-reference')})},
        description='Operational record, not a DAPPER scientific object. The result IDs point to minted DAPPER objects. Terminal-state guards prevent stale attempts from overwriting accepted results.'))
    SCHEMAS['Job']['allOf'] = [
        {'if': {'properties': {'kind': {'const': kind}}}, 'then': {'properties': {
            'research_request_id': uuid if kind == 'analysis' else {'type': 'null'},
            'input_account_id': {'type': 'null'} if kind == 'analysis' else did('ScientificAccount'),
            'result': nullable(ref(result))}}}
        for kind, result in [('analysis', 'AnalysisResult'), ('paragraph', 'ParagraphResult')]
    ] + [
        {'if': {'properties': {'status': {'const': 'succeeded'}}},
         'then': {'properties': {'result': {'type': 'object'}, 'failure': {'type': 'null'}, 'stage': {'const': 'complete'}}},
         'else': {'properties': {'result': {'type': 'null'}}}},
        {'if': {'properties': {'status': {'const': 'failed'}}},
         'then': {'properties': {'failure': ref('JobFailure')}},
         'else': {'properties': {'failure': {'type': 'null'}}}},
        {'if': {'properties': {'status': {'enum': ['succeeded', 'insufficient_evidence', 'failed', 'cancelled']}}},
         'then': {'properties': {'completed_at': timestamp}},
         'else': {'properties': {'completed_at': {'type': 'null'}}}},
        {'if': {'properties': {'status': {'const': 'insufficient_evidence'}}},
         'then': {'properties': {'kind': {'const': 'analysis'}}}}
    ]
    add('JobEvent', obj({'id': string(pattern='^[0-9]+$'), 'job_id': uuid, 'occurred_at': timestamp,
        'event_type': enum('status', 'progress', 'warning', 'result', 'failure'), 'status': SCHEMAS['Job']['properties']['status'],
        'stage': SCHEMAS['Job']['properties']['stage'], 'message': string(), 'result': nullable({'oneOf': [ref('AnalysisResult'), ref('ParagraphResult')]})}))
    add('JobEvents', obj({'items': array(ref('JobEvent')), 'next_after': string(pattern='^[0-9]+$'), 'terminal': {'type': 'boolean'}}))
    add('SchemaPin', obj({'schema_sha256': digest, 'dependency_snapshot_sha256': digest,
        'identity_profile': {'const': 'DAPPER-ID-1', 'type': 'string'}}))
    add('PayloadSnapshot', obj({'object_id': did(), 'payload_sha256': digest}))
    add('ArtifactAccess', obj({'file': ref('DapperFile'), 'download_url': nullable(string(format='uri')), 'expires_at': nullable(timestamp)},
        description='Authorized source-byte access when available; URLs can expire. SHA-256 on the File identifies the bytes. Null means no downloadable representation is currently exposed.'))
    common = {'root_id': did(), 'schema': ref('SchemaPin'), 'document': ref('DapperDocument'), 'payloads': array(ref('PayloadSnapshot')),
              'citation_metadata': array(ref('CitationMetadata')), 'artifacts': array(ref('ArtifactAccess'))}
    add('ObjectResult', obj(common, description='Full DAPPER records and their exact payload observations. The root ID selects one record in the document; reference/identity checks are mandatory server-side. DAPPER does not hash unhashable fields, so payload checksums pin the exact returned observation.'))
    for name, cls, group in [('AccountResult', 'ScientificAccount', 'scientific_accounts'), ('ClaimResult', 'Claim', 'claims'),
                             ('GeneSetResult', 'GeneSet', 'gene_sets'), ('ParagraphObjectResult', 'Paragraph', 'paragraphs')]:
        props = deepcopy(common);props['root_id'] = did(cls)
        props['document'] = {'allOf': [ref('DapperDocument'), {'required': [group], 'properties': {group: {'minItems': 1}}}]}
        if cls in ['ScientificAccount', 'Paragraph']:
            props['document']['allOf'][1]['required'].append('scientific_accounts') if group != 'scientific_accounts' else None
            props['document']['allOf'][1]['properties']['scientific_accounts'] = {'minItems': 1, 'maxItems': 1}
        add(name, obj(props))
    SCHEMAS['GeneSetResult']['properties']['aliases'] = array(obj({'model': enum(MODEL), 'node_id': string(pattern='^gene_set:'), 'import_id': digest}))
    SCHEMAS['GeneSetResult']['required'].append('aliases')
    SCHEMAS['GeneSetResult']['properties']['coverage'] = obj({'membership': enum('unknown', 'partial', 'complete'), 'construction_provenance': enum('unknown', 'partial', 'complete')})
    SCHEMAS['GeneSetResult']['required'].append('coverage')
    add('Problem', obj({'type': string(format='uri'), 'title': string(), 'status': {'type': 'integer', 'minimum': 400, 'maximum': 599},
        'code': string(), 'detail': string(), 'request_id': uuid, 'retryable': {'type': 'boolean'},
        'current_version': {'type': 'integer', 'minimum': 1}, 'field_errors': array(obj({'pointer': string(), 'message': string()}))},
        ['type', 'title', 'status', 'code', 'detail', 'request_id', 'retryable'], description='RFC 9457-style problem details; details never contain secrets or another user\'s private data.'))
    add('CitationRenderInput', obj({'paragraph_id': did('Paragraph'), 'style': enum('apa', 'mla'), 'locale': string(default='en-US')}, ['paragraph_id', 'style'],
        description='Render the saved paragraph\'s entire pinned citation set together. No mutable text or latest-revision substitution is accepted. Does not launch an agent or change Paragraph identity.'))
    add('CitationRendering', obj({'paragraph_id': did('Paragraph'), 'style': enum('apa', 'mla'), 'locale': string(),
        'in_text': array(obj({'occurrence_index': {'type': 'integer', 'minimum': 0}, 'label': string()})),
        'bibliography': array(obj({'target_id': did('(Claim|Question|KnowledgeGap)'), 'citation_metadata_revision': {'type': 'integer', 'minimum': 1}, 'text': string()})),
        'rendering_manifest': obj({'processor': string(), 'processor_version': string(), 'style_sha256': digest,
            'locale_sha256': digest, 'citation_profile': {'const': 'reveal-citation-v1', 'type': 'string'}, 'metadata_checksums': array(digest)})}))
    add('CslItem', obj({'id': did('(Claim|Question|KnowledgeGap)'), 'type': {'const': 'webpage', 'type': 'string'}, 'title': string(),
        'author': array({'oneOf': [obj({'family': string(), 'given': string()}, ['family']), obj({'literal': string()})]}),
        'issued': obj({'date-parts': array(array({'type': 'integer'}), minItems=1, maxItems=1)}),
        'URL': string(format='uri'), 'container-title': string(), 'publisher': string(), 'genre': string()}, ['id', 'type', 'title', 'URL'],
        description='CSL-JSON application export projection. Native CitationMetadata remains authoritative for roles, unknown dates and ORCID provenance.'))
    for name, item in [('DraftList', 'Draft'), ('ResearchRequestList', 'ResearchRequest'), ('GapList', 'GapRecord'), ('JobList', 'Job')]:
        add(name, obj({'items': array(ref(item)), 'page': ref('Page')}))


def parameter(name, location, schema, example, required=False, description=''):
    return {'name': name, 'in': location, 'required': required or location == 'path',
            'schema': schema, 'example': example, 'description': description or schema.get('description', '')}


def page_parameters():
    return [parameter('limit', 'query', {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 20}, 20),
            parameter('cursor', 'query', string(minLength=1), 'opaque-next-page', description='Omit for the first page. A returned next_cursor is opaque; the example is illustrative and cannot be used against a live service.')]


def problem(status, code, detail, **extra):
    return {'type': 'urn:reveal:problem:' + code.lower().replace('_', '-'), 'title': code.replace('_', ' ').title(),
        'status': status, 'code': code, 'detail': detail, 'request_id': 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        'retryable': status in [429, 503], **extra}


ERRORS = {
    '400': problem(400, 'INVALID_REQUEST', 'A parameter or cursor does not match the requested operation.'),
    '401': problem(401, 'AUTHENTICATION_REQUIRED', 'A valid trusted-gateway API assertion is required.'),
    '403': problem(403, 'SIGN_IN_REQUIRED', 'Sign in to publish a scientific account or exploration.'),
    '404': problem(404, 'NOT_FOUND', 'No accessible record exists for the supplied identifier or exact revision.'),
    '409': problem(409, 'VERSION_CONFLICT', 'The saved draft has changed. Reload and reconcile before retrying.', current_version=3),
    '422': problem(422, 'INVALID_INPUT', 'The request does not satisfy the operation contract.', field_errors=[{'pointer': '/inquiry', 'message': 'A valid DAPPER inquiry is required.'}]),
    '429': problem(429, 'RATE_LIMITED', 'Retry after the interval in Retry-After.'),
    '503': problem(503, 'DEPENDENCY_UNAVAILABLE', 'A required dependency is temporarily unavailable; no replacement ranking or scientific result was fabricated.')}


def content(schema, examples, media='application/json'):
    return {media: {'schema': ref(schema) if isinstance(schema, str) else schema,
                    'examples': {k: {'summary': k.replace('_', ' ').capitalize(), 'value': v} for k, v in examples.items()}}}


def operation(path, method, name, tag, summary, description, response_schema, response_examples,
              parameters=None, request_schema=None, request_examples=None, status=200, public=False,
              errors=('400', '401', '404', '429'), extra_media=None, idempotent=False):
    parameters = deepcopy(parameters or [])
    if idempotent:
        parameters.append(parameter('Idempotency-Key', 'header', string(minLength=8, maxLength=128),
            '66666666-6666-4666-8666-666666666666', True,
            'Unique per caller and operation for at least 7 days. Same key and same canonical body replay the original accepted response; changed body returns 409 IDEMPOTENCY_CONFLICT. Compare idempotency before draft-version checks on retries.'))
    response = {'description': 'Accepted job; poll Location or consume job events.' if status == 202 else 'Successful response.',
                'content': content(response_schema, response_examples)}
    if extra_media:
        response['content'].update(extra_media)
    response['headers'] = {'X-Request-ID': {'description': 'Correlation ID for this HTTP request.', 'schema': string(format='uuid')}}
    if status in [201, 202]:
        response['headers']['Location'] = {'description': 'Relative URL of the created resource.', 'schema': string(format='uri-reference'),
            'example': '/v1/jobs/' + JOB_ID if status == 202 else '/v1/drafts/' + DRAFT_ID}
    if status == 202:
        response['headers']['Retry-After'] = {'description': 'Suggested polling delay in seconds.', 'schema': {'type': 'integer', 'minimum': 1}, 'example': 2}
    responses = {str(status): response}
    for code in errors:
        responses[code] = {'description': ERRORS[code]['title'], 'content': content('Problem', {ERRORS[code]['code'].lower(): ERRORS[code]}, 'application/problem+json')}
        if code == '429':
            responses[code]['headers'] = {'Retry-After': {'schema': {'type': 'integer', 'minimum': 1}, 'example': 30}}
    op = {'operationId': name, 'tags': [tag], 'summary': summary, 'description': description,
          'parameters': parameters, 'responses': responses,
          'security': [{}, {'ApplicationBearer': []}] if public else [{'ApplicationBearer': []}]}
    if request_schema:
        op['requestBody'] = {'required': True, 'content': content(request_schema, request_examples)}
    cases = request_examples or {'request': None}
    for key, body in cases.items():
        query = {p['name']: p['example'] for p in parameters if p['in'] == 'query' and p['name'] != 'cursor'}
        path_values = {p['name']: p['example'] for p in parameters if p['in'] == 'path'}
        headers = {p['name']: p['example'] for p in parameters if p['in'] == 'header'}
        if name == 'createJob' and key == 'paragraph':
            headers['Idempotency-Key'] = '88888888-8888-4888-8888-888888888888'
        if not public:
            headers['Authorization'] = 'Bearer <api-key-or-gateway-assertion>'
        if body is not None:
            headers['Content-Type'] = 'application/json'
        headers['Accept'] = 'application/json'
        resolved_path = path
        for k, v in path_values.items():
            resolved_path = resolved_path.replace('{' + k + '}', quote(str(v), safe=''))
        url = BASE + resolved_path + ('?' + urlencode(query) if query else '')
        pieces = ['curl', '-X', method.upper(), shlex.quote(url)]
        for k, v in headers.items():
            pieces += ['-H', shlex.quote(k + ': ' + v)]
        if body is not None:
            pieces += ['--data-raw', shlex.quote(canonical(body))]
        curl = ' '.join(pieces)
        op.setdefault('x-codeSamples', []).append({'lang': 'Shell', 'label': key, 'source': curl})
        exchange = {'operation_id': name, 'case': key, 'method': method.upper(), 'path_template': path,
                    'request': {'url': url, 'path': path_values, 'query': query, 'headers': headers, 'body': body},
                    'responses': {str(status): {'content_type': 'application/json', 'examples':
                        {k: v for k, v in response_examples.items() if k.startswith(key + '_')} or response_examples}}, 'curl': curl}
        EXCHANGES.append(exchange)
    PATHS.setdefault(path, {})[method] = op
    return op


def sample_responses(f):
    def source_ref(record):
        return {k: record[k] for k in ['source', 'source_id', 'source_revision']} | {'dapper_id': record['object']['id']}
    anchor = {'reference': source_ref(f['factor_records'][0]), 'origin': 'manual', 'suggestion_id': None}
    inquiry = {'object_class': 'Question', 'object': {k: f['question'][k] for k in ['text', 'scope']}}
    composer = {'inquiry': inquiry, 'source_gap': None, 'dismech_context': [], 'eaggl_anchors': [anchor],
                'dismissed_source_ids': [], 'mechanism_subquery': 'CADinT2D', 'model': MODEL, 'selected_kgs': ['biomarkerkg', 'prokn']}
    draft = {'id': DRAFT_ID, 'owner_user_id': USER_ID, 'version': 1, 'composer': composer, 'created_at': NOW, 'updated_at': NOW}
    saved = {**deepcopy(draft), 'version': 2, 'updated_at': LATER}
    page = {'next_cursor': None, 'has_more': False, 'snapshot_id': 'illustrative-contract-snapshot'}
    attribution = {'user_id': USER_ID, 'person_id': f['account_document']['persons'][0]['id'], 'display_name': 'Example Researcher',
                   'orcid': None, 'orcid_authenticated': False, 'observed_at': NOW}
    request = {'id': REQUEST_ID, 'owner_user_id': USER_ID, 'source_draft_id': DRAFT_ID, 'source_draft_version': 2,
        'composer': composer, 'question_id': f['question']['id'], 'document': f['inquiry_document'], 'attribution': attribution, 'submitted_at': LATER}
    def job(kind, status='queued'):
        jid = JOB_ID if kind == 'analysis' else PARAGRAPH_JOB_ID
        return {'id': jid, 'kind': kind, 'owner_user_id': USER_ID, 'status': status,
            'stage': 'complete' if status == 'succeeded' else 'queued',
            'research_request_id': REQUEST_ID if kind == 'analysis' else None,
            'input_account_id': f['account']['id'] if kind == 'paragraph' else None,
            'created_at': LATER, 'updated_at': LATER, 'completed_at': LATER if status == 'succeeded' else None,
            'result': None, 'failure': None, 'warnings': [], 'last_event_id': '2' if status == 'succeeded' else '1',
            'links': {'self': '/v1/jobs/' + jid, 'events': '/v1/jobs/' + jid + '/events', 'cancel': '/v1/jobs/' + jid + '/cancel'}}
    complete = job('analysis', 'succeeded')
    complete['result'] = {'kind': 'analysis', 'request_id': REQUEST_ID, 'account_ids': [f['account']['id']],
        'enrichment': [{'graph': graph, 'status': 'skipped', 'source_claim_ids': [], 'detail': 'Contract fixture; no MCP agent run was performed.'} for graph in ['biomarkerkg', 'prokn']]}
    paragraph_complete = job('paragraph', 'succeeded')
    paragraph_complete['result'] = {'kind': 'paragraph', 'account_id': f['account']['id'], 'paragraph_id': f['paragraph']['id']}
    cancelled = job('analysis');cancelled.update(status='cancel_requested', stage='retrieving_cfde', last_event_id='2')
    queued_event = {'id': '1', 'job_id': JOB_ID, 'occurred_at': LATER, 'event_type': 'status', 'status': 'queued',
        'stage': 'queued', 'message': 'Illustrative analysis job is queued.', 'result': None}
    event = {'id': '2', 'job_id': JOB_ID, 'occurred_at': LATER, 'event_type': 'result', 'status': 'succeeded',
             'stage': 'complete', 'message': 'Illustrative saved account is available.', 'result': complete['result']}
    gap_source = {'source': 'dismech', 'source_id': f['gap_source']['id'], 'source_revision': sha(f['gap_source']['raw']),
        'status': f['gap_source']['status'], 'disease_label': f['gap_source']['document_name'], 'description_derivation': 'source_rationale'}
    attachments = [{'source_reference': a['source_reference'], 'target_kind': a['target_kind'], 'resolution': a['resolution'],
        'target': source_ref(f['dismech_records'][i]) if i < len(f['dismech_records']) else None, 'label': a['target_label']} for i, a in enumerate(f['attachments'])]
    gap_record = {'object': f['gap'], 'source': gap_source, 'attachments': attachments}
    search = {'query': 'AIP AHR tumor growth', 'mode': 'semantic', 'corpus_snapshot': 'illustrative-contract-snapshot',
        'embedding_model': EMBEDDING, 'embedding_revision': None, 'template_version': 'illustrative-template-v1', 'score_aggregation': 'maximum cosine over selected context'}
    rank = {'value': 0.83, 'metric': 'cosine_similarity', 'rank': 1}
    suggestions = {'suggestion_id': '77777777-7777-4777-8777-777777777777',
        'automatic_anchors': [{'factor': r, 'ranking': {**rank, 'value': round(0.83-i*.03, 2), 'rank': i+1},
            'matched_context_ids': [f['dismech_records'][0]['source_id']]} for i, r in enumerate(f['factor_records'])],
        'automatic_target_count': 5, 'search': {**search, 'query': 'AIP AHR signaling'},
        'limitations': ['Illustrative ranking only: no embeddings were requested and these factors are not established AIP mechanism matches.']}
    schema_pin = {'schema_sha256': PIN['root_schema_sha256'], 'dependency_snapshot_sha256': PIN['snapshot_sha256'], 'identity_profile': 'DAPPER-ID-1'}
    def object_result(root_id, doc, citations):
        nodes = [n for group in DOC_GROUPS for n in doc.get(group, [])]
        return {'root_id': root_id, 'schema': schema_pin, 'document': doc, 'payloads': [{'object_id': n['id'], 'payload_sha256': sha(n)} for n in nodes],
                'citation_metadata': citations, 'artifacts': [{'file': n, 'download_url': None, 'expires_at': None} for n in doc.get('files', [])]}
    gene_result = object_result(f['gene_set']['id'], {'gene_sets': [f['gene_set']], 'activities': [f['account_document']['activities'][0]]}, [])
    gene_result['aliases'] = [{'model': MODEL, 'node_id': f['gene_set']['alternate_identifier'][0],
        'import_id': json.loads((ROOT / 'data/cfde-genesets/2026-09-24/manifest.json').read_text())['import_id']}]
    gene_result['coverage'] = {'membership': 'unknown', 'construction_provenance': 'unknown'}
    return {'draft': draft, 'saved': saved, 'composer': composer, 'page': page, 'research_request': request,
        'analysis_queued': job('analysis'), 'paragraph_queued': job('paragraph'), 'complete': complete,
        'paragraph_complete': paragraph_complete, 'cancelled': cancelled, 'event': event, 'queued_event': queued_event, 'gap': gap_record,
        'gap_search': {'items': [{'gap': gap_record, 'ranking': rank}], 'page': page, 'search': search},
        'mechanism_search': {'items': [{'record': f['dismech_records'][0], 'ranking': rank}], 'page': page, 'search': search},
        'suggestions': suggestions, 'source_ref': source_ref,
        'account': object_result(f['account']['id'], f['account_document'], f['citations']),
        'claim': object_result(f['claim']['id'], f['account_document'], f['citations']),
        'paragraph': object_result(f['paragraph']['id'], f['paragraph_document'], f['citations']),
        'gene_set': gene_result, 'question_result': object_result(f['question']['id'], f['inquiry_document'], [f['citations'][0]])}


def endpoints(f, e):
    uuid = string(format='uuid')
    path_id = lambda name, value, schema=uuid: parameter(name, 'path', schema, value)
    common_search = [parameter('q', 'query', string(minLength=1, maxLength=2000), 'AIP AHR tumor growth', True),
        parameter('mode', 'query', enum('lexical', 'fuzzy', 'semantic', 'hybrid', default='hybrid'), 'semantic')]
    gap_filters = [parameter('kind', 'query', enum('KNOWLEDGE_GAP', 'HUMAN_MODEL_MISMATCH'), 'KNOWLEDGE_GAP'),
        parameter('status', 'query', enum('OPEN', 'RESOLVED', 'UNSPECIFIED'), 'OPEN', description='Omit to include all statuses. UNSPECIFIED matches null source status; it is not rewritten to OPEN.'),
        parameter('source', 'query', enum('dismech', 'user', 'all', default='all'), 'dismech'),
        parameter('disease_id', 'query', string(), 'MONDO:1060231', description='Source ontology identifier or canonical URI; no label-only entity equivalence.')]
    operation('/v1/me', 'get', 'getMe', 'Identity', 'Get the current researcher',
        'Resolve the verified gateway subject to a durable application user UUID. Profile fields may be null; no auth secrets are returned.',
        'Me', {'researcher': {'user_id': USER_ID, 'display_name': 'Example Researcher', 'email': None, 'email_verified': None,
            'orcid': None, 'orcid_authenticated': False, 'person': f['account_document']['persons'][0]}}, errors=('401', '429'))
    operation('/v1/drafts', 'get', 'listDrafts', 'Drafts', 'List saved drafts', 'Only drafts owned by the caller, ordered by updated_at descending then ID.',
        'DraftList', {'saved_drafts': {'items': [e['saved']], 'page': e['page']}}, parameters=page_parameters(), errors=('400', '401', '429'))
    operation('/v1/drafts', 'post', 'createDraft', 'Drafts', 'Create a draft',
        'Creates editable application state with an optional private display name. Does not mint an inquiry or start a job.',
        'Draft', {'created': e['draft']}, request_schema='DraftCreate', request_examples={'question_and_anchor': {'composer': e['composer']}},
        status=201, idempotent=True, errors=('401', '409', '422', '429'))
    operation('/v1/drafts/{draft_id}', 'get', 'getDraft', 'Drafts', 'Recover a saved draft', 'Returns only the server-confirmed version; local pending edits are not implied.',
        'Draft', {'saved': e['saved']}, parameters=[path_id('draft_id', DRAFT_ID)])
    operation('/v1/drafts/{draft_id}', 'patch', 'updateDraft', 'Drafts', 'Autosave a draft',
        'Rename or replace composer using expected_version. Omitted fields are preserved. The server increments the version only on commit. Stale revisions return 409 with current_version. Server derives ownership; unknown owner/provenance fields are rejected.',
        'Draft', {'saved': e['saved']}, parameters=[path_id('draft_id', DRAFT_ID)], request_schema='DraftPatch',
        request_examples={'save_revision_two': {'expected_version': 1, 'composer': e['composer']}}, idempotent=True,
        errors=('401', '404', '409', '422', '429'))
    operation('/v1/drafts/{draft_id}', 'delete', 'deleteDraft', 'Drafts', 'Delete a saved draft',
        'Delete an owned draft using expected_version. Active analysis jobs prevent deletion (409 DRAFT_IN_USE). Frozen research requests, jobs, accounts and explorations are preserved. Retry a lost acknowledgment with the same Idempotency-Key.',
        'DraftDeletion', {'deleted': {'id': DRAFT_ID, 'deleted': True}}, parameters=[path_id('draft_id', DRAFT_ID)],
        request_schema='DraftDelete', request_examples={'delete_saved_draft': {'expected_version': 2}}, idempotent=True,
        errors=('401', '404', '409', '422', '429'))
    operation('/v1/research-requests', 'get', 'listResearchRequests', 'Research history', 'List submitted questions',
        'Immutable request snapshots owned by the caller, ordered by submitted_at descending then ID. Questions are DAPPER content; request records preserve submission and selection context.',
        'ResearchRequestList', {'history': {'items': [e['research_request']], 'page': e['page']}}, parameters=page_parameters(), errors=('400', '401', '429'))
    operation('/v1/research-requests/{request_id}', 'get', 'getResearchRequest', 'Research history', 'Inspect a submitted question',
        'Returns the exact saved composer, DAPPER inquiry, actor snapshot and originating draft revision.',
        'ResearchRequest', {'submitted': e['research_request']}, parameters=[path_id('request_id', REQUEST_ID)])
    operation('/v1/knowledge-gaps/search', 'get', 'searchKnowledgeGaps', 'Knowledge gaps', 'Search knowledge gaps with free text',
        'The newly requested search endpoint. Returns DAPPER KnowledgeGap objects with source context and typed relevance scores. Semantic mode searches the whole permitted corpus. Hybrid combines independently retrieved candidates. Missing semantic service returns 503 rather than silently changing modes. All filters apply before pagination; only accessible gaps are returned. Example ranking is synthetic; the AIP source text is captured data.',
        'GapSearchResults', {'illustrative_ranked_gap': e['gap_search'], 'no_matches': {**e['gap_search'], 'items': []}},
        parameters=common_search + gap_filters + page_parameters(), public=True, errors=('400', '401', '429', '503'))
    operation('/v1/knowledge-gaps', 'get', 'listKnowledgeGaps', 'Knowledge gaps', 'Browse knowledge gaps',
        'Browse without ranked free-text retrieval; use /search for q. Omitted filters include all source kinds/statuses accessible to the caller. Stable sort is source ID then DAPPER ID.',
        'GapList', {'gaps': {'items': [e['gap']], 'page': e['page']}}, parameters=gap_filters + page_parameters(), public=True, errors=('400', '401', '429'))
    operation('/v1/knowledge-gaps/{gap_id}', 'get', 'getKnowledgeGap', 'Knowledge gaps', 'Inspect a knowledge gap',
        'Exact DAPPER KnowledgeGap plus one pinned source observation and attachment resolutions. source_revision selects a historical mapping; omission selects the latest accessible observation of this same digest. It never redirects to a different digest.',
        'GapRecord', {'aip_gap': e['gap']}, parameters=[path_id('gap_id', f['gap']['id'], did('KnowledgeGap')),
            parameter('source_revision', 'query', string(pattern='^[a-f0-9]{64}$'), e['gap']['source']['source_revision'])], public=True)
    operation('/v1/mechanisms/search', 'get', 'searchMechanisms', 'Mechanisms', 'Search DisMech mechanisms or EAGGL factors',
        'Manual keyword/fuzzy/semantic search, independent of the main inquiry. DisMech records contain DAPPER Mechanism; EAGGL records contain a DAPPER File for exact catalog bytes and the original CFDE anchor. Retrieval does not assert biological equivalence or support.',
        'MechanismSearchResults', {'illustrative_mechanism_match': e['mechanism_search']},
        parameters=common_search + [parameter('source', 'query', enum('dismech', 'eaggl', 'all', default='all'), 'dismech'),
            parameter('model', 'query', enum(MODEL, default=MODEL), MODEL)] + page_parameters(), public=True, errors=('400', '401', '429', '503'))
    operation('/v1/mechanisms/{source_id}', 'get', 'getMechanism', 'Mechanisms', 'Inspect a mechanism or factor record',
        'URL-encode the entire opaque source ID, including slashes, hashes and colons. The required source_revision prevents draft restoration from silently using changed source content.',
        'MechanismRecord', {'eaggl_factor': f['factor_records'][0]}, parameters=[path_id('source_id', f['factor_records'][0]['source_id'], string()),
            parameter('source_revision', 'query', string(pattern='^[a-f0-9]{64}$'), f['factor_records'][0]['source_revision'], True)], public=True)
    gap_input = {'object_class': 'KnowledgeGap', 'object': {k: f['gap'][k] for k in ['text', 'scope', 'about_entities', 'gap_description', 'gap_kind']}}
    operation('/v1/mechanisms/suggest', 'post', 'suggestMechanisms', 'Mechanisms', 'Suggest five EAGGL anchors total',
        'Returns selected-by-default candidates across all DisMech context, deduplicated by full factor ID. Preserve manual anchors and dismissals; do not silently refill a deleted chip. With no DisMech context use inquiry/subquery relevance, but require user selection before submission. Returned suggestions do not mutate a draft. Example factor rankings are illustrative, not measured AIP matches.',
        'Suggestions', {'illustrative_five_total': e['suggestions']}, request_schema='SuggestInput', request_examples={'dismech_context': {
            'inquiry': gap_input, 'dismech_context': [e['source_ref'](r) for r in f['dismech_records']],
            'manual_eaggl_anchors': [], 'dismissed_source_ids': [], 'subquery': 'AIP AHR signaling', 'mode': 'semantic', 'model': MODEL}},
        public=True, errors=('401', '404', '422', '429', '503'))
    operation('/v1/jobs', 'get', 'listJobs', 'Jobs', 'List analysis and paragraph jobs',
        'One job namespace for both kinds, ordered by created_at descending then ID. Only caller-owned or explicitly authorized jobs are visible.',
        'JobList', {'jobs': {'items': [e['complete']], 'page': e['page']}}, parameters=[
            parameter('kind', 'query', enum('analysis', 'paragraph'), 'analysis'),
            parameter('status', 'query', SCHEMAS['Job']['properties']['status'], 'succeeded'),
            parameter('research_request_id', 'query', uuid, REQUEST_ID)] + page_parameters(), errors=('400', '401', '429'))
    op = operation('/v1/jobs', 'post', 'createJob', 'Jobs', 'Start an analysis or paragraph job',
        'Replaces analysis-runs and account-specific paragraph-run creation. kind=analysis freezes draft/version, creates a research request and enqueues atomically. kind=paragraph uses an already saved account. Trusted backend supplies attribution, model/harness configuration and dates. Paid execution is asynchronous; 202 does not imply a successful scientific result. Idempotent replay returns the original job and never starts another paid attempt.',
        'Job', {'analysis_accepted': e['analysis_queued'], 'paragraph_accepted': e['paragraph_queued']},
        request_schema='JobCreate', request_examples={'analysis': {'kind': 'analysis', 'draft_id': DRAFT_ID, 'draft_version': 2, 'budgets': {'max_accounts': 3}},
            'paragraph': {'kind': 'paragraph', 'account_id': f['account']['id'], 'focus_claim_id': f['claim']['id'], 'audience': 'researcher',
                'language': 'en', 'target_words': 180, 'additional_citations': []}}, status=202, idempotent=True,
        errors=('401', '404', '409', '422', '429', '503'))
    op['responses']['422']['content']['application/problem+json']['examples']['missing_eaggl_anchor'] = {'summary': 'Draft has no valid EAGGL anchor',
        'value': problem(422, 'EAGGL_ANCHOR_REQUIRED', 'Select at least one resolvable EAGGL factor before creating an analysis job.',
                         field_errors=[{'pointer': '/draft/eaggl_anchors', 'message': 'At least one EAGGL anchor is required.'}])}
    op['responses']['409']['content']['application/problem+json']['examples']['idempotency_conflict'] = {'value': problem(409, 'IDEMPOTENCY_CONFLICT', 'This key was already used with a different canonical request body.')}
    operation('/v1/jobs/{job_id}', 'get', 'getJob', 'Jobs', 'Get job status and result IDs',
        'The same endpoint monitors either job kind. Polling remains available after the event stream ends. Terminal states are succeeded, insufficient_evidence, failed and cancelled. A CFDE-empty result is insufficient_evidence; an unavailable required service is a failure, not evidence of biological absence.',
        'Job', {'completed_analysis': e['complete']}, parameters=[path_id('job_id', JOB_ID)])
    sse = ''.join('id: ' + event['id'] + '\nevent: ' + event['event_type'] + '\ndata: ' + canonical(event) + '\n\n'
                  for event in [e['queued_event'], e['event']])
    operation('/v1/jobs/{job_id}/events', 'get', 'getJobEvents', 'Jobs', 'Read or stream job progress',
        'Accept application/json for a replayable event page, or text/event-stream for SSE. JSON supports after; SSE resumes with Last-Event-ID. If both are supplied they must agree. Events are per-job monotonic decimal IDs; delivery can repeat after reconnect, so deduplicate by job_id/id. SSE data is JobEvent JSON; colon-prefixed heartbeat comments are not events. Authenticate every reconnect. If history is no longer retained, return 409 EVENT_CURSOR_EXPIRED and recover with GET /jobs/{id}. Browser native EventSource cannot set a bearer header: use the authenticated Next.js proxy or fetch streaming.',
        'JobEvents', {'completed_events': {'items': [e['queued_event'], e['event']], 'next_after': '2', 'terminal': True}},
        parameters=[path_id('job_id', JOB_ID), parameter('after', 'query', string(pattern='^[0-9]+$', default='0'), '0'),
            parameter('limit', 'query', {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 100}, 100),
            parameter('Last-Event-ID', 'header', string(pattern='^[0-9]+$'), '0')],
        extra_media={'text/event-stream': {'schema': string(), 'examples': {'result_event': {'value': sse}}, 'x-event-data-schema': ref('JobEvent')}},
        errors=('400', '401', '404', '409', '429'))
    operation('/v1/jobs/{job_id}/cancel', 'post', 'cancelJob', 'Jobs', 'Request job cancellation',
        'No request body. Repeated calls are idempotent. Returns the durable current job; a running job normally becomes cancel_requested. Cancellation is best effort until the worker records cancelled. An already terminal job returns unchanged, including a result committed before cancellation. Browser disconnect alone never cancels a job.',
        'Job', {'cancellation_requested': e['cancelled'], 'already_completed': e['complete']}, parameters=[path_id('job_id', JOB_ID)], errors=('401', '404', '429'))
    for path, opid, tag, title, schema, ex, cls in [
        ('accounts', 'getAccount', 'Scientific content', 'Inspect a scientific account', 'AccountResult', e['account'], 'ScientificAccount'),
        ('claims', 'getClaim', 'Scientific content', 'Inspect a claim and its evidence', 'ClaimResult', e['claim'], 'Claim'),
        ('gene-sets', 'getGeneSet', 'Scientific content', 'Inspect a DAPPER GeneSet', 'GeneSetResult', e['gene_set'], 'GeneSet'),
        ('paragraphs', 'getParagraph', 'Scientific content', 'Get a cited paragraph', 'ParagraphObjectResult', e['paragraph'], 'Paragraph')]:
        operation('/v1/' + path + '/{dapper_id}', 'get', opid, tag, title,
            'Returns exact DAPPER records, schema pin, payload checksums and provenance document. Scientific IDs never redirect to a newer revision. Select payload_sha256 to recover an exact historical observation; otherwise the server returns its designated current observation and checksum. Unhashable locator changes can share a scientific ID. Access is enforced independently of citation metadata.',
            schema, {'document': ex}, parameters=[path_id('dapper_id', ex['root_id'], did(cls)),
                parameter('payload_sha256', 'query', string(pattern='^[a-f0-9]{64}$'), next(p['payload_sha256'] for p in ex['payloads'] if p['object_id'] == ex['root_id']))], public=True)
    operation('/v1/objects/{dapper_id}', 'get', 'resolveDapperObject', 'Scientific content', 'Resolve any supported DAPPER object',
        'Generic durable resolver for Questions, KnowledgeGaps, evidence, files and other schema-supported records. This supplies citation object_payload_ref resolution. DAPPER identity does not imply public access. Returns no other user\'s private provenance.',
        'ObjectResult', {'question': e['question_result']}, parameters=[path_id('dapper_id', f['question']['id'], did()),
            parameter('payload_sha256', 'query', string(pattern='^[a-f0-9]{64}$'), sha(f['question']))], public=True)
    c = next(x for x in f['citations'] if x['target_id'] == f['claim']['id'])
    bib = '@misc{' + c['target_id'] + ',\n  title = {' + c['title'] + '},\n  author = {Researcher, Example},\n  year = {2026},\n  archivePrefix = {DAPPER},\n  eprint = {' + c['target_id'].split(':', 1)[1] + '},\n  url = {' + c['canonical_url'] + '}\n}\n'
    csl = {'id': c['target_id'], 'type': 'webpage', 'title': c['title'], 'author': [{'family': 'Researcher', 'given': 'Example'}],
           'issued': {'date-parts': [[2026, 9, 24]]}, 'URL': c['canonical_url'], 'container-title': 'REVEAL Mechanisms', 'genre': 'DAPPER Claim'}
    prose = 'Researcher, E. (2026). ' + c['title'] + ' REVEAL Mechanisms. ' + c['canonical_url']
    operation('/v1/citations/{dapper_id}', 'get', 'getCitation', 'Citations', 'Get citation metadata or an export',
        'format controls the representation: native (default) application/json; csl-json application/vnd.citationstyles.csl+json; bibtex application/x-bibtex; biblatex application/x-biblatex; apa/mla text/plain. Incompatible Accept returns 406. revision pins exact metadata; omission selects latest accessible metadata for this exact scientific ID. Paragraph consumers MUST supply the pinned revision. Claims, Questions and KnowledgeGaps only. Unknown authors/dates stay unknown; no DOI is emitted without registration. APA/MLA examples illustrate the interface, not a tested renderer.',
        'CitationMetadata', {'native_metadata': c}, parameters=[path_id('dapper_id', c['target_id'], did('(Claim|Question|KnowledgeGap)')),
            parameter('format', 'query', enum('native', 'bibtex', 'biblatex', 'csl-json', 'apa', 'mla', default='native'), 'native'),
            parameter('revision', 'query', {'type': 'integer', 'minimum': 1}, 1), parameter('locale', 'query', string(default='en-US'), 'en-US')],
        public=True, extra_media={
            'application/x-bibtex': {'schema': string(), 'examples': {'bibtex': {'value': bib}}},
            'application/x-biblatex': {'schema': string(), 'examples': {'biblatex': {'value': bib.replace('year = {2026}', 'date = {2026-09-24}')}}},
            'application/vnd.citationstyles.csl+json': {'schema': ref('CslItem'), 'examples': {'csl_json': {'value': csl}}},
            'text/plain': {'schema': string(), 'examples': {'illustrative_apa': {'value': prose}, 'illustrative_mla': {'value': 'Researcher, Example. “' + c['title'] + '” REVEAL Mechanisms, 24 Sept. 2026, ' + c['canonical_url']}}}})
    PATHS['/v1/citations/{dapper_id}']['get']['responses']['406'] = {'description': 'Accept conflicts with format.',
        'content': content('Problem', {'not_acceptable': problem(406, 'NOT_ACCEPTABLE', 'Accept does not allow the representation selected by format.')}, 'application/problem+json')}
    rendering = {'paragraph_id': f['paragraph']['id'], 'style': 'apa', 'locale': 'en-US',
        'in_text': [{'occurrence_index': i, 'label': '(Illustrative citation ' + str(i + 1) + ')'} for i, _ in enumerate(f['paragraph']['citations'])],
        'bibliography': [{'target_id': x['target_id'], 'citation_metadata_revision': 1, 'text': 'Illustrative formatted entry: ' + next(c['title'] for c in f['citations'] if c['target_id'] == x['target_id'])} for x in f['paragraph']['citations']],
        'rendering_manifest': {'processor': 'illustrative-fixture-not-a-live-CSL-renderer', 'processor_version': 'not-executed',
            'style_sha256': sha('illustrative-style'), 'locale_sha256': sha('illustrative-locale'), 'citation_profile': 'reveal-citation-v1',
            'metadata_checksums': [sha(c) for c in f['citations'] if c['target_id'] in {x['target_id'] for x in f['paragraph']['citations']}]}}
    operation('/v1/citations/render', 'post', 'renderCitations', 'Citations', 'Render a paragraph’s complete citation set',
        'Format the saved paragraph\'s occurrences together for consistent numbering and author/year disambiguation. Exact metadata revisions and current permissions are required. Rendering is deterministic for pinned inputs/configuration and may be cached; it does not modify scientific IDs or launch an agent. Style/processor selection is deployment configuration, captured in the rendering manifest.',
        'CitationRendering', {'illustrative_rendering_shape': rendering}, request_schema='CitationRenderInput',
        request_examples={'apa': {'paragraph_id': f['paragraph']['id'], 'style': 'apa', 'locale': 'en-US'}},
        public=True, errors=('401', '404', '422', '429', '503'))


def main():
    OUT.mkdir(exist_ok=True);(OUT / 'examples').mkdir(exist_ok=True)
    import_dapper()
    import openapi_current
    module = sys.modules[__name__]
    fixture = openapi_current.fixtures(module, make_fixtures())
    application_schemas()
    openapi_current.schemas(module)
    examples = openapi_current.examples(module, fixture, sample_responses(fixture))
    endpoints(fixture, examples)
    openapi_current.endpoints(module, fixture, examples)
    import openapi_research
    openapi_research.extend(module, fixture, examples)
    import openapi_admin
    openapi_admin.extend(module, fixture, examples)
    import openapi_cfde_assessment
    openapi_cfde_assessment.extend(module, fixture, examples)
    import openapi_lightning
    openapi_lightning.extend(module, fixture, examples)
    # Pair representation-specific inputs with the correct output, and include
    # paragraph polling under its own job ID in the portable exchange library.
    def extra_exchange(operation_id, case, *, query=None, path=None, media=None, response_name=None, value=None):
        ex = deepcopy(next(x for x in EXCHANGES if x['operation_id'] == operation_id))
        ex['case'] = case
        ex['request']['query'].update(query or {})
        ex['request']['path'].update(path or {})
        if media: ex['request']['headers']['Accept'] = media
        op = PATHS[ex['path_template']][ex['method'].lower()]
        status = next(iter(ex['responses']))
        media = media or 'application/json'
        if value is None:
            value = op['responses'][status]['content'][media]['examples'][response_name]['value']
        ex['responses'] = {status: {'content_type': media, 'examples': {case: value}}}
        resolved = ex['path_template']
        for k, v in ex['request']['path'].items(): resolved = resolved.replace('{' + k + '}', quote(str(v), safe=''))
        ex['request']['url'] = BASE + resolved + ('?' + urlencode(ex['request']['query']) if ex['request']['query'] else '')
        pieces = ['curl', '-X', ex['method'], shlex.quote(ex['request']['url'])]
        for k, v in ex['request']['headers'].items(): pieces += ['-H', shlex.quote(k + ': ' + v)]
        ex['curl'] = ' '.join(pieces)
        op['x-codeSamples'].append({'lang': 'Shell', 'label': case, 'source': ex['curl']})
        EXCHANGES.append(ex)
    extra_exchange('getJob', 'paragraph', path={'job_id': PARAGRAPH_JOB_ID}, value=examples['paragraph_complete'])
    extra_exchange('getMechanism', 'kpn_factor', path={'source_id': examples['kpn_factor']['source_id']},
                   query={'source_revision': examples['kpn_factor']['source_revision']}, response_name='kpn_factor')
    for fmt, media, name in [('bibtex', 'application/x-bibtex', 'bibtex'), ('biblatex', 'application/x-biblatex', 'biblatex'),
            ('csl-json', 'application/vnd.citationstyles.csl+json', 'csl_json'), ('apa', 'text/plain', 'illustrative_apa'), ('mla', 'text/plain', 'illustrative_mla')]:
        extra_exchange('getCitation', fmt, query={'format': fmt}, media=media, response_name=name)
    for fmt in ['latex', 'bibtex', 'rich-text']:
        extra_exchange('exportParagraph', fmt, query={'format': fmt}, response_name=fmt)
    extra_exchange('getJobEvents', 'sse', media='text/event-stream', response_name='result_event')
    PATHS['/v1/jobs/{job_id}/events']['get']['responses']['409'] = {
        'description': 'Requested event history has expired.', 'content': content('Problem', {'cursor_expired':
            problem(409, 'EVENT_CURSOR_EXPIRED', 'Recover current job status and reconnect from its last_event_id.')}, 'application/problem+json')}
    spec = {'openapi': '3.1.1', 'jsonSchemaDialect': 'https://spec.openapis.org/oas/3.1/dialect/base',
        'info': {'title': 'REVEAL Mechanisms API', 'version': '0.2.0-draft',
            'summary': 'DAPPER scientific content, knowledge-gap search, and shared analysis/paragraph jobs.',
            'description': 'Local API implementation contract; production deployment is deferred. Scientific schemas are generated from the pinned DAPPER schema. Application envelopes handle ownership, drafts, search, jobs and pagination. All DAPPER IDs in scientific examples are computed, not placeholders. Research/job/user timestamps and semantic rankings are illustrative; no Claude Code or embedding run was performed. The account fixture is the approved 12-claim HTML example: captured CFDE observations plus explicitly invented KG/membership assertions, never a production grounding-pass example. The evidence package is a separately verified live capture, not a claim that this example account was generated from it. Examples using example.org are not live resources. Do not treat example output as published research.'},
        'servers': [{'url': 'http://127.0.0.1:18000', 'description': 'Local Docker deployment backend (private routes require a gateway assertion)'},
                    {'url': 'http://localhost:3000/api/backend', 'description': 'Local Next.js v1 gateway (browser session for private routes; OAuth protocol/metadata use the backend server)'},
                    {'url': BASE, 'description': 'Reserved example domain; replace for deployment'}],
        'tags': [{'name': n} for n in ['Identity', 'Knowledge gaps', 'Mechanisms', 'Drafts', 'Research history', 'Jobs', 'Local research', 'Scientific content', 'Citations', 'Administrator science read', 'CFDE assessment', 'Lightning audits']],
        'security': [{'ApplicationBearer': []}], 'paths': PATHS,
        'components': {'securitySchemes': {'ApplicationBearer': {'type': 'http', 'scheme': 'bearer', 'bearerFormat': 'API key or JWT',
            'description': 'Paste your rvl_ workspace API key or a short-lived trusted-gateway JWT. Swagger adds the Bearer prefix automatically. API keys resolve to one configured existing workspace and retain its ownership, expiry and job limits; they grant no internal or administrator access. Gateway JWTs are issued after registered login or anonymous session bootstrap. Never share gateway signing/service credentials or send provider access tokens or Auth.js cookies. Without a bearer, only public operations are available.'},
            'AdminReadBearer': openapi_admin.SECURITY_SCHEME}, 'schemas': SCHEMAS},
        'x-dapper-dependency': {'base_commit': PIN['base_commit'], 'schema_sha256': PIN['root_schema_sha256'],
            'snapshot_sha256': PIN['snapshot_sha256'], 'identity_profile': 'DAPPER-ID-1',
            'source': '../' + str((DAPPER / 'dapper.yaml').relative_to(ROOT))},
        'x-contract-rules': {'idempotency_retention_days': 7, 'schema_validation': 'Closed DAPPER schemas plus runtime digest, graph-reference, evidence-lineage and citation-span checks.',
            'naming': 'jobs is the only job resource. kind=analysis and kind=paragraph share creation, listing, status, events and cancellation.',
            'design_revision': 'v12.1',
            'initial_anchor_retrieval': 'Existing EAGGL label embeddings joined to a completed exact_trait_factor_number CFDE mapping run; pin routing provenance with each saved request. No label/gene agreement or full-catalog re-embedding prerequisite.',
            'evidence_package': 'reveal.evidence-package/0.2-draft; bundled from schema/evidence-package.schema.json',
            'agent_release_lock': '../services/backend/agent-runtime/dapper-release.json',
            'gateway_contract': '../docs/gateway-contract.md',
            'reference_generations': 'Each deployment serves one active EAGGL/CFDE reference generation (model cfde-inc-v2 or eaggl-capped-v1). Work built on a superseded generation carries archive: it stays readable, downloadable and publishable but cannot be re-analysed. See ../docs/reference-reload.md.',
            'wire_identity': 'Compact dapper:Class.digest IDs; absolute external entity URIs. Source aliases remain opaque and exact.'}}
    write_json(OUT / 'openapi.json', spec)
    (OUT / 'openapi.yaml').write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
    for ex in EXCHANGES:
        write_json(OUT / 'examples' / (ex['operation_id'] + '.' + ex['case'] + '.json'), ex)
    write_json(OUT / 'examples/exchanges.json', EXCHANGES)
    write_json(OUT / 'manifest.json', {'openapi_sha256': sha((OUT / 'openapi.json').read_bytes()),
        'schema_sha256': PIN['root_schema_sha256'], 'dapper_snapshot_sha256': PIN['snapshot_sha256'],
        'paths': len(PATHS), 'operations': sum(len(p) for p in PATHS.values()), 'component_schemas': len(SCHEMAS),
        'request_exchanges': len(EXCHANGES), 'version': spec['info']['version'], 'examples_are_contract_fixtures': True})
    print(f"Built {len(PATHS)} paths, {sum(len(p) for p in PATHS.values())} operations, {len(SCHEMAS)} schemas, {len(EXCHANGES)} request exchanges.")


if __name__ == '__main__':
    main()
