"""Whole-stack mapping and omitted-layer protection before native routing."""
import copy
from pathlib import Path
import tempfile
import unittest
from pcb_local.layer_stack import native_copper_id, serialized_copper_id, standard_stack, supported_stack
from pcb_local.pns_adapter import make_problem


class LayerStack(unittest.TestCase):
    def test_native_and_serialized_ids_are_distinct(self):
        for count in (2,4,6,8):
            stack=standard_stack(count)
            self.assertTrue(supported_stack(stack))
            self.assertEqual([native_copper_id(l) for l in stack], [0,*range(4,2*count,2),2])
            self.assertEqual([serialized_copper_id(l) for l in stack], [0,*range(1,count-1),31])

    def test_missing_reordered_or_unknown_stack_is_not_supported(self):
        for layers in (['F.Cu','In1.Cu','B.Cu'],['B.Cu','F.Cu'],standard_stack(8)[::-1],
                       ['F.Cu','In1.Cu','In3.Cu','B.Cu'],['F.Cu',*('In%d.Cu'%i for i in range(1,9)),'B.Cu']):
            self.assertFalse(supported_stack(layers))
        with self.assertRaises(ValueError):native_copper_id('In7.Cu')

    def test_missing_fixed_layer_cannot_make_pns_problem(self):
        with tempfile.TemporaryDirectory() as temp:
            model=dict(copper_layers=standard_stack(8), auxiliary_layers={})
            with self.assertRaisesRegex(ValueError,'every copper layer'):
                make_problem(Path(temp)/'unused.kicad_pcb',{'allowed_layer':'In6.Cu'},model,{},Path(temp)/'problem')

    def test_missing_layer_projection_cannot_make_pns_problem(self):
        stack=standard_stack(6)
        model=dict(copper_layers=stack,auxiliary_layers={layer:{} for layer in stack if layer!='In4.Cu'})
        projection=dict(auxiliary_layers={layer:{} for layer in stack if layer not in ('In4.Cu','In3.Cu')})
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError,'per-layer fixed copper'):
                make_problem(Path(temp)/'unused.kicad_pcb',{'allowed_layer':'In4.Cu'},model,{},Path(temp)/'problem',projection)
