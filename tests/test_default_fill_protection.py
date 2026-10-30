import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from pcb_local.validation import has_zones, verify
from pcb_local.zone_fill_witness import compare
from pcb_local.delivery import check_fill_evidence
from test_zone_fill_witness import snapshot, rectangle, task
from test_scope_candidate import BASE, RULES, TASK


class DefaultFillProtection(unittest.TestCase):
    def test_unselected_fill_is_fixed_including_inside_roi(self):
        value=task();value.pop('zone_refill')
        before=snapshot();after=copy.deepcopy(before)
        self.assertTrue(compare(before,after,value)['checks']['zone_refill_scope_preserved'])
        after['zones'][0]['fills']['F.Cu'][0]['holes']=[rectangle(3,3,4,4)['shell']]
        self.assertFalse(compare(before,after,value)['checks']['zone_refill_scope_preserved'])

    def test_empty_and_footprint_zones_are_detected_without_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'board.kicad_pcb'
            for text,expected in [('(kicad_pcb)',False),('(kicad_pcb (zone (uuid "z")))',True),
                                  ('(kicad_pcb (footprint "p" (zone (uuid "z"))))',True)]:
                p.write_text(text);self.assertEqual(has_zones(p),expected)

    def test_zone_board_without_geometry_runtime_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/'board.kicad_pcb'
            p.write_text(BASE[:-1]+'(zone (uuid "z")))');p.with_suffix('.kicad_pro').write_text(json.dumps(RULES))
            with patch('pcb_local.validation.measure') as measure:
                result=verify(p,p,TASK,root/'verification')
            measure.assert_not_called()
            self.assertFalse(result['verification_complete'])
            self.assertFalse(result['accepted'])

    def test_historical_accepted_without_fill_witness_not_deliverable(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/'board.kicad_pcb';p.write_text('(kicad_pcb (zone (uuid "z")))')
            report=dict(source_board=str(p),candidate=str(p),accepted=True,verification={})
            with self.assertRaisesRegex(ValueError,'fill protection evidence'):
                check_fill_evidence(root,report)


if __name__=='__main__':unittest.main()
