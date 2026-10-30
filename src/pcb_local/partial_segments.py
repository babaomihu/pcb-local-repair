"""Exact subdivision proposals for already-authorized original straight tracks.

This module never grants permission. The caller must independently witness the
parent against the original modification contract before using a subdivision.
Only integer-nanometre points on the original centreline may become cut points;
fixed end pieces and a movable middle therefore initially cover identical copper.
The inset is a search heuristic, not a bound on global zone-refill effects.
"""
from decimal import Decimal
from fractions import Fraction
import json
import math
import uuid


def _nm_fraction(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Subdivision coordinates and margins must be finite numbers')
    return Fraction(Decimal(str(value))) * 1_000_000


def _nm_integer(value):
    result = _nm_fraction(value)
    if result.denominator != 1:
        raise ValueError('Original subdivision geometry is not on the native nanometre grid')
    return result.numerator


def _point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError('Subdivision requires a two-coordinate endpoint')
    return tuple(_nm_integer(x) for x in value)


def _ceil(value):
    return -((-value.numerator) // value.denominator)


def _mm(point):
    return [float(Fraction(x, 1_000_000)) for x in point]


def split_segment(row, region_mm, margin_mm, *, allow_crossing=False):
    """Return a derived three-piece plan or None when no safe middle exists.

    ``row`` is an original PNS segment primitive (uuid/start/end/width_mm/net;
    optional problem_layer or layer). Locked or zero-length segments return None.
    Invalid numbers and sub-nanometre original geometry raise ValueError. The
    whole original copper envelope must fit in the existing ROI unless the caller
    explicitly requests the v2 local-middle contract. Both fixed tails
    are at least half a trace width long and contain at least one lattice step.
    The middle centreline lies in the ROI inset by the supplied native-derived
    margin. Neither the ROI nor this margin is inferred from fixture coordinates.
    """
    if not isinstance(row, dict) or row.get('kind', 'segment') != 'segment':
        raise ValueError('Subdivision requires an original straight segment')
    if not isinstance(row.get('uuid'), str) or not row['uuid']:
        raise ValueError('Subdivision requires a stable parent UUID')
    if not isinstance(row.get('net'), str) or not row['net']:
        raise ValueError('Subdivision requires a named original net')
    if row.get('locked'):
        return None
    start, end = _point(row['start']), _point(row['end'])
    width = _nm_integer(row['width_mm'])
    if width <= 0:
        raise ValueError('Subdivision width must be positive')
    if not isinstance(region_mm, (list, tuple)) or len(region_mm) != 4:
        raise ValueError('Subdivision requires the original rectangular ROI')
    region = tuple(_nm_fraction(x) for x in region_mm)
    if region[0] >= region[2] or region[1] >= region[3]:
        raise ValueError('Invalid subdivision ROI')
    margin = _nm_fraction(margin_mm)
    radius = Fraction(width, 2)
    if margin < radius:
        raise ValueError('Subdivision inset must cover at least the original copper radius')
    if not allow_crossing and any(min(start[i], end[i]) - radius < region[i]
           or max(start[i], end[i]) + radius > region[i + 2] for i in range(2)):
        return None
    delta = tuple(end[i] - start[i] for i in range(2))
    steps = math.gcd(abs(delta[0]), abs(delta[1]))
    if steps <= 1:
        return None
    step = tuple(x // steps for x in delta)
    squared_step = sum(x * x for x in step)
    # Smallest integer number of lattice steps whose length is >= width/2.
    # Integer arithmetic avoids an outward rounding at an oblique cut point.
    tail_steps = math.isqrt(width * width // (4 * squared_step))
    if 4 * tail_steps * tail_steps * squared_step < width * width:
        tail_steps += 1
    tail_steps = max(1, tail_steps)
    lower, upper = Fraction(tail_steps), Fraction(steps - tail_steps)
    for axis in range(2):
        low, high = region[axis] + margin, region[axis + 2] - margin
        if low > high:
            return None
        if step[axis] == 0:
            if not low <= start[axis] <= high:
                return None
            continue
        bounds = sorted(((low - start[axis]) / step[axis],
                         (high - start[axis]) / step[axis]))
        lower, upper = max(lower, bounds[0]), min(upper, bounds[1])
    first, last = _ceil(lower), upper.numerator // upper.denominator
    if not 0 < first < last < steps:
        return None
    cut_a = tuple(start[i] + first * step[i] for i in range(2))
    cut_b = tuple(start[i] + last * step[i] for i in range(2))
    layer = row.get('problem_layer', row.get('layer'))
    signature = dict(uuid=row['uuid'], start_nm=list(start), end_nm=list(end),
                     width_nm=width, net=row['net'], layer=layer)
    children = []
    for index, (a, b) in enumerate(zip((start, cut_a, cut_b), (cut_a, cut_b, end))):
        key = 'pcb-local-partial-segment:' + row['uuid'] + ':' + repr((a, b, width, layer, row['net']))
        children.append(dict(uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, key)),
                             start=_mm(a), end=_mm(b), locked=index != 1))
    return dict(schema_version=2 if allow_crossing else 1, parent_uuid=row['uuid'], source_signature=signature,
                region_nm=[str(x) for x in region], margin_nm=str(margin),
                lattice_step_nm=list(step), lattice_steps=steps,
                cut_indices=[first, last], minimum_tail_steps=tail_steps,
                children=children, middle_uuid=children[1]['uuid'])


def verify_plan(row, region_mm, margin_mm, plan, *, allow_crossing=False):
    """Recompute a plan from original data; never trust submitted child geometry.

    The caller supplies the original native-derived margin, not one read from
    ``plan``. Unknown fields, UUID edits, missing tails, or widened intervals fail.
    """
    expected = split_segment(row, region_mm, margin_mm, allow_crossing=allow_crossing)
    try:
        matches = (json.dumps(plan, sort_keys=True, allow_nan=False)
                   == json.dumps(expected, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError):
        matches = False
    if expected is None or not matches:
        raise ValueError('Subdivision plan differs from the independently reconstructed original geometry')
    return expected
