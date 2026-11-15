import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from test_support import WorldState


ROOT = Path(__file__).resolve().parents[1]


class BattleStateTests(unittest.TestCase):
    def test_real_own_battle_state_is_not_inside_fort_or_garrison_and_expiry_does_not_invent_a_transition(self):
        zone=timezone(timedelta(hours=8))
        at=lambda h,m,s:datetime(2026,11,13,h,m,s,tzinfo=zone).timestamp()
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime') as folder:
            state=WorldState(folder)
            state.health['capture_source']='dashen'
            state.connection('live','battle fixture')
            state.packet(at(20,30,53))
            scene=[None]*34
            raw=[0]*33
            raw[:6]=[7,12319,11370980,11370980,at(20,27,55),at(20,32,55)]
            raw[9],raw[32]=13,646832
            scene[6]={'93209065':raw}
            scene[17],scene[18]=[1124,966,1143,985],646833
            state.apply(5026,scene,at(20,30,53))
            row=state.snapshot(at(20,30,53))['armies'][0]
            self.assertEqual((row['state_label'],row['countdown_label'],row['remaining']),('战斗中','战斗剩余',122))
            self.assertNotEqual(row['kind'],'garrison')
            self.assertEqual(row['position'],[1137,980])
            self.assertEqual(state.snapshot(at(20,33,0))['armies'][0]['state_code'],7)
            raw[0],raw[2],raw[3],scene[18]=4,11370980,11450974,646834
            state.packet(at(20,33,1));state.apply(5028,scene,at(20,33,1))
            row=state.snapshot(at(20,33,1))['armies'][0]
            self.assertEqual(row['state_code'],4)
            self.assertNotEqual(row['state_label'],'战斗中')


if __name__=='__main__':unittest.main()
