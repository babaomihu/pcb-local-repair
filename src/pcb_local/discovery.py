"""Discover supported local connections from native, refilled components."""
from .endpoints import catalog as endpoint_catalog
from collections import Counter
import hashlib
import math
from .signal_scope import net_restriction, restrictions, require_signal

AUTO_REGION_POLICY = {"name": "endpoint-box-v1", "margin_mm": 2.0, "max_side_mm": 25.0}


def discover_automatic(snapshot, layer, check=None):
    """Scan read-only across this layer; authorize only a selected endpoint box.

    The scan envelope is never a repair permission. Each offered target has a
    fixed 2 mm margin and a 25 mm side limit; no retry expands those limits.
    """
    points = [e['position_mm'] for e in endpoint_catalog(snapshot, layer, check=check).values()]
    if check:
        check()
    if not points:
        raise ValueError("No supported conductive endpoints exist on the requested copper layer")
    xs, ys = zip(*points)
    scan = [min(xs) - 2., min(ys) - 2., max(xs) + 2., max(ys) + 2.]
    listing = discover(snapshot, scan, layer, check=check)
    listing["mode"] = "automatic-regions"
    listing["automatic_region_policy"] = dict(AUTO_REGION_POLICY)
    listing["scan_region_mm"] = listing.pop("region_mm")
    for target in listing["targets"]:
        xs, ys = zip(*(pad["position_mm"] for pad in target["pads"]))
        margin = AUTO_REGION_POLICY["margin_mm"]
        box = region([min(xs) - margin, min(ys) - margin,
                      max(xs) + margin, max(ys) + margin])
        target["region_mm"] = box
        if max(box[2] - box[0], box[3] - box[1]) > AUTO_REGION_POLICY["max_side_mm"]:
            target.update(supported=False, reason="Automatic local region exceeds 25 mm per side; use an explicitly reviewed manual ROI")
    listing["supported_target_count"] = sum(t["supported"] for t in listing["targets"])
    listing["scope"] = "Read-only layer-wide discovery, at most 64 targets; repair requires one selected target and its fixed endpoint box. Scan bounds never authorize copper additions."
    return listing


def region(value):
    result = [float(x) for x in value]
    if (len(result) != 4 or not all(math.isfinite(x) for x in result)
            or result[0] >= result[2] or result[1] >= result[3]):
        raise ValueError("ROI must be finite xmin ymin xmax ymax in mm with positive area")
    return result


def discover(snapshot, roi, layer, check=None):
    roi = region(roi)
    layers = snapshot.get("copper_layers", [])
    if layer not in layers:
        raise ValueError("Requested copper layer not present: " + layer)
    items = snapshot["items"]
    terminals = endpoint_catalog(snapshot, layer, check=check)
    uuids = Counter(row["uuid"] for row in items.values())
    by_item = {}
    for token, endpoint in terminals.items():
        by_item.setdefault(endpoint["item_id"], []).append((token,endpoint))
    names = {row["net"] for row in items.values() if row["net"]}
    components_by_net={}
    for component in snapshot["components"]:
        if check:check()
        components_by_net.setdefault(component["net"],[]).append(component)
    scope_reasons = restrictions(snapshot)
    nets, candidates = [], []
    xmin, ymin, xmax, ymax = roi
    for net in sorted(names):
        if check:
            check()
        components = []
        for comp in components_by_net.get(net,[]):
            eligible = []
            members = set(comp.get('items', comp.get('pads', [])))
            choices = [pair for item in members for pair in by_item.get(item,[])]
            for key, pad in choices:
                if check:
                    check()
                x, y = pad['position_mm']
                if (uuids[pad['uuid']] == 1
                        and xmin <= x <= xmax and ymin <= y <= ymax):
                    eligible.append(key)
            if eligible:
                components.append(sorted(eligible))
        def representative(c):
            return next((k for k in c if terminals[k]["kind"]=="pad"),c[0])
        reason = scope_reasons.get(net)
        if sum(map(len, components)) > 512:
            reason = "more than 512 eligible pads exceeds the bounded v0.1 discovery scope; narrow the ROI"
        if len(components) > 65:
            reason = "more than 65 local disconnected pad components exceeds the bounded v0.1 discovery scope"
        if len(candidates) >= 64:
            reason = "64 local candidate connections already listed; narrow the ROI for more targets"
        row = {"net": net, "local_component_count": len(components),
               "supported": not reason, "reason": reason,
               "eligible_pad_count": sum(map(len, components)), "targets": []}
        # A minimum spanning tree offers a bounded set of concrete two-component
        # choices; users can also request all eligible components of one net.
        edges = []
        if reason:
            row["representative_pad_uuids"] = [terminals[representative(c)]["uuid"] for c in components]
            nets.append(row)
            continue
        for i, left in enumerate(components):
            if check:
                check()
            for j in range(i + 1, len(components)):
                best = None
                for a in left:
                    if check:
                        check()
                    for b in components[j]:
                        distance = (math.dist(terminals[a]["position_mm"], terminals[b]["position_mm"]), int(terminals[a]["kind"]!="pad")+int(terminals[b]["kind"]!="pad"), a, b)
                        if best is None or distance < best:
                            best = distance
                edges.append((*best, i, j))
        parent = list(range(len(components)))
        def find(i):
            while parent[i] != i:
                i = parent[i]
            return i
        for distance, _endpoint_rank, left, right, i, j in sorted(edges):
            if len(candidates) >= 64:
                row["truncated"]=True
                row["listing_limit_reason"]="64 target limit reached; this is not a complete connection inventory"
                break
            if find(i) == find(j):
                continue
            parent[find(j)] = find(i)
            ids = [terminals[left]["uuid"], terminals[right]["uuid"]]
            extended = any(terminals[k]["kind"] != "pad" for k in (left,right))
            identity_tokens = [left,right] if extended else ids
            identifier = hashlib.sha256((net + "\0" + "\0".join(sorted(identity_tokens))).encode()).hexdigest()[:16]
            target = {"id": identifier, "net": net, "pad_uuids": ids,
                      "distance_mm": distance, "supported": not reason, "reason": reason,
                      "pads": [{"uuid": terminals[k]["uuid"], "reference": terminals[k]["reference"],
                                "number": terminals[k]["number"], "position_mm": terminals[k]["position_mm"],
                                "netclass": terminals[k].get("netclass")} for k in (left, right)]}
            if extended:
                target["endpoints"] = [terminals[k] for k in (left,right)]
            row["targets"].append(identifier)
            candidates.append(target)
        row["representative_pad_uuids"] = [terminals[representative(c)]["uuid"] for c in components]
        row["has_nonpad_representatives"] = any(terminals[representative(c)]["kind"] != "pad" for c in components)
        nets.append(row)
    return {"region_mm": roi, "allowed_layer": layer, "nets": nets,
            "target_limit_reached": len(candidates)>=64, "targets": candidates, "supported_target_count": sum(t["supported"] for t in candidates),
            "scope": "ROI-local native components; pads, fixed straight ends and standard through-via centres; no ambiguous identities, differential or power routing",
            "signal_review": "Network names and authored effective classes exclude known power/differential/timing scope. Remaining candidates still require explicit ordinary-single-ended design-intent confirmation."}


def select_task(discovery, *, net=None, target=None, pads=None):
    automatic = discovery.get("mode") == "automatic-regions"
    if automatic and not target:
        raise ValueError("Automatic region requires one target ID from inspect --auto-region; whole-net repair needs manual ROI")
    if target:
        matches = [t for t in discovery["targets"] if t["id"] == target]
        if len(matches) != 1:
            raise ValueError("Target ID is absent in this board/ROI/layer; run inspect again")
        row = matches[0]
        if not row["supported"]:
            raise ValueError(row["reason"])
        net, pads = row["net"], row["pad_uuids"]
    elif pads:
        if not net:
            raise ValueError("Explicit --pads also requires --net")
        if len(pads) != 2 or len(set(pads)) != 2:
            raise ValueError("--pads requires two distinct pad UUIDs")
        matches = [t for t in discovery["nets"] if t["net"] == net]
        if len(matches) != 1 or not matches[0]["supported"]:
            raise ValueError("Selected net is absent or outside the supported signal scope")
    else:
        matches = [t for t in discovery["nets"] if t["net"] == net]
        if len(matches) != 1:
            raise ValueError("Exact net name is absent: " + str(net))
        row = matches[0]
        if not row["supported"]:
            raise ValueError(row["reason"])
        if row.get("has_nonpad_representatives"):
            raise ValueError("Choose a concrete target ID for line/via endpoints")
        pads = row["representative_pad_uuids"]
        if len(pads) < 2:
            return None
    result = {"id": target or "user-task", "target_net": net, "target_pad_uuids": pads,
            "allowed_layer": discovery["allowed_layer"],
            "region_mm": row["region_mm"] if automatic else discovery["region_mm"],
            "policy": "Connect the requested pair, or every eligible in-ROI pad component of the selected net; preserve all existing objects."}
    if target and row.get('endpoints'):
        result['target_endpoints'] = row['endpoints']
        result['target_item_ids'] = [e['item_id'] for e in row['endpoints']]
        result['target_pad_uuids'] = [e['uuid'] for e in row['endpoints'] if e['kind']=='pad']
    return result



def validate_endpoints(task, snapshot):
    require_signal(snapshot, task["target_net"])
    if task.get("target_endpoints"):
        from .endpoints import validate
        return validate(task, snapshot)
    xmin, ymin, xmax, ymax = task["region_mm"]
    components = set()
    for uuid in task["target_pad_uuids"]:
        matches = [(key, row) for key, row in snapshot["items"].items()
                   if row["kind"] == "pad" and row["uuid"] == uuid]
        if len(matches) != 1:
            raise ValueError("Target pad missing or ambiguous: " + uuid)
        key, pad = matches[0]
        x, y = pad["position_mm"]
        if (pad["net"] != task["target_net"] or task["allowed_layer"] not in pad["copper_layers"]
                or not (xmin <= x <= xmax and ymin <= y <= ymax)):
            raise ValueError("Target pad is outside declared net/ROI/layer: " + uuid)
        components.add(snapshot["component_of"][key])
    return len(components) > 1
