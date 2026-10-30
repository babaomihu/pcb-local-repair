import copy
from decimal import Decimal
from fractions import Fraction
import math
import unittest

from pcb_local.partial_segments import split_segment, verify_plan


def track(start=(16, 13.2), end=(16, 16.8), width=.3):
    return dict(kind='segment', uuid='original-neighbour', net='NEIGHBOUR',
                start=list(start), end=list(end), width_mm=width, problem_layer='F.Cu')


def nm(point):
    result = [Decimal(str(x)) * 1_000_000 for x in point]
    assert all(x == x.to_integral_value() for x in result)
    return [int(x) for x in result]


class PartialSegments(unittest.TestCase):
    def assert_exact_subdivision(self, row, plan):
        self.assertIsNotNone(plan)
        children = plan['children']
        self.assertEqual(len(children), 3)
        self.assertEqual([c['locked'] for c in children], [True, False, True])
        self.assertEqual(len({c['uuid'] for c in children}), 3)
        self.assertNotIn(row['uuid'], {c['uuid'] for c in children})
        start, end = nm(row['start']), nm(row['end'])
        delta = [end[i] - start[i] for i in range(2)]
        self.assertEqual(nm(children[0]['start']), start)
        self.assertEqual(nm(children[-1]['end']), end)
        positions = []
        for child in children:
            a, b = nm(child['start']), nm(child['end'])
            self.assertNotEqual(a, b)
            for p in (a, b):
                displacement = [p[i] - start[i] for i in range(2)]
                self.assertEqual(displacement[0] * delta[1] - displacement[1] * delta[0], 0)
                dot = sum(displacement[i] * delta[i] for i in range(2))
                self.assertGreaterEqual(dot, 0)
                self.assertLessEqual(dot, sum(x * x for x in delta))
            positions.append((a, b))
        self.assertEqual(positions[0][1], positions[1][0])
        self.assertEqual(positions[1][1], positions[2][0])
        width = int(Decimal(str(row['width_mm'])) * 1_000_000)
        for i in (0, 2):
            a, b = positions[i]
            self.assertGreaterEqual(4 * sum((b[j] - a[j]) ** 2 for j in range(2)), width ** 2)
        inset = Fraction(plan['margin_nm'])
        roi = list(map(Fraction, plan['region_nm']))
        for point in positions[1]:
            for axis in range(2):
                self.assertGreaterEqual(point[axis], roi[axis] + inset)
                self.assertLessEqual(point[axis], roi[axis + 2] - inset)

    def test_original_boundary_track_has_exact_fixed_tails(self):
        row = track()
        original = copy.deepcopy(row)
        result = split_segment(row, [4, 13, 26, 17], .45)
        self.assert_exact_subdivision(row, result)
        self.assertEqual(result['children'][1]['start'], [16, 13.45])
        self.assertEqual(result['children'][1]['end'], [16, 16.55])
        self.assertEqual(row, original)
        self.assertEqual(result, split_segment(row, [4, 13, 26, 17], .45))
        self.assertEqual(result['middle_uuid'], result['children'][1]['uuid'])

    def test_rotation_translation_and_endpoint_reversal(self):
        row = track()
        base = split_segment(row, [4, 13, 26, 17], .45)
        transform = lambda p: [round(50.137-p[1], 6), round(p[0]+3.333, 6)]
        transformed = track(transform(row['start']), transform(row['end']))
        result = split_segment(transformed, [33.137, 7.333, 37.137, 29.333], .45)
        self.assert_exact_subdivision(transformed, result)
        for old, new in zip(base['children'], result['children']):
            self.assertEqual(new['start'], transform(old['start']))
            self.assertEqual(new['end'], transform(old['end']))
        reversed_row = track(row['end'], row['start'])
        reversed_plan = split_segment(reversed_row, [4, 13, 26, 17], .45)
        self.assert_exact_subdivision(reversed_row, reversed_plan)
        for old, new in zip(base['children'], reversed(reversed_plan['children'])):
            self.assertEqual(new['start'], old['end'])
            self.assertEqual(new['end'], old['start'])
            self.assertEqual(new['locked'], old['locked'])

    def test_oblique_cuts_use_exact_original_lattice_points(self):
        row = track((0, 0), (.003, .002), .0002)
        result = split_segment(row, [-.0001, -.0001, .0031, .0021], .0004005)
        self.assert_exact_subdivision(row, result)
        self.assertEqual(result['lattice_step_nm'], [3, 2])
        # Inset intersection has fractional nanometres; inward selection must
        # still land on the original 3:2 lattice without rounding the centreline.
        self.assertEqual(result['margin_nm'], '801/2')
        for child in result['children']:
            for point in (child['start'], child['end']):
                x, y = nm(point)
                self.assertEqual(2*x, 3*y)

    def test_no_integer_internal_points_and_zero_length_are_refused(self):
        self.assertIsNone(split_segment(track((0, 0), (.000003, .000002), .000001), [-1,-1,1,1], .01))
        self.assertIsNone(split_segment(track((0,0), (0,0)), [-1,-1,1,1], .2))

    def test_no_safe_middle_or_too_short_for_fixed_tails(self):
        self.assertIsNone(split_segment(track(), [4,13,26,17], 2.1))
        self.assertIsNone(split_segment(track((0,0), (.1,0)), [-1,-1,1,1], .2))
        self.assertIsNone(split_segment(track((10,13.2), (16,13.2)), [4,13,26,17], .45))

    def test_whole_original_copper_must_already_fit(self):
        row = track((0,0), (1,0), .2)
        self.assertIsNotNone(split_segment(row, [-.1,-1,1.1,1], .2))
        self.assertIsNone(split_segment(row, [-.099999,-1,1.1,1], .2))
        self.assertIsNone(split_segment(row, [-.1,-.099999,1.1,1], .2))
        locked = dict(row, locked=True)
        self.assertIsNone(split_segment(locked, [-1,-1,2,1], .2))

    def test_invalid_native_values_are_not_silently_quantized(self):
        row = track()
        for bad in [float('nan'), float('inf'), True, '0.45', -.1, .01]:
            with self.subTest(margin=bad), self.assertRaises(ValueError):
                split_segment(row, [4,13,26,17], bad)
        for key, value in [('width_mm', .3000001), ('start', [16,13.2000001]),
                           ('width_mm', 0), ('net', ''), ('uuid', '')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                split_segment(dict(row, **{key:value}), [4,13,26,17], .45)
        with self.assertRaises(ValueError):
            split_segment(row, [4,17,26,13], .45)

    def test_reconstruction_rejects_tampered_geometry_identity_and_permissions(self):
        row = track()
        plan = split_segment(row, [4,13,26,17], .45)
        self.assertEqual(verify_plan(row, [4,13,26,17], .45, plan), plan)
        edits = [lambda p: p['children'][0].update(locked=False),
                 lambda p: p['children'][1].update(start=[16,13.3]),
                 lambda p: p['children'][1].update(uuid='forged'),
                 lambda p: p.update(parent_uuid='unauthorized'),
                 lambda p: p.update(margin_nm='0'),
                 lambda p: p.update(schema_version=True),
                 lambda p: p['children'][0].update(locked=1),
                 lambda p: p.update(extra='unknown'),
                 lambda p: p['children'].pop()]
        for edit in edits:
            bad = copy.deepcopy(plan); edit(bad)
            with self.subTest(plan=bad), self.assertRaises(ValueError):
                verify_plan(row, [4,13,26,17], .45, bad)
        with self.assertRaises(ValueError):
            verify_plan(dict(row, net='OTHER'), [4,13,26,17], .45, plan)
        with self.assertRaises(ValueError):
            verify_plan(row, [4,13,26,17], .450001, plan)


if __name__ == '__main__':
    unittest.main()
