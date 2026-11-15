"""Verified, season-bound static terrain; never a source of live ownership."""
import gzip
import hashlib
import json
from pathlib import Path


def login_map_config(value):
    if isinstance(value, list):
        if len(value) == 2 and value[0] == "Tb_sys_param" and isinstance(value[1], list):
            for row in value[1]:
                if isinstance(row, list) and len(row) >= 2 and str(row[0]) == "26":
                    text = str(row[1])
                    return int(text) if text.isdigit() and 0 < int(text) < 100000 else None
        for child in value:
            if isinstance(child, (list, dict)):
                found = login_map_config(child)
                if found is not None:
                    return found
    elif isinstance(value, dict):
        for child in value.values():
            if isinstance(child, (list, dict)):
                found = login_map_config(child)
                if found is not None:
                    return found
    return None


class TerrainCatalog:
    def __init__(self, root):
        self.root = Path(root) / "catalog" / "terrain"
        self.cached = {}

    def response(self, profile, map_config_id=None):
        """Only use the verified season binding, and reject conflicting live IDs."""
        try:
            manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
            item = next((item for item in manifest["maps"]
                         if all(item["scope"].get(key) == profile.get(key) for key in ("server", "season"))
                         and (map_config_id is None or item["map_config_id"] == map_config_id)), None)
            if item is None:
                return None
            key = (item["map_config_id"], item["sha256"])
            if key not in self.cached:
                size = item["size"]
                if type(size) is not int or not 1 <= size <= 4096:
                    return None
                compressed = (self.root / f'{item["map_config_id"]}.bin.gz').read_bytes()
                raw = gzip.decompress(compressed)
                if len(raw) != size * size or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                    return None
                self.cached[key] = compressed
            return self.cached[key], item["size"], item["map_config_id"]
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            return None
