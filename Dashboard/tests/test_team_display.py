import unittest

import test_intelligence as fixtures
from team_display import attach


class TeamDisplayTests(unittest.TestCase):
    setUp=fixtures.IntelligenceTests.setUp
    tearDown=fixtures.IntelligenceTests.tearDown

    def record(self,bid,at,army,hero=100701,name='enemy',union=1008):
        report=fixtures.battle(bid,at,main=True)
        report.update(attack_idu=f'{army},11,12,13,0',attack_name=name,attack_unionid=union,
                      attack_role_id=name+str(union),attack_advance='0,0,0,0,0,0;0,1,1,0,0,0;3,1,1,0,0,0;5,1,1,0,0,0;')
        report['attack_all_hero_info']=report['attack_all_hero_info'].replace('100701',str(hero))
        self.archive.ingest(92,[[report]],at+1)
        return report

    def test_latest_report_and_two_same_owner_teams_stay_separate(self):
        self.record(1,100,9001)
        self.record(2,300,9001,100814)
        self.record(3,400,9002,100807)
        self.record(4,500,9001,100703,name='other')
        rows=[dict(army_id=aid,player_name='enemy',union_id='1008')for aid in ('9001','9002','9003')]
        attach(self.archive,rows)
        self.assertEqual([r['display_heroes'][0]['id']for r in rows[:2]],[100814,100807])
        self.assertEqual(rows[0]['display_report_at'],300)
        self.assertEqual([h['red_stars']for h in rows[0]['display_heroes']],[0,3,5])
        self.assertEqual(rows[2]['display_heroes'],[])

    def test_time_alliance_and_identity_scopes_and_empty_latest(self):
        self.record(1,100,9001)
        self.record(2,300,9001,100814)
        self.record(3,400,9001,100703,union=999)
        rows=[dict(army_id='9001',player_name='enemy',union_id='1008',display_before=200),
              dict(army_id='9001',player_name='enemy',union_id='999'),
              dict(army_id='9001',player_name='enemy',union_id='1008',identity='battle:other')]
        attach(self.archive,rows)
        self.assertEqual(rows[0]['display_heroes'][0]['id'],100701)
        self.assertEqual(rows[1]['display_heroes'][0]['id'],100703)
        self.assertEqual(rows[2]['display_heroes'],[])
        self.archive.db.execute("UPDATE combat_sides SET heroes='[]' WHERE battle_id='2' AND side='attack'")
        latest=dict(army_id='9001',player_name='enemy',union_id='1008')
        attach(self.archive,[latest])
        self.assertEqual(latest['display_heroes'],[],'do not restore an old build after incomplete newest evidence')

    def test_player_page_operations_and_archive_groups_have_scoped_labels(self):
        self.archive.ingest(5026,fixtures.world(aid='9001'),250)
        self.record(1,300,9001,100814)
        intel=self.archive.intelligence
        player=next(p for p in intel.query('personnel',relation='all')['items']if p['player_name']=='enemy')
        self.assertEqual(player['army_labels'][0]['display_heroes'][0]['id'],100814)
        operation=intel.query('operations',relation='all',army_id='9001')['items'][0]
        self.assertEqual(operation['display_heroes'][0]['id'],100814)
        groups=intel.query('team-log-groups',view='teams',group_name='enemy',group_union='1008',group_identity='')['items']
        self.assertEqual(groups[0]['display_heroes'][0]['id'],100814)
        historical=intel.query('team-log-groups',view='teams',group_name='enemy',group_union='1008',group_identity='',to_at='200')['items']
        self.assertEqual(historical[0]['display_heroes'],[],'historical labels cannot use a future battle')

    def test_batch_uses_constant_statement_count_not_per_row_queries(self):
        self.record(1,100,9001)
        calls=[]
        self.archive.db.set_trace_callback(calls.append)
        attach(self.archive,[dict(army_id='9001',player_name='enemy',union_id='1008')for _ in range(100)])
        self.archive.db.set_trace_callback(None)
        self.assertEqual(len([sql for sql in calls if sql.startswith('WITH wanted')]),1)

    def test_unique_arrival_labels_two_same_player_armies_independently(self):
        self.archive.ingest(5026,fixtures.world(aid='9001',end=300),250)
        self.archive.ingest(5026,fixtures.world(aid='9002',end=400),251)
        self.record(1,300,0,100814)
        self.record(2,400,0,100807)
        rows=[dict(army_id=aid,player_name='enemy',union_id='1008')for aid in ('9001','9002','9003')]
        attach(self.archive,rows)
        self.assertEqual([r['display_heroes'][0]['id']for r in rows[:2]],[100814,100807])
        self.assertEqual([r['display_report_id']for r in rows[:2]],['1','2'])
        self.assertEqual(rows[0]['display_binding'],'unique_attack_arrival')
        self.assertEqual(rows[2]['display_heroes'],[])
        group=self.archive.intelligence.query('team-log-groups',view='teams',group_name='enemy',group_union='1008')['items']
        self.assertEqual({r['army_id']:r['display_heroes'][0]['id']for r in group if r['army_id']},
                         {'9001':100814,'9002':100807})

    def test_newest_arrival_wins_but_incomplete_latest_never_restores_old_build(self):
        self.record(1,100,9001)
        self.archive.ingest(5026,fixtures.world(aid='9001',end=300),250)
        self.record(2,300,0,100814)
        row=dict(army_id='9001',player_name='enemy',union_id='1008')
        attach(self.archive,[row])
        self.assertEqual(row['display_heroes'][0]['id'],100814)
        self.archive.db.execute("UPDATE combat_sides SET heroes='[]' WHERE battle_id='2' AND side='attack'")
        attach(self.archive,[row])
        self.assertEqual(row['display_heroes'],[])
        self.assertEqual(row['display_report_id'],'2')
        historical={**row,'display_before':200}
        attach(self.archive,[historical])
        self.assertEqual(historical['display_report_id'],'1')

    def test_ambiguous_arrival_and_wrong_owner_never_supply_labels(self):
        self.archive.ingest(5026,fixtures.world(aid='9001',end=300),250)
        self.archive.ingest(5026,fixtures.world(aid='9002',end=300),251)
        self.record(1,300,0,100814)
        rows=[dict(army_id='9001',player_name='enemy',union_id='1008')]
        attach(self.archive,rows)
        self.assertEqual(rows[0]['display_heroes'],[])
        self.archive.ingest(5026,fixtures.world(aid='9001',end=500),450)
        self.record(2,500,0,100814)
        rows=[dict(army_id='9001',player_name='other',union_id='1008'),
              dict(army_id='9001',player_name='enemy',union_id='999'),
              dict(army_id='9001',player_name='enemy',union_id='1008',identity='battle:wrong')]
        attach(self.archive,rows)
        self.assertTrue(all(not r['display_heroes']for r in rows))

    def test_conflicting_army_link_never_supplies_arrival_label(self):
        self.archive.ingest(5026,fixtures.world(aid='9001',end=300),250)
        self.record(1,300,0,100814)
        self.archive.ingest(5026,fixtures.world(aid='9002',end=400),350)
        march=self.archive.db.execute("SELECT id FROM march_samples WHERE army_id='9002'").fetchone()[0]
        self.archive.db.execute('INSERT INTO battle_march_links VALUES(?,?,?,?,?,?)',
                               (self.archive.profile_id,'1','attack',march,'unique_attack_arrival',0))
        row=dict(army_id='9001',player_name='enemy',union_id='1008')
        attach(self.archive,[row])
        self.assertEqual(row['display_heroes'],[])

    def test_system_guard_never_supplies_a_player_team_label(self):
        report=fixtures.battle(1,100,main=True)
        report.update(npc=1,defender_force=1000001,defend_idu='9100,21,22,23,0')
        self.archive.ingest(92,[[report]],101)
        row=dict(army_id='9100',player_name='ally',union_id='1009')
        attach(self.archive,[row])
        self.assertEqual(row['display_heroes'],[])


if __name__=='__main__':unittest.main()
