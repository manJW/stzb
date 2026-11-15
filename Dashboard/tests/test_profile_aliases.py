import json
import sqlite3
import unittest
import zlib
from profile_aliases import resolve
from installed_mode import archive_scope
from test_installed_mode import login_snapshot


class ProfileAliases(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.executescript('CREATE TABLE profiles(id TEXT PRIMARY KEY,data TEXT); CREATE TABLE messages(id TEXT,profile_id TEXT,message_id INTEGER,first_seen REAL,payload BLOB);')
        self.old=dict(server='X旧编号',season='旧赛季标签',account_role_id='300',account_name='测试角色')
        self.current={**archive_scope(45122,4),'account_role_id':'200','account_name':'测试角色'}
    def tearDown(self):self.db.close()
    def seed(self,pid='old',uid=200,city=300):
        self.db.execute('INSERT OR IGNORE INTO profiles VALUES(?,?)',(pid,json.dumps(self.old)))
        value=login_snapshot(role=city,login_uid=uid);value[4][0][0][1][0][0]=uid
        value[5]=45122;value[4][0].append(['Tb_season_user',[[uid,4]]])
        self.db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(pid+str(uid),pid,99991,100,zlib.compress(json.dumps(value).encode())))
    def test_verified_legacy_login_reopens_original_archive(self):
        self.seed();self.assertEqual(resolve(self.db,self.current),'old')
        self.db.execute('UPDATE profiles SET data=?',(json.dumps(self.current),))
        self.assertEqual(resolve(self.db,self.current),'old')
    def test_new_role_logged_into_old_binding_does_not_inherit_old_archive(self):
        self.seed();self.seed(uid=201,city=301)
        self.assertNotEqual(resolve(self.db,{**self.current,'account_role_id':'201'}),'old')
    def test_new_season_and_ambiguous_old_archives_are_not_merged(self):
        self.seed();self.assertNotEqual(resolve(self.db,{**self.current,'season':'season-5'}),'old')
        self.seed(pid='second');self.assertNotEqual(resolve(self.db,self.current),'old')


if __name__=='__main__':unittest.main()
