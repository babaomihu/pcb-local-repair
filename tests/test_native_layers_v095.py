import atexit
import hashlib
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from pcb_local.native_layers import copper_layer_id, copper_layer_identity
from pcb_local.zone_fill_witness import extract


class Board:
    def __init__(self, aliases=None):
        self.names = {0: 'F.Cu', 4: 'In1.Cu', 6: 'In2.Cu', 2: 'B.Cu'}
        self.aliases = aliases or {0: 'custom A', 4: 'inner a', 6: 'inner b', 2: 'custom B'}
    def GetEnabledLayers(self): return self
    def CuStack(self): return list(self.names)
    def GetStandardLayerName(self, layer): return self.names[layer]
    def GetLayerName(self, layer): return self.aliases[layer]
    def Zones(self): return []
    def GetFootprints(self): return []


class NativeLayers(unittest.TestCase):
    def test_opaque_native_ids_and_stack_order(self):
        board = Board()
        self.assertEqual([r['canonical_name'] for r in copper_layer_identity(board)], ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'])
        self.assertEqual(copper_layer_id(board, 'In2.Cu'), 6)

    def test_colliding_alias_cannot_select_other_layer(self):
        board = Board({0: 'B.Cu', 4: 'same', 6: 'same', 2: 'F.Cu'})
        self.assertEqual(copper_layer_id(board, 'B.Cu'), 2)
        self.assertEqual(copper_layer_id(board, 'F.Cu'), 0)

    def test_display_name_not_machine_lookup(self):
        with self.assertRaises(ValueError): copper_layer_id(Board(), 'custom A')

    def test_disabled_layer(self):
        with self.assertRaises(ValueError): copper_layer_id(Board(), 'In3.Cu')

    def test_duplicate_or_invalid_canonical_names(self):
        for bad in ('F.Cu', 'top'):
            board = Board(); board.names[4] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError): copper_layer_identity(board)

    def test_extractor_keeps_alias_as_metadata(self):
        board = Board({0: 'B.Cu', 4: 'inner α', 6: 'inner β', 2: 'F.Cu'})
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'board.kicad_pcb'; p.write_bytes(b'fake read-only fixture')
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            wx = types.SimpleNamespace(GetApp=lambda: object())
            pcbnew = types.SimpleNamespace(Version=lambda: '10.0.4', LoadBoard=lambda path: board)
            with patch.dict(sys.modules, {'wx': wx, 'pcbnew': pcbnew}): data = extract(p, digest)
            self.assertEqual(data['copper_layers'], ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'])
            self.assertEqual(data['native_layer_identity'][0]['display_name'], 'B.Cu')
            self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), digest)


class WorkerLifecycle(unittest.TestCase):
    prefix = 'from pcb_local.zone_fill_witness import entrypoint;'
    def child(self, body):
        return subprocess.run([sys.executable, '-c', self.prefix+body], capture_output=True, text=True, timeout=2)
    def test_success_avoids_atexit_hang(self):
        result = self.child('import atexit,time;atexit.register(time.sleep,5);entrypoint(lambda:0)')
        self.assertEqual(result.returncode, 0)
    def test_failure_preserved(self):
        self.assertEqual(self.child('entrypoint(lambda:2)').returncode, 2)
    def test_exception_preserved(self):
        result = self.child('entrypoint(lambda:1/0)')
        self.assertEqual(result.returncode, 2); self.assertIn('ZeroDivisionError', result.stderr)
    def test_system_exit_preserved(self):
        self.assertEqual(self.child('import sys;entrypoint(lambda:sys.exit(7))').returncode, 7)
    def test_invalid_code_rejected(self):
        self.assertEqual(self.child('entrypoint(lambda:None)').returncode, 2)

if __name__ == '__main__': unittest.main()
