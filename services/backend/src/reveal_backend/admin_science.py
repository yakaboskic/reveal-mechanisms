"""Explicit cross-workspace scientific reads; never impersonate a workspace owner.

Only retained account/outcome records are exposed. Lists project small summaries
in SQL so pagination does not deserialize every scientific document or capture.
"""
from copy import deepcopy
import json
import logging
import re

from .auth import Problem
from . import analysis_outcomes, publication
from .repository import digest


def _publication_projection():
    fields = ('visibility', 'version', 'published_at', 'updated_at', 'paragraph_id')
    return 'JSON_OBJECT(' + ','.join("'" + field + "',JSON_EXTRACT(p.payload,'$." + field + "')" for field in fields) + ')'


def _state(tx, kind, owner, summary, row):
    if kind == 'account':
        value = publication.state(tx, owner, summary['account']['id'], can_manage=True,
            record=row, account_result={'research_statement': summary.get('research_statement', {})})
        value['can_manage'] = False
        return value
    return analysis_outcomes.publication_state(row, can_manage=False)


def collection(tx, kind, visibility):
    if visibility not in ('private', 'public', 'all'):
        raise Problem(422, 'INVALID_QUERY', 'Choose private, public or all for visibility.')
    if kind == 'account':
        source = "JSON_EXTRACT(r.payload,'$.summary')"
        publication_kind = 'publication'
    else:
        # Read retained records even when a historical summary index is absent.
        fields = ('id', 'outcome', 'summary', 'knowledge_gap', 'anchors', 'created_at', 'attribution', 'archive')
        source = 'JSON_OBJECT(' + ','.join("'" + field + "',JSON_EXTRACT(r.payload,'$.record." + field + "')" for field in fields) + ')'
        publication_kind = 'outcome_publication'
    # Projection and joins use fixed identifiers; all user-supplied values are
    # query parameters. No ORDER BY on large JSON payloads (MySQL filesort).
    rows = tx.execute(f'''SELECT r.id,r.owner_id,r.version,r.updated_at,{source},p.version,{_publication_projection()}
        FROM reveal_records r
        LEFT JOIN reveal_records p ON p.kind=%s AND p.id=r.id AND p.owner_id=r.owner_id
        WHERE r.kind=%s''', (publication_kind, kind)).fetchall()
    records = []
    for identity, owner, revision, updated, raw_summary, pub_revision, raw_publication in rows:
        if raw_summary is None:
            raise Problem(503, 'SCIENTIFIC_RECORD_INCOMPLETE', 'A retained scientific record has no summary.')
        summary = json.loads(raw_summary)
        if kind != 'account' and summary.get('archive') is None: summary.pop('archive', None)
        pub = {'owner': owner, 'data': json.loads(raw_publication)} if pub_revision is not None else None
        summary['publication'] = _state(tx, kind, owner, summary, pub)
        if kind == 'account': summary['votes'] = None
        if visibility != 'all' and summary['publication']['visibility'] != visibility: continue
        records.append((updated, {'record_id': identity, 'owner_user_id': owner, 'summary': summary},
            [identity, owner, revision, pub_revision]))
    records.sort(key=lambda row: row[1]['record_id'])
    records.sort(key=lambda row: row[0], reverse=True)
    return [row[1] for row in records], [row[2] for row in records]


def attribution(tx, items):
    """Keep original frozen authorship, including after workspace transfers."""
    jobs = tx.get_many('job', sorted({item['summary']['job_id'] for item in items if item['summary'].get('job_id')}))
    local_ids = {item['summary'].get('local_work_id') or item['summary'].get('job_id') for item in items
        if item['summary'].get('local_work_id') or item['summary'].get('job_id') not in jobs}
    local = tx.get_many('local_work', sorted(local_ids - {None}))
    request_ids = {row['data'].get('research_request_id') for row in [*jobs.values(), *local.values()]}
    requests = tx.get_many('request', sorted(request_ids - {None}))
    fields = ('user_id', 'person_id', 'principal_kind', 'display_name', 'orcid', 'orcid_authenticated', 'observed_at')
    for item in items:
        summary, owner = item['summary'], item['owner_user_id']
        job = jobs.get(summary.get('job_id')) or local.get(summary.get('local_work_id') or summary.get('job_id'))
        request = requests.get(job['data'].get('research_request_id')) if job and job['owner'] == owner else None
        original = request['data'].get('attribution') if request and request['owner'] == owner else None
        summary['attribution'] = ({key: deepcopy(original[key]) for key in fields}
            if isinstance(original, dict) and all(key in original for key in fields) else None)
        # Local acceptance keeps routing hints alongside the public summary.
        # Resolve frozen authorship first, then project the documented summary.
        summary.pop('local_work_id', None)
        summary.pop('execution_mode', None)


def _envelope(result):
    value = deepcopy(result)
    # Admin read authority does not grant artifact downloads, including stale
    # signed URLs retained in an envelope. File identity and hashes stay exact.
    for artifact in value.get('artifacts', []):
        artifact['download_url'] = None
        artifact['expires_at'] = None
    if 'coverage' in value: value['coverage']['next_cursor'] = None
    return value


def detail(tx, kind, identity):
    pattern = r'[a-f0-9]{64}' if kind == 'account' else r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}'
    row = tx.get(kind, identity) if re.fullmatch(pattern, identity) else None
    if row is None:
        raise Problem(404, 'NOT_FOUND', 'The retained scientific record is unavailable.')
    owner, data = row['owner'], row['data']
    if kind == 'account':
        summary = deepcopy(data['summary'])
        if identity != digest([owner, summary['account']['id']]):
            raise Problem(503, 'SCIENTIFIC_RECORD_INCOMPLETE', 'The retained account identity is inconsistent.')
        pub = tx.get('publication', identity)
    else:
        summary = analysis_outcomes.summary(data['record'])
        pub = tx.get('outcome_publication', identity)
    if pub and pub['owner'] != owner: pub = None
    state = _state(tx, kind, owner, summary, pub)
    summary['publication'] = state
    result = {'record_id': identity, 'owner_user_id': owner, 'summary': summary}
    if kind == 'account':
        summary['votes'] = None
        attribution(tx, [result])
        envelope = _envelope(data['result'])
        envelope['publication'] = deepcopy(state)
        if summary.get('archive'): envelope['archive'] = deepcopy(summary['archive'])
        paragraph = None
        statement = envelope.get('research_statement', {})
        if statement.get('status') == 'succeeded' and statement.get('paragraph_id'):
            saved = tx.get('paragraph', digest([owner, statement['paragraph_id']]))
            if saved and saved['owner'] == owner and saved['data'].get('account_id') == summary['account']['id']:
                paragraph = _envelope(saved['data']['result'])
        result.update(result=envelope, paragraph=paragraph)
    else:
        result['result'] = {**deepcopy(data['record']), 'publication': deepcopy(state)}
        provenance = result['result'].get('provenance', {})
        for artifact in provenance.get('source_artifacts', []):
            artifact['download_url'] = None
            artifact['expires_at'] = None
        for reference in provenance.get('evidence_refs', []):
            reference['download_url'] = None
    return result


def audit(key_id, action, count):
    # Operational audit excludes secrets, scientific contents and personal data.
    logging.getLogger('uvicorn.error').info('admin_science_read key_id=%s action=%s count=%d', key_id, action, count)
