import copy
import unittest
import tempfile
from pathlib import Path
from pcb_local.engine import routing_plan, check_layer_rule_floor, check_terminal_layers
from pcb_local.jobs import Halt


def native():
    return {'routing_rules': {'board_minima_mm': dict(clearance=.3, track_width=.25,
        via_diameter=.6, via_drill=.3, via_annular_width=.15), 'netclasses_mm': {
        '': dict(clearance=.2, track_width=.2, via_diameter=.6, via_drill=.3),
        'SIGNAL': dict(clearance=.2, track_width=.2, via_diameter=.6, via_drill=.3),
        'WIDE': dict(clearance=.7, track_width=.6, via_diameter=1., via_drill=.4)}}}


class NativeRoutingRules(unittest.TestCase):
    def test_board_floor_and_foreign_class_preserved(self):
        n=native(); original=copy.deepcopy(n); p=routing_plan(n,['SIGNAL'])
        self.assertEqual(p['net_clearances_mm'], {'':.3,'SIGNAL':.3,'WIDE':.7})
        self.assertEqual(p['track_width_mm'],.25); self.assertEqual(n,original)

    def test_common_width_cannot_thin_selected_neighbor(self):
        p=routing_plan(native(),['SIGNAL','WIDE'])
        self.assertEqual(p['track_width_mm'],.6)

    def test_valid_per_net_widths_are_not_flattened(self):
        n=native(); n['routing_rules']['board_minima_mm']['track_width']=.1
        self.assertNotIn('track_width_mm',routing_plan(n,['SIGNAL','WIDE']))

    def test_via_hole_and_annulus_both_preserved(self):
        n=native(); n['routing_rules']['board_minima_mm']['via_drill']=.8
        p=routing_plan(n,['SIGNAL','WIDE'])
        self.assertEqual(p['via_drill_mm'],.8)
        self.assertGreaterEqual(p['via_diameter_mm'],1.1)

    def test_incomplete_or_nonfinite_evidence_refused(self):
        for n in ({}, {'routing_rules':{}}, native()):
            if n.get('routing_rules',{}).get('netclasses_mm'):
                n['routing_rules']['netclasses_mm']['WIDE']['clearance']=float('nan')
            with self.subTest(n=n),self.assertRaises(Halt):routing_plan(n,['SIGNAL'])
        with self.assertRaises(Halt):routing_plan(native(),['MISSING'])

    def test_layer_override_cannot_bypass_board_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            board=Path(tmp)/'board.kicad_pcb';dru=board.with_suffix('.kicad_dru')
            for value, okay in [('0.2mm',False),('10mil',False),('0.3mm',True),('0.7mm',True)]:
                dru.write_text('(version 1) (rule "test" (layer "F.Cu") (constraint clearance (min '+value+')))')
                if okay:check_layer_rule_floor(board,.3)
                else:
                    with self.assertRaises(Halt):check_layer_rule_floor(board,.3)
            dru.write_text('(version 1) (rule "test" (constraint clearance (min 0.2mm) (opt 0.4mm)))')
            with self.assertRaises(Halt):check_layer_rule_floor(board,.3)

    def test_off_layer_terminal_cannot_authorize_access_via(self):
        n={'items':{'a':dict(kind='pad',net='SIGNAL',copper_layers=['B.Cu'])}}
        policy={'max_new_vias':0,'route_layers':['F.Cu']}
        with self.assertRaises(Halt):check_terminal_layers(n,['SIGNAL'],policy)
        check_terminal_layers(n,['OTHER'],policy)
        n['items']['a']['copper_layers']=['F.Cu','B.Cu']
        check_terminal_layers(n,['SIGNAL'],policy)

if __name__=='__main__':unittest.main()
