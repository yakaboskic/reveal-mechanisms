"""Read-only discovery of accepted accounts already accessible to a workspace.

Workspace membership rows never authorize another owner's private account or
its count. Explicit public snapshots are projected separately by publication.py.
"""
from collections import Counter
from copy import deepcopy

from .auth import Problem
from .reference_generation import is_archived

REFERENCE_STATES = ('current', 'archived', 'all')


def visible_accounts(tx, owner, *, attribution=False):
    if not owner:
        return []
    unique = {}
    for row in tx.list('account_membership', owner):
        if row['owner'] != owner:
            continue
        summary = row['data'].get('summary', {})
        account = summary.get('account', {})
        identity = account.get('id')
        # Count scientific identities, never attempts, paragraph jobs, text
        # matches, source aliases or two deliveries of the same account.
        if (not identity or row['data'].get('account_id') != identity or
                account.get('question') != summary.get('knowledge_gap', {}).get('id')):
            continue
        previous = unique.get(identity)
        if previous is None or summary['created_at'] < previous['created_at']:
            unique[identity] = deepcopy(summary)
    items = sorted(unique.values(), key=lambda item: item['account']['id'])
    items.sort(key=lambda item: item['created_at'], reverse=True)
    if attribution and items:
        from .repository import digest
        from .publication import state
        publications = tx.get_many('publication', [digest([owner, item['account']['id']]) for item in items])
        # Authored canonical fixtures have explicit origin metadata and no fake
        # research job. Never mix null and UUID keys when batching discovery.
        jobs = tx.get_many('job', sorted({item['job_id'] for item in items if item.get('job_id')}))
        local = tx.get_many('local_work', sorted({item.get('local_work_id') or item['job_id'] for item in items
            if item.get('local_work_id') or item.get('job_id') and item['job_id'] not in jobs}))
        request_ids = {row['data'].get('research_request_id') for row in [*jobs.values(), *local.values()] if row['owner'] == owner}
        requests = tx.get_many('request', sorted(identity for identity in request_ids if identity))
        for item in items:
            item['publication'] = state(tx, owner, item['account']['id'], can_manage=True,
                record=publications.get(digest([owner, item['account']['id']])),
                account_result={'research_statement': item.get('research_statement', {})})
            job = jobs.get(item.get('job_id')) or local.get(item.get('local_work_id') or item.get('job_id'))
            request = requests.get(job['data'].get('research_request_id')) if job and job['owner'] == owner else None
            # Transfers preserve historical authorship. Current owner/profile is
            # deliberately not used as a substitute for a missing snapshot.
            original = request['data'].get('attribution') if request and request['owner'] == owner else None
            fields = ('user_id', 'person_id', 'principal_kind', 'display_name', 'orcid', 'orcid_authenticated', 'observed_at')
            item['attribution'] = ({key: deepcopy(original[key]) for key in fields}
                if isinstance(original, dict) and all(key in original for key in fields) else None)
    return items


def by_reference_state(items, state='all'):
    """Filter listed summaries by archive state; `all` lists current work first.

    Archived work (built on a superseded reference generation) is never hidden
    by default. The sort is stable, so each group keeps its existing order.
    """
    if state not in REFERENCE_STATES:
        raise Problem(422, 'INVALID_QUERY', 'Choose current, archived or all for reference_state.')
    if state == 'all': return sorted(items, key=is_archived)
    return [item for item in items if is_archived(item) == (state == 'archived')]


def counts_by_gap(accounts):
    """(current, archived) accounts per gap. Gap ranking counts current work only;
    archived accounts are listed after it and counted apart."""
    current, archived = Counter(), Counter()
    for item in accounts:
        (archived if is_archived(item) else current)[item['account']['question']] += 1
    return current, archived


def counted_gap(gap, counts, owner, observed_at):
    current, archived = counts
    identity = gap['object']['id']
    return {**gap, 'scientific_accounts': {
        'count': current.get(identity, 0),
        'scope': 'owner_exact_gap' if owner else 'public_exact_gap',
        'as_of': observed_at, 'ranking': 'account_count', 'window_days': None,
        # Present only once archived work exists for the gap, so earlier records are unchanged.
        **({'archived_count': archived[identity]} if archived.get(identity) else {}),
    }}


def resolved_mechanism_count(gap):
    """Count canonical DisMech Mechanisms, not attachment rows or unresolved labels."""
    identities=set()
    for attachment in gap.get('attachments',[]):
        target=attachment.get('target') or {}
        identity=target.get('dapper_id','')
        if (attachment.get('resolution')=='resolved' and target.get('source')=='dismech'
                and target.get('source_id','').startswith('dismech:') and identity.startswith('dapper:Mechanism.')):
            identities.add(identity)
    return len(identities)


def gap_snapshot(items, corpus):
    """Cursor binding for a gap page without copying or hashing catalog payloads.

    Binds the catalog corpus, order and membership, each gap's content ids and every field that ranks or varies
    per request (account counts, votes, mechanism count, search ranking). Observation time (as_of) is excluded,
    so a fresh timestamp keeps a page. Catalog gaps are immutable within one load, so the corpus, object id,
    source revision and payload hash identify their static body.
    """
    rows = []
    for item in items:
        gap = item.get('gap', item); source = gap.get('source') or {}
        accounts = {key: value for key, value in (gap.get('scientific_accounts') or {}).items() if key != 'as_of'}
        rows.append([gap['object']['id'], source.get('source_id'), source.get('source_revision'),
                     (gap.get('source_detail') or {}).get('payload_sha256'), resolved_mechanism_count(gap),
                     accounts, gap.get('votes'), item.get('ranking')])
    return ['gap-snapshot-v2', corpus, rows]
