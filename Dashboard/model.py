"""Observed game state and conservative, locally calibrated alert rules."""
import copy
import json
import threading
import time
import uuid
from pathlib import Path

from observation_codec import describe_troops, map_player_traits, map_troop_codes, short_lineup_hero_count
from terrain import login_map_config
from presence_audit import PresenceAudit

STATE_KINDS = {
    "attack": "行军进攻", "garrison_move": "前往驻守", "garrison": "驻守中",
    "retreat": "撤退", "stay": "停留", "other": "其他状态",
}
STALE_SECONDS = 60
ALERT_MAX_AGE = 15
# A quiet delta subscription is not a disconnection. Only a matching, actively
# monitored capture may bridge packet silence, bounded by the stale-data limit.
CONNECTION_PROBE_SECONDS = 30
CAPTURE_SILENCE_SECONDS = STALE_SECONDS
# PC client ArmyState.IN_EXPEDITION + ArmyTargetType.FORCE_ATTACK.
# Matched against archived attacker army IDs, destinations and arrival times.
PC_STATE_RULES = {
    "1:13:*": {"kind": "attack", "verified": True, "require_different_coordinate": True},
    "4:0:*": {"kind": "retreat", "verified": True, "require_different_coordinate": True},
}
# Dashen uses these same two pairs in verified live protocol 5028 records:
# 93807031's force attack led to reports 6194451/6194452, then changed to 4:0.
# Keep an explicit source allowlist rather than inheriting future PC rules.
SOURCE_STATE_RULES = {
    "pc": PC_STATE_RULES,
    "dashen": {pair: dict(PC_STATE_RULES[pair]) for pair in ("1:13:*", "4:0:*")},
}


def integer(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return default


def coordinate(wid):
    wid = integer(wid)
    if wid <= 0:
        return None
    x, y = divmod(wid, 10000)
    return [x, y] if 0 < x < 10000 and 0 < y < 10000 else None


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def default_config():
    return {
        "self": {"user_id": "0", "name": "待识别", "union_id": "0",
                 "union_name": "未加入同盟", "confirmed": False, "evidence": ""},
        "relations": {}, "state_rules": {},
        "targets": [],
    }


class WorldState:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.config_path = self.root / "config.json"
        self.config = default_config()
        if self.config_path.exists():
            loaded = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            self.config.update({key: loaded[key] for key in self.config if key in loaded})
        else:
            write_json(self.config_path, self.config)
        self.players, self.unions, self.armies, self.tiles = {}, {}, {}, {}
        self.army_groups = {}
        self.area = None
        self.map_config_id = None
        self.last_order = 0
        self.epoch = 0
        self.view_epoch = 0
        self.baseline_ids = None
        self.health = {
            "status": "starting", "started_at": time.time(), "last_packet_at": 0,
            "last_message_at": 0, "last_scene_at": 0, "last_full_scene_at": 0, "packets": 0, "messages": 0,
            "scenes": 0, "tcp_gaps": 0, "decode_errors": 0, "schema_errors": 0,
            "message": "正在连接本机游戏数据", "connection": None,
            "connection_verified_at": 0,
            "resync_required": True,
        }
        self.events = []
        self.game_focus = None
        self.game_army_focus = None
        self.client_relations = {}
        self.presence_audit = PresenceAudit()
        self._load_cache()

    def _load_cache(self):
        path = self.root / "runtime" / "last-state.json"
        if not path.exists():
            return
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            for name in ("players", "unions", "armies", "tiles"):
                setattr(self, name, cached.get(name, {}))
            for row in self.armies.values():
                row["epoch"] = -1
                row.pop("injury_mark_at", None)
            for row in self.tiles.values():
                row["epoch"] = -1
            self.area = cached.get("area")
            self.events = cached.get("events", [])[-300:]
        except (ValueError, OSError):
            self.health["message"] = "上次缓存未能读取，等待游戏刷新"

    def persist(self):
        with self.lock:
            cutoff = time.time() - 3600
            self.armies = {key: row for key, row in self.armies.items() if row["seen_at"] >= cutoff}
            value = {name: copy.deepcopy(getattr(self, name)) for name in ("players", "unions", "armies", "tiles", "area", "events")}
            presence_report = copy.deepcopy(self.presence_audit.report())
        write_json(self.root / "runtime" / "last-state.json", value)
        try:
            write_json(self.root / "runtime" / "presence-audit.json", presence_report)
        except OSError as error:
            with self.lock:
                self.health.setdefault('presence_audit', {})['write_error'] = type(error).__name__

    def restore_player_traits(self,records):
        # Identity recovery never restores visibility, positions or alert timers.
        with self.lock:
            for record in records:
                player=self.players.get(record['player_key'].removeprefix('world:'))
                if player is not None and record['observed_at']>=player.get('traits_observed_at',0):
                    player.update(force_type=record['force_type'],is_wanderer=record['force_type']==1,
                                  clan_id=record['clan_id'],clan_name=record['clan_name'],
                                  traits_observed_at=record['observed_at'])

    def connection(self, status, message, detail=None):
        with self.lock:
            self.health.update(status=status, message=message, connection=detail)
            self.health["connection_verified_at"] = 0
            if status != "live":
                self.map_config_id = None
                self.game_focus = None
                self.game_army_focus = None
                self.client_relations = {}
                self.health['union_relations'] = {'status':'waiting'}
                self.health['relation_colors'] = {'status':'waiting'}
                self.last_order = 0
                self.health["resync_required"] = True
                self.epoch += 1
                self.baseline_ids = None

    def verify_connection(self, detail, observed_at):
        with self.lock:
            if self.health["status"] == "live" and detail == self.health["connection"]:
                self.health["connection_verified_at"] = observed_at

    def _transport_valid_until(self):
        packet_at = self.health["last_packet_at"]
        if not packet_at:
            return 0
        verified_at = self.health["connection_verified_at"]
        return max(packet_at + 10, min(verified_at + CONNECTION_PROBE_SECONDS,
                                      packet_at + CAPTURE_SILENCE_SECONDS))

    def set_game_focus(self, point, connection, observed_at, evidence='selected_tile_response_21'):
        with self.lock:
            if (not isinstance(point, list) or len(point) != 2 or
                    any(type(value) is not int or not 1 <= value <= 9999 for value in point)):
                return False
            h = self.health
            if (not connection or connection.get('source') != 'dashen' or
                    connection != h['connection'] or h['status'] != 'live' or
                    h.get('identity_pending') or h.get('scope_pending') or
                    not self.config['self'].get('confirmed') or h['schema_errors'] or
                    observed_at > self._transport_valid_until()):
                return False
            tile = self._tile_at(point)
            self.game_focus = {'event_id': uuid.uuid4().hex, 'coord': list(point),
                               'updated_at': observed_at, 'source': 'dashen_click',
                               'name': tile.get('name') or '', 'evidence': evidence}
            h.pop('game_click_error', None)
            return True

    def packet(self, observed_at):
        with self.lock:
            self.health["packets"] += 1
            self.health["last_packet_at"] = observed_at

    def set_game_army_focus(self, army_id, connection, observed_at, selected_at, *, repeated=False, group_id=None):
        with self.lock:
            h = self.health
            if (not connection or connection.get('source') != 'dashen' or connection != h['connection']
                    or h['status'] != 'live' or h.get('identity_pending') or h.get('scope_pending')
                    or not self.config['self'].get('confirmed') or h['schema_errors']
                    or observed_at > self._transport_valid_until()):
                return False
            if group_id is not None:
                if army_id is not None or h['resync_required'] or not any(
                        str(r.get('army_group_id'))==group_id and r.get('visible') and r.get('epoch')==self.epoch
                        for r in self.armies.values()):
                    return False
            elif army_id is not None:
                row = self.armies.get(army_id)
                if not row or not row.get('visible') or row.get('epoch') != self.epoch or h['resync_required']:
                    return False
            if repeated:
                if (not self.game_army_focus or self.game_army_focus['army_id'] != army_id
                        or self.game_army_focus.get('group_id')!=group_id):
                    return False
                self.game_army_focus['validated_at'] = observed_at
            else:
                self.game_army_focus = {'event_id':uuid.uuid4().hex, 'army_id':army_id, 'updated_at':observed_at,
                                       'group_id':group_id,
                                       'selected_at':selected_at,'validated_at':observed_at,'source':'client_selected_army'}
            return True

    def gap(self):
        with self.lock:
            self.health["tcp_gaps"] += 1
            self.health["message"] = "数据流有间断，刷新地图后重新核对"
            self.health["last_scene_at"] = 0
            self.health["resync_required"] = True
            self.epoch += 1
            self.baseline_ids = None

    def decode_error(self):
        with self.lock:
            self.health["decode_errors"] += 1
            self.health["resync_required"] = True
            self.epoch += 1
            self.baseline_ids = None
            self.health["message"] = "部分消息未能解析，请刷新地图后重新核对"

    def apply(self, message_id, value, observed_at):
        with self.lock:
            self.health["messages"] += 1
            self.health["last_message_at"] = observed_at
            if message_id == 99991:
                self.map_config_id = login_map_config(value)
            if message_id not in (5026, 5028):
                return
            if not isinstance(value, list) or len(value) != 34:
                self.health["schema_errors"] += 1
                self.health["message"] = "游戏数据格式变化，暂停状态判断"
                return
            order = integer(value[18])
            scope = value[20]
            # Verified Dashen deltas address a map region as [2, region_id].
            # The same version can update an army in one region and remove it
            # from another; neither notification is a global army deletion.
            region = (str(integer(scope[1])) if self.health.get("capture_source") == "dashen"
                      and isinstance(scope, list) and len(scope) == 2 and scope[0] == 2
                      and integer(scope[1]) > 0 else None)
            if message_id == 5028 and order != -999999999 and order <= self.last_order:
                return
            if message_id == 5026 and order > 0:
                self.last_order = order
                self.health["resync_required"] = False
                self.health["last_full_scene_at"] = observed_at
            if message_id == 5026:
                if self.baseline_ids is None:
                    self.baseline_ids = set()
                    self.view_epoch += 1
                self.baseline_ids.update(str(aid) for aid, raw in value[6].items()
                    if isinstance(raw, list) and raw and integer(raw[0]) != 0) if isinstance(value[6], dict) else None
                if isinstance(value[21], dict):
                    for ids in value[21].values():
                        if isinstance(ids, list):
                            self.baseline_ids.update(str(aid) for aid in ids)
            self.health["scenes"] += 1
            self.health["last_scene_at"] = observed_at
            self.health["message"] = "正在接收局部观测" if self.health["resync_required"] else "正在接收游戏数据"
            if isinstance(value[17], list) and len(value[17]) == 4:
                self.area = value[17]
            for uid, raw in value[1].items() if isinstance(value[1], dict) else ():
                if not isinstance(raw, list) or len(raw) < 13:
                    continue
                extra = raw[12] if isinstance(raw[12], list) else []
                union = str(integer(raw[2]))
                union_name = str(extra[2]) if len(extra) > 2 else ""
                self.players[str(uid)] = {"id": str(uid), "name": str(raw[0]),
                    "union_id": union, "union_name": union_name, "traits_observed_at":observed_at,
                    **map_player_traits(raw)}
                if union_name:
                    self.unions[union] = union_name
            for uid, raw in value[3].items() if isinstance(value[3], dict) else ():
                if isinstance(raw, list) and len(raw) >= 3:
                    self.unions[str(uid)] = str(raw[2])
            for gid,raw in value[12].items() if isinstance(value[12],dict) else ():
                if isinstance(raw,list):
                    self.army_groups[str(gid)]={'raw':raw,'observed_at':observed_at,'epoch':self.epoch}
            for wid, chunks in value[14].items() if isinstance(value[14], dict) else ():
                raw = chunks.get("0") if isinstance(chunks, dict) else None
                if isinstance(raw, list) and len(raw) >= 21 and coordinate(wid):
                    self.tiles[str(wid)] = {"coord": coordinate(wid), "owner": str(integer(raw[2])),
                        "union_id": str(integer(raw[3])), "name": str(raw[6] or ""), "seen_at": observed_at,
                        "epoch": self.epoch, "view_epoch": self.view_epoch, "city_type": integer(raw[0]),
                        "state_code": integer(raw[8], -1), "state_end_at": integer(raw[4])}
            removed = value[7] if isinstance(value[7], list) else []
            for aid in removed:
                aid = str(aid)
                current = self.armies.get(aid)
                if (region and order > 0 and current and current.get("epoch") == self.epoch
                        and current.get("update_order") == order):
                    regions = set(current.get("update_regions", []))
                    regions.discard(region)
                    current["update_regions"] = sorted(regions)
                    if regions:
                        continue
                self._hide_army(aid, observed_at)
            for aid, raw in value[6].items() if isinstance(value[6], dict) else ():
                aid = str(aid)
                if not isinstance(raw, list) or not raw:
                    continue
                if integer(raw[0]) == 0:
                    self._hide_army(aid, observed_at)
                    continue
                if len(raw) < 33:
                    self.health["schema_errors"] += 1
                    continue
                signature = f"{integer(raw[0])}:{integer(raw[9])}:{integer(raw[32])}"
                previous = self.armies.get(aid)
                regions = (set(previous.get("update_regions", [])) if region and previous
                           and previous.get("epoch") == self.epoch and previous.get("update_order") == order else set())
                if region:
                    regions.add(region)
                self.armies[aid] = {
                    "id": aid, "owner": str(integer(raw[1])), "from": coordinate(raw[2]),
                    "target": coordinate(raw[3]), "begin": integer(raw[4]), "end": integer(raw[5]),
                    "target_type": integer(raw[9]), "reside": coordinate(raw[10]),
                    "stay": coordinate(raw[11]), "state_code": integer(raw[0]),
                    "state_id": integer(raw[32]), "signature": signature,
                    "army_group_id": integer(raw[6]), "reside_time": integer(raw[23]),
                    "troop_codes": map_troop_codes(raw[16]),
                    "morale": integer(raw[27]), "seen_at": observed_at, "visible": True,
                    "epoch": self.epoch,
                    "update_order": order, "update_regions": sorted(regions),
                }
                watched = ("signature", "from", "target", "begin", "end", "visible")
                current = self.armies[aid]
                if previous is None or any(previous.get(key) != current.get(key) for key in watched):
                    label = "新增部队观测" if previous is None else "重新观测到部队" if not previous.get("visible") else "部队状态变化"
                    self._record_event(current, observed_at, label, previous)
            if message_id == 5026 and order > 0:
                # Completion is authoritative for this subscribed view. Keep the
                # previous view as history instead of renewing its observations.
                for aid in list(self.armies):
                    if aid not in self.baseline_ids:
                        self._hide_army(aid, observed_at)
                self.baseline_ids = None

            self.presence_audit.observe(self, value, observed_at)

    def _hide_army(self, aid, observed_at):
        row = self.armies.get(aid)
        if row and row.get("visible"):
            row["visible"] = False
            self._record_event(row, observed_at, "离开当前观测")

    def _record_event(self, row, observed_at, label, previous=None):
        player = self.players.get(row["owner"], {})
        rule = self._state_rule(row)
        self.events.append({"army_id": row["id"], "at": observed_at, "label": label,
            "player_name": player.get("name", "未识别玩家"),
            "relation": self._relation(row["owner"], player.get("union_id", "0")),
            "from": row["from"], "target": row["target"], "end": row["end"],
            "state_label": STATE_KINDS.get(rule.get("kind") if rule.get("verified") else None, "状态待校准"),
            "signature": row["signature"], "visible": row.get("visible", True)})
        self.events = self.events[-300:]

    def _relation_entry(self, owner, union):
        own = self.config['self']
        personal = self.config['relations'].get('player:' + str(owner), {})
        if (personal.get('manual_override') and personal.get('own_user_id') == own.get('user_id')
                and personal.get('relation') in ('enemy', 'friendly', 'unknown')):
            return personal
        entry = self.config['relations'].get(str(union),{}) if str(union) not in ('', '0', 'None') else {}
        if entry.get('manual_override') and entry.get('own_user_id') not in (None, own.get('user_id')):
            return {}
        if entry.get('source')=='client' and (entry.get('own_union_id')!=own.get('union_id')
                or entry.get('user_id')!=own.get('user_id')
                or self.health.get('union_relations',{}).get('status')!='ready'):
            return {}
        return entry

    def _declared_relation(self, owner, union):
        own = self.config['self']
        if own.get('confirmed') and owner == own.get('user_id'):
            return 'self'
        if own.get('confirmed') and union not in (None, '', '0') and union == own.get('union_id'):
            return 'ally'
        configured = self._relation_entry(owner, union).get('relation')
        return configured if configured in ('enemy', 'friendly') else 'unknown'

    def _relation(self, owner, union):
        declared = self._declared_relation(owner, union)
        if declared != 'unknown':
            return declared
        # Manual neutral suppresses the existing wanderer threat fallback.
        if self._relation_entry(owner, union).get('manual_override'):
            return 'unknown'
        if self.players.get(owner, {}).get('is_wanderer'):
            return 'enemy'
        return 'unknown'

    def _faction(self, owner, union):
        declared = self._declared_relation(owner, union)
        wanderer = bool(self.players.get(owner, {}).get('is_wanderer'))
        if declared in ('self', 'ally'):
            group = 'own'
        elif wanderer:
            group = {'enemy':'enemy_wanderer', 'friendly':'friendly_wanderer'}.get(declared, 'wanderer')
        else:
            group = {'enemy':'enemy_union', 'friendly':'friendly_union'}.get(declared, 'neutral')
        entry = self._relation_entry(owner, union)
        source = 'own' if declared in ('self', 'ally') else 'manual_player' if entry.get('scope') == 'player' else 'manual_union' if entry.get('manual_override') else 'client' if entry.get('source') == 'client' else 'configured' if entry else 'unmarked'
        return {'faction_group':group, 'declared_relation':declared, 'relation_source':source,
                'is_wanderer':wanderer}

    def _state_rule(self, row):
        if row.get('army_group_id') and self.health.get('capture_source') in ('pc','dashen'):
            # Official ArmyState values: group expedition/return are 12/13;
            # shared garrison and stay states retain their personal meanings.
            kind = {1:'attack',3:'garrison_move',5:'stay',6:'garrison',12:'attack',13:'retreat',26:'stay'}.get(row['state_code'])
            if kind:
                return {'kind':kind,'verified':True}
        pair = f"{row['state_code']}:{row['target_type']}:*"
        source_rule = SOURCE_STATE_RULES.get(self.health.get("capture_source"), {}).get(pair, {})
        rule = self.config["state_rules"].get(row["signature"],
            self.config["state_rules"].get(pair, source_rule))
        if rule.get("require_same_coordinate") and row["from"] != row["target"]:
            return {}
        if rule.get("require_different_coordinate") and row["from"] == row["target"]:
            return {}
        return rule

    def _tile_union(self, tile):
        union = tile.get("union_id")
        owner = tile.get("owner")
        if union in (None, "", "0") and owner not in (None, "", "0"):
            return self.players.get(owner, {}).get("union_id")
        return union

    def _tile_at(self, point):
        return self.tiles.get(str(point[0] * 10000 + point[1]), {}) if point else {}

    def _tile_fresh(self, tile, now):
        point = tile.get("coord")
        inside = bool(point and self.area and self.area[0] <= point[0] <= self.area[2]
                      and self.area[1] <= point[1] <= self.area[3])
        return tile.get("epoch", -1) == self.epoch and (now - tile.get("seen_at", 0) <= ALERT_MAX_AGE
                or (inside and tile.get("view_epoch") == self.view_epoch))

    def _target_ownership(self, tile, now):
        own = self.config["self"]
        owner, union = tile.get("owner"), self._tile_union(tile)
        owned = (union not in (None, "", "0") and union == own.get("union_id")) or (
            owner not in (None, "", "0") and owner == own.get("user_id"))
        # Keep the last observed side separate from freshness. A missing member's
        # alliance is unknown; it is not evidence that the target is another side.
        side = "own" if owned else "other" if union not in (None, "", "0") or owner == "0" else "unknown"
        return {"target_side": side, "target_owner_id": owner, "target_union_id": union,
                "target_ownership_fresh": self._tile_fresh(tile, now),
                "target_observed_at": tile.get("seen_at")}

    def _forts(self, now):
        result = []
        for tile in self.tiles.values():
            city_type = tile.get("city_type")
            if city_type not in (4, 12) and not (city_type == 9 and tile.get("name") == "要塞"):
                continue
            if not self._tile_fresh(tile, now):
                continue
            code, end = tile.get("state_code"), tile.get("state_end_at", 0)
            status = "ready" if code == 0 else "building" if city_type == 4 and code == 1 and end > now else "transition"
            result.append({"coord": tile["coord"], "name": tile["name"] or "要塞", "status": status,
                           "owner_id": tile.get("owner"), "union_id": self._tile_union(tile),
                           "relation": self._relation(tile["owner"], self._tile_union(tile) or "0"),
                           **self._faction(tile['owner'], self._tile_union(tile) or '0'),
                           "end_at": end if status == "building" else None})
        return result

    def _land_tiles(self, now):
        result = []
        for tile in self.tiles.values():
            # Ordinary free/player land, suburbs, construction land and battlefield.
            # Ownership remains independent from the static terrain layer.
            if tile.get("city_type") not in (0, 2, 5, 7, 17, 20):
                continue
            if not self._tile_fresh(tile, now):
                continue
            relation = self._relation(tile["owner"], self._tile_union(tile) or "0") if tile.get("owner") not in (None, "", "0") else "neutral"
            result.append({"coord": tile["coord"], "name": tile.get("name") or "占领地", "relation": relation,
                           **self._faction(tile['owner'], self._tile_union(tile) or '0'),
                           "owner_id": tile.get("owner"), "union_id": self._tile_union(tile)})
        return result

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            rows, alerts, candidates = [], [], []
            transport_valid_until = self._transport_valid_until()
            live = self.health["status"] == "live" and now <= transport_valid_until
            subscribed = bool(self.health["last_scene_at"] and not self.health["resync_required"])
            reliable = live and not self.health["schema_errors"] and (
                subscribed or now - self.health["last_scene_at"] <= ALERT_MAX_AGE)
            for raw in self.armies.values():
                row = copy.deepcopy(raw)
                row["troops"] = describe_troops(row.pop("troop_codes", ()))
                player = self.players.get(row["owner"], {})
                union = player.get("union_id", "0")
                row.update(player_name=player.get("name", "未识别玩家"), union_id=union,
                    union_name=player.get("union_name") or self.unions.get(union, "未识别同盟"))
                row.update(force_type=player.get('force_type'), is_wanderer=bool(player.get('is_wanderer')),
                           clan_id=player.get('clan_id', 0), clan_name=player.get('clan_name', ''))
                row["relation"] = self._relation(row["owner"], union)
                row.update(self._faction(row['owner'], union))
                rule = self._state_rule(row)
                row["kind"] = rule.get("kind") if rule.get("verified") else None
                row["state_label"] = STATE_KINDS.get(row["kind"], "状态待校准")
                # Verified on 2026-11-13 against the own army's battle screen and 5-minute timer.
                battling = self.health.get('capture_source') == 'dashen' and row['state_code'] == 7 and row['target_type'] == 13
                if battling:
                    row['state_label'] = '战斗中'
                row["current_connection"] = row.get("epoch", -1) == self.epoch
                # Scene packets are deltas: unchanged, subscribed armies remain
                # observed until a removal, stream gap, or reconnection occurs.
                current = row["current_connection"] and row.get("visible", True)
                row["subscription_valid_until"] = transport_valid_until if current and subscribed and reliable else 0
                # Never roll the observed time backwards when traffic goes quiet.
                row["observed_at"] = max(row["seen_at"], self.health["last_scene_at"]) if current else row["seen_at"]
                row["age"] = max(0, now - row["observed_at"])
                subscription_active = bool(row["subscription_valid_until"] >= now and current and reliable)
                row["stale"] = (row["age"] > STALE_SECONDS and not subscription_active) or not current
                row["position"] = None
                row["position_label"] = "出发坐标"
                if row["kind"] in ("garrison", "stay"):
                    # The observed garrison sample uses equal origin/target. The reside
                    # field points to a different fort; it is not a verified location.
                    if row["from"] and row["from"] == row["target"]:
                        row["position"] = row["from"]
                        row["position_label"] = "驻守坐标" if row["kind"] == "garrison" else "停留坐标"
                elif row["from"] and row["from"] == row["target"]:
                    row["position"] = row["from"]
                    row["position_label"] = "观测坐标"
                row["display_position"] = row["position"] or row["from"]
                tile = self._tile_at(row["target"])
                row.update(self._target_ownership(tile, now))
                row["from_name"] = self._tile_at(row["from"]).get("name") or ""
                row["target_name"] = tile.get("name") or ""
                row["remaining"] = row["end"] - now if row["end"] > 0 else None
                row["countdown_label"] = "行军剩余" if row["kind"] in ("attack", "garrison_move", "retreat") else "状态倒计时"
                if battling:
                    row['countdown_label'] = '战斗剩余'
                row["incoming"] = False
                # Coordinates are bookmarks, never the recording/alert scope.
                targets = []
                if row["target"]:
                    point = row["target"]
                    owned = row["target_side"] == "own"
                    ownership_fresh = row["target_ownership_fresh"]
                    targets = [{"x": point[0], "y": point[1], "friendly": owned and ownership_fresh}]
                for target in targets:
                    if row["target"] != [target["x"], target["y"]] or row["stale"]:
                        continue
                    marching = bool(row["from"] and row["from"] != row["target"] and row["end"] > now)
                    fresh_march = marching and reliable and (subscription_active or row["age"] <= ALERT_MAX_AGE)
                    candidate = {"army_id": row["id"], "player_name": row["player_name"],
                        **self._faction(row['owner'], union),
                        'owner':row['owner'], 'union_id':union,
                        "is_wanderer": row['is_wanderer'], "force_type": row['force_type'],
                        "troops": row["troops"],
                        "from": row["from"], "from_name": row["from_name"], "target_name": row["target_name"],
                        "coord": row["target"], "end": row["end"],
                        "relation": row["relation"], "state_label": row["state_label"],
                        "observation": "行军目标待核对" if marching else "据点部队观测"}
                    hostile=(row['relation']=='enemy'or self.config['self'].get('is_wanderer')and
                             row['relation']not in ('self','ally','friendly'))
                    if fresh_march and target["friendly"] and hostile and row["kind"] == "attack":
                        row["incoming"] = True
                        candidate["map_hero_count"] = short_lineup_hero_count(raw.get("troop_codes", ()))
                        alerts.append(candidate)
                    elif row["relation"] not in ("self", "ally", "friendly"):
                        if (fresh_march and row["kind"] in (None, "attack")
                                and (target["friendly"] or not ownership_fresh)):
                            checks = []
                            if row["relation"] != "enemy":
                                checks.append("敌我关系")
                            if row["kind"] is None:
                                checks.append("进攻状态")
                            if not target["friendly"]:
                                checks.append("地块归属")
                            candidate["pending_reason"] = "、".join(checks) + "待核对"
                        candidates.append(candidate)
                rows.append(row)
            rows.sort(key=lambda row: (row["stale"], not row["incoming"], row["relation"] != "self", -(row["seen_at"])))
            config = copy.deepcopy(self.config)
            source_rules = SOURCE_STATE_RULES.get(self.health.get("capture_source"), {})
            config["state_rules"] = {**copy.deepcopy(source_rules), **config["state_rules"]}
            groups = {}
            for row in rows:
                gid = str(row.get('army_group_id') or '')
                if gid in ('','0') or row['stale'] or row.get('visible') is False or not row['current_connection']:
                    continue
                group = groups.setdefault(gid,{'id':gid,'member_ids':[],'from':row['from'],'target':row['target'],
                    'begin':row['begin'],'end':row['end'],'name':'军团','relation':row['relation']})
                group['member_ids'].append(row['id'])
            return {"app": "stzb-local-observer", "server_now": now, "health": copy.deepcopy(self.health), "live": live, "reliable": reliable,
                "transport_valid_until": transport_valid_until,
                "game_focus": copy.deepcopy(self.game_focus),
                "game_army_focus": copy.deepcopy(self.game_army_focus),
                "army_groups":list(groups.values()),
                "area": self.area, "map_config_id": self.map_config_id, "config": config, "armies": rows, "forts": self._forts(now),
                "land_tiles": self._land_tiles(now),
                "players": list(self.players.values()), "unions": self.unions,
                "targets": copy.deepcopy(self.config["targets"]), "alerts": alerts, "candidates": candidates,
                "events": copy.deepcopy(self.events[-50:][::-1]),
                "counts": {"fresh": sum(not row["stale"] for row in rows), "observed": len(rows),
                    "self": sum(row["relation"] == "self" and not row["stale"] for row in rows),
                    "unmapped": sum(not row["kind"] and not row["stale"] for row in rows), "alerts": len(alerts)},
                "state_kinds": STATE_KINDS}

    def calibrate(self, payload):
        with self.lock:
            action = payload.get("action")
            if action == "relation":
                scope, relation = payload.get('scope', 'union'), payload.get('relation')
                uid = str(payload.get('player_id' if scope == 'player' else 'union_id', ''))
                if scope not in ('union', 'player') or not uid.isascii() or not uid.isdigit() or not 0 < int(uid) <= 2147483647 or relation not in ('enemy', 'friendly', 'unknown', 'auto'):
                    raise ValueError('请选择有效的同盟／聚义或玩家，以及关系')
                uid = str(int(uid))
                if scope == 'player' and uid not in self.players:
                    raise ValueError('请选择已观测到的玩家')
                if scope == 'union' and uid == self.config["self"].get("union_id"):
                    raise ValueError("本方同盟不能标记为敌方")
                if scope == 'player' and self._declared_relation(uid, self.players[uid].get('union_id', '0')) in ('self', 'ally'):
                    raise ValueError('不能修改自己或本盟成员的关系')
                key = 'player:' + uid if scope == 'player' else uid
                if relation=='auto':
                    if scope == 'union' and uid in self.client_relations:
                        self.config['relations'][key]=dict(self.client_relations[uid])
                    else:
                        self.config['relations'].pop(key,None)
                else:
                    self.config["relations"][key] = {"relation": relation, "verified_at": time.time(),
                        'source':'manual','manual_override':True, 'scope':scope,
                        'own_user_id':self.config['self'].get('user_id')}
            elif action == "state":
                army = self.armies.get(str(payload.get("army_id", "")))
                kind = payload.get("kind")
                if army is None or kind not in STATE_KINDS:
                    raise ValueError("请选择部队和观察到的状态")
                moment = time.time()
                continuing = (army.get("epoch") == self.epoch and self.health["status"] == "live"
                    and moment - self.health["last_packet_at"] <= 10
                    and moment - self.health["last_scene_at"] <= 15 and not self.health["schema_errors"])
                if not army.get("visible") or (not continuing and moment - army["seen_at"] > 30):
                    raise ValueError("部队记录已过期，请刷新游戏中的部队详情后再校准")
                self.config["state_rules"][army["signature"]] = {
                    "kind": kind, "verified": True, "verified_at": time.time(),
                    "evidence_army": army["id"], "evidence_seen_at": moment if continuing else army["seen_at"],
                }
            elif action in ("target", "target_add"):
                x, y = integer(payload.get("x")), integer(payload.get("y"))
                if not (0 < x < 10000 and 0 < y < 10000):
                    raise ValueError("请输入有效坐标")
                name = str(payload.get("name", "关注据点")).strip()[:30] or "关注据点"
                target = {"name": name, "x": x, "y": y}
                if action == "target_add":
                    if len(self.config["targets"]) >= 8 or any(t["x"] == x and t["y"] == y for t in self.config["targets"]):
                        raise ValueError("该坐标已经关注，或关注点已达 8 个")
                    self.config["targets"].append(target)
                else:
                    index = integer(payload.get("target_index"))
                    if not 0 <= index < len(self.config["targets"]):
                        raise ValueError("请选择要修改的关注点")
                    self.config["targets"][index] = target
            else:
                raise ValueError("不支持的校准操作")
            write_json(self.config_path, self.config)
