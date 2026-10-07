"""Canonical, public vote totals with one transactional ballot per registered user."""
import json

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


KEYED = 250  # keys one get_records statement reads exactly


def states(tx, targets, user=None):
    """Public totals and the registered viewer's own ballot per (kind, id) target.

    Few targets are read at their exact keys. A larger set (a catalog-wide browse) reads the stored totals and
    this viewer's ballots instead, one statement whatever the catalog size: only voted targets have rows.
    """
    targets = set(targets)
    if len(targets) * (2 if user else 1) <= KEYED:
        keys = {target: (digest(list(target)), digest([user, *target]) if user else None) for target in targets}
        records = tx.get_records([('vote_total', total) for total, _ in keys.values()] +
                                 [('vote', ballot) for _, ballot in keys.values() if ballot])
        totals = {target: records.get(('vote_total', total)) for target, (total, _) in keys.items()}
        ballots = {target: records.get(('vote', ballot)) for target, (_, ballot) in keys.items() if ballot}
    else:
        totals, ballots = stored(tx, targets, user)
    result = {}
    for target in targets:
        total = (totals.get(target) or {}).get('data', {}); ballot = ballots.get(target)
        mine = ballot['data']['vote'] if ballot and ballot['owner'] == user else 0
        up, down = total.get('upvotes', 0), total.get('downvotes', 0)
        result[target] = {'upvotes': up, 'downvotes': down, 'score': up-down, 'user_vote': mine if user else None}
    return result


def stored(tx, targets, user=None):
    """Every stored total and the viewer's ballots (owner index) in one statement, by target."""
    sql, args = 'SELECT kind,id,owner_id,payload FROM reveal_records WHERE kind=%s', ['vote_total']
    if user:
        sql += ' UNION ALL SELECT kind,id,owner_id,payload FROM reveal_records WHERE kind=%s AND owner_id=%s'
        args += ['vote', user]
    rows = {'vote_total': [], 'vote': []}
    for kind, identity, owner, payload in tx.execute(sql, args).fetchall():
        rows[kind].append({'id': identity, 'owner': owner, 'data': json.loads(payload)})
    return (keyed(tx, rows['vote_total'], targets, lambda target: digest(list(target))),
            keyed(tx, rows['vote'], targets, lambda target: digest([user, *target])) if user else {})


def keyed(tx, rows, targets, key):
    """Rows by target exactly as reads of key(target) find them: a row counts only at its own key, so a
    re-owned (transferred) ballot or a row under another target's key never answers for its payload's target."""
    found, other = {}, []
    for row in rows:
        data = row['data'] if isinstance(row['data'], dict) else {}
        target = (data.get('target_kind'), data.get('target_id'))
        if isinstance(target[0], str) and isinstance(target[1], str) and row['id'] == key(target):
            if target in targets: found[target] = row
        else: other.append(row)
    if other:
        # Never written by change(); resolve them like the keyed read (ascii_bin ids are PAD SPACE in MySQL).
        keys = {key(target): target for target in targets}
        for row in other:
            target = keys.get(row['id'] if tx.sqlite else row['id'].rstrip(' '))
            if target is not None: found[target] = row
    return found


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


def account_summaries(tx, items, user=None, *, published=None):
    """published: this transaction's publication.published() rows, when the caller already read them."""
    public = {row['data']['account_id'] for row in (publication.published(tx) if published is None else published)}
    totals = states(tx, [('account', item['account']['id']) for item in items if item['account']['id'] in public], user)
    return [{**item, 'votes': totals.get(('account', item['account']['id']))} for item in items]
