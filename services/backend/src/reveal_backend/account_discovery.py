"""Read-only discovery of accepted accounts already accessible to a workspace.

Workspace membership rows never authorize another owner's private account or
its count. Explicit public snapshots are projected separately by publication.py.
"""
from collections import Counter
from copy import deepcopy


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
        jobs = tx.get_many('job', sorted({item['job_id'] for item in items}))
        request_ids = {row['data'].get('research_request_id') for row in jobs.values() if row['owner'] == owner}
        requests = tx.get_many('request', sorted(identity for identity in request_ids if identity))
        for item in items:
            item['publication'] = state(tx, owner, item['account']['id'], can_manage=True,
                record=publications.get(digest([owner, item['account']['id']])),
                account_result={'research_statement': item.get('research_statement', {})})
            job = jobs.get(item['job_id'])
            request = requests.get(job['data'].get('research_request_id')) if job and job['owner'] == owner else None
            # Transfers preserve historical authorship. Current owner/profile is
            # deliberately not used as a substitute for a missing snapshot.
            original = request['data'].get('attribution') if request and request['owner'] == owner else None
            fields = ('user_id', 'person_id', 'principal_kind', 'display_name', 'orcid', 'orcid_authenticated', 'observed_at')
            item['attribution'] = ({key: deepcopy(original[key]) for key in fields}
                if isinstance(original, dict) and all(key in original for key in fields) else None)
    return items


def counts_by_gap(accounts):
    return Counter(item['account']['question'] for item in accounts)


def counted_gap(gap, counts, owner, observed_at):
    return {**gap, 'scientific_accounts': {
        'count': counts.get(gap['object']['id'], 0),
        'scope': 'owner_exact_gap' if owner else 'public_exact_gap',
        'as_of': observed_at, 'ranking': 'account_count', 'window_days': None,
    }}


def count_snapshot(items):
    """Fresh observation timestamps do not invalidate an otherwise stable page."""
    result = deepcopy(items)
    for item in result:
        gap = item.get('gap', item)
        gap.get('scientific_accounts', {}).pop('as_of', None)
    return result
