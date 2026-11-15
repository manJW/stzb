"""Wanderer identity comes from the client's force code, never alliance absence."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from test_support import Archive
from defense import historical_cases
from lineup_prediction import player_history, predict_alerts
from lineup_projection import install as install_projection
from test_support import WorldState
from observation_codec import map_player_traits
from share_protocol import project_scene
from season_seed import _merge_table
from test_dashboard import scene
from test_intelligence import battle


def wanderer_scene(force=1, union=1135, order=10):
    value=scene(order)
    value[1]['200'][2]=union
    value[1]['200'][5]=force
    value[1]['200'][12]=[0,0,'聚义']
    value[6]['army'][0]=1
    value[6]['army'][9]=13
    value[6]['army'][16]='2,21;1,11;2,31;'
    value[17]=[995,890,1030,950]
    return value


class WandererTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.archive=Archive(self.root)
        self.state=WorldState(self.root)
        self.state.health['capture_source']='dashen'
        self.state.connection('live','test')
        self.state.packet(1000)

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def apply(self,value,at=1000):
        self.state.packet(at)
        self.state.apply(5026,value,at)
        self.archive.ingest(5026,value,at)
        return self.state.snapshot(at+1)

    def test_force_one_is_monitored_and_raises_own_land_incoming(self):
        answer=self.apply(wanderer_scene())
        row=answer['armies'][0]
        self.assertEqual((row['is_wanderer'],row['force_type'],row['relation']),(True,1,'enemy'))
        self.assertTrue(row['incoming'])
        self.assertTrue(answer['alerts'][0]['is_wanderer'])
        self.assertEqual(answer['alerts'][0]['army_id'],'army')

    def test_no_alliance_normal_unknown_and_npc_are_not_wanderers(self):
        for force in (0,2,1000001,None,True,'1'):
            with self.subTest(force=force):
                p=map_player_traits(wanderer_scene(force,union=0)[1]['200'])
                self.assertFalse(p['is_wanderer'])
        answer=self.apply(wanderer_scene(0,union=0))
        self.assertEqual(answer['armies'][0]['relation'],'unknown')
        self.assertEqual(answer['alerts'],[])

    def test_explicit_friendship_and_own_alliance_are_preserved(self):
        self.state.config['relations']['1135']={'relation':'friendly'}
        answer=self.apply(wanderer_scene())
        self.assertEqual(answer['armies'][0]['relation'],'friendly')
        self.assertEqual(answer['alerts'],[])
        answer=self.apply(wanderer_scene(union=1009,order=11),1002)
        self.assertEqual(answer['armies'][0]['relation'],'ally')
        self.assertTrue(answer['armies'][0]['is_wanderer'])

    def test_other_target_and_retreat_are_not_own_land_incoming(self):
        value=wanderer_scene()
        value[14]['10150926']['0'][2]=200
        value[14]['10150926']['0'][3]=1008
        answer=self.apply(value)
        self.assertEqual(answer['armies'][0]['target_side'],'other')
        self.assertEqual(answer['alerts'],[])
        value=wanderer_scene(order=11)
        value[6]['army'][0]=4
        value[6]['army'][9]=0
        answer=self.apply(value,1002)
        self.assertEqual(answer['armies'][0]['kind'],'retreat')
        self.assertEqual(answer['alerts'],[])

    def test_traits_persist_and_old_packets_do_not_undo_new_identity(self):
        self.apply(wanderer_scene())
        self.archive.ingest(5028,wanderer_scene(force=0,order=9),900)
        found=next(p for p in self.archive.query('players')['items']if p['player_key']=='world:200')
        self.assertEqual(found['force_type'],1)
        self.assertTrue(found['is_wanderer'])
        value=wanderer_scene(force=0,order=11)
        self.apply(value,1002)
        found=next(p for p in self.archive.query('players')['items']if p['player_key']=='world:200')
        self.assertFalse(found['is_wanderer'])
        history=self.archive.db.execute("SELECT data FROM observations WHERE kind='player_traits' ORDER BY observed_at").fetchall()
        self.assertEqual(len(history),2)

    def test_shared_scene_preserves_wanderer_code(self):
        value=wanderer_scene()
        value[6]['9001']=value[6].pop('army')
        projected=project_scene(value)
        self.assertEqual(projected[1]['200'][5],1)
        self.assertTrue(self.apply(projected)['armies'][0]['is_wanderer'])

    def test_unaffiliated_wanderer_reports_feed_main_matchups_and_defense(self):
        report=battle(main=True)
        report.update(attack_unionid=0,attacker_force=1)
        self.archive.ingest(92,[[report]],301)
        result=self.archive.intelligence.query('main-matchups')
        self.assertEqual(result['coverage']['reports'],1)
        self.assertEqual([r['battle_id']for r in historical_cases(self.archive.db,self.archive.profile_id,'1009',400)],['1'])
        # Removing the force proof must not reclassify an unaffiliated NPC.
        normal=copy.deepcopy(report)
        normal.update(battle_id=2,time=500,attacker_force=0,npc=1)
        self.archive.ingest(92,[[normal]],501)
        self.assertEqual(self.archive.intelligence.query('main-matchups')['coverage']['reports'],1)

    def test_unaffiliated_wanderer_pve_reference_is_human_but_npc_camp_is_not(self):
        self.apply(wanderer_scene(union=0))
        report=battle(at=900,main=True)
        report.update(attack_name='test',attack_unionid=0,attacker_force=1,npc=1,attack_hero_type='0,21,11,31,')
        self.archive.ingest(92,[[report]],901)
        history=player_history(self.archive.db,self.archive.profile_id,'test','0')
        self.assertEqual([r['battle_id']for r in history[0]],['1'])
        answer=self.state.snapshot(1001)
        prediction=predict_alerts(self.archive,answer)['army']
        self.assertEqual(prediction['display_report']['battle_id'],'1')
        report.update(battle_id=2,time=950,attacker_force=1000001)
        self.archive.ingest(92,[[report]],951)
        self.assertEqual([r['battle_id']for r in player_history(self.archive.db,self.archive.profile_id,'test','0')[0]],['1'])

    def test_enemy_archive_filters_include_wanderers_without_grouping_all_unaffiliated(self):
        self.apply(wanderer_scene(union=0))
        operations=self.archive.intelligence.query('operations')
        self.assertEqual(operations['total'],1)
        normal=wanderer_scene(force=0,union=0,order=11)
        self.apply(normal,1002)
        self.assertEqual(self.archive.intelligence.query('operations')['total'],0)

    def test_friendly_wanderer_reports_do_not_enter_enemy_only_statistics(self):
        value=json.loads((self.root/'config.json').read_text(encoding='utf-8'))
        value['relations']['1135']={'relation':'friendly'}
        (self.root/'config.json').write_text(json.dumps(value),encoding='utf-8')
        self.apply(wanderer_scene())
        report=battle(main=True)
        report.update(attack_unionid=1135,attacker_force=1)
        self.archive.ingest(92,[[report]],301)
        self.assertEqual(self.archive.intelligence.query('encounters')['total'],0)
        self.assertEqual(self.archive.intelligence.query('main-matchups')['coverage']['reports'],0)
        self.assertEqual(self.archive.intelligence.query('main-matchups',relation='all')['coverage']['reports'],1)

    def test_season_trait_merge_is_latest_only_and_accepts_old_seeds(self):
        self.archive.db.execute("ATTACH DATABASE ':memory:' AS seed")
        _merge_table(self.archive.db,'player_traits',self.archive.profile_id)
        self.archive.db.execute('''CREATE TABLE seed.player_traits(player_key TEXT,force_type INTEGER,
            clan_id INTEGER,clan_name TEXT,observed_at REAL,message_ref TEXT)''')
        self.archive.db.execute("INSERT INTO seed.player_traits VALUES('world:200',1,0,'',1000,NULL)")
        _merge_table(self.archive.db,'player_traits',self.archive.profile_id)
        self.assertEqual(self.archive.db.execute('SELECT force_type FROM player_traits').fetchone()[0],1)
        self.archive.db.execute('UPDATE seed.player_traits SET force_type=0,observed_at=900')
        _merge_table(self.archive.db,'player_traits',self.archive.profile_id)
        self.assertEqual(self.archive.db.execute('SELECT force_type FROM player_traits').fetchone()[0],1)

    def test_history_migration_only_invalidates_affected_wanderers_once(self):
        report=battle(main=True)
        report.update(attack_name='wanderer',attack_unionid=0,attacker_force=1,npc=1)
        normal={**report,'battle_id':2,'attack_name':'normal','attacker_force':0}
        self.archive.ingest(92,[[report,normal]],301)
        def versions():
            return dict(self.archive.db.execute('SELECT player_name,revision FROM lineup_player_revisions WHERE union_id=\'0\''))
        before=versions()
        self.archive.db.execute("DELETE FROM lineup_rule_migrations WHERE rule='wanderer-human-reference-1'")
        install_projection(self.archive.db,self.archive.profile_id)
        after=versions()
        self.assertEqual(after['wanderer'],before['wanderer']+1)
        self.assertEqual(after['normal'],before['normal'])
        install_projection(self.archive.db,self.archive.profile_id)
        self.assertEqual(versions(),after)

    def test_old_scene_identity_backfill_never_replays_old_armies(self):
        self.apply(wanderer_scene())
        # Earlier member formats replaced the force slot with zero.
        self.archive.ingest(5028,wanderer_scene(force=0,order=11),1002,source='member:old-client')
        with self.archive.db:
            self.archive.db.execute('DELETE FROM player_traits')
            self.archive.db.execute("DELETE FROM lineup_rule_migrations WHERE rule='player-traits-recent-scenes-1'")
        before=copy.deepcopy(self.state.armies)
        self.state.players['200'].pop('force_type')
        self.state.players['200'].pop('is_wanderer')
        self.archive.seed_player_traits()
        rows=self.archive.db.execute('SELECT * FROM player_traits').fetchall()
        self.state.restore_player_traits(rows)
        self.assertTrue(self.state.players['200']['is_wanderer'])
        self.assertEqual(self.state.armies,before)
        self.assertEqual(rows[0]['observed_at'],1000)
        self.archive.seed_player_traits()
        self.assertEqual(self.archive.db.execute('SELECT count(*) FROM player_traits').fetchone()[0],1)

    def test_new_live_identity_wins_over_historical_restore(self):
        self.apply(wanderer_scene(force=0))
        self.state.restore_player_traits([{'player_key':'world:200','force_type':1,'clan_id':0,
                                          'clan_name':'','observed_at':900}])
        self.assertFalse(self.state.players['200']['is_wanderer'])


if __name__=='__main__':
    unittest.main()
