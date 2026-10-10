"""Scientific-account provenance application views; canonical DAPPER is unchanged."""


def extend(b, fixture, examples):
    from reveal_backend.account_provenance import collect, CLASSIFICATIONS
    from reveal_backend.provenance_api import paginate, POLICY_VERSION
    from reveal_backend.repository import digest
    from reveal_backend.evidence_package import canonical_json, sha256
    ref, obj, array, string, nullable = b.ref, b.obj, b.array, b.string, b.nullable
    count = {'type': 'integer', 'minimum': 0}
    raw = {'type': 'object', 'additionalProperties': True}
    strings = array(string())
    status = b.enum('complete', 'incomplete')
    step = obj({'edge_id': string(), 'direction': b.enum('forward', 'reverse')})
    annotation = obj({'trace_count': count, 'page_trace_count': count, 'trace_ids': strings,
        'dataset_ids': strings, 'organization_ids': strings,
        'resolution': b.enum('complete', 'incomplete', 'not_applicable')},
        description='trace_count and reached IDs cover the entire account; trace_ids and page_trace_count cover this page. Scientific records are never rewritten.')
    b.add('ProvenanceAnnotation', annotation)
    for kind in ('ScientificAccount', 'Claim', 'Proposition', 'EvidenceItem'):
        b.add('Annotated' + kind, obj({'record': ref('Dapper' + kind), 'provenance': ref('ProvenanceAnnotation')}))
    b.add('ProvenanceTrace', obj({'id': string(), 'claim_id': nullable(string()),
        'proposition_id': nullable(string()), 'evidence_item_id': nullable(string()),
        'origin_id': string(), 'source_field': string(), 'source_id': string(), 'dataset_id': string(),
        'route_classifications': array(b.enum(*CLASSIFICATIONS)), 'input_roles': strings,
        'organization_ids': strings, 'organization_attributions': array(obj({'organization_id': string(),
            'role': string(), 'path_steps': array(step)})), 'path_steps': array(step), 'path_edge_ids': strings,
        'direction': b.enum('SUPPORTS', 'DISPUTES', 'MIXED', 'NEUTRAL', 'UNKNOWN')},
        required=['id', 'claim_id', 'proposition_id', 'evidence_item_id', 'origin_id', 'source_field', 'source_id',
                  'dataset_id', 'route_classifications', 'input_roles', 'organization_ids',
                  'organization_attributions', 'path_steps', 'path_edge_ids'],
        description='Deterministic witness path for an originating scientific use. Direction is present only for evidence-origin traces and is not multiplied through source claims. Alternative recorded branches remain in the shared graph.'))
    b.add('ProvenanceNode', obj({'id': string(), 'kind': string(), 'status': b.enum('resolved', 'unresolved', 'conflicting'),
        'name': nullable(string()), 'sha256': nullable(string()), 'filename': nullable(string()), 'ror': nullable(string())}, required=['id', 'kind', 'status']))
    b.add('ProvenanceEdge', obj({'id': string(), 'subject': string(), 'predicate': string(), 'object': string(),
        'relation': string(), 'input_role': string(), 'observations': array(raw), 'provenance_source': array(raw)},
        required=['id', 'subject', 'predicate', 'object', 'relation']))
    b.add('DatasetReuse', obj({'dataset_id': string(), 'claim_count': count, 'claim_ids': strings,
        'trace_count': count, 'classification_claim_counts': obj({key: count for key in CLASSIFICATIONS}),
        'classification_claim_ids': obj({key: strings for key in CLASSIFICATIONS}), 'input_roles': strings},
        description='Distinct claims reaching an exact dataset. Route classifications overlap and must not be added. This measures reuse/reference lineage, not evidentiary strength.'))
    organization_counts = {'claim_count': count, 'claim_ids': strings, 'dataset_count': count,
                           'dataset_ids': strings, 'claim_dataset_count': count}
    b.add('OrganizationReuse', obj({'organization_id': string(), **organization_counts,
        'roles': {'type': 'object', 'additionalProperties': obj(organization_counts)}},
        description='Distinct claims, datasets and claim-dataset pairs, grouped by recorded organization roles; publisher and funder do not imply data producer.'))
    b.add('ProvenanceCoverage', obj({'status': status, 'counts_are_lower_bounds': {'type': 'boolean'},
        'claim_count': count, 'resolved_claim_count': count, 'trace_count': count,
        'lineage_resolution': obj({'status': status, 'issue_count': count}),
        'retained_projection_coverage': array(raw), 'recovery_status': array(raw), 'issues': array(raw)},
        description='Independent lineage, retained projection and source-recovery coverage. Complete retained projection rows do not establish complete model-construction lineage. Missing or withheld sources make counts lower bounds.'))
    b.add('ProvenanceSnapshot', obj({'id': string(), 'policy_version': string(), 'account_id': b.did('ScientificAccount'),
        'account_payload_sha256': string(), 'document_sha256': string(), 'publication_version': count,
        'reference_generation_ids': strings, 'supplement_ids': strings,
        'source_observations': array(obj({'kind': string(), 'key': string(), 'sha256': string()}))}))
    b.add('AccountProvenance', obj({'account': ref('AnnotatedScientificAccount'),
        'claims': array(ref('AnnotatedClaim')), 'propositions': array(ref('AnnotatedProposition')),
        'evidence_items': array(ref('AnnotatedEvidenceItem')), 'traces': array(ref('ProvenanceTrace')),
        'nodes': array(ref('ProvenanceNode')), 'edges': array(ref('ProvenanceEdge')),
        'dataset_reuse': array(ref('DatasetReuse')), 'organization_reuse': array(ref('OrganizationReuse')),
        'coverage': ref('ProvenanceCoverage'), 'snapshot': ref('ProvenanceSnapshot'),
        'page': obj({'limit': {'type': 'integer', 'minimum': 1, 'maximum': 250}, 'offset': count,
                     'total': count, 'next_cursor': nullable(string())})}))
    document = fixture['account_document']
    account = fixture['account']
    sample = paginate(collect(document, account['id']), {'policy_version': POLICY_VERSION,
        'account_id': account['id'], 'account_payload_sha256': sha256(canonical_json(account)), 'document_sha256': digest(document),
        'publication_version': 1, 'reference_generation_ids': [], 'supplement_ids': [], 'source_observations': []},
        ['illustrative-publication'], {'limit': '100'}, secret=b'illustrative-contract-only-secret-32-bytes')
    b.operation('/v1/accounts/{account_id}/provenance', 'get', 'getAccountProvenance', 'Scientific accounts',
        'Expand scientific account provenance and dataset reuse',
        'Read the full authorized account and annotate its propositions and evidence with dataset and organization lineage. '
        'Totals count distinct claims, before trace pagination. Stored factor projections are distinguished from direct lineage; '
        'no complete model-input provenance is implied. Source gaps produce explicit lower bounds. The processing bounds are '
        '10,000 nodes, 50,000 edges and depth 128; responses are capped at 8 MiB. Exceeding bounds returns '
        'PROVENANCE_LIMIT_EXCEEDED without rankings. Cursors bind authorization, immutable observations and tracing policy. '
        'Public reads use frozen publication records and public reference sources only. The example uses the existing illustrative '
        'scientific-account fixture and does not claim historical provenance recovery.',
        'AccountProvenance', {'account': sample}, parameters=[
            b.parameter('account_id', 'path', b.did('ScientificAccount'), account['id'], True),
            b.parameter('payload_sha256', 'query', string(pattern='^[a-f0-9]{64}$'), sha256(canonical_json(account)),
                        description='Optional exact account payload observation; a mismatch returns 404.'),
            b.parameter('limit', 'query', {'type': 'integer', 'minimum': 1, 'maximum': 250, 'default': 100}, 100),
            b.parameter('cursor', 'query', string(), 'opaque-signed-cursor',
                        description='Opaque trace continuation. Changed sources, visibility, or explicit limit return 409.')],
        public=True, errors=('401', '404', '409', '422', '503'))
