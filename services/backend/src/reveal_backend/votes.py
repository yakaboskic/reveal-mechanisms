"""Canonical, public vote totals with one transactional ballot per registered user."""
from .auth import Problem, principal
from .repository import digest, now
from . import publication


def viewer(tx, authorization):
    if authorization is None: return None
    identity = principal(tx, authorization)
    return identity['user_id'] if identity['principal_kind'] == 'registered' else None


def voter(tx, authorization):
    identity = principal(tx, authorization)
    if identity['principal_kind'] != 'registered':
        raise Problem(403, 'SIGN_IN_REQUIRED', 'Sign in to vote on knowledge gaps and published accounts.')
    return identity['user_id']


def target(tx, catalog, kind, identity):
    if kind == 'gap':
        gap = catalog.gap(identity)['object']['id']
        return gap, gap
    # Only a currently published, exact scientific identity can be voted on,
    # including by its owner. Private and missing accounts are indistinguishable.
    _, snapshot = publication.find(tx, identity, 'account')
    account = next((item for item in snapshot['document'].get('scientific_accounts', [])
                    if item.get('id') == identity), None)
    if not account or snapshot.get('account_id') != identity:
        raise Problem(404, 'NOT_FOUND', 'The requested scientific resource is unavailable.')
    gap = catalog.gap(account['question'])['object']['id']
    if gap != account['question']:
        raise Problem(404, 'NOT_FOUND', 'The requested scientific resource is unavailable.')
    return identity, gap


def states(tx, targets, user=None):
    targets = set(targets)
    # A browse may cover the full imported catalog; get_records bounds SQL
    # parameter batches independently of catalog size.
    records = tx.get_records([('vote_total', digest(target)) for target in targets]+
        ([('vote', digest([user, *target])) for target in targets] if user else []))
    result = {}
    for kind, identity in targets:
        total = records.get(('vote_total', digest([kind, identity])), {}).get('data', {})
        ballot = records.get(('vote', digest([user, kind, identity]))) if user else None
        mine = ballot['data']['vote'] if ballot and ballot['owner'] == user else 0
        up, down = total.get('upvotes', 0), total.get('downvotes', 0)
        result[(kind, identity)] = {'upvotes': up, 'downvotes': down, 'score': up-down,
                                   'user_vote': mine if user else None}
    return result


def change(tx, kind, identity, gap_id, user, value):
    if type(value) is not int or value not in (-1, 0, 1):
        raise Problem(422, 'INVALID_REQUEST', 'Choose an upvote, downvote or clear vote.')
    key = digest([user, kind, identity]); previous = tx.get('vote', key)
    old = previous['data']['vote'] if previous else 0
    if previous and (previous['owner'] != user or previous['data']['gap_id'] != gap_id):
        raise Problem(409, 'VOTE_CONFLICT', 'The saved vote binding differs; refresh this resource.')
    if old != value:
        total_key = digest([kind, identity]); row = tx.get('vote_total', total_key)
        total = row['data'] if row else {'target_kind': kind, 'target_id': identity, 'gap_id': gap_id, 'upvotes': 0, 'downvotes': 0}
        if total['gap_id'] != gap_id:
            raise Problem(409, 'VOTE_CONFLICT', 'The saved vote binding differs; refresh this resource.')
        total['upvotes'] += int(value == 1)-int(old == 1)
        total['downvotes'] += int(value == -1)-int(old == -1)
        if min(total['upvotes'], total['downvotes']) < 0:
            raise Problem(409, 'VOTE_CONFLICT', 'Vote totals require reconciliation.')
        tx.put('vote_total', total_key, 'system', total)
        tx.put('vote', key, user, {'target_kind': kind, 'target_id': identity, 'gap_id': gap_id,
                                 'vote': value, 'updated_at': now()})
    return states(tx, [(kind, identity)], user)[(kind, identity)]


def account_summaries(tx, items, user=None):
    public = {row['data']['account_id'] for row in publication.published(tx)}
    totals = states(tx, [('account', item['account']['id']) for item in items if item['account']['id'] in public], user)
    return [{**item, 'votes': totals.get(('account', item['account']['id']))} for item in items]
