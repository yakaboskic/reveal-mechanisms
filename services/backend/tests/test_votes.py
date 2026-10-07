"""Vote admission, exact public identities, concurrent totals and viewer-bound pages."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import time
import unittest
from urllib.parse import urlencode
from unittest.mock import Mock, patch

import jwt
from reveal_backend import app as api, votes
from reveal_backend.repository import Transaction, digest, uid
import test_publication as published


def keyed_states(tx, targets, user=None):
    """The exact-key reference: states() before the catalog-wide read (one get_records batch per 250 keys)."""
    targets = set(targets)
    records = tx.get_records([('vote_total', digest(target)) for target in targets]+
        ([('vote', digest([user, *target])) for target in targets] if user else []))
    result = {}
    for kind, identity in targets:
        total = records.get(('vote_total', digest([kind, identity])), {}).get('data', {})
        ballot = records.get(('vote', digest([user, kind, identity]))) if user else None
        mine = ballot['data']['vote'] if ballot and ballot['owner'] == user else 0
        up, down = total.get('upvotes', 0), total.get('downvotes', 0)
        result[(kind, identity)] = {'upvotes': up, 'downvotes': down, 'score': up-down, 'user_vote': mine if user else None}
    return result


class VotingTests(unittest.TestCase):
    setUp = published.PublicationTests.setUp
    seed = published.PublicationTests.seed
    principal = published.PublicationTests.principal
    request = published.PublicationTests.request

    def headers(self, owner):
        with self.repo.read_transaction() as tx: kind=tx.get('principal',owner)['data']['me']['principal_kind']
        stamp=int(time.time())
        return {'Authorization':'Bearer '+jwt.encode({'sub':owner,'principal_kind':kind,
            'iss':'reveal-nextjs','aud':'reveal-api','iat':stamp,'exp':stamp+120,'jti':uid()},'s'*40,algorithm='HS256')}

    def gap_route(self, identity=None):
        return '/v1/knowledge-gaps/'+(identity or self.gap['object']['id'])+'/vote'

    def vote(self, value, *, owner=None, route=None, key=None, body=None):
        return self.request('post',route or self.gap_route(),owner or self.owner,
            headers={'Idempotency-Key':key or uid()},json={'vote':value} if body is None else body)

    def publish(self, visibility='public', version=0, owner=None):
        response=self.request('post',self.control,owner or self.owner,
            headers={'Idempotency-Key':uid()},json={'visibility':visibility,'expected_version':version})
        self.assertEqual(response.status_code,200,response.text)

    def test_double_vote_change_clear_and_stale_idempotent_replay(self):
        key=uid(); first=self.vote(1,key=key)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json(),{'upvotes':1,'downvotes':0,'score':1,'user_vote':1})
        self.assertEqual(self.vote(1,key=key).json(),first.json())
        self.assertEqual(self.vote(1).json(),first.json())
        self.assertEqual(self.vote(-1).json(),{'upvotes':0,'downvotes':1,'score':-1,'user_vote':-1})
        # An older HTTP retry never overwrites the user's later choice.
        self.assertEqual(self.vote(1,key=key).json(),first.json())
        self.assertEqual(self.request('get',self.gap_route(),self.owner).json()['user_vote'],-1)
        self.assertEqual(self.vote(-1,key=key).status_code,409)
        self.assertEqual(self.vote(0).json(),{'upvotes':0,'downvotes':0,'score':0,'user_vote':0})
        self.assertEqual(self.vote(0).json()['score'],0)
        with self.repo.read_transaction() as tx:
            self.assertEqual(len(tx.list('vote')),1)
            self.assertEqual(len(tx.list('vote_total')),1)

    def test_signed_in_only_and_current_auth_before_idempotent_replay(self):
        self.assertEqual(self.client.post(self.gap_route(),json={'vote':1},headers={'Idempotency-Key':uid()}).status_code,401)
        anonymous=self.principal()
        with self.repo.transaction() as tx:
            row=tx.get('principal',anonymous)['data']; row['me']['principal_kind']='anonymous'; tx.put('principal',anonymous,anonymous,row)
        self.assertEqual(self.vote(1,owner=anonymous).status_code,403)
        self.assertEqual(self.request('get',self.gap_route(),anonymous).json()['user_vote'],None)
        key=uid(); self.assertEqual(self.vote(1,key=key).status_code,200)
        with self.repo.transaction() as tx:
            row=tx.get('principal',self.owner)['data']; row['retired']=True; tx.put('principal',self.owner,self.owner,row)
        self.assertEqual(self.vote(1,key=key).status_code,401)
        self.assertEqual(self.request('get',self.gap_route(),self.owner).status_code,401)
        self.assertEqual(self.client.get(self.gap_route(),headers={'Authorization':'Bearer invalid'}).status_code,401)

    def test_viewers_only_see_their_own_vote_and_public_totals(self):
        self.vote(1); self.vote(-1,owner=self.other)
        for owner,expected in ((self.owner,1),(self.other,-1),(None,None)):
            result=self.request('get',self.gap_route(),owner)
            self.assertEqual(result.status_code,200,result.text); api.validate(result.json(),'VoteState')
            self.assertEqual(result.json(),{'upvotes':1,'downvotes':1,'score':0,'user_vote':expected})
            self.assertNotIn(self.owner,result.text); self.assertNotIn(self.other,result.text)

    def test_private_unknown_and_unpublished_accounts_do_not_allow_votes_or_replay(self):
        route='/v1/accounts/'+self.account_id+'/vote'
        missing='/v1/accounts/dapper:ScientificAccount.'+'x'*32+'/vote'
        for path in (route,missing):
            for owner in (self.owner,self.other,None):
                self.assertEqual(self.request('get',path,owner).status_code,404)
            self.assertEqual(self.vote(1,route=path).status_code,404)
        self.assertIsNone(self.request('get','/v1/accounts',self.owner).json()['items'][0]['votes'])
        self.publish(); key=uid(); result=self.vote(1,route=route,key=key)
        self.assertEqual(result.status_code,200,result.text)
        with self.repo.read_transaction() as tx:
            data=tx.list('vote')[0]['data']; self.assertEqual(data['gap_id'],self.gap['object']['id'])
        # Account and gap counts are independent, even for the same user.
        self.assertEqual(self.request('get',self.gap_route(),self.owner).json()['score'],0)
        self.publish('private',1)
        self.assertEqual(self.request('get',route,self.owner).status_code,404)
        self.assertEqual(self.vote(1,route=route,key=key).status_code,404)
        self.publish('public',2)
        self.assertEqual(self.request('get',route,self.owner).json()['upvotes'],1)

    def test_independent_publications_share_canonical_account_vote_and_exact_gap(self):
        self.seed(self.other); self.publish(); self.publish(owner=self.other)
        route='/v1/accounts/'+self.account_id+'/vote'
        self.vote(1,route=route); self.vote(-1,owner=self.other,route=route)
        self.publish('private',1)
        summary=self.request('get','/v1/knowledge-gaps/'+self.gap['object']['id']+'/accounts',self.owner).json()['items'][0]
        self.assertEqual(summary['votes'],{'upvotes':1,'downvotes':1,'score':0,'user_vote':1})
        self.assertFalse(summary['publication']['can_manage'])
        self.assertIsNone(summary['job_id'])
        self.publish('private',1,owner=self.other)
        self.assertEqual(self.request('get',route).status_code,404)

    def test_every_vote_wakes_other_viewers_on_the_public_catalog_collection(self):
        # An open Composer refreshes its gap vote only on 'catalog' invalidations, never on its own draft or
        # exploration echoes, so gap and account votes (cast, changed or cleared) must each commit one.
        from reveal_backend import workspace_events as events
        def public(positions):
            _, items, highwater, _ = events.replay(self.repo, self.headers(self.other)['Authorization'], positions)
            return [(item['event_type'], item['entity_id'], item['collections']) for item in items if item['scope'] == 'public'], highwater
        self.publish(); route = '/v1/accounts/' + self.account_id + '/vote'
        _, positions = public({'workspace': 0, 'public': 0})
        for value, path in ((1, None), (-1, None), (0, None), (1, route)):
            with self.subTest(value=value, account=bool(path)):
                self.assertEqual(self.vote(value, route=path).status_code, 200)
                changes, positions = public(positions)
                self.assertEqual(changes, [('catalog.updated', 'catalog', ['catalog', 'accounts', 'gaps', 'explorations'])])

    def test_known_catalog_gap_required_and_body_is_closed(self):
        self.assertEqual(self.vote(1,route=self.gap_route('missing-gap')).status_code,404)
        for body in ({'vote':True},{'vote':2},{'vote':1,'owner':self.other},{'vote':1,'gap_id':self.gap['object']['id']},{}):
            with self.subTest(body=body): self.assertEqual(self.vote(1,body=body).status_code,422)
        self.assertEqual(self.request('post',self.gap_route(),self.owner,json={'vote':1}).status_code,400)

    def test_concurrent_same_user_and_distinct_user_writes_do_not_double_count(self):
        identity=self.gap['object']['id']
        owners=[self.owner]*8+[self.principal() for _ in range(8)]
        def cast(owner):
            with self.repo.transaction() as tx: return votes.change(tx,'gap',identity,identity,owner,1)
        with ThreadPoolExecutor(max_workers=8) as pool: list(pool.map(cast,owners))
        result=self.request('get',self.gap_route()).json()
        self.assertEqual(result['upvotes'],9); self.assertEqual(result['downvotes'],0)
        def change(value):
            with self.repo.transaction() as tx: return votes.change(tx,'gap',identity,identity,self.owner,value)
        with ThreadPoolExecutor(max_workers=8) as pool: list(pool.map(change,[1,-1,0,1,-1,0,1,-1]))
        with self.repo.read_transaction() as tx:
            ballots=tx.list('vote'); total=votes.states(tx,[('gap',identity)])[('gap',identity)]
        self.assertEqual(total['upvotes'],sum(row['data']['vote']==1 for row in ballots))
        self.assertEqual(total['downvotes'],sum(row['data']['vote']==-1 for row in ballots))

    def test_failed_transaction_rolls_back_ballot_aggregate_and_notifications(self):
        identity=self.gap['object']['id']
        with self.assertRaises(RuntimeError):
            with self.repo.transaction() as tx:
                votes.change(tx,'gap',identity,identity,self.owner,1)
                raise RuntimeError('failure before commit')
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.list('vote'),[]); self.assertEqual(tx.list('vote_total'),[])

    def test_trending_score_then_public_accounts_and_cursor_is_viewer_sort_vote_bound(self):
        original=self.gap['object']['id']
        others=[]
        for letter in 'xy':
            gap=deepcopy(self.gap); gap['object']['id']='dapper:KnowledgeGap.'+letter*32
            gap['source']['source_id']='dismech:'+letter
            self.catalog.gaps[gap['object']['id']]=gap; self.catalog.by_source[gap['source']['source_id']]=gap
            others.append(gap['object']['id'])
        self.publish()  # Default accounts order puts this gap first.
        self.vote(1,route=self.gap_route(others[0])); self.vote(-1,route=self.gap_route(others[1]))
        def browse(owner=self.owner,**query):
            return self.request('get','/v1/knowledge-gaps?'+urlencode(query),owner)
        self.assertEqual(browse().json()['items'][0]['object']['id'],original)
        response=browse(sort='votes',limit=1); first=response.json(); api.validate(first,'GapList')
        self.assertEqual(first['items'][0]['object']['id'],others[0])
        cursor=first['page']['next_cursor']
        self.assertEqual(browse(sort='votes',cursor=cursor,limit=1).json()['items'][0]['object']['id'],original)
        self.assertEqual(browse(sort='accounts',cursor=cursor).status_code,409)
        self.assertEqual(browse(self.other,sort='votes',cursor=cursor).status_code,409)
        self.vote(1)
        self.assertEqual(browse(sort='votes',cursor=cursor).status_code,409)
        self.assertEqual(browse(sort='votes').json()['items'][0]['object']['id'],original)
        self.assertEqual(browse(sort='bad').status_code,422)

    def test_search_and_gap_detail_embed_current_viewer_votes(self):
        self.vote(-1)
        detail=self.request('get','/v1/knowledge-gaps/'+self.gap['object']['id'],self.owner)
        self.assertEqual(detail.json()['votes']['user_vote'],-1); api.validate(detail.json(),'GapRecord')
        query=self.gap['object']['text'].split()[0]
        searched=self.request('get','/v1/knowledge-gaps/search?'+urlencode({'q':query}),self.owner)
        self.assertEqual(searched.json()['items'][0]['gap']['votes']['user_vote'],-1)
        public=self.request('get','/v1/knowledge-gaps/'+self.gap['object']['id'])
        self.assertIsNone(public.json()['votes']['user_vote'])
        self.assertEqual(public.headers['cache-control'],'private, no-store')
        self.assertIn('Authorization',public.headers['vary'])

    def statements(self, call):
        executed = []; original = Transaction.execute
        def counted(tx, sql, params=()): executed.append(sql); return original(tx, sql, params)
        with patch.object(Transaction, 'execute', counted): result = call()
        return result, executed

    def test_catalog_wide_states_read_stored_rows_once_and_match_exact_keys(self):
        targets = [('gap', 'dapper:KnowledgeGap.%032x' % index) for index in range(300)] + [('account', 'dapper:ScientificAccount.' + 'a' * 32)]
        third = self.principal(); fourth = self.principal()
        with self.repo.transaction() as tx:
            for index, target in enumerate(targets[:40]):
                votes.change(tx, *target, target[1], self.owner, (1, -1, 0)[index % 3])
                if index % 2: votes.change(tx, *target, target[1], self.other, 1)
                if index % 5 == 0: votes.change(tx, *target, target[1], third, -1)
            votes.change(tx, *targets[-1], self.gap['object']['id'], self.owner, 1)
            # A total stored under another target's key and a ballot whose payload names a different target
            # count only at their keys; rows under no target's key never count.
            tx.put('vote_total', digest(list(targets[50])), 'system', {'target_kind': 'gap', 'target_id': targets[51][1], 'upvotes': 7, 'downvotes': 2})
            tx.put('vote_total', 'not-a-target-key', 'system', {'target_kind': 'gap', 'target_id': targets[52][1], 'upvotes': 9, 'downvotes': 0})
            tx.put('vote', digest([self.owner, *targets[60]]), self.owner, {'target_kind': 'gap', 'target_id': targets[61][1], 'vote': -1})
            tx.put('vote', 'stray-ballot', self.owner, {'target_kind': 'gap', 'target_id': targets[62][1], 'vote': 1})
            # A transfer re-owns ballots without re-keying them: neither owner's view may count them.
            tx.transfer(third, fourth)
        for user in (None, self.owner, self.other, third, fourth):
            with self.subTest(user=user), self.repo.read_transaction() as tx:
                expected = keyed_states(tx, targets, user)
                actual, executed = self.statements(lambda: votes.states(tx, targets, user))
                self.assertEqual(actual, expected)
                self.assertEqual(len(executed), 1, executed)   # stored totals plus this viewer's ballots, whatever the target count
                self.assertNotEqual(expected[targets[50]]['upvotes'], 0)
                with patch.object(votes, 'KEYED', 0):   # a small set gives the same states on either path
                    self.assertEqual(votes.states(tx, targets[45:75], user), keyed_states(tx, targets[45:75], user))
                small, executed = self.statements(lambda: votes.states(tx, targets[45:75], user))
                self.assertEqual((small, len(executed)), (keyed_states(tx, targets[45:75], user), 1))
        with self.repo.read_transaction() as tx:
            mine = votes.states(tx, targets, self.owner)
            self.assertEqual({state['user_vote'] for state in votes.states(tx, targets, fourth).values()}, {0})
        self.assertEqual(mine[targets[60]]['user_vote'], -1); self.assertEqual(mine[targets[61]]['user_vote'], 0)
        self.assertEqual(mine[targets[62]]['user_vote'], 0); self.assertEqual(mine[targets[-1]]['user_vote'], 1)

    def test_gap_routes_read_one_snapshot_with_one_principal_lookup(self):
        for index in range(300):
            gap = deepcopy(self.gap); gap['object']['id'] = 'dapper:KnowledgeGap.%032x' % index
            gap['source']['source_id'] = 'dismech:%03d' % index; self.catalog.gaps[gap['object']['id']] = gap
        self.publish(); self.vote(1)
        principals = []; original = api.principal
        def counted(tx, authorization): principals.append(authorization); return original(tx, authorization)
        routes = ['/v1/knowledge-gaps?limit=5', '/v1/knowledge-gaps?limit=5&sort=votes', '/v1/knowledge-gaps?limit=5&scope=workspace',
                  '/v1/knowledge-gaps/search?' + urlencode({'q': self.gap['object']['text'].split()[0]}),
                  '/v1/knowledge-gaps/' + self.gap['object']['id'], '/v1/knowledge-gaps/' + self.gap['object']['id'] + '/accounts',
                  '/v1/accounts?scope=public']
        for route in routes:
            for owner in (self.owner, None):
                if owner is None and 'scope=workspace' in route: continue
                with self.subTest(route=route, owner=owner):
                    principals.clear(); reads = []; leases = Mock(side_effect=AssertionError('discovery took the write fence'))
                    original_read = self.repo.read_transaction
                    def read(*args, **kwargs): reads.append(1); return original_read(*args, **kwargs)
                    headers = self.headers(owner) if owner else {}
                    with patch.object(api, 'principal', counted), patch.object(self.repo, 'read_transaction', read), \
                            patch.object(self.repo, 'transaction', leases):
                        response = self.client.get(route, headers=headers)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual((len(reads), len(principals)), (1, 1 if owner else 0))
        listed = self.request('get', '/v1/knowledge-gaps?sort=votes&limit=1', self.owner).json()
        self.assertEqual(listed['items'][0]['votes'], {'upvotes': 1, 'downvotes': 0, 'score': 1, 'user_vote': 1})
        self.assertIsNone(self.request('get', '/v1/knowledge-gaps?sort=votes&limit=1').json()['items'][0]['votes']['user_vote'])
        self.assertEqual(self.client.get('/v1/knowledge-gaps', headers={'Authorization': 'Bearer invalid'}).status_code, 401)
        self.assertEqual(self.client.get('/v1/knowledge-gaps?scope=other').status_code, 422)
        self.assertEqual(self.client.get('/v1/knowledge-gaps?scope=workspace').status_code, 401)

