#!/usr/bin/env python3
"""Exercise the actual local gateway/API/Aurora contract without provider secrets.

Creates two anonymous principals and preserves its development drafts/cancelled
job as acceptance evidence. Run only against the explicit deterministic stack.
This supplements, and does not replace, browser validation.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import http.cookiejar
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, base):
        self.base = base.rstrip('/')
        self.cookies = http.cookiejar.CookieJar()
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def call(self, path, body=None, *, method=None, key=None, status=200, origin=True, headers=None):
        fields = {'Accept': 'application/json', **(headers or {})}
        if origin: fields['Origin'] = self.base
        if key: fields['Idempotency-Key'] = key
        if body is not None: fields['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.base+path, data=None if body is None else json.dumps(body).encode(), headers=fields, method=method)
        try:
            with self.http.open(request, timeout=120) as response:
                code, content = response.status, response.read()
        except urllib.error.HTTPError as failure:
            code, content = failure.code, failure.read()
        assert code == status, f'{method or ("POST" if body is not None else "GET")} {path}: expected {status}, got {code}; {content[:600]!r}'
        return json.loads(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://localhost:3000')
    parser.add_argument('--api', default='http://127.0.0.1:8000')
    parser.add_argument('--report', type=Path, default=ROOT/'data/validation/local-stack/gateway-validation.json')
    args = parser.parse_args()
    ready = Client(args.api).call('/health/ready')
    assert ready['execution_mode'] == 'deterministic', 'Only run this acceptance harness against explicit deterministic execution.'
    a, b = Client(args.base), Client(args.base)
    evidence = {'checked_at': datetime.now(timezone.utc).isoformat(), 'mode': 'deterministic', 'readiness': ready, 'checks': []}
    def checked(label):
        evidence['checks'].append(label)
        print('PASS '+label, flush=True)
    assert a.call('/api/session/anonymous', {}, key=str(uuid4()), origin=False, status=403)['code'] == 'CSRF_REJECTED'
    checked('gateway rejects a write without Origin')
    owner = a.call('/api/session/anonymous', {}, key=str(uuid4()), status=201)
    other = b.call('/api/session/anonymous', {}, key=str(uuid4()), status=201)
    assert owner['principal_kind'] == other['principal_kind'] == 'anonymous' and owner['user_id'] != other['user_id']
    evidence['owner_user_id'], evidence['other_user_id'] = owner['user_id'], other['user_id']
    assert all(c.has_nonstandard_attr('HttpOnly') for c in a.cookies)
    checked('separate anonymous sessions provision separate Aurora owners with HttpOnly cookies')
    gap = a.call('/api/backend/v1/knowledge-gaps/search?q=coronary&limit=1')['items'][0]['gap']
    # An unfiltered catalog lookup needs no biological-label correspondence;
    # this is an ownership/version test, not a scientific relevance assertion.
    factor = a.call('/api/backend/v1/mechanisms/search?mode=lexical&limit=1')['items'][0]['record']
    composer = {'source_gap': {'id': gap['object']['id'], 'source_id': gap['source']['source_id'], 'source_revision': gap['source']['source_revision']},
        'eaggl_anchors': [{'reference': {'source': 'eaggl', 'source_id': factor['source_id'], 'source_revision': factor['source_revision'], 'dapper_id': factor['object']['id']}, 'origin': 'manual', 'suggestion_id': None}],
        'dismissed_source_ids': [], 'mechanism_subquery': '', 'model': 'cfde-inc-v2', 'selected_kgs': ['biomarkerkg', 'prokn']}
    evidence['source_gap'], evidence['factor_source_id'] = composer['source_gap'], factor['source_id']
    checked('real imported DisMech and mapped EAGGL records resolve through the frontend gateway')
    key = str(uuid4())
    draft = a.call('/api/backend/v1/drafts', {'composer': composer}, key=key, status=201)
    repeated = a.call('/api/backend/v1/drafts', {'composer': composer}, key=key, status=201)
    assert repeated == draft
    evidence['draft_id'] = draft['id']
    checked('draft creation retry is idempotent')
    b.call('/api/backend/v1/drafts/'+draft['id'], status=404)
    assert not b.call('/api/backend/v1/drafts')['items']
    checked('another anonymous owner cannot retrieve or list the draft')
    changed = deepcopy(composer); changed['mechanism_subquery'] = 'integration persistence check'
    patch = {'composer': changed, 'expected_version': draft['version']}; patch_key = str(uuid4())
    updated = a.call('/api/backend/v1/drafts/'+draft['id'], patch, method='PATCH', key=patch_key)
    assert updated['version'] == draft['version']+1
    assert a.call('/api/backend/v1/drafts/'+draft['id'], patch, method='PATCH', key=patch_key) == updated
    conflict = a.call('/api/backend/v1/drafts/'+draft['id'], patch, method='PATCH', key=str(uuid4()), status=409)
    assert conflict['code'] == 'VERSION_CONFLICT'
    checked('draft update increments version, retries once, and rejects a stale version')
    submission = {'kind':'analysis', 'draft_id': draft['id'], 'draft_version': updated['version']}; job_key = str(uuid4())
    job = a.call('/api/backend/v1/jobs', submission, key=job_key, status=202)
    assert a.call('/api/backend/v1/jobs', submission, key=job_key, status=202)['id'] == job['id']
    evidence['job_id'], evidence['research_request_id'] = job['id'], job['research_request_id']
    checked('submission retry creates one immutable request and one job')
    a.call('/api/backend/v1/jobs/'+job['id']+'/cancel', {}, status=200)
    deadline = time.monotonic()+180
    while time.monotonic()<deadline:
        saved_job = a.call('/api/backend/v1/jobs/'+job['id'])
        if saved_job['status']=='cancelled': break
        time.sleep(1)
    assert saved_job['status']=='cancelled', saved_job['status']
    assert a.call('/api/backend/v1/jobs/'+job['id']+'/cancel', {})['status']=='cancelled'
    checked('cancellation is persisted, terminal, and repeatable')
    events = a.call('/api/backend/v1/jobs/'+job['id']+'/events')['items']
    ids = [int(event['id']) for event in events]
    assert ids == sorted(set(ids)) and events[-1]['status']=='cancelled'
    midpoint = events[len(events)//2]['id']
    replay = a.call('/api/backend/v1/jobs/'+job['id']+'/events?after='+midpoint)['items']
    assert replay == [event for event in events if int(event['id'])>int(midpoint)]
    evidence['event_ids'] = ids
    checked('event replay resumes strictly after the persisted event ID')
    frozen = a.call('/api/backend/v1/research-requests/'+job['research_request_id'])
    assert frozen['composer']==changed
    b.call('/api/backend/v1/jobs/'+job['id'], status=404)
    b.call('/api/backend/v1/research-requests/'+job['research_request_id'], status=404)
    checked('frozen request preserves selected revisions and is isolated with its job')
    evidence['draft_version'] = updated['version']; evidence['status']='passed'
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(evidence, indent=2)+'\n')
    print('Evidence saved to '+str(args.report))


if __name__ == '__main__': main()
