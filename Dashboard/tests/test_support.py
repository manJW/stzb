"""Explicit legacy scenario identities; production defaults remain unbound."""
from pathlib import Path
import json
from archive import Archive as RealArchive
from model import WorldState as RealWorldState, write_json

LEGACY_PROFILE={"server":"X6147","season":"current-20261105","season_label":"当前赛季（赛季编号待核对）",
 "account_role_id":"9320906","account_name":"时运丨天远","union_id":"1009","union_name":"風丨金球凯恩"}
LEGACY_SELF={"user_id":"12319","name":"时运丨天远","union_id":"1009","union_name":"風丨金球凯恩","confirmed":True,"evidence":"explicit test fixture"}
class Archive(RealArchive):
    def __init__(self,root,profile=None,database_path=None):
        # Existing replay fixtures explicitly exercise the legacy archive policy.
        policy=Path(root)/'archive-policy.json'
        if not policy.exists():policy.write_text(json.dumps({'reported_teams_only':False}),encoding='utf8')
        if profile is None and not (Path(root)/'archive-profile.json').exists():profile=dict(LEGACY_PROFILE)
        super().__init__(root,profile,database_path)
class WorldState(RealWorldState):
    def __init__(self,root):
        fresh=not (Path(root)/'config.json').exists()
        super().__init__(root)
        if fresh:
            self.config['self']=dict(LEGACY_SELF)
            write_json(self.config_path,self.config)
