import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from defense import analyze_alert, current_enemy_items


class LatestEnemyBuildTests(unittest.TestCase):
    def test_old_build_is_not_a_second_hypothesis_after_same_cards_refresh(self):
        latest={'battle_id':'2'}
        prediction={'items':[{'battle_id':'1'},latest,{'battle_id':'3'}],
                    'current_report_id':'2','current_resolution':{'source':'same_card_instances'}}
        self.assertEqual(current_enemy_items(prediction),[latest])

    def test_incomplete_latest_report_cannot_generate_a_counter_recommendation(self):
        prediction={'items':[{'battle_id':'1'}], 'current_report_id':'2',
                    'current_resolution':{'unresolved':'incomplete_roster'}}
        archive=SimpleNamespace(lock=threading.RLock())
        with patch('defense.predict_alerts',return_value={'A':prediction}), patch('defense.historical_cases') as history:
            result=analyze_alert(archive,{'alerts':[{'army_id':'A'}]},'A')
        self.assertEqual(result,{'active':True,'hypotheses':[]})
        history.assert_not_called()

    def test_unverified_personal_lineups_remain_separate(self):
        items=[{'battle_id':str(i)} for i in range(4)]
        self.assertEqual(current_enemy_items({'items':items}),items[:3])


if __name__=='__main__':unittest.main()
