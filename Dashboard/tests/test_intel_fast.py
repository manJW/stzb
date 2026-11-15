import json
import threading
import time
import unittest
from unittest.mock import patch
import test_intelligence as fixtures
battle,world=fixtures.battle,fixtures.world
from intel_fast import read
from read_cache import ReadCache

class FastReads(unittest.TestCase):
    setUp=fixtures.IntelligenceTests.setUp
    tearDown=fixtures.IntelligenceTests.tearDown
    def sample(self):
        self.archive.ingest(5026,world(aid='9001',end=300),250)
        for bid,name,union in [(1,'enemy',1008),(2,'enemy-two',1008),(3,'ally',1009)]:
            report=battle(bid,300+bid,main=True)
            report.update(attack_name=name,attack_unionid=union,attack_role_id=name+'-role',attack_idu=str(9000+bid)+',11,12,13,0')
            self.archive.ingest(92,[[report]],310+bid)
        return self.archive.intelligence
    def test_summary_matches_full_reference_across_filters(self):
        intel=self.sample()
        for q in ({'relation':'all'},{'relation':'enemy'},{'relation':'ally'},{'relation':'all','search':'enemy'},
                  {'relation':'all','region':'500,600,520,620'},{'relation':'all','from_at':301,'to_at':302},
                  {'relation':'all','army_id':'9001'},{'union_id':'1008'}):
            with self.subTest(q=q):
                f=intel.filters(q);old=intel.summary(f);new=read(intel,'summary',f)
                self.assertEqual({k:new[k]for k in old},old)
    def test_pages_preserve_counts_and_only_enrich_selected_records(self):
        intel=self.sample();f=intel.filters({'relation':'all'})
        reference=intel.encounters(f)
        with patch('intel_fast.side_heroes',wraps=__import__('intel_fast').side_heroes) as decode:
            page=read(intel,'encounters',f,1,1)
            self.assertEqual(len(page['items']),1);self.assertEqual(page['total'],len(reference))
            self.assertEqual(decode.call_count,1)
        original={(r['battle_id'],r['side']):r for r in reference}
        row=page['items'][0];expected=original[row['battle_id'],row['side']]
        self.assertEqual({k:row[k]for k in expected},expected)
        old=intel.personnel(f)
        page=read(intel,'personnel',f,1,1)
        self.assertEqual(page['total'],len(old))
        self.assertEqual([{k:r[k]for k in old[1]}for r in page['items']],old[1:2])
    def test_player_and_hero_search_remain_exact(self):
        intel=self.sample()
        for search in ['enemy','100701','转','武将','曹','9001','not-present']:
            f=intel.filters({'relation':'all','search':search})
            actual=read(intel,'encounters',f)['items'];expected=intel.encounters(f)
            self.assertEqual({(r['battle_id'],r['side'])for r in actual},{(r['battle_id'],r['side'])for r in expected},search)
    def test_operations_keep_battle_links_and_sources(self):
        intel=self.sample();f=intel.filters({'relation':'all'})
        old=intel.operations(f);new=read(intel,'operations',f)
        self.assertEqual(new['total'],len(old))
        for row in new['items']:
            expected=next(r for r in old if r['id']==row['id'])
            for key in expected:self.assertEqual(row[key],expected[key])
    def test_army_filter_keeps_exact_and_unique_arrival_reports(self):
        intel=self.sample()
        for army in ('9001','9002','9003','not-known'):
            f=intel.filters({'relation':'all','army_id':army})
            old=intel.encounters(f);new=read(intel,'encounters',f)['items']
            self.assertEqual([(r['battle_id'],r['side'])for r in new],[(r['battle_id'],r['side'])for r in old])
    def test_npc_guards_do_not_reappear_as_landowner_formations(self):
        intel=self.sample();npc=battle(9,500,main=True);npc['npc']=1;npc['defend_idu']='0,0,0,0,0'
        npc['defend_name']='guard-owner';self.archive.ingest(92,[[npc]],501)
        rows=read(intel,'personnel',intel.filters({'relation':'all'}))['items']
        self.assertTrue(all(not r['lineups']for r in rows if r['player_name']=='guard-owner'))

class CacheTests(unittest.TestCase):
    def test_identical_concurrent_queries_share_one_result(self):
        cache=ReadCache();entered=threading.Event();release=threading.Event();calls=[];results=[]
        def compute():calls.append(1);entered.set();release.wait(2);return {'ok':True}
        workers=[threading.Thread(target=lambda:results.append(cache.run(('role-a','same'),compute)))for _ in range(6)]
        for worker in workers:worker.start()
        self.assertTrue(entered.wait(1));release.set()
        for worker in workers:worker.join(2)
        self.assertEqual(len(calls),1);self.assertEqual(len(results),6)
        self.assertEqual(cache.run(('role-a','same'),lambda:None),{'ok':True})
        self.assertEqual(cache.run(('role-b','same'),lambda:{'other':True}),{'other':True})
    def test_expired_and_failed_results_are_not_reused(self):
        cache=ReadCache(ttl=0)
        self.assertEqual(cache.run('a',lambda:1),1);self.assertEqual(cache.run('a',lambda:2),2)
        with self.assertRaises(ValueError):cache.run('bad',lambda:(_ for _ in ()).throw(ValueError('bad')))
        self.assertEqual(cache.run('bad',lambda:3),3)
        self.assertFalse(cache.pending)
