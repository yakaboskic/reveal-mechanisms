"""Collection lineage: producer + curated provenance, and one gene set's bounded closure (no I/O)."""
from reveal_backend.account_provenance import ORG_ROLES
from reveal_backend.provenance_schema import PROVENANCE_GROUPS
from reveal_backend.reference_lineage import GROUP_THRESHOLD, ORG_FIELDS, collection_lineage, lineage_closure
from reveal_backend.repository import canonical


def ref(cls, name): return f'dapper:{cls}.{name:x<32}'


def used(activity, entity, role='data_input'): return {'subject': activity, 'predicate': 'prov:used', 'object': entity, 'edge_role': role}
def generated(entity, activity): return {'subject': entity, 'predicate': 'prov:wasGeneratedBy', 'object': activity}
def derived(entity, source): return {'subject': entity, 'predicate': 'prov:wasDerivedFrom', 'object': source}


GENE_SET = ref('GeneSet', 'gs')
EXTRACT, PREPARE, EMBED = ref('Activity', 'extract'), ref('Activity', 'prepare'), ref('Activity', 'embed')
DEG, GMT, VECTORS = ref('File', 'deg'), ref('C2M2File', 'gmt'), ref('File', 'vectors')
READS, SAMPLES, SUBJECTS, GENE_INFO = (ref('File', name) for name in ('reads', 'samples', 'subjects', 'geneinfo'))
GTEX, NCBI = ref('Dataset', 'gtex'), ref('Dataset', 'ncbi')
BROAD, CONSORTIUM, NCBI_ORG = ref('Organization', 'broad'), ref('Organization', 'gtexc'), ref('Organization', 'ncbi')


def gtex_like():
    """A GTEx HZ1 document's provenance: the gene sets' extractor used a DEG table that a prepare step made from three GTEx
    files and an NCBI one; the collection's GMT and its embedding are downstream."""
    return {
        'prefixes': {'HGNC.SYMBOL': 'https://identifiers.org/hgnc.symbol:'},
        'organizations': [{'id': BROAD, 'name': 'Broad Institute'}, {'id': CONSORTIUM, 'name': 'GTEx Consortium'}, {'id': NCBI_ORG, 'name': 'NCBI'}],
        'datasets': [{'id': GTEX, 'name': 'GTEx v8', 'publisher': BROAD, 'was_attributed_to': [CONSORTIUM]}, {'id': NCBI, 'name': 'gene_info', 'publisher': NCBI_ORG}],
        'files': [{'id': node, 'name': node[12:16]} for node in (DEG, VECTORS, READS, SAMPLES, SUBJECTS, GENE_INFO)],
        'c2m2_files': [{'id': GMT, 'name': 'genesets.gmt'}],
        'activities': [{'id': node, 'name': node[16:23]} for node in (EXTRACT, PREPARE, EMBED)],
        'used_edges': [used(PREPARE, READS), used(PREPARE, SAMPLES, 'metadata_input'), used(PREPARE, SUBJECTS, 'metadata_input'),
                       used(PREPARE, GENE_INFO), used(EXTRACT, DEG), used(EMBED, GMT)],
        'was_generated_by_edges': [generated(DEG, PREPARE), generated(GMT, EXTRACT), generated(VECTORS, EMBED)],
        'was_derived_from_edges': [derived(READS, GTEX), derived(SAMPLES, GTEX), derived(SUBJECTS, GTEX), derived(GENE_INFO, NCBI)]}


def ids(closure, group): return [node['id'] for node in closure['provenance'].get(group, [])]


def test_org_fields_are_the_dataset_organization_roles():
    assert set(ORG_FIELDS) == set(ORG_ROLES) - {'was_associated_with'} and {'publisher', 'was_attributed_to', 'has_creator', 'has_contributor'} <= set(ORG_FIELDS)


def test_collection_lineage_merges_curated_groups_under_the_producers():
    producer = {'prefixes': {'a': 'https://a/', 'b': 'https://b/'}, 'datasets': [{'id': GTEX, 'name': 'producer'}],
                'was_derived_from_edges': [derived(READS, GTEX)], 'files': [{'id': READS}]}
    curated = {'source': 'lap/config/source_datasets.yaml', 'groups': {
        'prefixes': {'b': 'https://other/', 'c': 'https://c/'}, 'datasets': [{'id': GTEX, 'name': 'curated'}, {'id': NCBI, 'name': 'new'}],
        'organizations': [{'id': BROAD}], 'was_derived_from_edges': [derived(READS, GTEX), derived(GENE_INFO, NCBI)]}}
    merged = collection_lineage({'provenance': producer, 'curated_provenance': curated})
    assert merged['prefixes'] == {'a': 'https://a/', 'b': 'https://b/', 'c': 'https://c/'}
    assert merged['datasets'] == [{'id': GTEX, 'name': 'producer'}, {'id': NCBI, 'name': 'new'}]  # the producer's node wins
    assert merged['was_derived_from_edges'] == [derived(READS, GTEX), derived(GENE_INFO, NCBI)]  # one edge per (subject, predicate, object)
    assert merged['organizations'] == [{'id': BROAD}] and merged['files'] == [{'id': READS}]
    assert list(merged) == [group for group in PROVENANCE_GROUPS if group in merged]
    assert collection_lineage({'provenance': producer}) == producer and collection_lineage({}) == {}


def test_closure_follows_generation_inputs_derivations_and_organizations_only():
    lineage = gtex_like()
    closure = lineage_closure({'id': GENE_SET, 'was_generated_by': EXTRACT}, lineage)
    assert closure['collapsed'] == []
    assert ids(closure, 'activities') == sorted([EXTRACT, PREPARE])  # not the downstream embedding
    assert ids(closure, 'files') == sorted([DEG, READS, SAMPLES, SUBJECTS, GENE_INFO]) and 'c2m2_files' not in closure['provenance']
    assert ids(closure, 'datasets') == sorted([GTEX, NCBI]) and ids(closure, 'organizations') == sorted([BROAD, CONSORTIUM, NCBI_ORG])
    provenance = closure['provenance']
    assert provenance['used_edges'] == sorted([edge for edge in lineage['used_edges'] if edge['subject'] != EMBED],
                                              key=lambda edge: (edge['subject'], edge['predicate'], edge['object']))
    assert provenance['used_edges'][0]['edge_role'] in ('data_input', 'metadata_input')  # used edges keep their role
    assert provenance['was_generated_by_edges'] == [generated(DEG, PREPARE)] and len(provenance['was_derived_from_edges']) == 4
    assert provenance['prefixes'] == lineage['prefixes'] and list(provenance) == [group for group in PROVENANCE_GROUPS if group in provenance]
    # No group exceeds the threshold (the three GTEx files are the largest): unchanged at any threshold >= 3.
    assert lineage_closure({'id': GENE_SET, 'was_generated_by': EXTRACT}, lineage, threshold=3) == closure
    collapsed = lineage_closure({'id': GENE_SET, 'was_generated_by': EXTRACT}, lineage, threshold=2)
    assert collapsed['collapsed'] == [{'activity': PREPARE, 'representative': min(READS, SAMPLES, SUBJECTS), 'collapsed_inputs': 3}]
    assert ids(collapsed, 'files') == sorted([DEG, min(READS, SAMPLES, SUBJECTS), GENE_INFO]) and ids(collapsed, 'datasets') == sorted([GTEX, NCBI])


def test_was_generated_by_may_be_a_string_a_list_or_an_edge():
    lineage = gtex_like()
    expected = lineage_closure({'id': GENE_SET, 'was_generated_by': EXTRACT}, lineage)
    assert lineage_closure({'id': GENE_SET, 'was_generated_by': [EXTRACT]}, lineage) == expected
    by_edge = lineage_closure({'id': GENE_SET}, {**lineage, 'was_generated_by_edges': lineage['was_generated_by_edges'] + [generated(GENE_SET, EXTRACT)]})
    assert by_edge['provenance']['was_generated_by_edges'] == sorted([generated(DEG, PREPARE), generated(GENE_SET, EXTRACT)], key=lambda e: e['subject'])
    assert {group: items for group, items in by_edge['provenance'].items() if group != 'was_generated_by_edges'} == \
        {group: items for group, items in expected['provenance'].items() if group != 'was_generated_by_edges'}
    assert lineage_closure({'id': GENE_SET}, lineage) == {'provenance': {'prefixes': lineage['prefixes']}, 'collapsed': []}


def test_a_glygen_hz2_like_fan_in_keeps_one_input_per_lineage_and_reaches_the_dataset():
    """6,620 cached API responses, all derived (by curated edges) from the GlyGen release, used by one extractor."""
    extract, glygen = ref('Activity', 'glygen'), ref('Dataset', 'glygen')
    gwu, consortium, sheet = ref('Organization', 'gwu'), ref('Organization', 'glygenc'), ref('C2M2File', 'sheet')
    responses = [f'dapper:File.{index:0>32}' for index in range(6620)]
    producer = {'files': [{'id': node, 'filename': f'G{index:05d}AB.json'} for index, node in enumerate(responses)],
                'c2m2_files': [{'id': sheet, 'filename': 'glycan_masterlist.csv'}], 'activities': [{'id': extract}],
                'used_edges': [used(extract, node, 'data_input') for node in responses] + [used(extract, sheet, 'metadata_input')]}
    curated = {'groups': {'organizations': [{'id': gwu, 'name': 'George Washington University'}, {'id': consortium, 'name': 'GlyGen Consortium'}],
                          'datasets': [{'id': glygen, 'name': 'GlyGen data release v1.12.1', 'publisher': gwu, 'was_attributed_to': [consortium]}],
                          'was_derived_from_edges': [derived(node, glygen) for node in responses]}}
    closure = lineage_closure({'id': GENE_SET, 'was_generated_by': extract}, collection_lineage({'provenance': producer, 'curated_provenance': curated}))
    assert closure['collapsed'] == [{'activity': extract, 'representative': responses[0], 'collapsed_inputs': 6620}]
    assert ids(closure, 'files') == [responses[0]] and ids(closure, 'c2m2_files') == [sheet]  # the unlinked sheet is its own group of one
    assert closure['provenance']['used_edges'] == [used(extract, sheet, 'metadata_input'), used(extract, responses[0])]
    assert closure['provenance']['was_derived_from_edges'] == [derived(responses[0], glygen)]
    assert ids(closure, 'datasets') == [glygen] and ids(closure, 'organizations') == sorted([gwu, consortium])
    assert len(canonical(closure)) < 4096  # far under the 1 MB capture budget
    assert GROUP_THRESHOLD == 25 and len(lineage_closure({'id': GENE_SET, 'was_generated_by': extract}, collection_lineage(
        {'provenance': producer, 'curated_provenance': curated}), threshold=10_000)['provenance']['files']) == 6620


def test_cycles_and_memberships_terminate_and_never_reach_siblings():
    activity, a, b, member, sibling, parent = (ref('Activity', 'loop'), ref('File', 'a'), ref('File', 'b'), ref('File', 'member'),
                                               ref('File', 'sibling'), ref('Dataset', 'parent'))
    lineage = {'files': [{'id': a}, {'id': b}, {'id': member}, {'id': sibling}], 'activities': [{'id': activity, 'used': [member]}],
               'datasets': [{'id': parent, 'has_file': [member, sibling]}],
               'used_edges': [used(activity, a)], 'was_generated_by_edges': [generated(a, activity)],
               'was_derived_from_edges': [derived(a, b), derived(b, a)]}
    closure = lineage_closure({'id': GENE_SET, 'was_generated_by': activity}, lineage)
    assert ids(closure, 'files') == sorted([a, b, member]) and ids(closure, 'datasets') == [parent]  # a File's Dataset, not its siblings
    assert closure['provenance']['was_derived_from_edges'] == sorted([derived(a, b), derived(b, a)], key=lambda e: e['subject'])
