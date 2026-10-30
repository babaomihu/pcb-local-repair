"""The search-only clearance envelope must never drop authored rule semantics."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from pcb_local.custom_clearance import resolve


class CustomClearance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.board = Path(self.temp.name) / 'board.kicad_pcb'
        self.board.write_text('(kicad_pcb)\n')
        self.board.with_suffix('.kicad_pro').write_text(json.dumps({}))
        self.dru = self.board.with_suffix('.kicad_dru')
        self.stack = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']

    def rules(self, text):
        self.dru.write_text(text, encoding='utf-8')

    def value(self, layer='F.Cu', **kwargs):
        return resolve(self.board, layer, self.stack, **kwargs)

    def test_absent_and_version_only_are_supported_without_an_invented_floor(self):
        absent = self.value()
        self.assertIsNone(absent['authored_rules_sha256'])
        self.assertEqual(absent['effective_layer_minima_mm'], dict.fromkeys(self.stack, 0))
        self.assertEqual(absent['selected_layer_min_mm'], 0)
        self.assertEqual(absent['search_minimum_mm'], 0)
        self.rules('(version 1)\n')
        present = self.value()
        self.assertEqual(present['search_minimum_mm'], 0)
        self.assertEqual(present['authored_rules_sha256'], hashlib.sha256(self.dru.read_bytes()).hexdigest())

    def test_global_direct_inner_outer_and_file_order(self):
        self.rules('''(version 1)
          (rule "first" (constraint clearance (min 0.6mm)))
          (rule "outer" (layer outer) (constraint clearance (min 0.4mm)))
          (rule "inner" (layer inner) (constraint clearance (min 0.5mm)))
          (rule "front" (layer "F.Cu") (constraint clearance (min 0.3mm)))
          (rule "one inner" (layer "In1.Cu") (constraint clearance (min 0.2mm)))
        ''')
        expected = {'F.Cu': .3, 'In1.Cu': .2, 'In2.Cu': .5, 'B.Cu': .4}
        for layer in self.stack:
            with self.subTest(layer=layer):
                result = self.value(layer)
                self.assertEqual(result['effective_layer_minima_mm'], expected)
                self.assertAlmostEqual(result['selected_layer_min_mm'], expected[layer])
                self.assertAlmostEqual(result['search_minimum_mm'], expected[layer])
        # PNS has one solver-wide floor: take the largest *effective* layer
        # requirement, not the overwritten historical rule or selected layer.
        conservative = self.value(conservative_stack=True)
        self.assertAlmostEqual(conservative['search_minimum_mm'], .5)
        self.assertAlmostEqual(conservative['selected_layer_min_mm'], .3)

    def test_warning_constraints_are_not_silently_ignored(self):
        self.rules('(version 1)(rule "warn" (severity warning) '
                   '(constraint clearance (min 0.7mm)))')
        self.assertAlmostEqual(self.value()['search_minimum_mm'], .7)

    def test_units_and_subnanometer_values_are_never_rounded_down(self):
        for token, required in [('10mil', .254), ('0.01in', .254),
                                ('0.254mm', .254), ('0.0000001mm', .0000001)]:
            with self.subTest(token=token):
                self.rules('(version 1)(rule "units" (constraint clearance (min '+token+')))')
                result = self.value()['search_minimum_mm']
                self.assertGreaterEqual(result, required)
                self.assertLessEqual(result-required, .000001001)

    def test_rule_evidence_is_bound_to_bytes_and_no_source_is_modified(self):
        self.rules('(version 1)(rule "keep" (constraint clearance (min .3mm)))\n')
        paths = [self.board, self.board.with_suffix('.kicad_pro'), self.dru]
        before = {p: p.read_bytes() for p in paths}
        first = self.value()
        self.assertEqual(before, {p: p.read_bytes() for p in paths})
        self.rules(self.dru.read_text() + '# authored comment\n')
        second = self.value()
        self.assertNotEqual(first['authored_rules_sha256'], second['authored_rules_sha256'])
        self.assertEqual(first['search_minimum_mm'], second['search_minimum_mm'])

    def test_unsupported_or_ambiguous_rules_fail_closed(self):
        cases = [
            '(version 2)',
            '(version 1)(version 1)',
            '(version 1)(unknown 1)',
            '(version 1)(rule "condition" (condition "A.NetName == \'SIGNAL\'") (constraint clearance (min .2mm)))',
            '(version 1)(rule "length" (constraint length (min 1mm)))',
            '(version 1)(rule "width" (constraint track_width (min .2mm)))',
            '(version 1)(rule "max" (constraint clearance (max .2mm)))',
            '(version 1)(rule "both" (constraint clearance (min .2mm) (max .4mm)))',
            '(version 1)(rule "ignore" (severity ignore) (constraint clearance (min .2mm)))',
            '(version 1)(rule "unknown severity" (severity exclusion) (constraint clearance (min .2mm)))',
            '(version 1)(rule "quoted group" (layer "outer") (constraint clearance (min .2mm)))',
            '(version 1)(rule "bad layer" (layer F.SilkS) (constraint clearance (min .2mm)))',
            '(version 1)(rule "duplicate scope" (layer F.Cu) (layer B.Cu) (constraint clearance (min .2mm)))',
            '(version 1)(rule "duplicate constraint" (constraint clearance (min .2mm)) (constraint clearance (min .3mm)))',
            '(version 1)(rule "negative" (constraint clearance (min -1mm)))',
            '(version 1)(rule "unitless" (constraint clearance (min .2)))',
            '(version 1)(rule "nonfinite" (constraint clearance (min NaNmm)))',
            '(version 1)(rule "field" (something x) (constraint clearance (min .2mm)))',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.rules(text)
                with self.assertRaises(ValueError):
                    self.value()

    def test_missing_project_is_not_repaired_by_having_simple_custom_rules(self):
        self.board.with_suffix('.kicad_pro').unlink()
        self.rules('(version 1)(rule "safe" (constraint clearance (min .2mm)))')
        with self.assertRaises(ValueError):
            self.value()


if __name__ == '__main__':
    unittest.main()
