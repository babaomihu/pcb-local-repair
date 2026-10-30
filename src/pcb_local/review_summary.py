"""Read-only capability and copper-quality summaries, never acceptance gates."""
import math
from collections import Counter, defaultdict
from decimal import Decimal


def quality(changes):
    """Describe exactly the native verifier's routing delta, not whole-board SI."""
    details = changes.get('added_details', [])
    lengths = []
    layers = Counter()
    joints = defaultdict(list)
    others = Counter()
    for row in details:
        kind = row['kind']
        if kind != 'segment':
            others[kind] += 1
            continue
        a, b = row['start_mm'], row['end_mm']
        if not all(math.isfinite(float(x)) for x in (*a, *b)):
            raise ValueError('Nonfinite quality metric geometry')
        length = math.dist(a, b)
        lengths.append(length)
        layers[row['layer']] += length
        # Only exact shared endpoints on the same net/layer count as joints.
        def nm(p):
            return tuple(Decimal(str(x)) * 1000000 for x in p)
        a, b = nm(a), nm(b)
        key = (row['net'], row['layer'])
        joints[(*key, a)].append((b[0]-a[0], b[1]-a[1]))
        joints[(*key, b)].append((a[0]-b[0], a[1]-b[1]))
    bends = 0
    for vectors in joints.values():
        if len(vectors) != 2:
            continue
        a, b = vectors
        if a[0]*b[1] != a[1]*b[0]:
            bends += 1
    return dict(schema_version=1, display_only=True,
        added_straight_segments=len(lengths), added_vias=others.get('via', 0),
        added_arc_count=others.get('arc', 0),
        straight_length_mm=sum(lengths), straight_length_by_layer_mm=dict(layers),
        short_straight_segments=sum(x < .1 for x in lengths), short_segment_threshold_mm=.1,
        noncollinear_two_segment_joints=bends,
        modified_original_objects=len(changes.get('changed_routing', [])),
        removed_original_objects=len(changes.get('removed_routing', [])),
        limitation='仅新增直线段的几何指标，不含弧长/分支处转角；短段不是DRC违规，指标不能代替电气、阻抗或完整验收。')


def capabilities(task, config):
    """Show the real portfolio permission subsets; geometry is still unproven."""
    from .auto_strategy import plans
    rows = []
    for label, strategy, child in plans(task, bool(config.get('pns'))):
        policy = child['permissions']
        reason = '权限允许尝试，仍须通过原生几何建模和完整验收'
        state = 'requires_native_check'
        if child.get('zone_refill') and strategy != 'pns':
            state = 'unsupported_combination'
            reason = '显式局部重填铜目前仅由PNS策略处理；其他策略不能据此忽略铺铜'
        rows.append(dict(name=label, strategy=strategy, state=state, reason=reason,
            route_layers=policy['route_layers'], max_new_vias=policy['max_new_vias'],
            original_track_mode=child.get('modification', {}).get('mode', 'none')))
    return dict(schema_version=1, display_only=True, strategies=rows,
        limitations=['原孔、未授权原线和区域外对象保持保护；列表不授予新权限。',
            '跨层几何可尝试复用已见证同网贯通孔；0孔额度表示不新增孔，不保证存在可复用通道。',
            '推挤仍为所选单层；跨层几何保持原铜固定，不直接组合拆线或显式重填铜。',
            '联合修复仅2–4个所选目标，共享原目标预算和全组8次回退额度。'])


def quality_text(value):
    return ('新增直线段 {added_straight_segments}，新增圆弧 {added_arc_count}，新增过孔 {added_vias}，'
            '直线段总长 {straight_length_mm:.3f} mm，短于0.1 mm的段 {short_straight_segments}，'
            '两段相接的非共线转角 {noncollinear_two_segment_joints}；'
            '修改/删除原对象 {modified_original_objects}/{removed_original_objects}。').format(**value)
