"""Bounded old-track permissions with independently reconstructed terminal witnesses.

Only whole unlocked straight tracks with endpoint-to-endpoint/centre attachments
are eligible. Arbitrary mid-track taps and open-ended branches are refused.
"""
import math
from .task_policy import permissions
from .signal_scope import restrictions


def contract(task):
    value = task.get("modification")
    if value is None:
        return {"mode": "none", "routing_uuids": [], "nets": [], "max_modified_tracks": 0}
    if not isinstance(value, dict) or set(value) != {"schema_version", "mode", "routing_uuids", "nets", "max_modified_tracks"}:
        raise ValueError("Unknown modification permission contract")
    ids, nets, limit = value["routing_uuids"], value["nets"], value["max_modified_tracks"]
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["mode"] not in {"ripup", "shove", "shove+ripup"}
            or type(limit) is not int or not 1 <= limit <= 32
            or not isinstance(ids, list) or not ids or len(ids) > limit
            or not isinstance(nets, list) or not nets
            or any(not isinstance(x, str) or not x for x in ids + nets)
            or len(ids) != len(set(ids)) or len(nets) != len(set(nets))):
        raise ValueError("Invalid bounded modification permission")
    return value


def fits(row, task):
    if row["kind"] != "segment" or row.get("locked"):
        return False
    if not set(row["copper_layers"]) <= set(permissions(task)["route_layers"]):
        return False
    a, b, width = row["start_mm"], row["end_mm"], row["width_mm"]
    if not all(math.isfinite(x) for x in [*a, *b, width]) or width <= 0 or a == b:
        return False
    x0, y0, x1, y1 = task["region_mm"]
    return (min(a[0], b[0]) - width/2 >= x0 and max(a[0], b[0]) + width/2 <= x1
            and min(a[1], b[1]) - width/2 >= y0 and max(a[1], b[1]) + width/2 <= y1)


def witness(snapshot, task):
    spec = contract(task); selected = set(spec["routing_uuids"])
    if not selected:
        return {"selected": [], "components": [], "anchors": []}
    if selected.intersection(task.get("target_item_ids", [])):
        raise ValueError("Selected target endpoint objects must remain fixed")
    items = snapshot["items"]; names = {row["net"] for row in items.values()}
    scope_reasons = restrictions(snapshot)
    local_plans={p['parent_uuid']:p for p in task.get('partial_segments',[]) if p.get('schema_version')==2}
    if len(local_plans)!=sum(p.get('schema_version')==2 for p in task.get('partial_segments',[])):
        raise ValueError('Duplicate local-middle parent')
    if local_plans and (spec['mode'] not in ('shove','shove+ripup') or not set(local_plans)<=selected):
        raise ValueError('Local-middle plans require explicit selected shove objects')
    for net in spec["nets"]:
        reason = scope_reasons.get(net, "net absent")
        if reason:
            raise ValueError("Modification net is unsupported: " + net + ": " + reason)
    for uid in selected:
        if uid in local_plans:
            from .local_segments import reconstruct
            reconstruct(snapshot,task,uid,local_plans[uid])
        if uid not in items or items[uid]["net"] not in spec["nets"] or (uid not in local_plans and not fits(items[uid], task)):
            raise ValueError("Modified item is missing, locked, outside ROI/layers, or not a straight track: " + uid)
    if {items[uid]["net"] for uid in selected} != set(spec["nets"]):
        raise ValueError("Every authorized modification net must identify eligible original tracks")
    def endpoints(row):
        pts = [row["start_mm"], row["end_mm"]] if row["kind"] in {"segment", "arc"} else [row["position_mm"]]
        return {(round(p[0]*1e6), round(p[1]*1e6), layer) for p in pts for layer in row["copper_layers"]}
    endpoint_map = {}
    for uid, row in items.items():
        for pt in endpoints(row):
            endpoint_map.setdefault((row["net"], pt), set()).add(uid)
    anchors = set()
    for uid in selected:
        for pt in endpoints(items[uid]):
            attached = endpoint_map.get((items[uid]["net"], pt), set()) - {uid}
            if not attached:
                raise ValueError("Cannot witness an open branch or non-centre/mid-track attachment: " + uid)
            anchors.update(attached - selected)
    components = sorted({snapshot["component_of"][uid] for uid in selected})
    for i in components:
        stable = set(snapshot["components"][i]["items"]) - selected
        if len(stable & anchors) < 2:
            raise ValueError("Modified component needs at least two distinct unchanged terminal anchors")
    return {"selected": sorted(selected), "components": components, "anchors": sorted(anchors),
            "local_middle_uuids":sorted(local_plans),
            "scope": "Whole selected tracks or independently reconstructed local middles with fixed tails; preserved original terminal anchors"}


def select(snapshot, task, nets, budget, mode="ripup", *, fixed_fallback=False):
    # Invalid requests are not a reason to try a different permission policy.
    if type(budget) is not int or not 1 <= budget <= 32 or mode not in {'ripup','shove','shove+ripup'}:
        raise ValueError('Invalid bounded modification permission')
    names = {row["net"] for row in snapshot["items"].values()}
    scope_reasons = restrictions(snapshot)
    if len(nets) != len(set(nets)) or any(net not in names or scope_reasons.get(net) for net in nets):
        raise ValueError("Modification permission names an absent, duplicate or unsupported net")
    chosen = sorted(uid for uid, row in snapshot["items"].items() if row["net"] in nets and fits(row, task))
    active_nets = sorted({snapshot["items"][uid]["net"] for uid in chosen})
    if fixed_fallback and len(chosen) > budget:
        # Zero motion is a subset of any valid bounded motion permission.
        # Never choose an arbitrary first N tracks or inherit previous motion.
        child=dict(task)
        for key in ('modification','requested_modification_nets','partial_segments'):
            child.pop(key,None)
        reduction=dict(schema_version=1,reason='net_selection_exceeds_track_limit',
            requested_nets=list(nets),requested_mode=mode,requested_max_modified_tracks=budget,
            eligible_track_count=len(chosen),effective_modification=contract(child),
            policy='Freeze every original routing object; no subset selection, extra attempts or permission expansion')
        child['permission_reduction']=reduction
        result=witness(snapshot,child)
        task.clear();task.update(child)
        return dict(result,permission_reduction=reduction)
    child=dict(task,requested_modification_nets=list(nets))
    child["modification"] = dict(schema_version=1, mode=mode, nets=active_nets,
                                routing_uuids=chosen, max_modified_tracks=budget)
    result=witness(snapshot,child)
    task.clear();task.update(child)
    return result
