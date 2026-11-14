"""Depositing an account in the claims repository: off unless configured, the key only ever in one header to one
address, and every failure reported instead of raised. No network: the opener is injected."""
import io
import json
import urllib.error

import pytest

from reveal_backend import claims_deposit

DOCUMENT = b'{"claims":[{"id":"dapper:Claim.x"}]}'
KEY = 'clm_' + 'k' * 43


class Reply(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): return False


class Opener:
    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception): raise reply
        return Reply(json.dumps(reply).encode())


def refused(status, detail=None):
    body = json.dumps({'detail': detail}).encode() if detail else b''
    return urllib.error.HTTPError('https://claims.test/v1/deposits', status, 'refused', {}, io.BytesIO(body))


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('REVEAL_CLAIMS_API_URL', 'https://claims.test/'); monkeypatch.setenv('REVEAL_CLAIMS_API_KEY', KEY)


@pytest.mark.parametrize('url, key', [('', KEY), ('https://claims.test', ''), ('http://claims.test', KEY), ('ftp://claims.test', KEY),
                                      ('https://user:pass@claims.test', KEY), ('https://claims.test/?next=x', KEY)])
def test_nothing_is_sent_unless_both_settings_are_present_and_the_address_is_safe_for_a_key(monkeypatch, url, key):
    monkeypatch.setenv('REVEAL_CLAIMS_API_URL', url); monkeypatch.setenv('REVEAL_CLAIMS_API_KEY', key)
    opener = Opener()
    assert claims_deposit.configuration() is None
    assert claims_deposit.send(DOCUMENT, opener=opener) == {'status': 'not_configured'} and opener.requests == []


def test_a_local_repository_may_be_plain_http(monkeypatch):
    monkeypatch.setenv('REVEAL_CLAIMS_API_URL', 'http://localhost:8010'); monkeypatch.setenv('REVEAL_CLAIMS_API_KEY', KEY)
    assert claims_deposit.configuration() == ('http://localhost:8010', KEY)


def test_the_whole_document_goes_in_one_keyed_request_and_the_receipt_is_summarised(configured):
    opener = Opener({'deposit_id': 'dep_1', 'depositor': {'key_id': 'key_1', 'name': 'reveal'}, 'edges': 2, 'unresolved_references': [],
                     'deposited': [{'id': 'dapper:Claim.x', 'stored': 'new'}, {'id': 'dapper:Person.y', 'stored': 'existing', 'stored_differs': True}]})
    outcome = claims_deposit.send(DOCUMENT, opener=opener)
    assert outcome == {'status': 'deposited', 'deposit_id': 'dep_1', 'objects': 2, 'stored_differs': 1, 'unresolved_references': 0}
    (request, timeout), = opener.requests
    assert request.full_url == 'https://claims.test/v1/deposits' and request.get_method() == 'POST' and timeout == claims_deposit.TIMEOUT_SECONDS
    assert request.get_header('X-api-key') == KEY and json.loads(request.data) == {'document': json.loads(DOCUMENT)}
    assert KEY not in json.dumps(outcome)


def test_a_refusal_is_final_and_says_why_while_an_outage_is_tried_again(configured):
    rejected = Opener(refused(400, 'malformed deposit: an id does not match its content'))
    assert claims_deposit.send(DOCUMENT, opener=rejected) == {
        'status': 'failed', 'reason': 'rejected', 'http_status': 400, 'detail': 'malformed deposit: an id does not match its content'}
    assert len(rejected.requests) == 1
    revoked = Opener(refused(401, 'A valid X-API-Key is required to deposit.'))
    assert claims_deposit.send(DOCUMENT, opener=revoked) == {'status': 'failed', 'reason': 'unauthorized', 'http_status': 401}
    busy = Opener(refused(503), {'deposit_id': 'dep_2', 'deposited': [{}], 'unresolved_references': []})   # a real receipt
    assert claims_deposit.send(DOCUMENT, opener=busy)['status'] == 'deposited' and len(busy.requests) == 2
    down = Opener(TimeoutError('timed out'), OSError('unreachable'))
    assert claims_deposit.send(DOCUMENT, opener=down) == {'status': 'failed', 'reason': 'unavailable'} and len(down.requests) == 2
    assert claims_deposit.send(DOCUMENT, opener=Opener(refused(502), refused(502))) == {'status': 'failed', 'reason': 'unavailable', 'http_status': 502}


def test_a_redirect_is_refused_not_followed_so_the_key_goes_to_the_configured_address_only(configured, monkeypatch):
    import http.server, threading
    seen = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def answer(self):
            seen.append((self.server.name, self.command, self.headers.get('X-API-Key')))
            self.rfile.read(int(self.headers.get('Content-Length') or 0))
            if self.server.name == 'repository':
                self.send_response(302); self.send_header('Location', f'http://127.0.0.1:{other.server_address[1]}/elsewhere'); self.end_headers()
            else:
                body = b'{"anything": true}'
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        do_GET = do_POST = answer
    servers = []
    for name in ('repository', 'other'):
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler); server.name = name
        threading.Thread(target=server.serve_forever, daemon=True).start(); servers.append(server)
    repository, other = servers
    try:
        monkeypatch.setenv('REVEAL_CLAIMS_API_URL', f'http://127.0.0.1:{repository.server_address[1]}')
        outcome = claims_deposit.send(DOCUMENT)          # the real opener, not an injected one
    finally:
        for server in servers: server.shutdown(); server.server_close()
    assert outcome == {'status': 'failed', 'reason': 'rejected', 'http_status': 302}
    assert seen == [('repository', 'POST', KEY)]


def test_an_answer_that_is_not_a_receipt_is_never_recorded_as_a_deposit(configured):
    for body in ({'anything': True}, {'deposit_id': 'dep_1'}, {'deposited': []}, ['dep_1']):
        assert claims_deposit.send(DOCUMENT, opener=Opener(body, body)) == {'status': 'failed', 'reason': 'unavailable'}


def test_a_malformed_address_is_no_configuration(monkeypatch):
    monkeypatch.setenv('REVEAL_CLAIMS_API_KEY', KEY)
    for url in ('https://claims.test:99999', 'https://[claims.test', 'https://claims.test:port'):
        monkeypatch.setenv('REVEAL_CLAIMS_API_URL', url)
        assert claims_deposit.configuration() is None and claims_deposit.send(DOCUMENT, opener=Opener()) == {'status': 'not_configured'}


def test_send_never_raises_and_never_sends_what_the_repository_would_refuse_for_size(configured, monkeypatch):
    class Broken:
        def open(self, request, timeout): raise RuntimeError('not an HTTP failure')
    assert claims_deposit.send(DOCUMENT, opener=Broken()) == {'status': 'failed', 'reason': 'unavailable'}
    assert claims_deposit.send(DOCUMENT, opener=Opener('not a receipt', 'not a receipt')) == {'status': 'failed', 'reason': 'unavailable'}
    monkeypatch.setattr(claims_deposit, 'MAX_BODY_BYTES', 20)
    opener = Opener()
    assert claims_deposit.send(DOCUMENT, opener=opener) == {'status': 'failed', 'reason': 'too_large'} and opener.requests == []
