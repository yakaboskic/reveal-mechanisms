"""Bounded audit observations retain exact locators without promoting hypotheses."""
from copy import deepcopy

import pytest
from jsonschema import ValidationError, validate

from reveal_backend.auth import Problem
from reveal_backend import lightning_payload as payload


def source_state():
    return {'gap': {'id': 'dapper:KnowledgeGap.' + 'a' * 32, 'text': {'text': 'Could secretion explain this gap?'},
            'rationale': {'text': 'Unresolved mechanism'}, 'disease': 'Fixture disease',
            'source': {'source_id': 'dismech:gap:1', 'source_revision': 'b' * 64}},
        'source_pins': {'generation_id': 'a' * 64, 'model': 'fixture'},
        'dismech': {'mechanisms': [{'name': 'Recorded mechanism', 'description': {'text': 'A source observation'},
            'qualifications': {'notes': 'Mouse only; not established in humans.'},
            'evidence': [{'reference': 'PMID:fixture', 'supports': 'PARTIAL', 'snippet': 'A bounded source observation.'}],
            'source': {'source_id': 'dismech:mechanism:1'}}]},
        'factors': [{'id': 'factor:1', 'label': 'Secretion', 'source_revision': 'c' * 64,
            'trait': {'name': 'Disease'}, 'genes': {'columns': ['symbol', 'gene_index', 'loading'],
                'rows': [['GENE_A', 7, .3]], 'source': {'table': 'gene_loadings'}, 'has_more': True},
            'gene_sets': {'columns': ['gene_set_index', 'joint_loading', 'marginal_loading', 'joint_loading_text', 'marginal_loading_text'],
                'rows': [[0, .4, .2, '0.4000000000000000000000001', '0.2'], [1, .1, None, '0.1', None]],
                'source': {'table': 'gene_set_loadings'}, 'has_more': True}}],
        'gene_sets': {'columns': ['id', 'name', 'collection_id', 'library', 'n_genes', 'context'],
            'rows': [['set:human', 'Human signature', 'collection:1', 'fixture', 20, {'species': 'human', 'perturbation_target': 'GENE_B'}],
                     ['set:mouse', 'Mouse signature', 'collection:1', 'fixture', 10, {'species': 'mouse'}]]},
        'collections': {'collection:1': {'context': {'name': 'Public signatures', 'species': 'human'}}},
        'user_inputs': {'research_direction': {'text': 'Investigate secretion'}, 'context': {'text': 'Researcher hypothesis'},
            'hypotheses': {'text': ''}, 'uploads': [{'id': 'upload:1', 'original_sha256': 'd' * 64,
                'extraction_sha256': 'e' * 64, 'excerpt_chars': 16, 'total_chars': 100,
                'segments': [{'text': 'An uploaded note', 'locator': {'page': 2}, 'pointer': '/segments/1/text'}]}]},
        'coverage': {'factor_count': 1, 'gene_loading_count': 1, 'gene_set_loading_count': 2, 'unique_gene_set_count': 2,
            'missing': ['set:mouse:marginal_loading'], 'truncations': [{'source': 'upload:1', 'included_chars': 16, 'total_chars': 100}],
            'complete': False}}


def audit_result(ref='E4'):
    return {'assessment': 'partial', 'summary': 'A secretion direction merits investigation.',
        'observations': [{'text': 'The retained factor contains a loading for GENE_A.', 'evidence_refs': [ref]}],
        'recommended_direction': 'Inspect the selected secretion factor and confirm species-qualified gene identities.',
        'missing_evidence': ['GeneSet membership was not supplied.'], 'next_steps': ['Inspect exact GeneSet members.'],
        'limitations': ['Top loadings are a bounded sample.']}


def test_projection_preserves_exact_sources_precision_species_and_coverage():
    state = source_state(); original = deepcopy(state)
    projected, references = payload.project(state)
    assert state == original
    assert all(payload.pointer_value(state, ref['pointer']) == ref['value'] for ref in references)
    rows = projected['eaggl_mechanisms'][0]['top_gene_sets']['rows']
    assert rows[0][2] == '0.4000000000000000000000001'
    assert {source['species'] for source in projected['gene_set_sources']} == {'human', 'mouse'}
    assert rows[0][1] != rows[1][1]
    assert projected['coverage'] == state['coverage']
    mechanism = projected['dismech_mechanisms'][0]
    assert mechanism['qualifications']['notes'] == 'Mouse only; not established in humans.'
    assert mechanism['source_evidence_excerpts'] == state['dismech']['mechanisms'][0]['evidence']
    gene = next(ref for ref in references if ref['pointer'] == '/factors/0/genes/rows/0')
    assert gene['value'] == ['GENE_A', 7, .3]
    assert gene['source']['columns'] == ['symbol', 'gene_index', 'loading']
    assert 'not independently verified' in payload.SYSTEM
    assert 'Co-loading does not establish GeneSet membership' in payload.SYSTEM


def test_upload_and_researcher_references_stay_distinct_and_addressable():
    state = source_state(); projected, references = payload.project(state)
    by_id = {ref['id']: ref for ref in references}
    context = by_id[projected['additional_context']['context']['evidence_ref']]
    assert context['source']['kind'] == 'researcher_assertion'
    attachment = by_id[projected['attachments'][0]['segments'][0]['evidence_ref']]
    assert attachment['source']['kind'] == 'researcher_attachment'
    assert attachment['source']['locator'] == {'page': 2}
    assert attachment['source']['extraction_pointer'] == '/segments/1/text'
    assert attachment['source']['extraction_sha256'] == 'e' * 64
    assert len({ref['id'] for ref in references}) == len(references)


@pytest.mark.parametrize(('change', 'code'), [
    ('unknown', 'LIGHTNING_REFERENCES_INVALID'), ('empty', 'LIGHTNING_RESPONSE_INVALID'),
    ('duplicate', 'LIGHTNING_REFERENCES_INVALID'), ('huge', 'LIGHTNING_RESPONSE_INVALID'),
    ('extra', 'LIGHTNING_RESPONSE_INVALID'), ('empty_summary', 'LIGHTNING_RESPONSE_EMPTY'),
])
def test_invalid_model_content_is_rejected(change, code):
    _, references = payload.project(source_state()); result = audit_result()
    if change == 'unknown': result['observations'][0]['evidence_refs'] = ['invented-ref']
    if change == 'empty': result['observations'][0]['evidence_refs'] = []
    if change == 'duplicate': result['observations'][0]['evidence_refs'] *= 2
    if change == 'huge': result['recommended_direction'] = 'x' * 8001
    if change == 'extra': result['scientific_accounts'] = []
    if change == 'empty_summary': result['summary'] = ' '
    with pytest.raises(Problem) as error:
        payload.validate_result(result, references)
    assert error.value.code == code


def test_unsupported_audit_can_have_no_observations():
    result = audit_result(); result.update(assessment='unsupported', observations=[])
    assert payload.validate_result(result, []) == result


def test_observed_empty_provider_shell_cannot_pass_validation():
    result = {'assessment': 'partial', 'limitations': [], 'missing_evidence': [], 'next_steps': [],
        'observations': [], 'recommended_direction': '', 'summary': ''}
    with pytest.raises(ValidationError): validate(result, payload.RESULT_SCHEMA)
    with pytest.raises(Problem) as error: payload.validate_result(result, [])
    assert error.value.code == 'LIGHTNING_RESPONSE_EMPTY'
    assert 'without a rationale or research direction' in error.value.detail
    assert 'references' not in error.value.detail


@pytest.mark.parametrize('field', ['summary', 'recommended_direction'])
@pytest.mark.parametrize('empty', ['', '   ', '\n\t'])
def test_lightweight_provider_schema_keeps_strict_local_nonblank_checks(field, empty):
    result = audit_result(); result[field] = empty
    validate(result, payload.RESULT_SCHEMA)  # The provider grammar intentionally avoids string patterns.
    with pytest.raises(Problem) as error: payload.validate_result(result, [])
    assert error.value.code == 'LIGHTNING_RESPONSE_EMPTY'


@pytest.mark.parametrize('field', ['next_steps', 'limitations'])
def test_provider_schema_requires_useful_next_checks_and_scope(field):
    result = audit_result(); result[field] = []
    with pytest.raises(ValidationError): validate(result, payload.RESULT_SCHEMA)


@pytest.mark.parametrize('assessment', ['partial', 'promising'])
def test_supported_direction_requires_referenced_observations(assessment):
    result = audit_result(); result.update(assessment=assessment, observations=[])
    with pytest.raises(Problem) as error: payload.validate_result(result, [])
    assert error.value.code == 'LIGHTNING_INCOMPLETE'


def test_populated_scientific_rationale_preserves_exact_evidence_references():
    projected, references = payload.project(source_state())
    gene_ref = projected['eaggl_mechanisms'][0]['top_genes']['rows'][0][-1]
    mechanism_ref = projected['dismech_mechanisms'][0]['evidence_ref']
    result = audit_result(gene_ref)
    result['summary'] = ('The selected secretion factor offers a starting point because its retained GENE_A '
        'loading can be inspected alongside the recorded DisMech mechanism. This is a proposed connection, '
        'not evidence that secretion explains the disease.\n\n'
        'The DisMech qualification is mouse-only, while the package includes human and mouse signatures. '
        'The immediate research question is whether that mechanism and the factor overlap in an appropriate '
        'species and context; the supplied rows do not establish GeneSet membership.')
    result['observations'].append({'text': 'The DisMech mechanism is qualified as mouse-only, limiting direct '
        'translation to a human disease explanation.', 'evidence_refs': [mechanism_ref]})
    assert payload.validate_result(result, references) == result
    for observation in result['observations']:
        assert set(observation['evidence_refs']) <= {ref['id'] for ref in references}


def test_provider_schema_omits_expensive_string_constraints():
    def inspect(node):
        if isinstance(node, dict):
            assert not {'pattern', 'minLength', 'maxLength'} & node.keys()
            for child in node.values(): inspect(child)
        elif isinstance(node, list):
            for child in node: inspect(child)
    inspect(payload.RESULT_SCHEMA)


@pytest.mark.parametrize('field', ['observation', 'missing_evidence', 'next_steps', 'limitations'])
def test_local_validation_rejects_whitespace_content_beyond_required_prose(field):
    _, references = payload.project(source_state()); result = audit_result()
    if field == 'observation': result['observations'][0]['text'] = ' \n\t'
    else: result[field] = [' \n\t']
    validate(result, payload.RESULT_SCHEMA)
    with pytest.raises(Problem) as error: payload.validate_result(result, references)
    assert error.value.code == 'LIGHTNING_RESPONSE_INVALID'
