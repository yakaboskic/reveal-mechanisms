"""All-time public contribution projections, rebuilt from frozen publications.

No workspace, profile, job, request or private scientific rows are consulted.
The only non-public input is canonical ballots, used solely for aggregate voter
counts and exclusion of a credited researcher's own vote.
"""
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction
import hashlib
import hmac
import os
from urllib.parse import quote, urlencode

from . import analysis_outcomes, publication, votes
from .auth import Problem
from .repository import digest, now

SCORE_VERSION = 'public-contribution-v1'
DIRECTIONS = ('SUPPORTS', 'DISPUTES', 'MIXED', 'NEUTRAL', 'UNKNOWN')
SORTS = {'researchers': ('overall', 'accounts', 'votes', 'gaps', 'explored'),
         'accounts': ('votes',), 'datasets': ('accounts', 'claims', 'traced_claims', 'gaps', 'researchers')}
METRICS = {'researchers': ('accounts', 'votes', 'gaps', 'explored'),
           'accounts': ('accounts', 'votes', 'claims', 'gaps', 'researchers'),
           'datasets': ('accounts', 'claims', 'traced_claims', 'traced_accounts', 'gaps', 'researchers', 'files')}
METRIC_KEYS = {'overall': 'overall_score', 'accounts': 'account_count', 'votes': 'net_votes',
               'gaps': 'account_gap_count', 'explored': 'explored_gap_count',
               'claims': 'claim_count', 'researchers': 'researcher_count',
               'traced_claims': 'traced_claim_count', 'traced_accounts': 'traced_account_count'}
# Dataset rankings over stored account traces (account_data_sources). scope=all adds every saved account.
SCOPES = ('published', 'all')
TRACED = ('traced_claims', 'traced_accounts')
GWAS_BOTTOM_LINE = 'gwas_bottom_line'  # = account_data_sources.GWAS_BOTTOM_LINE: a stored trait GWAS source
DEFINITIONS = [
    {'id': 'overall', 'label': 'Overall contribution', 'description': '30% account percentile + 40% community-vote percentile + 30% account-gap percentile. Relative contribution, not scientific validity or expertise.'},
    {'id': 'accounts', 'label': 'Public scientific accounts', 'description': 'Distinct currently public accepted canonical accounts. Dataset counts require explicit evidence paths.'},
    {'id': 'votes', 'label': 'Community recognition', 'description': 'Upvotes minus downvotes. Researcher totals exclude that original researcher’s own ballots; account totals retain all canonical ballots. Zero-vote accounts are unrated.'},
    {'id': 'gaps', 'label': 'Gaps covered by accounts', 'description': 'Distinct exact knowledge-gap identities addressed by qualifying accounts, not proof that a gap is closed.'},
    {'id': 'explored', 'label': 'Gaps explored', 'description': 'Distinct gaps with a qualifying public account or an explicitly published insufficient-evidence exploration. Drafts and started runs do not count.'},
    {'id': 'claims', 'label': 'Claims informed', 'description': 'Distinct component claims linked to a dataset through evidence targeting the claim’s proposition. Supporting and disputing evidence both inform a claim.'},
    {'id': 'researchers', 'label': 'Researcher adoption', 'description': 'Distinct eligible original registered contributors with qualifying public accounts using the dataset as evidence.'},
    {'id': 'files', 'label': 'Files reached by evidence', 'description': 'Only concrete files reached by a qualifying evidence path and explicitly associated with this dataset. Other dataset members and direct analysis inputs do not count.'},
    {'id': 'traced_claims', 'label': 'Claims traced', 'description': 'Distinct component claims whose stored provenance trace reaches the dataset: through cited evidence, a gene set or mechanism the claim names, a mechanism’s projected gene sets, or a reached KPN trait’s GWAS bottom line. Lineage reach, not evidential support. With all accounts, private accounts add anonymous totals and are never listed.'},
    {'id': 'traced_accounts', 'label': 'Accounts traced', 'description': 'Distinct accounts with at least one claim whose stored provenance trace reaches the dataset. With all accounts, private accounts add anonymous totals and are never listed.'},
]
METHODOLOGY = {'weights': {'accounts': .3, 'votes': .4, 'gaps': .3},
    'scope': 'All time; currently public frozen accounts and published explorations only. Canonical identities are deduplicated. Demo/test fixtures are excluded. Original registered attribution is used; missing, anonymous and conflicting attribution earns no researcher credit.',
    'score': 'For x > 0, P(x) = 100 × (contributors below x + 0.5 × contributors tied at x) / cohort size. Otherwise P(x) = 0. A sole contributor receives 50 for a positive component. Scores rank before rounding, equal values share rank, and the cohort includes contributors with only published explorations.',
    'own_votes': 'Researchers’ recognition excludes their own ballots on their credited accounts. Individual-account rankings retain the existing canonical totals. Voting participants are distinct voters with an active ballot on an eligible public account; identities are never returned.',
    'evidence': 'Only explicit evidence interpretations targeting a component claim’s proposition count. SUPPORTS concerns that proposition, not endorsement of the claim assessment. Direction counts deduplicate claims within each direction and can overlap. Analysis inputs, ambiguous and missing paths do not imply evidence support. Dataset identity is canonical, never inferred from names or storage prefixes.',
    'traced': 'Claims and accounts traced read each account’s stored data sources: its provenance trace, taken once when the account was saved and frozen with each publication. Published accounts count by default; the all-accounts option adds every saved account, private ones as anonymous totals that are never listed. Demo/test fixtures are excluded, and the evidence filter does not apply to traced lineage.'}


def options(view, sort=None, evidence='all', metric=None, scope='published'):
    if view not in SORTS or evidence not in ('all', 'supporting') or scope not in SCOPES or (scope == 'all' and view != 'datasets'):
        raise Problem(422, 'INVALID_QUERY', 'Choose a leaderboard view, evidence filter and account scope.')
    # All saved accounts rank datasets only by their stored traces: evidence metrics stay public-only.
    sorts, metrics = (('traced_claims',), TRACED) if scope == 'all' else (SORTS[view], METRICS[view])
    sort = sort or sorts[0]
    if sort not in sorts or metric is not None and metric not in metrics:
        raise Problem(422, 'INVALID_QUERY', 'Choose a ranking or record metric available for this view.')
    return sort


def contributor_key(identity):
    secret = os.getenv('REVEAL_GATEWAY_SECRET', '').encode()
    if len(secret) < 32:
        raise Problem(503, 'GATEWAY_NOT_CONFIGURED', 'Public contributor identity signing is unavailable.')
    return 'researcher_' + hmac.new(secret, ('leaderboard-contributor-v1:' + identity).encode(), hashlib.sha256).hexdigest()


def fixture(*values):
    """Inspect frozen origin metadata before projecting public display fields."""
    for value in values:
        if not isinstance(value, dict): continue
        if any(value.get(key) for key in ('fixture_origin', 'demo_origin', 'test_origin', 'is_fixture', 'is_demo', 'is_test')): return True
        origin = value.get('origin')
        if isinstance(origin, str) and origin.lower() in ('fixture', 'demo', 'test', 'canonical_fixture'): return True
        if isinstance(origin, dict) and str(origin.get('kind', '')).lower() in ('fixture', 'demo', 'test', 'canonical_fixture'): return True
    return False


def actor(attribution):
    if not isinstance(attribution, dict) or attribution.get('principal_kind') != 'registered': return None
    identity = attribution.get('user_id')
    if not isinstance(identity, str) or not identity: return None
    return identity


def credited(attributions):
    """A missing/anonymous copy cannot establish or silently override authorship."""
    identities = {actor(value) for value in attributions}
    eligible = {identity for identity in identities if identity}
    if len(identities) != 1 or not eligible:
        return None, bool(eligible and len(identities) > 1)
    identity = next(iter(eligible))
    candidates = sorted(attributions, key=lambda value: (value.get('observed_at') or '', digest(value)))
    selected = candidates[0]
    return {'internal_id': identity, 'id': contributor_key(identity),
            'label': selected.get('display_name') or 'Registered researcher',
            'orcid': selected.get('orcid') if selected.get('orcid_authenticated') is True else None}, False


def directions(): return dict.fromkeys(DIRECTIONS, 0)


def row(identity, kind, label):
    return {'id': identity, 'kind': kind, 'label': label, 'rank': 0,
        'metrics': {key: 0 for key in ('overall_score', 'account_count', 'net_votes', 'upvotes', 'downvotes',
                    'voter_count', 'account_gap_count', 'explored_gap_count', 'claim_count', 'researcher_count',
                    'traced_claim_count', 'traced_account_count')},
        'components': {'accounts': 0, 'votes': 0, 'gaps': 0}, 'orcid': None,
        'account_id': None, 'gap_id': None, 'gap': None, 'attribution': None, 'directions': directions(),
        'organizations': [], 'url': None}


def public_actor(value):
    return {key: value[key] for key in ('id', 'label', 'orcid')} if value else None


def record(identity, kind, label, *, account_ids=(), claim_ids=(), evidence_ids=(), value=1, direction=None):
    paths = {'account': 'accounts', 'gap': 'knowledge-gaps', 'claim': 'claims', 'file': 'id', 'exploration': 'analyses'}
    url = ('/leaderboard?' + urlencode({'view': 'researchers', 'selected': identity, 'metric': 'accounts'})
           if kind == 'researcher' else '/' + paths[kind] + '/' + quote(identity, safe=''))
    return {'id': identity, 'kind': kind, 'label': label, 'url': url,
        'account_ids': sorted(set(account_ids)), 'claim_ids': sorted(set(claim_ids)),
        'evidence_ids': sorted(set(evidence_ids)), 'directions': direction or directions(), 'value': value}


def build(tx, evidence='all', scope='published'):
    """Return the complete public projection and its matching inspectable units. scope='all' adds every saved
    account's stored trace to the traced dataset metrics, as anonymous totals; nothing private is ever listed."""
    from .leaderboard_sources import collect
    options('datasets' if scope == 'all' else 'researchers', evidence=evidence, scope=scope)
    exclusions = dict.fromkeys(('fixture_accounts', 'uncredited_accounts', 'conflicting_accounts',
        'invalid_public_accounts', 'excluded_evidence_paths', 'fixture_explorations', 'uncredited_explorations'), 0)
    copies, fixture_ids, invalid_ids = defaultdict(list), set(), set()
    frozen_sources = defaultdict(list)   # identity -> (owner, snapshot id) of each public copy
    for pub in publication.published(tx):
        data = pub['data']; identity = data.get('account_id'); snapshot = tx.get('publication_snapshot', data['snapshot_id'])
        if not snapshot or snapshot['owner'] != pub['owner']:
            invalid_ids.add(identity); continue
        frozen = snapshot['data']; summary = frozen.get('summary', {}); document = frozen.get('document', {})
        roots = [node for node in document.get('scientific_accounts', []) if node.get('id') == identity]
        if fixture(data, frozen, summary, *roots): fixture_ids.add(identity); continue
        if (frozen.get('account_id') != identity or len(roots) != 1 or summary.get('account') != roots[0]
                or not roots[0].get('component_claims') or roots[0].get('question') != summary.get('knowledge_gap', {}).get('id')):
            invalid_ids.add(identity); continue
        copies[identity].append(frozen); frozen_sources[identity].append((pub['owner'], data['snapshot_id']))
    # A flagged canonical identity must not gain credit through an unflagged copy.
    for identity in fixture_ids: copies.pop(identity, None)
    exclusions.update(fixture_accounts=len(fixture_ids), invalid_public_accounts=len(invalid_ids))
    totals = votes.states(tx, [('account', identity) for identity in copies])
    ballots = defaultdict(dict)
    for ballot in tx.list('vote'):
        data = ballot['data']
        if data.get('target_kind') == 'account' and data.get('target_id') in copies and data.get('vote') in (-1, 1):
            # Only canonical per-user ballot keys establish participation.
            if ballot['id'] == digest([ballot['owner'], 'account', data['target_id']]):
                ballots[data['target_id']][ballot['owner']] = data['vote']
    accounts, researchers, datasets = {}, {}, {}
    records = defaultdict(dict)
    researcher_accounts, researcher_explorations = defaultdict(set), defaultdict(dict)
    account_actors, account_gaps, gap_labels, claim_labels, file_labels = {}, {}, {}, {}, {}
    dataset_uses = defaultdict(list)

    def researcher(credit):
        key = credit['id']
        if key not in researchers:
            researchers[key] = row(key, 'researcher', credit['label']); researchers[key]['orcid'] = credit['orcid']
        return key

    for identity, snapshots in sorted(copies.items()):
        snapshots.sort(key=digest)
        frozen = snapshots[0]; summary = frozen['summary']; account = summary['account']; gap = summary['knowledge_gap']
        credit, conflict = credited([item.get('summary', {}).get('attribution') for item in snapshots])
        if not credit: exclusions['uncredited_accounts'] += 1
        if conflict: exclusions['conflicting_accounts'] += 1
        account_actors[identity] = credit; account_gaps[identity] = gap['id']
        gap_labels[gap['id']] = gap.get('name') or gap.get('text') or gap['id']
        item = row(identity, 'account', account.get('name') or identity)
        item.update(account_id=identity, gap_id=gap['id'], gap={'id': gap['id'], 'label': gap_labels[gap['id']]}, attribution=public_actor(credit))
        total = totals[('account', identity)]
        item['metrics'].update(account_count=1, account_gap_count=1, explored_gap_count=1,
            claim_count=len(set(account['component_claims'])), researcher_count=int(credit is not None),
            net_votes=total['score'], upvotes=total['upvotes'], downvotes=total['downvotes'],
            voter_count=total['upvotes'] + total['downvotes'])
        accounts[identity] = item
        if credit: researcher_accounts[researcher(credit)].add(identity)
        for snapshot in snapshots:
            document = snapshot['document']
            for node in document.get('claims', []): claim_labels.setdefault(node['id'], node.get('name') or node['id'])
            for collection in ('files', 'c2m2_files'):
                for node in document.get(collection, []): file_labels.setdefault(node['id'], node.get('name') or node.get('filename') or node['id'])
            traced = collect(document, identity)
            exclusions['excluded_evidence_paths'] += traced['excluded_paths']
            for use in traced['uses']:
                if evidence == 'supporting' and use['direction'] != 'SUPPORTS': continue
                dataset_uses[use['dataset_id']].append({**use, 'account_id': identity})

    exploration_copies = defaultdict(list)
    for pub, frozen in analysis_outcomes.published(tx):
        value = frozen.get('record', {})
        if fixture(pub['data'], frozen, value): exclusions['fixture_explorations'] += 1; continue
        if (value.get('id') != pub['id'] or value.get('outcome') != 'insufficient_evidence'
                or not value.get('knowledge_gap', {}).get('id')): continue
        exploration_copies[value['id']].append(value)
    for identity, snapshots in sorted(exploration_copies.items()):
        credit, _ = credited([value.get('attribution') for value in snapshots])
        if not credit: exclusions['uncredited_explorations'] += 1; continue
        value = sorted(snapshots, key=digest)[0]; key = researcher(credit)
        gap = value['knowledge_gap']; gap_labels.setdefault(gap['id'], gap.get('name') or gap.get('text') or gap['id'])
        researcher_explorations[key][identity] = value

    for identity, item in accounts.items():
        account = copies[identity][0]['summary']['account']; gap_id = account_gaps[identity]
        own = record(identity, 'account', item['label'], account_ids=[identity], claim_ids=account['component_claims'])
        records[('accounts', identity)]['accounts'] = [own]
        records[('accounts', identity)]['votes'] = [{**own, 'value': item['metrics']['net_votes']}]
        records[('accounts', identity)]['claims'] = [record(cid, 'claim', claim_labels.get(cid, cid), account_ids=[identity], claim_ids=[cid]) for cid in sorted(set(account['component_claims']))]
        records[('accounts', identity)]['gaps'] = [record(gap_id, 'gap', gap_labels[gap_id], account_ids=[identity])]
        credit = account_actors[identity]
        records[('accounts', identity)]['researchers'] = [record(credit['id'], 'researcher', credit['label'], account_ids=[identity])] if credit else []

    for identity, item in researchers.items():
        ids = sorted(researcher_accounts[identity]); public_gaps = {account_gaps[aid] for aid in ids}
        explorations = researcher_explorations[identity]
        explored = public_gaps | {value['knowledge_gap']['id'] for value in explorations.values()}
        community = {aid: {user: value for user, value in ballots[aid].items() if user != account_actors[aid]['internal_id']} for aid in ids}
        # Canonical vote totals remain authoritative, minus this original actor's ballot.
        values = {aid: accounts[aid]['metrics']['net_votes'] - ballots[aid].get(account_actors[aid]['internal_id'], 0) for aid in ids}
        item['metrics'].update(account_count=len(ids), account_gap_count=len(public_gaps), explored_gap_count=len(explored),
            net_votes=sum(values.values()),
            upvotes=sum(accounts[aid]['metrics']['upvotes'] - int(ballots[aid].get(account_actors[aid]['internal_id']) == 1) for aid in ids),
            downvotes=sum(accounts[aid]['metrics']['downvotes'] - int(ballots[aid].get(account_actors[aid]['internal_id']) == -1) for aid in ids),
            voter_count=len({user for values in community.values() for user in values}))
        details = records[('researchers', identity)]
        details['accounts'] = [deepcopy(records[('accounts', aid)]['accounts'][0]) for aid in ids]
        details['votes'] = [{**deepcopy(records[('accounts', aid)]['accounts'][0]), 'value': values[aid]} for aid in ids]
        details['gaps'] = [record(gid, 'gap', gap_labels[gid], account_ids=[aid for aid in ids if account_gaps[aid] == gid]) for gid in sorted(public_gaps)]
        # A gap is the counted unit; link to the public gap where its public
        # accounts and published explorations can both be inspected.
        details['explored'] = [record(gid, 'gap', gap_labels[gid], account_ids=[aid for aid in ids if account_gaps[aid] == gid]) for gid in sorted(explored)]

    for identity, uses in sorted(dataset_uses.items()):
        labels = sorted({use['dataset_label'] for use in uses if use.get('dataset_label')})
        item = row(identity, 'dataset', labels[0] if labels else identity)
        a_ids = {use['account_id'] for use in uses}; c_ids = {use['claim_id'] for use in uses}
        g_ids = {account_gaps[aid] for aid in a_ids}; r_ids = {account_actors[aid]['id'] for aid in a_ids if account_actors[aid]}
        item['metrics'].update(account_count=len(a_ids), claim_count=len(c_ids), account_gap_count=len(g_ids), researcher_count=len(r_ids))
        item['directions'] = {direction: len({use['claim_id'] for use in uses if use['direction'] == direction}) for direction in DIRECTIONS}
        datasets[identity] = item; details = records[('datasets', identity)]
        for metric, values, kind, label in [('accounts', a_ids, 'account', lambda key: accounts[key]['label']),
                ('claims', c_ids, 'claim', lambda key: claim_labels.get(key, key)),
                ('gaps', g_ids, 'gap', lambda key: gap_labels[key]),
                ('researchers', r_ids, 'researcher', lambda key: researchers[key]['label']),
                ('files', {file for use in uses for file in use['file_ids']}, 'file', lambda key: file_labels.get(key, key))]:
            details[metric] = []
            for key in sorted(values):
                selected = [use for use in uses if {'accounts': use['account_id'] == key, 'claims': use['claim_id'] == key,
                    'gaps': account_gaps[use['account_id']] == key,
                    'researchers': (account_actors[use['account_id']] or {}).get('id') == key,
                    'files': key in use['file_ids']}[metric]]
                detail_directions = {direction: len({use['claim_id'] for use in selected if use['direction'] == direction}) for direction in DIRECTIONS}
                details[metric].append(record(key, kind, label(key), account_ids=[use['account_id'] for use in selected],
                    claim_ids=[use['claim_id'] for use in selected], evidence_ids=[use['evidence_id'] for use in selected], direction=detail_directions))

    traced_datasets(tx, scope, copies, frozen_sources, fixture_ids, datasets, records, accounts, claim_labels)

    cohort = len(researchers)
    for item in researchers.values():
        components = {}
        for component, metric in [('accounts', 'account_count'), ('votes', 'net_votes'), ('gaps', 'account_gap_count')]:
            value = item['metrics'][metric]
            below = sum(other['metrics'][metric] < value for other in researchers.values())
            tied = sum(other['metrics'][metric] == value for other in researchers.values())
            components[component] = Fraction(100 * (2 * below + tied), 2 * cohort) if value > 0 else Fraction(0)
        item['components'] = {key: float(value) for key, value in components.items()}
        item['metrics']['overall_score'] = float((3 * components['accounts'] + 4 * components['votes'] + 3 * components['gaps']) / 10)
    metadata = {'as_of': now(), 'score_version': SCORE_VERSION, 'cohort_size': cohort,
        'voting_participants': len({user for values in ballots.values() for user in values}), 'exclusions': exclusions,
        'definitions': deepcopy(DEFINITIONS), 'methodology': deepcopy(METHODOLOGY)}
    rows = {'researchers': list(researchers.values()), 'accounts': list(accounts.values()), 'datasets': list(datasets.values())}
    # Hash only public projection inputs, never an identity, snapshot or ballot
    # key in a client-readable cursor. as_of is intentionally not a revision.
    revision = digest([rows, sorted((list(key), value) for key, value in records.items()),
                       {key: value for key, value in metadata.items() if key != 'as_of'}])
    return {'rows': rows, 'records': records, 'metadata': metadata, 'revision': revision}


def traced_datasets(tx, scope, copies, frozen_sources, fixture_ids, datasets, records, accounts, claim_labels):
    """Add the traced dataset metrics from stored account data sources. Public copies use the record frozen with their
    snapshot; scope='all' adds each other saved account's own record. Private units count but are never listed."""
    from .account_data_sources import KIND, current, owner_key, publication_key
    uses, names, locations, organizations, credits, kinds = [], {}, {}, {}, defaultdict(lambda: defaultdict(set)), {}

    def add(data, account_id, public):
        if not data or not current(data) or not isinstance(data.get('claims'), list): return
        for item in data.get('datasets') or []:
            names.setdefault(item['id'], item.get('name'))
            if item.get('location'): locations.setdefault(item['id'], item['location'])
            # A trait's GWAS bottom-line artifact (the release names it; its url is the portal artifact page).
            if item.get('kind') == GWAS_BOTTOM_LINE: kinds.setdefault(item['id'], GWAS_BOTTOM_LINE)
        # Credited organizations are public reference lineage, recorded per dataset: never per account.
        for item in data.get('organizations') or []: organizations.setdefault(item['id'], item)
        for claim in data['claims']:
            for connection in claim.get('datasets') or []:
                uses.append((connection['dataset_id'], claim['claim_id'], account_id, public))
                for credit in connection.get('organizations') or []:
                    credits[connection['dataset_id']][credit['organization_id']].update(credit.get('roles') or [])

    for identity in sorted(copies):
        for owner, snapshot_id in sorted(frozen_sources[identity]):
            frozen = tx.get(KIND, publication_key(snapshot_id))
            if frozen and frozen['owner'] == owner and current(frozen['data']): add(frozen['data'], identity, True); break
    if scope == 'all':
        saved = {}
        for member in sorted(tx.list('account_membership'), key=lambda value: (value['data'].get('account_id') or '', value['owner'])):
            identity = member['data'].get('account_id')
            if isinstance(identity, str) and identity not in copies and identity not in fixture_ids: saved.setdefault(identity, member['owner'])
        found = tx.get_records([key for identity, owner in saved.items()
                                for key in (('account', digest([owner, identity])), (KIND, owner_key(owner, identity)))])
        for identity, owner in sorted(saved.items()):
            account, stored = found.get(('account', digest([owner, identity]))), found.get((KIND, owner_key(owner, identity)))
            if not account or account['owner'] != owner or not stored or stored['owner'] != owner: continue
            if fixture(account['data'].get('result'), account['data'].get('summary')): continue
            add(stored['data'], identity, False)
    by_dataset = defaultdict(list)
    for dataset, claim, account, public in uses: by_dataset[dataset].append((claim, account, public))
    for identity, entries in sorted(by_dataset.items()):
        item = datasets.setdefault(identity, row(identity, 'dataset', names.get(identity) or identity))
        claims, linked = {claim for claim, _, _ in entries}, {account for _, account, _ in entries}
        item['metrics'].update(traced_claim_count=len(claims), traced_account_count=len(linked))
        item['url'] = item['url'] or locations.get(identity)
        if identity in kinds: item['source_kind'] = kinds[identity]
        item['organizations'] = [{'id': key, 'name': (organizations.get(key) or {}).get('name') or key,
                                  'ror': (organizations.get(key) or {}).get('ror'), 'roles': sorted(roles)}
                                 for key, roles in sorted(credits[identity].items())]
        public_claims = sorted({claim for claim, _, public in entries if public})
        public_accounts = sorted({account for _, account, public in entries if public})
        details = records[('datasets', identity)]
        details['traced_claims'] = [record(claim, 'claim', claim_labels.get(claim, claim), claim_ids=[claim],
            account_ids=[account for value, account, public in entries if public and value == claim]) for claim in public_claims]
        details['traced_accounts'] = [record(account, 'account', accounts[account]['label'], account_ids=[account],
            claim_ids=[claim for claim, value, public in entries if public and value == account]) for account in public_accounts]
        details['withheld'] = {'traced_claims': len(claims) - len(public_claims), 'traced_accounts': len(linked) - len(public_accounts)}


def ranking(projection, view, sort):
    metric = METRIC_KEYS[sort]
    items = sorted(deepcopy(projection['rows'][view]), key=lambda item: (-item['metrics'][metric], item['id']))
    # A dataset is ranked only by measures it has: a traced-only dataset has no evidence ranking, and vice versa.
    if view == 'datasets': items = [item for item in items if item['metrics'][metric] > 0]
    previous, rank = None, 0
    for index, item in enumerate(items, 1):
        value = item['metrics'][metric]
        if previous is None or value != previous: rank = index
        item['rank'] = rank; previous = value
    return items


def details(projection, view, identity, metric):
    """(entry, the public records counted by metric, the number of private units it also counts)."""
    sort = 'traced_claims' if metric in TRACED else SORTS[view][0]
    entry = next((item for item in ranking(projection, view, sort) if item['id'] == identity), None)
    if entry is None: raise Problem(404, 'NOT_FOUND', 'The public leaderboard contribution is unavailable.')
    detail = projection['records'].get((view, identity), {})
    return entry, detail.get(metric, []), detail.get('withheld', {}).get(metric, 0)
