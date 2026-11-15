import unittest

import test_intelligence as fixtures
from archive_timeline import report_bindings


class ArchiveBindingsTests(unittest.TestCase):
    setUp = fixtures.IntelligenceTests.setUp
    tearDown = fixtures.IntelligenceTests.tearDown

    def record(self, bid, at, army=0, cards=(11,12,13), **changes):
        report = fixtures.battle(bid,at,main=True)
        report.update(attack_idu=','.join(map(str,(army,*cards,0))),**changes)
        self.archive.ingest(92,[[report]],at+1)
        return report

    def groups(self, view='teams', **query):
        return self.archive.intelligence.query('team-log-groups',view=view,
            group_name='enemy',group_union='1008',group_identity='',**query)

    def test_same_cards_follow_unique_army_anchor_and_slot_reordering(self):
        self.archive.ingest(5026,fixtures.world(end=300),250)
        self.record(1,300)
        newer=self.record(2,400,cards=(12,11,13))
        slots=newer['attack_all_hero_info'].split(';')
        newer['attack_all_hero_info']=';'.join([slots[1],slots[0],slots[2],''])
        self.archive.ingest(92,[[newer]],402)
        binding=report_bindings(self.archive,'enemy','1008')
        self.assertEqual(binding['1','attack']['army_id'],'9001')
        self.assertEqual(binding['2','attack']['army_id'],'9001')
        self.assertEqual(binding['2','attack']['binding'],'same_card_instances')
        teams=self.groups()['items']
        self.assertEqual([t['team_ref']for t in teams],['9001'])
        events=self.groups('events',team='9001')['items']
        self.assertEqual({e['id']for e in events if e['kind']=='battle'},{'1','2'})
        self.assertEqual(self.groups('events',team='pending')['items'],[])
        detail=self.archive.intelligence.query('team-log-detail',entry='battle',id='2',side='attack',grouped='1')
        self.assertEqual(detail['army_id'],'9001')
        self.assertIsNone(detail['march'],'a card link must not invent a missing route')
        filtered=self.groups('events',team='9001',army_id='9001')['items']
        self.assertEqual({e['id']for e in filtered if e['kind']=='battle'},{'1','2'})

    def test_same_heroes_different_cards_are_separate_report_teams(self):
        self.record(1,100,cards=(11,12,13))
        self.record(2,200,cards=(21,22,23))
        teams=self.groups()['items']
        self.assertEqual(len(teams),2)
        self.assertTrue(all(t['report_only'] and not t['army_id'] for t in teams))
        self.assertEqual(len({t['team_ref']for t in teams}),2)
        for team in teams:
            events=self.groups('events',team=team['team_ref'])['items']
            self.assertEqual(len(events),1)
        player=self.archive.intelligence.query('team-log-groups',player='enemy')['items'][0]
        self.assertEqual((player['team_count'],player['report_only_team_count'],player['pending_count']),(2,2,0))

    def test_late_anchor_moves_reports_out_of_report_only_group(self):
        self.record(1,100)
        first=self.groups()['items'][0]
        self.assertTrue(first['report_only'])
        self.archive.ingest(5026,fixtures.world(end=300),250)
        self.record(2,300)
        teams=self.groups()['items']
        self.assertEqual([t['team_ref']for t in teams],['9001'])
        self.assertFalse(teams[0]['report_only'])
        self.assertEqual(teams[0]['record_count'],3)
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM battle_reports').fetchone()[0],2)

    def test_two_army_anchors_for_same_cards_do_not_guess_an_army(self):
        self.record(1,100,army=9001)
        self.record(2,200,army=9002)
        self.record(3,300)
        bindings=report_bindings(self.archive,'enemy','1008')
        self.assertEqual(bindings['3','attack']['army_id'],'')
        self.assertTrue(bindings['3','attack']['formation_key'])
        self.assertNotEqual(bindings['1','attack']['army_id'],bindings['2','attack']['army_id'])

    def test_two_cards_or_wrong_role_or_template_do_not_link_to_known_army(self):
        self.record(1,100,army=9001)
        self.record(2,200,cards=(11,12,0))
        self.record(3,300,attack_role_id='another-role')
        changed=self.record(4,400)
        changed['attack_all_hero_info']=changed['attack_all_hero_info'].replace('100701','100814')
        self.archive.ingest(92,[[changed]],402)
        bindings=report_bindings(self.archive,'enemy','1008')
        self.assertTrue(all(not bindings[str(bid),'attack']['army_id'] for bid in (2,3,4)))
        self.assertEqual(bindings['2','attack']['formation_key'],'')

    def test_removed_arrival_link_invalidates_cached_membership(self):
        self.archive.ingest(5026,fixtures.world(end=300),250)
        self.record(1,300)
        self.assertEqual(report_bindings(self.archive,'enemy','1008')['1','attack']['army_id'],'9001')
        self.archive.db.execute("UPDATE battle_march_links SET method='ambiguous_marches'")
        self.assertEqual(report_bindings(self.archive,'enemy','1008')['1','attack']['army_id'],'')

    def test_time_scope_does_not_use_future_anchor_and_pagination_is_disjoint(self):
        for bid,at in ((1,100),(2,150),(3,200)):
            self.record(bid,at)
        self.record(4,300,army=9001)
        early=report_bindings(self.archive,'enemy','1008',end=250)
        self.assertTrue(all(not e['army_id'] for e in early.values()))
        current=report_bindings(self.archive,'enemy','1008')
        self.assertEqual(current['1','attack']['army_id'],'9001')
        first=self.groups('events',team='9001',limit='2')
        second=self.groups('events',team='9001',limit='2',offset='2')
        self.assertTrue(first['has_more'])
        self.assertFalse({e['id']for e in first['items']} & {e['id']for e in second['items']})

    def test_system_guard_not_counted_as_player_team_and_report_is_retained(self):
        report=self.record(1,100,npc=1,defender_force=1000001)
        row=report_bindings(self.archive,'ally','1009')['1','defend']
        self.assertTrue(row['skip'])
        group=self.archive.intelligence.query('team-log-groups',view='teams',group_name='ally',group_union='1009')['items']
        self.assertEqual(group,[])
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM battle_reports').fetchone()[0],1)

    def test_cache_is_scoped_by_role_alliance_and_database(self):
        self.record(1,100,army=9001)
        self.record(2,200,attack_unionid=999)
        other=report_bindings(self.archive,'enemy','999')
        self.assertEqual(other['2','attack']['army_id'],'')
        identity=report_bindings(self.archive,'enemy','1008',identity='battle:wrong-role')
        self.assertEqual(identity,{})


if __name__=='__main__':unittest.main()
