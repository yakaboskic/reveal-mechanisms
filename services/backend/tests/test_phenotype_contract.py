"""Each small-model reader advertises one bounded argument contract everywhere."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import Draft202012Validator
import pytest

from reveal_backend import research_public, research_tools, user_inputs
from reveal_backend.auth import Problem
from reveal_backend.box_research import ResearchAccessError
from reveal_backend.evidence_package import canonical_json
from reveal_backend.repository import Repository
from reveal_backend.research_data import SmallModelBioIndex
from reveal_backend.research_work import ResearchWorkService, issue_grant
from test_box_research import hosted
import test_research_data as data_fixtures

GEN = data_fixtures.GEN


NAMES = {'get_pigean_gene_phenotype', 'get_pigean_gene_set_phenotype'}
INVALID = [{}, {'gene': 'VEGFA', 'phenotype': '0000213'},
    {'phenotype_id': '0000213', 'gene': 'VEGFA'},
    {'phenotype_id': '0000213', 'gene_set': 'SIGNATURE'},
    {'phenotype_id': '0000213', 'model': 'large'}, {'phenotype_id': '0000213', 'sigma': 1},
    {'phenotype_id': '0000213', 'limit': 0}, {'phenotype_id': '0000213', 'limit': 501}, {'phenotype_id': '0000213', 'limit': '5'},
    {'phenotype_id': '0000213', 'cursor': 'next'},
    {'phenotype_id': 'x' * 201}, {'phenotype_id': ''}, {'phenotype_id': '  '},
    {'phenotype_id': '0000213,2,large'}, {'phenotype_id': '0000213\n'},
    {'genes': []}, {'genes': 'INS'}, {'genes': ['INS', 'INS']}, {'genes': ['A', 'B', 'C', 'D', 'E', 'F']},
    {'genes': ['IN,S']}, {'genes': ['INS\n']}, {'genes': ['ENSEMBL:ENSG1']}, {'genes': ['INS'], 'gene_set': 'S'}]
VALID = {'get_pigean_gene_phenotype': [{'phenotype_id': '0000213'}, {'phenotype_id': 'x' * 200}, {'phenotype_id': 'T2D', 'limit': 500},
             {'genes': ['INS']}, {'phenotype_id': 'KPN.TRAIT:0000398', 'genes': ['HGNC.SYMBOL:INS', 'C10orf71'], 'limit': 1}],
         'get_pigean_gene_set_phenotype': [{'phenotype_id': '0000213'}, {'phenotype_id': 'x' * 200}, {'phenotype_id': 'T2D', 'limit': 1}]}
GENE_ONLY = [{'genes': ['INS']}, {'phenotype_id': 'T2D', 'genes': ['INS']}]


@pytest.fixture
def scoped(tmp_path, monkeypatch):
    reference = data_fixtures.ReferenceQueryTests(); reference.setUp()
    repo = Repository(str(tmp_path / 'app.sqlite')); repo.migrate()
    monkeypatch.setattr(user_inputs, 'artifacts_root', lambda: tmp_path / 'artifacts')
    monkeypatch.setattr(user_inputs, 's3_enabled', lambda: False)
    monkeypatch.setenv('REVEAL_SMALL_PHENOTYPE_VERIFIED', 'false')
    monkeypatch.setenv('REVEAL_PUBLIC_READS_PER_MINUTE', '600')
    work = {'id': 'fixture-work', 'research_request_id': 'fixture-request', 'state': 'ready',
            'reference_generation_id': GEN, 'expires_at': '2999-01-01T00:00:00Z'}
    with repo.transaction() as tx:
        tx.put('principal', 'owner', 'owner', {'me': {'user_id': 'owner', 'principal_kind': 'registered'}})
        tx.put('local_work', work['id'], 'owner', work)
        tx.put('request', work['research_request_id'], 'owner', {'id': work['research_request_id'], 'composer': {}})
        grant = issue_grant(tx, 'owner', work['id'], 'fixture')
    try:
        yield SimpleNamespace(service=ResearchWorkService(repo, data_service=reference.service), repo=repo,
                              work=work, authorization='Bearer ' + grant['token'])
    finally:
        reference.doCleanups()


def test_public_private_and_hosted_catalogs_share_exact_phenotype_schema(hosted):
    private = {item['name']: item for item in research_tools.private_definitions()}
    public = {item['name']: item for item in research_public.definitions()}
    combined = {item['name']: item for item in research_tools.definitions()}
    proxy, state, _ = hosted
    advertised = {item['name']: item for item in proxy.definitions()}
    assert set(SmallModelBioIndex.INDEXES) == NAMES
    assert SmallModelBioIndex.arguments_schema('get_pigean_gene_phenotype') != SmallModelBioIndex.arguments_schema('get_pigean_gene_set_phenotype')
    for name in NAMES:
        expected = SmallModelBioIndex.arguments_schema(name)
        for contract in (private, public, combined, advertised):
            assert contract[name]['inputSchema']['properties']['arguments'] == expected
        validator = Draft202012Validator(expected)
        for valid in VALID[name]:
            assert validator.is_valid(valid), valid
        for invalid in INVALID + (GENE_ONLY if name == 'get_pigean_gene_set_phenotype' else []):
            assert not validator.is_valid(invalid)
            before = deepcopy(state['calls'])
            with pytest.raises(ResearchAccessError):
                proxy.call(name, {'arguments': invalid})
            assert state['calls'] == before, 'Invalid hosted inputs must fail before a scientific query executes'
        proxy.call(name, {'arguments': {'phenotype_id': '0000213'}})
        called = next(args for tool, args in state['calls'] if tool == name)
        assert called['arguments'] == {'phenotype_id': '0000213'}
        assert called['research_request_id'] == 'request'
        assert called['idempotency_key'].startswith('hosted-')


def test_describe_and_list_report_same_schema_and_effective_gate(scoped, monkeypatch):
    with patch.object(SmallModelBioIndex, '_fetch', side_effect=AssertionError('No upstream call')), \
            patch.object(SmallModelBioIndex, 'verify', side_effect=AssertionError('No deployment probe')):
        for gate in ('false', 'true'):
            monkeypatch.setenv('REVEAL_SMALL_PHENOTYPE_VERIFIED', gate)
            private = research_tools.dispatch(scoped.service, scoped.authorization, 'list_data_operations',
                {'research_request_id': scoped.work['research_request_id']})
            assert private['phenotype_source']['arguments'] == {name: SmallModelBioIndex.arguments_schema(name) for name in NAMES}
            for name in NAMES:
                owner = research_tools.dispatch(scoped.service, scoped.authorization, 'describe_data_operation',
                    {'research_request_id': scoped.work['research_request_id'], 'operation_id': name})
                public = research_public.public_dispatch(scoped.service, 'describe_data_operation',
                    {'reference_generation_id': GEN, 'operation_id': name})
                assert owner == public
                assert owner['arguments'] == SmallModelBioIndex.arguments_schema(name)
                assert owner['deployment_verified'] == (gate == 'true')
                assert (owner['model'], owner['sigma']) == ('small', 2)
                assert set(owner['operations']) == NAMES


def test_named_queries_reject_invalid_arguments_before_enqueue_or_upstream(scoped):
    with patch.object(SmallModelBioIndex, '_fetch', side_effect=AssertionError('No upstream call')), \
            patch.object(SmallModelBioIndex, 'verify', side_effect=AssertionError('No deployment probe')):
        for name in NAMES:
            for invalid in INVALID + (GENE_ONLY if name == 'get_pigean_gene_set_phenotype' else []):
                for auth, args in ((scoped.authorization, {'research_request_id': scoped.work['research_request_id'],
                                       'idempotency_key': 'invalid', 'arguments': invalid}),
                                   (None, {'reference_generation_id': GEN, 'arguments': invalid})):
                    with pytest.raises(Problem) as error:
                        research_tools.dispatch(scoped.service, auth, name, args)
                    assert (error.value.status, error.value.code) == (422, 'INVALID_ARGUMENTS')
        with scoped.repo.read_transaction() as tx:
            assert tx.list('research_operation') == []
            assert tx.list('public_capture') == []
        for name, arguments in [(name, {'phenotype_id': '0000213'}) for name in sorted(NAMES)] + [
                ('get_pigean_gene_phenotype', {'genes': ['INS'], 'limit': 5})]:
            queued = research_tools.dispatch(scoped.service, scoped.authorization, name,
                {'research_request_id': scoped.work['research_request_id'], 'idempotency_key': name + str(len(arguments)),
                 'arguments': arguments})
            scoped.service.run_operation(queued['operation_id'])
            with scoped.repo.read_transaction() as tx:
                operation = tx.get('research_operation', queued['operation_id'])['data']
            assert operation['state'] == 'succeeded', operation
            assert operation['result']['result']['status'] == 'source_unavailable'
            assert operation['result']['source']['model'] == 'small'
            assert operation['result']['source']['sigma'] == 2


def test_verified_gene_queries_resolve_the_pinned_trait_on_owner_and_public_paths(scoped, monkeypatch):
    monkeypatch.setenv('REVEAL_SMALL_PHENOTYPE_VERIFIED', 'true')
    page = {'index': 'pigean-gene', 'q': ['portal', 'INS', '2', 'small'], 'continuation': None,
            'progress': {'bytes_read': 1, 'bytes_total': 1},
            'data': [{'phenotype': 'T2D', 'gene': 'INS', 'trait_group': 'portal', 'gene_set_size': 'small', 'sigma': 2, 'combined': 12.3},
                     {'phenotype': 'BMI', 'gene': 'INS', 'trait_group': 'portal', 'gene_set_size': 'small', 'sigma': 2, 'combined': 1.1}]}
    urls = []
    def fetch(url, max_bytes, timeout):
        urls.append(url)
        return canonical_json(page)
    arguments = {'phenotype_id': 'KPN.TRAIT:0000398', 'genes': ['INS']}
    with patch.object(SmallModelBioIndex, '_fetch', side_effect=fetch), \
            patch('reveal_backend.acceptance.public_runtime', return_value=data_fixtures.SeedTests.runtime()):
        queued = research_tools.dispatch(scoped.service, scoped.authorization, 'get_pigean_gene_phenotype',
            {'research_request_id': scoped.work['research_request_id'], 'idempotency_key': 'trait', 'arguments': arguments})
        scoped.service.run_operation(queued['operation_id'])
        public = research_tools.dispatch(scoped.service, None, 'get_pigean_gene_phenotype',
            {'reference_generation_id': GEN, 'arguments': arguments})
    with scoped.repo.read_transaction() as tx:
        operation = tx.get('research_operation', queued['operation_id'])['data']
    assert operation['state'] == 'succeeded', operation
    for captured in (operation['result'], public):
        assert captured['result']['status'] == 'complete'
        assert [(row['phenotype'], row['combined']) for row in captured['result']['items']] == [('T2D', 12.3)]
        assert captured['source']['trait']['kpn_trait_id'] == 'KPN.TRAIT:0000398'
        assert (captured['source']['phenotype'], captured['source']['trait']['trait_group']) == ('T2D', 'portal')
    assert len(urls) == 2 and all('/api/bio/query/pigean-gene?q=portal%2CINS%2C2%2Csmall' in url and 'limit' not in url for url in urls)
