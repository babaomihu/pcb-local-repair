import sys
import types
import unittest
from unittest.mock import patch

from pcb_local.pns_worker import native_pad_witness


class Board:
    names = {7: 'F.Cu', 13: 'In1.Cu', 21: 'In2.Cu', 45: 'B.Cu'}
    aliases = {7: 'B.Cu', 13: 'same', 21: 'same', 45: 'F.Cu'}
    def GetEnabledLayers(self): return self
    def CuStack(self): return list(self.names)
    def GetStandardLayerName(self, layer): return self.names[layer]
    def GetLayerName(self, layer): return self.aliases[layer]
    def GetLayerID(self, name): raise AssertionError('Ambiguous name lookup used')
    def GetPads(self): return [Pad(7), Pad(21), Pad(45)]


class Pad:
    def __init__(self, layer):
        self.layer = layer
        self.m_Uuid = types.SimpleNamespace(AsString=lambda: 'pad-'+str(layer))
    def IsOnLayer(self, layer): return layer == self.layer
    def GetParentFootprint(self): return types.SimpleNamespace(GetReference=lambda: 'J1')
    def GetNetname(self): return 'ordinary'
    def GetPosition(self): return types.SimpleNamespace(x=1000000, y=2000000)
    def ShapePos(self, layer):
        assert layer == self.layer
        return types.SimpleNamespace(x=1500000, y=2000000)


class PnsLayerWitness(unittest.TestCase):
    def witness(self, layer):
        with patch.dict(sys.modules, {'wx':types.SimpleNamespace(App=lambda value: object()),
                                    'pcbnew':types.SimpleNamespace(LoadBoard=lambda path: Board())}):
            return native_pad_witness('read-only-board.kicad_pcb', layer)
    def test_back_pad_not_front_alias(self):
        rows=self.witness('B.Cu')
        self.assertEqual([r['uuid'] for r in rows], ['pad-45'])
        self.assertEqual(rows[0]['anchor'], [1.0, 2.0])
        self.assertEqual(rows[0]['shape_center'], [1.5, 2.0])
    def test_front_pad_not_back_alias(self):
        self.assertEqual([r['uuid'] for r in self.witness('F.Cu')], ['pad-7'])
    def test_internal_layer_with_duplicate_display_names(self):
        self.assertEqual([r['uuid'] for r in self.witness('In2.Cu')], ['pad-21'])
    def test_disabled_or_display_only_layer_rejected(self):
        for layer in ['same', 'In3.Cu', 'F.Mask']:
            with self.subTest(layer=layer), self.assertRaises(ValueError): self.witness(layer)


if __name__ == '__main__': unittest.main()
