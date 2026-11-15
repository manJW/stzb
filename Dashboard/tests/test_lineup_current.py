import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lineup_current import card_identity, latest_same_cards


def record(bid, at, cards=(11,12,13), ids=(101,102,103), role='role-A', army=None):
    return {'battle_id':str(bid),'event_at':at,'observed_at':at+1,'army_id':army,'role_raw':role,
            'idu_raw':'0,'+','.join(map(str,cards))+',0','hero_key':','.join(map(str,ids)),
            'heroes':json.dumps([{'id':hid,'position':pos,'level':40,'remaining_hp':9000}
                                for pos,hid in enumerate(ids,1)])}


class CurrentLineupTests(unittest.TestCase):
    def test_same_three_cards_may_change_positions_but_two_shared_cards_are_insufficient(self):
        old=record(1,100,army='A')
        moved=record(2,200,cards=(12,11,13),ids=(102,101,103))
        partial=record(3,250,cards=(12,11,99),ids=(102,101,104))
        current,evidence=latest_same_cards(old,[partial,moved,old],'A',300,310)
        self.assertEqual(current['battle_id'],'2')
        self.assertEqual(evidence['card_instances'],[12,11,13])

    def test_same_cards_on_another_army_are_retained_as_other_army_evidence(self):
        old=record(1,100,army='A'); other=record(2,200,army='B')
        current,evidence=latest_same_cards(old,[other,old],'A',300,310)
        self.assertIs(current,old)
        self.assertEqual(evidence['rejected'],[{'battle_id':'2','reason':'another_army_reference'}])

    def test_role_namespace_and_card_templates_must_agree(self):
        old=record(1,100,army='A'); other=record(2,200,role='role-B')
        bad=record(3,250,ids=(999,102,103))
        current,evidence=latest_same_cards(old,[bad,other,old],'A',300,310)
        self.assertIs(current,old)
        self.assertEqual(evidence['rejected'][0]['reason'],'card_template_conflict')

    def test_unobserved_or_future_reports_cannot_refresh_a_prediction(self):
        old=record(1,100,army='A'); unseen=record(2,200);unseen['observed_at']=400
        future=record(3,500);future['observed_at']=200
        current,_=latest_same_cards(old,[future,unseen,old],'A',300,310)
        self.assertIs(current,old)

    def test_hp_changes_keep_the_build_version_but_skill_changes_create_a_version(self):
        old=record(1,100,army='A'); hp=record(2,200)
        heroes=json.loads(hp['heroes']);heroes[0]['remaining_hp']=20;hp['heroes']=json.dumps(heroes)
        _,before=latest_same_cards(old,[old],'A',300,310)
        _,after=latest_same_cards(old,[hp,old],'A',300,310)
        self.assertEqual(before['version_key'],after['version_key'])
        heroes[0]['skill_slot_ids']=[1,2,3];hp['heroes']=json.dumps(heroes)
        _,changed=latest_same_cards(old,[hp,old],'A',300,310)
        self.assertNotEqual(before['version_key'],changed['version_key'])


if __name__=='__main__':unittest.main()
