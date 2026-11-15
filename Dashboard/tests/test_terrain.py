import gzip
import hashlib
import http.client
import json
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_support import WorldState
from terrain import TerrainCatalog, login_map_config


class TerrainTests(unittest.TestCase):
    def test_login_config_uses_param_26_not_schema_strings(self):
        self.assertEqual(login_map_config(["Tb_sys_param:26", ["Tb_sys_param", [[1, "other"], [26, "34"]]]]), 34)
        self.assertIsNone(login_map_config(["Tb_sys_param", [[26, "wrong"]]]))
        self.assertIsNone(login_map_config(["Tb_sys_param", [[260, "34"]]]))

    def test_real_catalog_is_season_bound_and_has_correct_coordinate_order(self):
        item = json.loads((ROOT / 'catalog/terrain/manifest.json').read_text(encoding='utf-8'))['maps'][0]
        catalog = TerrainCatalog(ROOT)
        compressed, size, config = catalog.response(item['scope'])
        raw = gzip.decompress(compressed)
        self.assertEqual((size, config, len(raw)), (1501, 34, 1501 * 1501))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), item['sha256'])
        self.assertEqual(raw[(970 - 1) * size + 917 - 1], 1)
        self.assertEqual(raw[(973 - 1) * size + 911 - 1], 3)
        self.assertEqual(raw[(1009 - 1) * size + 928 - 1], 4)
        self.assertIsNone(catalog.response({**item['scope'], 'season': 'other-season'}))
        self.assertIsNone(catalog.response(item['scope'], 5))

    def test_bad_catalog_is_not_shown_and_reclassification_preserves_ownership(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            state = WorldState(root)
            self.assertIsNone(TerrainCatalog(root).response({}))
            state.apply(99991, [None, ['Tb_sys_param', [[26, '34']]]], 1000)
            self.assertEqual(state.snapshot(1000)['map_config_id'], 34)
            state.epoch = state.view_epoch = 1
            state.area = [980, 910, 990, 930]
            for x, owner, union in [(981, '0', '0'), (982, '42', '555'), (983, '43', '1009'), (984, '44', '1008')]:
                state.tiles[str(x * 10000 + 920)] = {'coord': [x, 920], 'owner': owner, 'union_id': union,
                    'name': '', 'city_type': 2, 'seen_at': 1000, 'epoch': 1, 'view_epoch': 1}
            state.config['relations']['1008'] = {'relation': 'enemy'}
            self.assertEqual([r['relation'] for r in state._land_tiles(1001)], ['neutral', 'unknown', 'ally', 'enemy'])
            # Gray third-party land is not promoted to friendly alert ownership.
            self.assertEqual(state._target_ownership(state.tiles['9820920'], 1001)['target_side'], 'other')
            state.connection('waiting', 'test')
            self.assertIsNone(state.map_config_id)
            self.assertEqual(state._land_tiles(1001), [])

    def test_http_resource_is_gzipped_and_rejects_wrong_live_map(self):
        from server import ThreadingHTTPServer, make_handler
        scope = json.loads((ROOT / 'catalog/terrain/manifest.json').read_text(encoding='utf-8'))['maps'][0]['scope']
        with tempfile.TemporaryDirectory() as name:
            state = WorldState(name)
            archive = SimpleNamespace(profile=scope)
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(state, 0, archive=archive))
            port = server.server_address[1]
            server.RequestHandlerClass = make_handler(state, port, archive=archive)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection('127.0.0.1', port)
                conn.request('GET', '/api/map-terrain')
                response = conn.getresponse()
                self.assertEqual((response.status, response.getheader('Content-Encoding'),
                                  response.getheader('X-Map-Size')), (200, 'gzip', '1501'))
                self.assertEqual(len(gzip.decompress(response.read())), 1501 * 1501)
                conn.close()
                state.map_config_id = 5
                conn = http.client.HTTPConnection('127.0.0.1', port)
                conn.request('GET', '/api/map-terrain')
                response = conn.getresponse()
                self.assertEqual(response.status, 404)
                response.read()
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
