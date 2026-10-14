"""Public reference traits, trait -> gene-set betas, factor -> phenotype links and Mechanism id lookup (reveal_ref_* release tables)."""
from copy import deepcopy
import shlex
from urllib.parse import quote, urlencode

KPN_PATTERN = r'^KPN\.TRAIT:[0-9]{7}$'


def schemas(b):
    from reveal_backend.trait_details import BETA_SORTS, LINK_SORTS, TOP_PER_LIBRARY
    ref, obj, array, string, null = b.ref, b.obj, b.array, b.string, b.nullable
    number, boolean = {'type': 'number'}, {'type': 'boolean'}
    count, rank = {'type': 'integer', 'minimum': 0}, {'type': 'integer', 'minimum': 1}
    probability = {'type': 'number', 'minimum': 0, 'maximum': 1}
    kpn, release = string(pattern=KPN_PATTERN), string(pattern='^[a-f0-9]{64}$', description='The published reference release these rows were read from.')
    page = {'total': count, 'next_offset': null(count), 'coverage': string(), 'release_id': release}
    artifact = obj({'ancestry_code': string(), 'ancestry_name': string(), 'artifact_id': string(), 'href': string(format='uri')})
    b.add('TraitDetail', obj({'kpn_trait_id': kpn, 'phenotype': string(), 'name': string(), 'category': null(string()), 'is_atlas': boolean,
        'legacy_phenotype_id': null(string()), 'trait_group': null(string()), 'trait_type': null(string()), 'description': null(string()),
        'kpn_url': string(format='uri'),
        'gwas': obj({'status': b.enum('available', 'not_a_gwas_trait', 'not_on_portal'), 'portal_trait_id': null(string()),
            'gwas_artifact_id': null(string()), 'portal_href': null(string(format='uri')), 'artifacts': array(artifact)},
            description='The Translator provenance portal\'s bottom-line GWAS of the trait, as links built from stored ids (never fetched). gwas_artifact_id is the one artifact the account trace\'s GWAS route uses. Rare-disease gene lists have no GWAS (not_a_gwas_trait); other traits without artifacts are not_on_portal.'),
        'factors': array(obj({'source_id': string(), 'label': string(), 'factor_number': rank})),
        'gene_set_libraries': array(obj({'library': string(), 'n_gene_sets': count, 'n_nonzero': count,
            'top_gene_sets': array(ref('TraitGeneSet'), maxItems=TOP_PER_LIBRARY)})),
        'linked_factors': obj({'n_factors': count}), 'release_id': release},
        description='A KPN trait of the published reference release. Atlas traits (is_atlas) have fitted EAGGL factors; a non-atlas phenotype is one the factor-trait linkage linked factors to, with no factors, betas or trait group of its own. phenotype is the PIGEAN phenotype id (an atlas trait without a phenotype row falls back to its EAGGL phenotype code). factors are fitted on this trait, in factor order; gene_set_libraries count its PIGEAN-kept gene sets per library and those with a nonzero beta_uncorrected, with its best ' + str(TOP_PER_LIBRARY) + ' nonzero gene sets of each library by library_rank (page the rest at /v1/trait-gene-sets); linked_factors counts factors of other traits linked to this trait.'))
    b.add('TraitGeneSet', obj({'kpn_trait_id': kpn, 'trait_name': string(), 'gene_set_id': b.did('GeneSet'), 'gene_set_name': string(),
        'library': string(), 'collection_id': b.did('GeneSetCollection'), 'n_genes': count, 'beta_uncorrected': number, 'beta': number,
        'avg_postp': probability, 'library_rank': rank},
        description='One trait x gene-set row of the release\'s PIGEAN betas mode (one fit per CFDE library on the trait\'s PIGEAN gene statistics, no outer Gibbs). beta_uncorrected is the gene set\'s uncorrected effect, beta its effect corrected for the library\'s other gene sets, avg_postp its average posterior inclusion probability; library_rank orders the trait\'s gene sets within the library by beta_uncorrected. These are not the BioIndex small-model values.'))
    b.add('TraitGeneSets', obj({'trait_id': null(kpn), 'gene_set_id': null(b.did('GeneSet')), 'library': null(string(maxLength=64)),
        'sort': b.enum(*BETA_SORTS), 'items': array(ref('TraitGeneSet')), **page},
        description='Bounded page of gene-set betas for one trait (trait_id) or of one gene set across traits (gene_set_id). Sorted descending by sort with zero values last (below the few negative corrected betas), ties by library_rank then gene_set_id, before pagination. Only gene sets PIGEAN kept have rows: an absent pair is not a zero beta, and some kept rows are exactly 0.'))
    b.add('FactorTraitLink', obj({'source_id': string(), 'factor_label': string(), 'fitted_kpn_trait_id': kpn, 'fitted_trait_name': string(),
        'phenotype': string(), 'phenotype_kpn_trait_id': null(kpn), 'phenotype_name': string(), 'is_own_trait': boolean, 'is_atlas_trait': boolean,
        'nnls_loading': number, 'phewas_beta': number, 'phewas_p_onesided': probability, 'phewas_q': probability},
        description='A factor linked to a PIGEAN phenotype at BH q <= 0.05. nnls_loading is the factor\'s non-negative least-squares loading in eaggl\'s trait linkage; phewas_beta, phewas_p_onesided and phewas_q are the factor-PheWAS effect, its one-sided p value and its Benjamini-Hochberg q over all factor x phenotype tests. is_own_trait marks the phenotype the factor was fitted on; is_atlas_trait a phenotype that is an atlas trait. phenotype_name is the display name; phenotypes without a KPN id have a null phenotype_kpn_trait_id.'))
    b.add('FactorTraitLinks', obj({'source_id': null(string()), 'trait_id': null(kpn), 'scope': b.enum('atlas', 'all'), 'sort': b.enum(*LINK_SORTS),
        'summary': null(obj({'n_phenotypes': count, 'n_linked': count, 'n_linked_kpn_traits': count, 'n_linked_atlas_traits': count,
            'own_trait_q': null(probability)})), 'items': array(ref('FactorTraitLink')), **page},
        description='Bounded page of the phenotypes linked to one factor (source_id), or of the factors linked to one trait (trait_id; the trait\'s own factors are flagged is_own_trait). scope=atlas keeps links whose phenotype is an atlas trait. phewas_q sorts ascending, phewas_beta and nnls_loading descending, ties by phenotype then factor, before pagination. summary (source_id only) is the factor\'s linkage over every phenotype tested: own_trait_q is null when its own trait was not tested.'))
    b.add('MechanismObject', obj({'dapper_id': b.did('Mechanism'), 'status': {'type': 'string', 'const': 'current'}, 'source_id': string(),
        'release_id': release}, description='The served factor whose minted DAPPER Mechanism this id is. Resolve the factor at /v1/mechanisms/{source_id}.'))


def _requests(b, op, cases):
    """One request exchange per case {case: (query, response example names)}: the alternative selectors of a listing (trait or gene
    set, factor or trait) are never sent together, which an exchange built from every parameter example would do."""
    template = next(x for x in b.EXCHANGES if x['operation_id'] == op['operationId'])
    at = b.EXCHANGES.index(template)
    exchanges, op['x-codeSamples'] = [], []
    for case, (query, names) in cases.items():
        ex = deepcopy(template); ex['case'] = case; ex['request']['query'] = query
        ex['request']['url'] = b.BASE + ex['path_template'] + '?' + urlencode(query)
        ex['curl'] = ' '.join(['curl', '-X', ex['method'], shlex.quote(ex['request']['url'])]
                              + [piece for k, v in ex['request']['headers'].items() for piece in ('-H', shlex.quote(k + ': ' + v))])
        ex['responses'] = {'200': {'content_type': 'application/json', 'examples': {name: ex['responses']['200']['examples'][name] for name in names}}}
        op['x-codeSamples'].append({'lang': 'Shell', 'label': case, 'source': ex['curl']})
        exchanges.append(ex)
    b.EXCHANGES[at:at + 1] = exchanges


def endpoints(b, e):
    from reveal_backend.trait_details import BETA_COVERAGE, BETA_SORTS, KPN_URL, LINK_COVERAGE, LINK_SORTS, PORTAL_URL
    from openapi_current import KPN_EXAMPLE
    string, param = b.string, b.parameter
    factor, archived = e['kpn_factor'], e['reference']['archived_factor']
    release, trait = factor['reference_generation_id'], KPN_EXAMPLE['trait']
    kpn = string(pattern=KPN_PATTERN)
    illustrative = ' Examples are illustrative: no release build, linkage or portal read was performed for them.'
    sets = [('dapper:GeneSet.' + c * 32, name, library, 'dapper:GeneSetCollection.' + d * 32, size, values, rank)
            for c, name, library, d, size, values, rank in [('a', 'Example imported gene set', 'GTEx', 'c', 120, (.031, .027, .62), 1),
                                                             ('b', 'Second example gene set', 'MoTrPAC', 'd', 85, (.012, .0, .18), 1)]]
    rows = [{'kpn_trait_id': trait['id'], 'trait_name': trait['name'], 'gene_set_id': identity, 'gene_set_name': name, 'library': library,
             'collection_id': collection, 'n_genes': size, 'beta_uncorrected': values[0], 'beta': values[1], 'avg_postp': values[2], 'library_rank': rank}
            for identity, name, library, collection, size, values, rank in sets]
    artifacts = [{'ancestry_code': code, 'ancestry_name': name, 'artifact_id': identity, 'href': PORTAL_URL + '/artifact/' + quote(identity, safe='')}
                 for code, name, identity in [('EU', 'European', 'bl-t2d-eu'), ('Mixed', 'Mixed ancestry', 'bl-t2d-mixed')]]
    atlas = {'kpn_trait_id': trait['id'], 'phenotype': trait['legacy_phenotype_id'], 'name': trait['name'], 'category': KPN_EXAMPLE['gwas_source_category'],
             'is_atlas': True, 'legacy_phenotype_id': trait['legacy_phenotype_id'], 'trait_group': trait['trait_group'], 'trait_type': trait['trait_type'],
             'description': None, 'kpn_url': KPN_URL.format(trait['id'].split(':')[1]),
             'gwas': {'status': 'available', 'portal_trait_id': trait['legacy_phenotype_id'], 'gwas_artifact_id': 'bl-t2d-mixed',
                      'portal_href': PORTAL_URL + '/bottom_line/' + quote(trait['legacy_phenotype_id'], safe=''), 'artifacts': artifacts},
             'factors': [{'source_id': factor['source_id'], 'label': KPN_EXAMPLE['label'], 'factor_number': 1}],
             'gene_set_libraries': [{'library': 'GTEx', 'n_gene_sets': 140, 'n_nonzero': 112, 'top_gene_sets': deepcopy(rows[:1])},
                                    {'library': 'MoTrPAC', 'n_gene_sets': 64, 'n_nonzero': 41, 'top_gene_sets': deepcopy(rows[1:])}],
             'linked_factors': {'n_factors': 37}, 'release_id': release}
    linked = {'kpn_trait_id': 'KPN.TRAIT:0001360', 'phenotype': 'gcat_trait_illustrative_phenotype', 'name': 'illustrative phenotype', 'category': None,
              'is_atlas': False, 'legacy_phenotype_id': None, 'trait_group': None, 'trait_type': None, 'description': None,
              'kpn_url': KPN_URL.format('0001360'), 'gwas': {'status': 'not_on_portal', 'portal_trait_id': None, 'gwas_artifact_id': None,
              'portal_href': None, 'artifacts': []}, 'factors': [], 'gene_set_libraries': [], 'linked_factors': {'n_factors': 1}, 'release_id': release}
    b.operation('/v1/traits/{kpn_trait_id}', 'get', 'getTrait', 'Mechanisms', 'Inspect a reference trait',
        'Public reference data of the published release: an atlas trait with its fitted factors, per-library gene-set beta counts and linked factors of other traits, or a non-atlas phenotype the factor-trait linkage reached (is_atlas false). GWAS links point to the Translator provenance portal and are built from stored ids; the portal is never called. Unknown or malformed ids return 404.' + illustrative,
        'TraitDetail', {'atlas_trait': atlas, 'linked_phenotype': linked},
        parameters=[param('kpn_trait_id', 'path', kpn, trait['id'], True)], public=True, errors=('404', '503'))
    page = [param('limit', 'query', {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 50}, 50),
            param('offset', 'query', {'type': 'integer', 'minimum': 0, 'default': 0}, 0)]
    betas = lambda **values: {'trait_id': None, 'gene_set_id': None, 'library': None, 'sort': 'beta_uncorrected', 'total': len(values['items']),
                              'next_offset': None, 'coverage': BETA_COVERAGE, 'release_id': release, **values}
    op = b.operation('/v1/trait-gene-sets', 'get', 'listTraitGeneSets', 'Mechanisms', 'Page gene-set betas of a trait or of a gene set',
        'Give exactly one of trait_id (the trait\'s gene sets) or gene_set_id (the traits of one gene set); otherwise 422. Values are the release\'s PIGEAN betas mode, distinct from the BioIndex small-model values. A release without betas returns 404.' + illustrative,
        'TraitGeneSets', {'trait': betas(trait_id=trait['id'], items=deepcopy(rows)), 'gene_set': betas(gene_set_id=rows[0]['gene_set_id'], items=deepcopy(rows[:1]))},
        parameters=[param('trait_id', 'query', kpn, trait['id'], description='A KPN trait id: list its gene sets. Exclusive with gene_set_id.'),
            param('gene_set_id', 'query', b.did('GeneSet'), rows[0]['gene_set_id'], description='A gene set: list the traits it has a beta for. Exclusive with trait_id.'),
            param('library', 'query', string(minLength=1, maxLength=64), 'GTEx', description='Keep one CFDE library.'),
            param('sort', 'query', b.enum(*BETA_SORTS, default='beta_uncorrected'), 'beta_uncorrected', description='Descending with zero values last; ties by library_rank, then gene_set_id.'),
            *page], public=True, errors=('404', '422', '503'))
    _requests(b, op, {'trait': ({'trait_id': trait['id'], 'sort': 'beta_uncorrected', 'limit': 50, 'offset': 0}, ['trait']),
                      'gene_set': ({'gene_set_id': rows[0]['gene_set_id'], 'limit': 50, 'offset': 0}, ['gene_set'])})
    link = {'source_id': factor['source_id'], 'factor_label': KPN_EXAMPLE['label'], 'fitted_kpn_trait_id': trait['id'], 'fitted_trait_name': trait['name'],
            'phenotype': 'gcat_trait_illustrative_phenotype', 'phenotype_kpn_trait_id': 'KPN.TRAIT:0001360', 'phenotype_name': 'illustrative phenotype',
            'is_own_trait': False, 'is_atlas_trait': False, 'nnls_loading': .042, 'phewas_beta': .31, 'phewas_p_onesided': 2.1e-07, 'phewas_q': 3.4e-05}
    own = {**link, 'phenotype': trait['legacy_phenotype_id'], 'phenotype_kpn_trait_id': trait['id'], 'phenotype_name': trait['name'],
           'is_own_trait': True, 'is_atlas_trait': True, 'nnls_loading': .18, 'phewas_beta': .52, 'phewas_p_onesided': 1e-12, 'phewas_q': 4.6e-10}
    links = lambda **values: {'source_id': None, 'trait_id': None, 'scope': 'all', 'sort': 'phewas_q', 'summary': None, 'total': len(values['items']),
                              'next_offset': None, 'coverage': LINK_COVERAGE, 'release_id': release, **values}
    summary = {'n_phenotypes': 6698, 'n_linked': 2, 'n_linked_kpn_traits': 2, 'n_linked_atlas_traits': 1, 'own_trait_q': own['phewas_q']}
    op = b.operation('/v1/factor-trait-links', 'get', 'listFactorTraitLinks', 'Mechanisms', 'Page factor-trait links of a factor or of a trait',
        'Give exactly one of source_id (the phenotypes a factor is linked to) or trait_id (the factors linked to a trait: the gap use case); otherwise 422. Only links at BH q <= 0.05 are stored: an absent phenotype is unlinked at this threshold, not a negative result. An unknown source_id, or a release without links, returns 404.' + illustrative,
        'FactorTraitLinks', {'factor': links(source_id=factor['source_id'], summary=summary, items=[deepcopy(own), deepcopy(link)]),
                             'trait': links(trait_id='KPN.TRAIT:0001360', items=[deepcopy(link)])},
        parameters=[param('source_id', 'query', string(minLength=1, maxLength=255), factor['source_id'], description='A served factor: list its linked phenotypes. Exclusive with trait_id.'),
            param('trait_id', 'query', kpn, 'KPN.TRAIT:0001360', description='A KPN trait id: list the factors linked to it. Exclusive with source_id.'),
            param('scope', 'query', b.enum('atlas', 'all', default='all'), 'all', description='atlas keeps links whose phenotype is an atlas trait.'),
            param('sort', 'query', b.enum(*LINK_SORTS, default='phewas_q'), 'phewas_q', description='phewas_q ascends; phewas_beta and nnls_loading descend; ties by phenotype, then factor.'),
            *page], public=True, errors=('404', '422', '503'))
    _requests(b, op, {'factor': ({'source_id': factor['source_id'], 'scope': 'all', 'sort': 'phewas_q', 'limit': 50, 'offset': 0}, ['factor']),
                      'trait': ({'trait_id': 'KPN.TRAIT:0001360', 'limit': 50, 'offset': 0}, ['trait'])})
    mechanism = factor['object']['id']
    op = b.operation('/v1/mechanism-objects/{dapper_id}', 'get', 'getMechanismObject', 'Mechanisms', 'Resolve a minted Mechanism id to its factor',
        'A DAPPER Mechanism id the catalog minted for a served factor resolves to that factor\'s source_id. A Mechanism of a factor a superseded release served returns 410 REFERENCE_GENERATION_SUPERSEDED with the frozen archived_reference_factor; other ids return 404. The example factor is from an illustrative eaggl-capped-v1 release.',
        'MechanismObject', {'current': {'dapper_id': mechanism, 'status': 'current', 'source_id': factor['source_id'], 'release_id': release}},
        parameters=[param('dapper_id', 'path', b.did('Mechanism'), mechanism, True)], public=True, errors=('404', '503'))
    op['responses']['410'] = {'description': 'Reference Generation Superseded', 'content': b.content('Problem', {'reference_generation_superseded':
        b.problem(410, 'REFERENCE_GENERATION_SUPERSEDED', 'This Mechanism names a factor the published reference release no longer serves.',
                  archived_reference_factor=archived)}, 'application/problem+json')}
    op['responses'] = dict(sorted(op['responses'].items()))


def extend(b, f, e):
    schemas(b)
    endpoints(b, e)
