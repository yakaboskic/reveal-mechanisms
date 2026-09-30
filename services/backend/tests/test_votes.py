"""Vote admission, exact public identities, concurrent totals and viewer-bound pages."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import time
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

import jwt
from reveal_backend import app as api, votes
from reveal_backend.repository import digest, uid
import test_publication as published


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
