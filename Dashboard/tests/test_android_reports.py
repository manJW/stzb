import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from android_reports import AndroidReportGame, AndroidReportList, AndroidReportMonitor, match_visible, timestamp_interval


def team(main=True):
    return json.dumps([{'id':i,'position':i,'level':40 if main else 20,
                       'skill_slot_ids':[11,12,13], 'skills':[{'id':11},{'id':12},{'id':13}]}
                      for i in (1,2,3)])


class Device:
    def __init__(self):
        self.commands=[]
        self.package='com.netease.stzb.netease'
    def screenshot(self):return Image.new('RGB',(1920,1080))
    def foreground_package(self):return self.package
    def execute(self,command):self.commands.append(command)


class Desktop:
    marker=100
    def tick(self):return self.marker
    def idle(self):return 7000


class AndroidReportTests(unittest.TestCase):
    def test_other_desktop_work_does_not_interrupt_background_reading(self):
        class FocusDesktop(Desktop):
            active=False
            def active_in(self,processes):return self.active
            def idle(self):return 0
        desktop,device=FocusDesktop(),Device()
        with AndroidReportGame({'source':'dashen','pid':12,'host_pid':34},'角色',lambda _:True,
                               device=device,desktop=desktop) as game:
            frame=game.capture();desktop.marker+=1
            game.swipe_list(frame)
            self.assertEqual(len(device.commands),1)
            desktop.active=True;desktop.marker+=1
            with self.assertRaisesRegex(RuntimeError,'用户操作'):game.swipe_list(frame)
            self.assertEqual(len(device.commands),1)

    def test_map_detection_uses_the_verified_emulator_search_control(self):
        calls=[]
        reports=AndroidReportList.__new__(AndroidReportList)
        reports.game=SimpleNamespace(ui=SimpleNamespace(find=lambda image,name:calls.append(name) or (100,80,1)))
        self.assertTrue(reports.is_map(SimpleNamespace(frame=('image',None))))
        self.assertEqual(calls,['search'])

    def test_replayed_body_is_not_a_new_report_and_foreign_alliance_is_ineligible(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor=self.monitor(folder);self.addCleanup(monitor.archive.db.close)
            db=monitor.archive.db
            db.execute('INSERT INTO report_bodies VALUES(?,?,?,?)',('test-profile','1','body',200))
            for side in ('attack','defend'):
                db.execute('INSERT INTO combat_sides VALUES(?,?,?,?,?,?,?,?)',
                           ('test-profile','1',side,100,side,'other-union',team(),0))
            self.assertEqual(monitor.received_reply(100,[]),('1',False))
            db.execute("UPDATE combat_sides SET union_id='1009' WHERE side='defend'")
            self.assertEqual(monitor.received_reply(100,['1']),('1',True))

    def test_timed_swipe_is_source_bound_and_stops_on_user_input(self):
        device,desktop=Device(),Desktop()
        connection={'source':'dashen','pid':123}
        with AndroidReportGame(connection,'角色',lambda expected:expected==connection,
                               device=device,desktop=desktop) as game:
            frame=game.capture()
            game.swipe_list(frame)
            self.assertEqual(device.commands,['shell:input swipe 960 982 960 367 600'])
            desktop.marker+=1
            with self.assertRaisesRegex(RuntimeError,'用户操作'):
                game.swipe_list(frame)
            self.assertEqual(len(device.commands),1)

    def test_other_foreground_application_never_receives_a_swipe(self):
        device=Device()
        with AndroidReportGame({'source':'dashen'},'角色',lambda _:True,
                               device=device,desktop=Desktop()) as game:
            frame=game.capture();device.package='another.package'
            with self.assertRaises(RuntimeError):game.swipe_list(frame)
            self.assertEqual(device.commands,[])

    def test_both_players_are_verified_and_main_missed_players_take_priority(self):
        rows=[{'battle_id':str(i),'event_at':100+i,'player_name':'enemy','opponent':'ally',
               'union_id':'1008','opponent_union':'1009','heroes':team(i==1),'opponent_heroes':team()}
              for i in (1,2)]
        self.assertEqual(match_visible(rows,('enemy','someone else'),{}),[])
        self.assertEqual(match_visible(rows,('enemy','ally'),{})[0]['battle_id'],'1')
        other={**rows[1],'player_name':'frequent'}
        self.assertEqual(match_visible([other],('frequent','ally'),{('1008','frequent'):8})[0]['priority'][0],8)

    def test_timestamps_include_seconds_and_minute_ambiguity(self):
        at,span=timestamp_interval('2026/11/1209:22:41')
        minute,width=timestamp_interval('2026/11/1209:22')
        self.assertEqual((at-minute,span,width),(41,1,60))
        self.assertIsNone(timestamp_interval('通知：点这里'))

    def monitor(self,folder):
        db=sqlite3.connect(':memory:',check_same_thread=False);db.row_factory=sqlite3.Row
        db.executescript('''CREATE TABLE report_bodies(profile_id TEXT,battle_id TEXT,body TEXT,observed_at REAL);
          CREATE TABLE battle_reports(profile_id TEXT,battle_id TEXT,event_at REAL);
          CREATE TABLE messages(profile_id TEXT,message_id INTEGER,receipts INTEGER);
          CREATE TABLE combat_sides(profile_id TEXT,battle_id TEXT,side TEXT,event_at REAL,player_name TEXT,
              union_id TEXT,heroes TEXT,npc INTEGER);''')
        archive=SimpleNamespace(root=Path(folder),profile_id='test-profile',profile={'account_name':'角色','union_id':'1009'},
                                lock=threading.RLock(),db=db,
                                intelligence=SimpleNamespace(main_report_gaps=lambda:{'items':[]},alliance_report_coverage=lambda:{}))
        return AndroidReportMonitor(archive,SimpleNamespace(lock=threading.Lock()),lambda:{'source':'dashen'},lambda:False)

    def test_body_receipt_needs_matching_profile_id_battle_id_and_fresh_response(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor=self.monitor(folder)
            self.addCleanup(monitor.archive.db.close)
            db=monitor.archive.db
            db.executemany('INSERT INTO report_bodies VALUES(?,?,?,?)',[
                ('other-profile','1','body',200),('test-profile','2','body',200),('test-profile','1','old',50)])
            self.assertIsNone(monitor.received(['1'],100))
            db.execute('INSERT INTO report_bodies VALUES(?,?,?,?)',('test-profile','1','fresh',201))
            self.assertEqual(monitor.received(['1'],100),'1')

    def test_visible_query_excludes_other_alliances_npc_and_already_saved_bodies(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor=self.monitor(folder);self.addCleanup(monitor.archive.db.close)
            db=monitor.archive.db
            for i in range(1,5):
                bid=str(i);db.execute('INSERT INTO battle_reports VALUES(?,?,?)',('test-profile',bid,100))
                for side,union in [('attack','1008'),('defend','1009' if i!=2 else '2000')]:
                    db.execute('INSERT INTO combat_sides VALUES(?,?,?,?,?,?,?,?)',
                               ('test-profile',bid,side,100,'enemy' if side=='attack' else 'ally',union,team(),int(i==3)))
            db.execute('INSERT INTO report_bodies VALUES(?,?,?,?)',('test-profile','4','saved',200))
            self.assertEqual([row['battle_id'] for row in monitor.pending_at(100,1)],['1'])

    def test_batch_opens_only_verified_row_and_counts_only_database_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor=self.monitor(folder);self.addCleanup(monitor.archive.db.close)
            at,_=timestamp_interval('2026/11/1209:22:41')
            row={'battle_id':'1','event_at':at,'player_name':'enemy','opponent':'ally',
                 'union_id':'1008','opponent_union':'1009','heroes':team(),'opponent_heroes':team()}
            monitor.pending_at=lambda moment,span:[row]
            for side,union,name in [('attack','1008','enemy'),('defend','1009','ally')]:
                monitor.archive.db.execute('INSERT INTO combat_sides VALUES(?,?,?,?,?,?,?,?)',
                           ('test-profile','1',side,at,name,union,team(),0))
            class Game:
                def __enter__(self):return self
                def __exit__(self,*_):pass
                def undisturbed(self):pass
            screen=SimpleNamespace(width=1440)
            class Reports:
                def __init__(self,*_):self.closed=0
                def open_alliance(self):return screen
                def selected_point(self,s):return None
                def search_panel(self,s):return False
                def report_times(self,s):return [(400,'2026/11/1209:22:41')]
                def players(self,s,y):return ('enemy','ally')
                def click(self,s,x,y):
                    monitor.archive.db.execute('INSERT INTO report_bodies VALUES(?,?,?,?)',
                                               ('test-profile','1','body',time.time()))
                def wait(self,*_):return screen
                def close_detail(self):self.closed+=1
                def screen(self):return screen
            report=Reports()
            monitor.game_factory=lambda *a,**k:Game()
            monitor.reports_factory=lambda *a:report
            monitor.ocr_factory=lambda:Game()
            status=monitor.poll(1,{'source':'dashen'})
            self.assertEqual((monitor.progress['saved'],monitor.progress['last_saved_id'],report.closed),(1,'1',1))
            self.assertIn('确认入库',status)


if __name__=='__main__':unittest.main()
