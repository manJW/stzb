"""Account-scoped report visibility and auditable, per-report read rejection."""
import json
import time
import uuid

from combat_loadout import structured_report_complete
from role_context import perspective_id


TERMINAL = ('expired', 'denied', 'unavailable')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS report_read_access(
 profile_id TEXT NOT NULL,scope_id TEXT NOT NULL,battle_id TEXT NOT NULL,
 status TEXT NOT NULL,session_id TEXT,listed_at REAL,last_reply_at REAL,
 rejected_at REAL,error_text TEXT,source TEXT,battle_at REAL,
 PRIMARY KEY(profile_id,scope_id,battle_id));
CREATE TABLE IF NOT EXISTS report_read_attempts(
 id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,scope_id TEXT NOT NULL,battle_id TEXT NOT NULL,
 kind TEXT NOT NULL,started_at REAL NOT NULL,sent_at REAL,finished_at REAL,
 outcome TEXT NOT NULL,error_text TEXT,evidence TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS report_attempt_time ON report_read_attempts(profile_id,scope_id,started_at);
CREATE INDEX IF NOT EXISTS report_attempt_id ON report_read_attempts(profile_id,scope_id,battle_id);
'''


def rejection(value):
    if not isinstance(value, list) or len(value) != 2 or value[0] != 10 or not isinstance(value[1], str):
        return None
    text = value[1].strip()
    if text in ('战报已过期', '该战报已过期'):
        return 'expired', text
    if text in ('无权查看该战报', '无权查看战报', '没有权限查看该战报', '战报查看权限不足'):
        return 'denied', text
    if text in ('战报不存在', '该战报不存在', '战报已删除'):
        return 'unavailable', text
    return None


def request_ids(value):
    if (not isinstance(value, list) or len(value) != 3 or value[1:] != [0, 0]
            or not isinstance(value[0], list) or not 1 <= len(value[0]) <= 15
            or any(type(bid) is not int or not 0 < bid <= 2147483647 for bid in value[0])
            or len(set(value[0])) != len(value[0])):
        return None
    return [str(bid) for bid in value[0]]


class ReportAccess:
    def __init__(self, archive):
        self.archive, self.db = archive, archive.db
        self.session_id = uuid.uuid4().hex
        self.db.executescript(SCHEMA)

    @property
    def scope(self):
        return perspective_id(self.archive.profile)

    @property
    def profile(self):
        return self.archive.profile_id

    def row(self, bid):
        return self.db.execute('SELECT * FROM report_read_access WHERE profile_id=? AND scope_id=? AND battle_id=?',
                               (self.profile, self.scope, str(bid))).fetchone()

    def blocked(self, bid):
        row = self.row(bid)
        return bool(row and row['status'] in TERMINAL)

    def allowed(self, bid):
        row = self.row(bid)
        return bool(row and row['status'] in ('listed', 'available', 'probe') and row['session_id'] == self.session_id)

    def observe_report(self, mid, report, at):
        if mid not in (10, 92):
            return
        bid = str(report['battle_id'])
        # Repeated list summaries cannot resurrect a server-rejected read.
        # Actual profile replies or already complete fields are positive evidence.
        positive = mid == 10 or structured_report_complete(report)
        if self.blocked(bid) and not positive:
            return
        self.admit(bid, at, 'profile_reply' if mid == 10 else 'account_report_list', positive,
                   report.get('time'))

    def admit(self, bid, at, source, positive=False, battle_at=None):
        if self.blocked(bid) and not positive:
            return
        status = 'available' if positive else 'listed'
        self.db.execute('''INSERT INTO report_read_access VALUES(?,?,?,?,?,?,?,NULL,NULL,?,?)
          ON CONFLICT(profile_id,scope_id,battle_id) DO UPDATE SET status=excluded.status,
          session_id=excluded.session_id,listed_at=excluded.listed_at,
          last_reply_at=COALESCE(excluded.last_reply_at,report_read_access.last_reply_at),
          rejected_at=NULL,error_text=NULL,source=excluded.source,battle_at=excluded.battle_at''',
          (self.profile,self.scope,str(bid),status,self.session_id,at,at if positive else None,source,battle_at))
        self.db.execute("""UPDATE report_read_jobs SET state='queued',last_error=NULL,updated_at=?
            WHERE profile_id=? AND battle_id=? AND state IN ('awaiting_scope','unavailable')""",
            (at,self.profile,str(bid)))

    def personal_index(self, bid, uid, at):
        try:
            own = json.loads((self.archive.root/'config.json').read_text(encoding='utf-8-sig'))['self']
        except (OSError,ValueError,KeyError):
            own = {}
        if ((own.get('confirmed') and str(own.get('user_id')) == str(uid))
                or str(self.archive.profile.get('account_role_id')) == str(uid)):
            self.admit(bid,at,'own_report_index')

    def defer_unlisted(self):
        # Queue state may have come from another organization or an older
        # process. The current scope's explicit refusal always takes precedence.
        self.db.execute('''UPDATE report_read_jobs SET state='unavailable',last_error=(
            SELECT a.error_text FROM report_read_access a WHERE a.profile_id=report_read_jobs.profile_id
            AND a.scope_id=? AND a.battle_id=report_read_jobs.battle_id)
          WHERE profile_id=? AND state NOT IN ('done','unavailable') AND EXISTS(
            SELECT 1 FROM report_read_access a WHERE a.profile_id=report_read_jobs.profile_id
            AND a.scope_id=? AND a.battle_id=report_read_jobs.battle_id
            AND a.status IN ('expired','denied','unavailable'))''',(self.scope,self.profile,self.scope))
        # A scope change permits a new scope check, never inherits another
        # account/organization's rejection. Old local data is left untouched.
        self.db.execute('''UPDATE report_read_jobs SET state='awaiting_scope'
          WHERE profile_id=? AND state IN ('queued','unavailable') AND NOT EXISTS(
            SELECT 1 FROM report_read_access a WHERE a.profile_id=report_read_jobs.profile_id
            AND a.scope_id=? AND a.battle_id=report_read_jobs.battle_id
            AND (a.status IN ('expired','denied','unavailable') OR
                 (a.status IN ('listed','available','probe') AND a.session_id=?)))''',
          (self.profile,self.scope,self.session_id))

    def prepare_probe(self, bid):
        """Explicit diagnostics get one attempt, never an automatic permission grant."""
        if self.blocked(bid) or self.db.execute('''SELECT 1 FROM report_read_attempts
            WHERE profile_id=? AND scope_id=? AND battle_id=?
            AND json_extract(evidence,'$.source')='explicit_scope_check' LIMIT 1''',
            (self.profile,self.scope,str(bid))).fetchone():
            return False
        self.admit(bid,time.time(),'explicit_scope_check')
        self.db.execute("UPDATE report_read_access SET status='probe' WHERE profile_id=? AND scope_id=? AND battle_id=?",
                        (self.profile,self.scope,str(bid)))
        self.db.execute("""UPDATE report_read_jobs SET state='queued',priority=MAX(priority,250)
            WHERE profile_id=? AND battle_id=? AND kind='detail'""",(self.profile,str(bid)))
        return True

    def begin(self, bid, kind):
        token = uuid.uuid4().hex
        row = self.row(bid)
        evidence = {key:row[key] for key in ('source','listed_at','last_reply_at','battle_at','session_id')} if row else {}
        self.db.execute("INSERT INTO report_read_attempts VALUES(?,?,?,?,?,?,NULL,NULL,'submitted',NULL,?)",
                        (token,self.profile,self.scope,str(bid),kind,time.time(),json.dumps(evidence)))
        if row and row['status']=='probe':
            self.db.execute("UPDATE report_read_access SET status='unconfirmed' WHERE profile_id=? AND scope_id=? AND battle_id=?",
                            (self.profile,self.scope,str(bid)))
        return token

    def finish(self, token, outcome, sent_at=None, error=None):
        self.db.execute('''UPDATE report_read_attempts SET outcome=?,sent_at=?,finished_at=?,error_text=? WHERE id=?''',
                        (outcome,sent_at,time.time(),error,token))

    def reject(self, bid, status, text, at):
        if status not in TERMINAL:
            raise ValueError('Only an explicit, correlated server rejection can stop a read')
        self.db.execute('''INSERT INTO report_read_access VALUES(?,?,?,?,?,NULL,NULL,?,?,?,NULL)
          ON CONFLICT(profile_id,scope_id,battle_id) DO UPDATE SET status=excluded.status,
          rejected_at=excluded.rejected_at,error_text=excluded.error_text,source=excluded.source''',
          (self.profile,self.scope,str(bid),status,self.session_id,at,text,'correlated_server_error'))
        self.db.execute("""UPDATE report_read_jobs SET state='unavailable',last_error=?,updated_at=?
          WHERE profile_id=? AND battle_id=? AND state<>'done'""",(text,at,self.profile,str(bid)))

    def snapshot(self):
        counts = {r[0]:r[1] for r in self.db.execute('''SELECT status,COUNT(*) FROM report_read_access
            WHERE profile_id=? AND scope_id=? GROUP BY status''',(self.profile,self.scope))}
        rows = [dict(r) for r in self.db.execute('''SELECT battle_id,kind,started_at,sent_at,finished_at,outcome,error_text,evidence
            FROM report_read_attempts WHERE profile_id=? AND scope_id=? ORDER BY started_at DESC LIMIT 12''',
            (self.profile,self.scope))]
        return {'scope_id':self.scope,'counts':counts,'recent_attempts':rows,'per_report_reads':True}
