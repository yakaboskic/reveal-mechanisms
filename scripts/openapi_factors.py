"""Public reference browsing contract, separate from account object permissions."""


def schemas(b):
    ref, obj, array, string, null = b.ref, b.obj, b.array, b.string, b.nullable
    number = {'type': 'number'}
    count = {'type': 'integer', 'minimum': 0}
    generation = null(string(pattern='^[a-f0-9]{64}$'))
    raw = {'type': 'object', 'additionalProperties': True}
    b.add('GnomadImport', obj({'import_id': string(pattern='^[a-f0-9]{64}$'), 'version': string(),
        'source_sha256': string(pattern='^[a-f0-9]{64}$'), 'source_url': null(string()), 'selection_policy': string()},
        description='Immutable gnomAD constraint source active for this environment. Independent of the factor reference generation. Pin import_id for paginated gene reads.'))
    b.add('GnomadGeneConstraint', obj({'symbol': string(), 'gene_id': null(string()), 'transcript': null(string()),
        'status': b.enum('selected', 'ambiguous_gene', 'ambiguous_transcript', 'no_primary_transcript', 'no_ensembl_gene'),
        'selection_method': null(b.enum('mane_select', 'canonical')), 'selection_reason': string(),
        'pli': null({'type': 'number', 'minimum': 0, 'maximum': 1}), 'loeuf': null({'type': 'number', 'minimum': 0}),
        'mis_z': null(number), 'lof_oe': null({'type': 'number', 'minimum': 0}), 'flags': array(string())},
        description='Exact-symbol gnomAD constraint annotation. A unique Ensembl MANE Select transcript is preferred, otherwise a unique canonical transcript. pLI is probability of loss-of-function intolerance (higher = stronger constraint); LOEUF is the LoF observed/expected upper confidence bound (lower = stronger constraint); mis_z is the missense Z score. Missing or ambiguous metrics stay null. Source quality flags are retained.'))
    b.add('LoadingSummary', obj({'total': count, 'min': null(number), 'max': null(number),
        'coverage': string(), 'available': {'type': 'boolean'}},
        description='Statistics over the entire retained factor loading set, unaffected by search or pagination. available means numeric scores are stored. Missing values are not zero.'))
    b.add('FactorLoading', obj({'id': string(), 'label': string(), 'loading': null(number), 'rank': {'type': 'integer', 'minimum': 1},
        'gene_set_id': null(b.did('GeneSet')), 'library': string(), 'gene_count': count,
        'joint_loading': number, 'marginal_loading': number, 'joint_rank': {'type': 'integer', 'minimum': 1},
        'marginal_rank': {'type': 'integer', 'minimum': 1}, 'gnomad': null(ref('GnomadGeneConstraint'))}, required=['id', 'label', 'loading', 'rank'],
        description='One retained gene weight or gene-set projection. rank is the full factor gene rank or the original selected-metric gene-set projection rank, unchanged by search/pagination; legacy sets preserve imported rank. joint_rank and marginal_rank are original projection ranks. Legacy gene-set weights are null.'))
    b.add('FactorDetail', obj({'factor': ref('EagglFactor'), 'generation_id': generation,
        'provenance': obj({'eaggl_import_id': string(), 'factor_id': string(), 'factor_key': null(string()), 'model': string()}),
        'genes': ref('LoadingSummary'), 'gene_sets': ref('LoadingSummary'), 'gnomad': null(ref('GnomadImport'))},
        required=['factor', 'generation_id', 'provenance', 'genes', 'gene_sets']))
    b.add('FactorLoadings', obj({'source_id': string(), 'generation_id': generation, 'kind': b.enum('gene', 'gene_set'),
        'metric': b.enum('joint', 'marginal'), 'sort': b.enum('alphabetical', 'loading', 'gnomad_pli', 'gnomad_loeuf', 'gnomad_mis_z'), 'items': array(ref('FactorLoading')), 'total': count, 'offset': count,
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500}, 'next_offset': null(count), 'summary': ref('LoadingSummary'), 'gnomad': null(ref('GnomadImport'))},
        required=['source_id', 'generation_id', 'kind', 'metric', 'sort', 'items', 'total', 'offset', 'limit', 'next_offset', 'summary'],
        description='Bounded loading page. sort=loading (default) orders by descending score; alphabetical orders case-insensitive labels. Gene-only gnomad_pli and gnomad_mis_z sort descending; gnomad_loeuf sorts ascending, all with missing values last and stable symbol ties. Sorting applies globally before pagination. Ranks and heatmap score ranges always remain original factor loadings. Pin generation_id, source_revision, and gnomad_import_id returned by FactorDetail.'))
    b.add('CatalogGeneSet', obj({'id': b.did('GeneSet'), 'generation_id': generation, 'name': string(), 'library': null(string()),
        'gene_count': null(count), 'genes_in_universe': null(count), 'object': null(raw), 'metadata': raw, 'provenance': raw,
        'collection': null(obj({'id': b.did('GeneSetCollection'), 'label': string(), 'library': string(), 'gene_set_count': count,
            'object': null(raw), 'payload_sha256': null(string())})), 'limitations': array(string())},
        description='Imported public reference data, never an owner account graph. object is the exact stored GeneSet or null; provenance preserves imported collection prefixes, organizations, datasets, files and activities. Unknown/derived account aliases return 404.'))


def endpoints(b, factor):
    from reveal_backend.factor_details import GENE_COVERAGE, SET_COVERAGE
    string, param = b.string, b.parameter
    generation = factor['reference_generation_id']
    pins = [param('generation_id', 'query', string(pattern='^[a-f0-9]{64}$'), generation,
                  description='Pin the reference generation returned by factor detail. A changed active generation returns 409.'),
            param('source_revision', 'query', string(pattern='^[a-f0-9]{64}$'), factor['source_revision'],
                  description='Pin the factor source revision; a mismatch returns 409 without returning new-generation loadings.')]
    gene_summary = {'total': 633, 'min': .0001, 'max': .98, 'coverage': GENE_COVERAGE, 'available': True}
    set_summary = {'total': 71, 'min': .0003, 'max': .24, 'coverage': SET_COVERAGE, 'available': True}
    detail = {'factor': factor, 'generation_id': generation,
        'provenance': {'eaggl_import_id': 'e' * 64, 'factor_id': 'T2D::Factor1', 'factor_key': 'KPN.TRAIT:0000398::Factor1', 'model': factor['model']},
        'genes': gene_summary, 'gene_sets': set_summary, 'gnomad': None}
    b.operation('/v1/factors/{source_id}', 'get', 'getFactorDetail', 'Mechanisms', 'Inspect a factor and its loading coverage',
        'Read the active imported factor and score ranges. Requests never run research or model inference. Pass source_revision from the anchor to prevent a same-id factor silently changing across reference reloads.',
        'FactorDetail', {'factor': detail}, parameters=[param('source_id', 'path', string(), factor['source_id'], True), *pins],
        public=True, errors=('404', '409', '422', '503'))
    loadings = {'source_id': factor['source_id'], 'generation_id': generation, 'kind': 'gene', 'metric': 'joint', 'sort': 'loading',
        'items': [{'id': 'LEPR', 'label': 'LEPR', 'loading': .98, 'rank': 1, 'gnomad': None}], 'total': 1, 'offset': 0, 'limit': 200, 'next_offset': None, 'summary': gene_summary, 'gnomad': None}
    b.operation('/v1/factor-loadings', 'get', 'getFactorLoadings', 'Mechanisms', 'Search and page factor loadings',
        'Search literal gene symbols or gene-set names, library and identifier. Gene scores are nonzero EAGGL weights; gene-set scores are joint/marginal projections retained in the top 50 by either rank. Gene scores and set scores have separate scales. Missing legacy set scores stay null.',
        'FactorLoadings', {'genes': loadings}, parameters=[param('source_id', 'query', string(), factor['source_id'], True),
            param('kind', 'query', b.enum('gene', 'gene_set'), 'gene'), param('metric', 'query', b.enum('joint', 'marginal'), 'joint'),
            param('sort', 'query', b.enum('alphabetical', 'loading', 'gnomad_pli', 'gnomad_loeuf', 'gnomad_mis_z', default='loading'), 'loading', description='Global label/loading order or gene-only gnomAD constraint order. pLI and missense Z descend; LOEUF ascends; missing metrics sort last. gnomAD sorting requires an active imported annotation source (503 otherwise).'),
            param('gnomad_import_id', 'query', string(pattern='^(none|[a-f0-9]{64})$'), 'none', description='Gene-only annotation pin from FactorDetail.gnomad.import_id. Use none when annotations are absent. A change returns 409 to prevent mixed-source append pages.'),
            param('q', 'query', string(maxLength=200), 'LEPR'), param('limit', 'query', {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 200}, 200),
            param('offset', 'query', {'type': 'integer', 'minimum': 0, 'default': 0}, 0), *pins], public=True, errors=('404', '409', '422', '503'))
    gene_set = {'id': 'dapper:GeneSet.' + 'a' * 32, 'generation_id': generation, 'name': 'Example imported gene set', 'library': 'GO_BP',
        'gene_count': 0, 'genes_in_universe': 0, 'object': None, 'metadata': {}, 'provenance': {}, 'collection': None,
        'limitations': ['Illustrative unavailable-object response; no provenance is fabricated.']}
    b.operation('/v1/catalog/gene-sets/{gene_set_id}', 'get', 'getCatalogGeneSet', 'Mechanisms', 'Inspect imported gene-set provenance',
        'Returns exact imported reference GeneSet and collection provenance. This route does not expose private scientific-account objects; /v1/gene-sets retains its existing publication/owner permissions.',
        'CatalogGeneSet', {'unavailable_object': gene_set}, parameters=[param('gene_set_id', 'path', b.did('GeneSet'), gene_set['id'], True), pins[0]],
        public=True, errors=('404', '409', '422', '503'))
