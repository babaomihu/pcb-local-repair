import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from pcb_local.zone_fill_witness import compare, extract


def rectangle(x0=0, y0=0, x1=10, y1=10):
    return dict(shell=[[x0, y0], [x1, y0], [x1, y1], [x0, y1]], holes=[])


def snapshot():
    return dict(status='ready', schema_version=1, native_version='10.0.4',
                board_sha256='0' * 64, board_path='/not-read-by-compare/board.kicad_pcb',
                copper_layers=['F.Cu', 'B.Cu'],
                zones=[dict(uuid='plane', net='GND', locked=False, rule_area=False,
                            source_owner_uuid=None, layers=['F.Cu', 'B.Cu'],
                            fills={'F.Cu': [rectangle()], 'B.Cu': [rectangle()]}),
                       dict(uuid='fixed', net='VCC', locked=True, rule_area=False,
                            source_owner_uuid=None, layers=['F.Cu'],
                            fills={'F.Cu': [rectangle(20, 20, 30, 30)]})])


def task():
    return dict(target_net='SIGNAL', allowed_layer='F.Cu', region_mm=[2, 2, 8, 8],
                zone_refill={'schema_version': 1, 'zone_uuids': ['plane']})


class ZoneFillWitness(unittest.TestCase):
    def test_unchanged_and_local_void_are_exactly_accepted(self):
        before = snapshot()
        for hole in (None, [[3, 3], [7, 3], [7, 7], [3, 7]]):
            after = copy.deepcopy(before)
            if hole:
                after['zones'][0]['fills']['F.Cu'][0]['holes'] = [hole]
            frozen = copy.deepcopy((before, after))
            result = compare(before, after, task())
            self.assertTrue(result['checks']['zone_refill_scope_preserved'])
            self.assertEqual((before, after), frozen)
            self.assertEqual(result['source_sha256']['after'], '0' * 64)
            if hole:
                changed = next(r for r in result['zones'] if not r['exact_equal'])
                self.assertEqual(changed['removed_area_mm2'], 16)
                self.assertEqual(changed['unauthorized_change_area_mm2'], 0)

    def test_boundary_touch_allowed_but_one_nanometre_overrun_rejected(self):
        before = snapshot()
        for edge, expected in ((2, True), (1.999999, False)):
            after = copy.deepcopy(before)
            after['zones'][0]['fills']['F.Cu'][0]['holes'] = [rectangle(edge, 3, 4, 5)['shell']]
            result = compare(before, after, task())
            self.assertEqual(result['checks']['zone_refill_scope_preserved'], expected)
            if not expected:
                row = next(r for r in result['zones'] if not r['scope_preserved'])
                self.assertGreater(row['unauthorized_change_area_mm2'], 0)
                self.assertLess(row['unauthorized_change_area_mm2'], .000003)

    def test_local_added_fill_is_allowed_but_outside_is_not(self):
        before = snapshot()
        before['zones'][0]['fills']['F.Cu'][0]['holes'] = [rectangle(3, 3, 4, 4)['shell']]
        after = snapshot()
        self.assertTrue(compare(before, after, task())['checks']['zone_refill_scope_preserved'])
        after['zones'][0]['fills']['F.Cu'].append(rectangle(10, 1, 11, 2))
        self.assertFalse(compare(before, after, task())['checks']['zone_refill_scope_preserved'])

    def test_unselected_zone_and_other_layer_are_protected(self):
        for variant in ('fixed', 'other-layer'):
            before = snapshot(); after = copy.deepcopy(before)
            index, layer = (1, 'F.Cu') if variant == 'fixed' else (0, 'B.Cu')
            after['zones'][index]['fills'][layer] = []
            with self.subTest(variant=variant):
                result = compare(before, after, task())
                self.assertFalse(result['checks']['zone_refill_scope_preserved'])
                self.assertTrue(any(not r['scope_preserved'] and not r['authorized_local_refill'] for r in result['zones']))

    def test_zone_identity_metadata_and_stack_changes_are_rejections(self):
        for variant in ('uuid', 'net', 'locked', 'owner', 'layer', 'stack', 'missing', 'extra'):
            before = snapshot(); after = copy.deepcopy(before)
            if variant == 'uuid': after['zones'][0]['uuid'] = 'different'
            elif variant == 'net': after['zones'][0]['net'] = 'OTHER'
            elif variant == 'locked': after['zones'][0]['locked'] = True
            elif variant == 'owner': after['zones'][0]['source_owner_uuid'] = 'footprint'
            elif variant == 'layer':
                after['zones'][0]['layers'].remove('B.Cu'); after['zones'][0]['fills'].pop('B.Cu')
            elif variant == 'stack': after['copper_layers'].insert(1, 'In1.Cu')
            elif variant == 'missing': after['zones'].pop(1)
            else:
                extra = copy.deepcopy(after['zones'][1]); extra['uuid'] = 'new'; after['zones'].append(extra)
            with self.subTest(variant=variant):
                result = compare(before, after, task())
                self.assertEqual(result['status'], 'verified')
                self.assertFalse(result['checks']['zone_refill_scope_preserved'])

    def test_invalid_selected_zone_is_unsupported(self):
        for variant in ('locked', 'owner', 'target', 'no-net', 'rule', 'missing'):
            before = snapshot()
            if variant == 'locked': before['zones'][0]['locked'] = True
            elif variant == 'owner': before['zones'][0]['source_owner_uuid'] = 'footprint'
            elif variant == 'target': before['zones'][0]['net'] = 'SIGNAL'
            elif variant == 'no-net': before['zones'][0]['net'] = ''
            elif variant == 'missing': before['zones'].pop(0)
            else:
                before['zones'][0]['rule_area'] = True
                before['zones'][0]['fills'] = {'F.Cu': [], 'B.Cu': []}
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                compare(before, copy.deepcopy(before), task())

    def test_malformed_or_incomplete_coverage_fails_closed(self):
        for variant in ('missing-fill-layer', 'duplicate', 'hash', 'nan', 'fractional-nm', 'crossed', 'version'):
            before = snapshot(); after = copy.deepcopy(before)
            if variant == 'missing-fill-layer': after['zones'][0]['fills'].pop('B.Cu')
            elif variant == 'duplicate': after['zones'].append(copy.deepcopy(after['zones'][0]))
            elif variant == 'hash': after['board_sha256'] = 'unbound'
            elif variant == 'version': after['native_version'] = '9.0.8'
            elif variant == 'nan': after['zones'][0]['fills']['F.Cu'][0]['shell'][0][0] = float('nan')
            elif variant == 'fractional-nm': after['zones'][0]['fills']['F.Cu'][0]['shell'][0][0] = .0000001
            else: after['zones'][0]['fills']['F.Cu'][0]['shell'] = [[0, 0], [10, 10], [10, 0], [0, 10]]
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                compare(before, after, task())

    def test_holes_and_fracture_bridge_represent_same_native_copper(self):
        before = snapshot(); after = copy.deepcopy(before)
        # KiCad may serialize a hole using a doubled bridge. It is an exact
        # topological equivalence, not a polygon simplification tolerance.
        before['zones'][0]['fills']['F.Cu'][0]['holes'] = [rectangle(3, 3, 7, 7)['shell']]
        after['zones'][0]['fills']['F.Cu'] = [dict(holes=[], shell=[
            [0, 0], [10, 0], [10, 10], [0, 10], [0, 0],
            [3, 3], [3, 7], [7, 7], [7, 3], [3, 3], [0, 0]])]
        result = compare(before, after, task())
        self.assertTrue(result['checks']['zone_refill_scope_preserved'])
        self.assertTrue(all(r['exact_equal'] for r in result['zones']))

    def test_bad_hash_refused_before_loading_native_library(self):
        with tempfile.TemporaryDirectory() as directory:
            board = Path(directory) / 'board.kicad_pcb'; board.write_bytes(b'not a board')
            with self.assertRaisesRegex(ValueError, 'differs before'):
                extract(board, '0' * 64)

    def test_cli_distinguishes_rejected_and_unverifiable(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            before = snapshot(); after = copy.deepcopy(before)
            after['zones'][0]['fills']['B.Cu'] = []
            for variant, expected in (('rejected', 1), ('unsupported', 2)):
                if variant == 'unsupported': after['board_sha256'] = 'invalid'
                for name, value in (('before', before), ('after', after), ('task', task())):
                    (folder / (name + '.json')).write_text(json.dumps(value))
                code = subprocess.run([sys.executable, '-m', 'pcb_local.zone_fill_witness', 'compare',
                                       '--before', str(folder / 'before.json'), '--after', str(folder / 'after.json'),
                                       '--task', str(folder / 'task.json'), '--output', str(folder / 'result.json')],
                                      capture_output=True).returncode
                with self.subTest(variant=variant):
                    self.assertEqual(code, expected)
                    result = json.loads((folder / 'result.json').read_text())
                    self.assertEqual(result['status'], 'verified' if expected == 1 else 'unsupported')


if __name__ == '__main__':
    unittest.main()
