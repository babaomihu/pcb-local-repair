"""Small immutable priority relations, never a physical infeasibility proof.

Project-authored implementation informed by tscircuit's MIT priority-DAG
design. No router geometry, relaxed checks, or source implementation is copied.
Only fully witnessed conflicts may cause the joint coordinator to add edges.
"""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PriorityGraph:
    targets: tuple
    ancestors: tuple

    @classmethod
    def empty(cls, targets):
        targets = tuple(targets)
        if not 2 <= len(targets) <= 4 or len(set(targets)) != len(targets):
            raise ValueError('Priority graph requires 2–4 distinct original targets')
        return cls(targets, tuple(frozenset() for _ in targets))

    def extend(self, higher, lower):
        """Return closure after higher→lower, or None for existing/cyclic edge."""
        a, b = self.targets.index(higher), self.targets.index(lower)
        if a == b or b in self.ancestors[a] or a in self.ancestors[b]:
            return None
        added = self.ancestors[a] | {a}
        closure = tuple(parents | added if i == b or b in parents else parents
                        for i, parents in enumerate(self.ancestors))
        return PriorityGraph(self.targets, closure)

    def order(self):
        """Stable topological order; equal-priority ties keep requested order."""
        pending = list(range(len(self.targets)))
        done = set(); ordered = []
        while pending:
            ready = next((i for i in pending if self.ancestors[i] <= done), None)
            if ready is None:
                raise ValueError('Cyclic priority state')
            pending.remove(ready); done.add(ready); ordered.append(self.targets[ready])
        return ordered

    def key(self):
        return tuple(tuple(sorted(parents)) for parents in self.ancestors)

    def evidence(self):
        return {target: [self.targets[i] for i in sorted(self.ancestors[n])]
                for n, target in enumerate(self.targets)}


def copper_crossings(left, left_ids, right, right_ids, *, check=None):
    """Exact centreline intersections between independently native-accepted copper.

    This deliberately only recognizes positive-width straight segments on the
    same copper layer and DIFFERENT nets. It does not guess clearance, infer
    copper shape from a bbox, or declare non-intersecting routes conflict-free.
    Inputs are parsed immutable boards bound to full native accepted reports.
    Return at most one actual intersection witness; never enumerate a quadratic
    collision set or imply that no witness proves absence of clearance conflicts.
    """
    from .validation import name, field
    check = check or (lambda: None)
    check()
    def segment(board, uid):
        check()
        node = board['routing'].get(uid)
        if name(node) != 'segment': return None
        width = Decimal(str(field(node, 'width')[1]))
        if not width.is_finite() or width <= 0: return None
        points = []
        for key in ('start', 'end'):
            point = []
            for value in field(node, key)[1:3]:
                number = Decimal(str(value)) * 1000000
                if not number.is_finite() or number != number.to_integral_value():
                    return None
                point.append(int(number))
            if len(point) != 2: return None
            points.append(tuple(point))
        if points[0] == points[1]: return None
        return points[0], points[1], str(field(node, 'layer')[1]), board['identities'][uid]['net']
    def cross(a, b, c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    def touch(a, b, c, d):
        if any(max(a[i], b[i]) < min(c[i], d[i]) or max(c[i], d[i]) < min(a[i], b[i]) for i in (0, 1)):
            return False
        return cross(a,b,c)*cross(a,b,d) <= 0 and cross(c,d,a)*cross(c,d,b) <= 0
    result = []
    a = [(uid, segment(left, uid)) for uid in sorted(left_ids)]
    b = [(uid, segment(right, uid)) for uid in sorted(right_ids)]
    for left_id, l in a:
        check()
        if l is None: continue
        for index, (right_id, r) in enumerate(b):
            if index % 128 == 0: check()
            if r is None or l[2] != r[2] or l[3] == r[3] or not l[3] or not r[3]: continue
            if touch(l[0], l[1], r[0], r[1]):
                result.append(dict(left_uuid=left_id, right_uuid=right_id, layer=l[2],
                    kind='exact_native_accepted_copper_centerline_intersection'))
                check()
                return result
    check()
    return result
