"""Reading a deposited account back from the claims repository: rebuilt byte for byte from the deposit's object list,
its objects (batched by 500) and its edges in stored order, checked against the job's record, cached briefly by
deposit id. No network: FakeClaims stores and presents documents as the repository's deposit, deposit and batchGet
routes do (service/app.py, service/api_keys.py, service/objects.py)."""
import io
import json
import urllib.error
from urllib.parse import urlsplit

import pytest

from reveal_backend import account_deposit, claims_read
from reveal_backend.evidence_package import canonical_json, sha256

DEPOSIT = 'dep_' + '1' * 32


class Reply(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): return False


def edge_class(group):
    return ''.join(part[:1].upper() + part[1:] for part in group[:-len('_edges')].split('_'))


class FakeClaims:
    """The repository's side of a deposit and its reads. It stores each node without null slots or class_name, lists a
    deposit's objects by id with their class, keeps its edges in the order the document gave them, and presents an
    object with class_name added, as GET /v1/objects/{id} does."""
    def __init__(self):
        self.objects, self.deposits, self.requests = {}, {}, []
        self.down = False

    def deposit(self, data, deposit_id=DEPOSIT):
        document, ids, edges = json.loads(data), [], []
        for group, rows in document.items():
            for row in rows:
                content = {key: value for key, value in row.items() if value is not None}
                if group.endswith('_edges'):
                    edges.append({'class_name': edge_class(group), **dict(sorted(content.items()))})
                else:
                    stored = {key: value for key, value in content.items() if key != 'class_name'}
                    self.objects.setdefault(row['id'], json.loads(json.dumps(stored, sort_keys=True)))
                    ids.append(row['id'])
        self.deposits[deposit_id] = {'deposit_id': deposit_id, 'object_count': len(set(ids)), 'collection': None,
                                     'objects': [{'id': identity, 'class_name': identity.split(':')[1].split('.')[0], 'status': 'created'}
                                                 for identity in sorted(set(ids))],
                                     'edges': edges}
        return deposit_id

    def open(self, request, timeout):
        self.requests.append(request)
        if self.down:
            raise OSError('unreachable')
        path = urlsplit(request.full_url).path
        if request.get_method() == 'GET' and path.startswith('/v1/deposits/'):
            found = self.deposits.get(path.rsplit('/', 1)[1])
            if found is None:
                raise urllib.error.HTTPError(request.full_url, 404, 'Unknown deposit.', {}, io.BytesIO(b'{}'))
            return Reply(json.dumps(found).encode())
        if request.get_method() == 'POST' and path == '/v1/objects:batchGet':
            ids = json.loads(request.data)['ids']
            assert 0 < len(ids) <= 500
            present = lambda node: {'id': node['id'], 'class_name': node['id'].split(':')[1].split('.')[0],
                                    **{key: node[key] for key in sorted(node) if key != 'id'}}
            return Reply(json.dumps({'objects': {i: present(self.objects[i]) for i in ids if i in self.objects},
                                     'missing': [i for i in ids if i not in self.objects]}).encode())
        raise urllib.error.HTTPError(request.full_url, 404, 'Not Found', {}, io.BytesIO(b'{}'))


@pytest.fixture
def claims(monkeypatch):
    """A fresh cache and the fake repository behind REVEAL_CLAIMS_API_URL; the deposit key is set too, to show reads
    never send it."""
    fake = FakeClaims()
    monkeypatch.setenv('REVEAL_CLAIMS_API_URL', 'https://claims.test/'); monkeypatch.setenv('REVEAL_CLAIMS_API_KEY', 'clm_' + 'k' * 43)
    monkeypatch.delenv('REVEAL_CLAIMS_READ_CACHE_SECONDS', raising=False)
    monkeypatch.setattr(claims_read, '_cache', None)
    monkeypatch.setattr(claims_read, 'build_opener', lambda: fake)
    return fake


def minted():
    """A real account in its deposit form: the account_deposit fixture, minted with the vendored DAPPER."""
    import test_account_deposit
    case = test_account_deposit.AccountDepositTests('test_two_quotes_of_one_span_are_one_locator'); case.setUp()
    try:
        document, _ = case.deposit(case.account())
    finally:
        case.doCleanups()
    return document


def synthetic(claims=3):
    """A document of the account's shape with placeholder ids: the rebuild never recomputes ids."""
    account = 'dapper:ScientificAccount.' + 'A' * 32
    ids = [f'dapper:Claim.{index:032d}' for index in range(claims)]
    return account_deposit.layout({
        'scientific_accounts': [{'id': account, 'name': 'An account', 'component_claims': ids}],
        'claims': [{'id': identity, 'statement': f'Claim {index}.', 'score': index / 7, 'count': index} for index, identity in enumerate(ids)],
        'used_edges': [{'subject': 'dapper:Activity.' + 'B' * 32, 'predicate': 'prov:used', 'object': identity} for identity in reversed(ids)],
        'activities': [{'id': 'dapper:Activity.' + 'B' * 32, 'name': 'run', 'command': 'curl -sSL "https://example.org/é"'}]})


def test_a_deposited_account_is_rebuilt_byte_for_byte_and_no_key_is_sent(claims):
    document = minted()
    assert account_deposit.layout(document) == document          # the deposit form is already in the one layout
    data = canonical_json(document)
    claims.deposit(data)
    served = claims_read.account_document(DEPOSIT, document['scientific_accounts'][0]['id'], sha256(data))
    assert canonical_json(served) == data and served == document
    assert [request.get_method() for request in claims.requests] == ['GET', 'POST']
    assert claims.requests[0].full_url == f'https://claims.test/v1/deposits/{DEPOSIT}'
    assert all(request.get_header('X-api-key') is None for request in claims.requests)


def test_a_cached_deposit_is_not_fetched_again_but_is_still_checked(claims):
    document = synthetic(); data = canonical_json(document); claims.deposit(data)
    account = document['scientific_accounts'][0]['id']
    claims_read.account_document(DEPOSIT, account, sha256(data))
    fetched = len(claims.requests)
    assert claims_read.account_document(DEPOSIT, account, sha256(data)) == document and len(claims.requests) == fetched
    # A cached document is checked against each job's record on every read.
    with pytest.raises(claims_read.Mismatch) as caught:
        claims_read.account_document(DEPOSIT, account, '0' * 64)
    assert caught.value.reason == 'sha256' and len(claims.requests) == fetched
    # One that fails its checks is not kept: the next read asks the repository again.
    claims_read.cache().items.clear()
    with pytest.raises(claims_read.Mismatch):
        claims_read.account_document(DEPOSIT, account, '0' * 64)
    assert claims_read.cache().get(DEPOSIT) is None


def test_an_object_the_repository_holds_in_its_legacy_envelope_is_the_same_object(claims):
    """The repository takes an object stored earlier with class_name and identity_profile as the one deposited (its
    _plain), so the receipt is clean; the rebuild drops the envelope the same way."""
    document = synthetic(); data = canonical_json(document); claims.deposit(data)
    claims.objects['dapper:Activity.' + 'B' * 32].update(class_name='Activity', identity_profile='DAPPER-ID-1')
    assert claims_read.account_document(DEPOSIT, document['scientific_accounts'][0]['id'], sha256(data)) == document


def test_the_cache_expires_turns_off_and_stays_bounded(claims, monkeypatch):
    now = [0.0]
    cache = claims_read.Cache(10, entries=2, size=10, clock=lambda: now[0])
    cache.put('a', b'1234'); cache.put('b', b'5678')
    assert cache.get('a') == b'1234'
    cache.put('c', b'9')                     # three entries: the least recently used (b) goes
    assert cache.get('b') is None and cache.get('c') == b'9'
    cache.put('d', b'12345678')              # over the byte bound: the oldest go until it fits
    assert cache.get('a') is None and cache.get('d') == b'12345678'
    cache.put('huge', b'x' * 11); assert cache.get('huge') is None
    now[0] = 11; assert cache.get('d') is None
    monkeypatch.setenv('REVEAL_CLAIMS_READ_CACHE_SECONDS', '0')
    document = synthetic(); data = canonical_json(document); claims.deposit(data)
    for _ in range(2):
        claims_read.account_document(DEPOSIT, document['scientific_accounts'][0]['id'], sha256(data))
    assert len(claims.requests) == 4


def test_a_tampered_or_missing_object_or_another_account_is_a_mismatch(claims):
    document = synthetic(); data = canonical_json(document); claims.deposit(data)
    account = document['scientific_accounts'][0]['id']
    claims.objects['dapper:Claim.' + '0' * 32]['statement'] = 'Something else.'
    with pytest.raises(claims_read.Mismatch) as caught:
        claims_read.account_document(DEPOSIT, account, sha256(data))
    assert caught.value.reason == 'sha256'
    with pytest.raises(claims_read.Mismatch) as caught:
        claims_read.account_document(DEPOSIT, 'dapper:ScientificAccount.' + 'Z' * 32, sha256(data))
    assert caught.value.reason == 'account_id'
    claims_read.cache().items.clear()
    del claims.objects[f'dapper:Claim.{1:032d}']
    with pytest.raises(claims_read.Mismatch) as caught:
        claims_read.rebuild(DEPOSIT)
    assert caught.value.reason == 'missing_objects'
    claims.deposits[DEPOSIT]['objects'][0]['class_name'] = None    # an object the repository no longer holds at all
    with pytest.raises(claims_read.Mismatch) as caught:
        claims_read.rebuild(DEPOSIT)
    assert caught.value.reason == 'unknown_class'
    with pytest.raises(claims_read.Mismatch):
        claims_read.rebuild('not-a-deposit')


def test_a_deposit_of_more_than_five_hundred_objects_is_read_in_batches(claims):
    document = synthetic(claims=1100); data = canonical_json(document); claims.deposit(data)
    served = claims_read.account_document(DEPOSIT, document['scientific_accounts'][0]['id'], sha256(data))
    assert canonical_json(served) == data
    batches = [json.loads(request.data)['ids'] for request in claims.requests if request.get_method() == 'POST']
    assert [len(ids) for ids in batches] == [500, 500, 102]
    assert sorted(sum(batches, [])) == sorted(claims.objects)


def test_an_unreachable_or_unconfigured_repository_is_unavailable_and_not_cached(claims, monkeypatch):
    document = synthetic(); data = canonical_json(document); claims.deposit(data)
    account = document['scientific_accounts'][0]['id']
    claims.down = True
    with pytest.raises(claims_read.Unavailable):
        claims_read.account_document(DEPOSIT, account, sha256(data))
    claims.down = False
    assert claims_read.account_document(DEPOSIT, account, sha256(data)) == document     # the failure was not cached
    with pytest.raises(claims_read.Unavailable):
        claims_read.rebuild('dep_' + '2' * 32)                                           # 404: no such deposit
    for url in ('', 'http://claims.test', 'https://user:pass@claims.test'):
        monkeypatch.setenv('REVEAL_CLAIMS_API_URL', url)
        with pytest.raises(claims_read.Unavailable, match='no claims repository address'):
            claims_read.rebuild(DEPOSIT)
