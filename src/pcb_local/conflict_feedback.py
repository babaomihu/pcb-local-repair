"""Evidence-ranked search choices; never expands authority or accepts copper.

DRC participants are observed candidate violations. A native straight-corridor
intersection is only a relevance hint, not proof that the item blocks all paths.
"""
import copy
import hashlib
import math

from .modification import contract, witness
from .recommendations import segments_distance


def collect(report):
    """Bind failure participants to objects present in this native baseline."""
    native = report.get('preflight', {}).get('measurement', {}).get('snapshot', {})
    items = native.get('items', {})
    objects = {row.get('uuid', key):row for key,row in items.items()}
    found = {}
    contexts = [('task', report)] + [(a.get('name', 'strategy'), a.get('details', {}))
        for a in report.get('automatic_strategy', {}).get('attempts', [])
        if a.get('status') in ('no_progress', 'rejected', 'timed_out')]
    def add(uid, kind, source):
        if not isinstance(uid, str) or uid not in objects:
            return
        row = found.setdefault(uid, dict(uuid=uid, net=objects[uid].get('net'),
            kinds=[], sources=[]))
        if kind not in row['kinds']:row['kinds'].append(kind)
        if source not in row['sources']:row['sources'].append(source)
    for label, detail in contexts:
        for obj in detail.get('pns', {}).get('direct_path_obstacles', {}).get('items', []):
            add(obj.get('uuid'), 'direct_corridor_hint', label)
        verification = detail.get('verification', {})
        if not verification.get('verification_complete'):
            continue
        for scope in ('drc', 'supplemental_drc'):
            for section in verification.get(scope, {}).get('sections', {}).values():
                for issue in section.get('new_or_changed', []):
                    for item in issue.get('items', []):
                        add(item.get('uuid'), 'native_drc_participant', label)
    return dict(schema_version=1, objects=[found[u] for u in sorted(found)],
        authority='Search ordering only; no permission or acceptance grant',
        physical_impossibility_proven=False)


def changed_objects(report):
    protection = report.get('verification', {}).get('protection', {})
    return sorted(set(protection.get('added_routing', [])) |
                  set(protection.get('changed_routing', [])))


def corridor_hits(native, task):
    """Cheap native straight-track hints for existing authorized components."""
    items = native.get('items', {})
    by_uuid = {row.get('uuid', key):row for key,row in items.items()}
    ids = task.get('target_item_ids') or task.get('target_pad_uuids', [])
    ends = [items.get(u, by_uuid.get(u, {})) for u in ids]
    if len(ends) != 2 or any(not e.get('position_mm') for e in ends):return []
    points = [e['position_mm'] for e in ends]
    if any(len(p) != 2 or not all(math.isfinite(x) for x in p) for p in points):return []
    width = max((e.get('netclass') or {}).get('track_width_mm', 0) for e in ends)
    gap = max((e.get('netclass') or {}).get('clearance_mm', 0) for e in ends)
    if width <= 0 or gap < 0:return []
    hits = []
    for uid in contract(task)['routing_uuids']:
        row = items.get(uid, {})
        if row.get('kind') != 'segment' or task['allowed_layer'] not in row.get('copper_layers', []):continue
        clearance = max(gap, (row.get('netclass') or {}).get('clearance_mm', 0))
        if segments_distance(*points, row['start_mm'], row['end_mm']) <= width/2 + row['width_mm']/2 + clearance:
            hits.append(uid)
    return sorted(hits)


def focused_tasks(native, task, feedback=None, limit=2):
    """At most two witnessed, strictly smaller original permission subsets.

Keep all authorized pieces of a selected original electrical component to
avoid inventing new internal attachment semantics. If this cannot be witnessed,
skip the subset. The full original plan remains available afterwards.
"""
    spec = contract(task)
    if not spec['routing_uuids'] or task.get('partial_segments'):return []
    original = set(spec['routing_uuids']);items = native.get('items', {})
    components = native.get('component_of', {})
    if any(u not in items or u not in components for u in original):return []
    scores = {u:1 for u in corridor_hits(native, task)}
    for row in (feedback or {}).get('objects', []):
        if row['uuid'] in original:
            scores[row['uuid']] = 10 if 'native_drc_participant' in row['kinds'] else 2
    groups = {}
    for uid in sorted(original):groups.setdefault(components[uid], []).append(uid)
    ranked = sorted(groups.values(), key=lambda ids:(-sum(scores.get(u,0) for u in ids),len(ids),ids))
    result = []
    for ids in ranked:
        if not any(u in scores for u in ids) or set(ids) == original:continue
        child = copy.deepcopy(task)
        child['modification'] = dict(spec, routing_uuids=ids,
            nets=sorted({items[u]['net'] for u in ids}), max_modified_tracks=min(spec['max_modified_tracks'],len(ids)))
        child.pop('requested_modification_nets', None)
        try:proof = witness(native, child)
        except ValueError:continue
        key = hashlib.sha256('\n'.join(ids).encode()).hexdigest()[:12]
        result.append((key, child, dict(selected_uuids=ids,
            retained_fixed_uuids=sorted(original-set(ids)), terminal_witness=proof,
            evidence_uuids=sorted(set(ids)&set(scores)),
            reason='Original authorized component intersects a native direct corridor or observed failure; relevance only')))
        if len(result) >= limit:break
    return result
