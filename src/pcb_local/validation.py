#!/usr/bin/env python3
"""Strict local-routing acceptance gate; standard library plus a KiCad worker.

Exit 0 = accepted, 1 = rejected, 2 = incomplete verification. Source boards and
sidecars are never written. Each --out-dir must be new. This gate allows only
straight segments, additive three-point arcs with a whole-circle scope proof,
and explicitly authorized through vias inside the declared
region/layers. Original routing is immutable unless an explicit, independently
witnessed bounded replacement permission is provided. Other definitions remain unchanged. Full-board DRC uses authored rules, without severity or
clearance overrides. Existing findings are matched strictly, not by counts.
"""
import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from types import SimpleNamespace

try:
    from .task_policy import permissions
    from .modification import contract, witness
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pcb_local.task_policy import permissions
    from pcb_local.modification import contract, witness

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_WORKER = Path(__file__).with_name("native_snapshot.py")
CLI_DEFAULT = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
PYTHON_DEFAULT = "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9"
SIDECARS = (".kicad_pro", ".kicad_dru", ".kicad_prl", ".design-brief.json")
ROUTING = {"segment", "via", "arc"}
META = {"version", "generator", "generator_version"}
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
TEARDROP_KEYS = {"best_length_ratio", "max_length", "best_width_ratio", "max_width",
                 "curved_edges", "filter_ratio", "enabled", "allow_two_segments",
                 "prefer_zone_connections"}

# KiCad 10.0.4 drc_engine.cpp:61-62, 907-924. --all-track-errors does
# not remove per-error-code limits. At a limit we cannot prove completeness.
SUPPORTED_KICAD = "10.0.4"
DRC_EXTENDED_LIMIT_TYPES = {"clearance", "unconnected_items"}
# All 27 DRC_REGISTER_TEST_PROVIDER registrations in KiCad 10.0.4's
# pcbnew/CMakeLists.txt:297-322. Names come from each provider's GetName().
# A successful-return trace is emitted only after RunTests returns true.
DRC_EXPECTED_PROVIDERS = frozenset({
    "annular_width", "disallow", "creepage", "connectivity", "copper width",
    "clearance", "physical_clearance", "courtyard_clearance", "edge_clearance",
    "footprint checks", "hole_to_hole_clearance", "hole_size", "library_parity",
    "schematic_parity", "miscellaneous", "text_dimensions", "angle", "width",
    "segment_length", "zone connections", "diameter", "solder_mask_issues",
    "silk_clearance", "length", "diff_pair_coupling", "sliver checker", "text_mirroring",
})
CRITICAL_DRC_CHECKS = {
    # Actual report/settings keys in KiCad 10.0.4 drc_item.cpp. Constraint
    # names such as physical_hole_clearance are not DRC keys: those providers
    # report clearance/hole_clearance. New copper can approach existing holes
    # and alter filled-zone connectivity without creating a new drill.
    "clearance", "hole_clearance", "shorting_items", "copper_edge_clearance",
    "creepage", "track_width", "track_angle", "track_segment_length",
    "track_dangling", "via_dangling", "unconnected_items", "isolated_copper",
    "invalid_outline", "zones_intersect", "tracks_crossing", "items_not_allowed",
    "connection_width", "starved_thermal", "copper_sliver", "item_on_disabled_layer",
    "track_on_post_machined_layer", "track_not_centered_on_via",
    "solder_mask_bridge", "silk_over_copper",
    "annular_width", "drill_out_of_range", "via_diameter", "hole_to_hole",
    # Search supports ordinary signals only, but may still affect existing
    # nets or expose authored connection/path constraints; never hide these.
    "length_out_of_range", "skew_out_of_range", "too_many_vias",
    "diff_pair_gap_out_of_range", "diff_pair_uncoupled_length_too_long",
    "missing_tuning_profile", "tuning_profile_track_geometries",
}


class VerificationIncomplete(RuntimeError):
    """Native evidence is insufficient; never turn this into a passed gate."""


def semantic_string(value):
    """Decode KiCad quoted escapes for names, without weakening AST equality."""
    if not isinstance(value, Quoted):
        return str(value)
    # KiCad escaping removes the backslash for an escaped character. Preserve
    # actual string contents in canonical() independently of this name lookup.
    return re.sub(r"\\(.)", lambda match: match.group(1), str(value), flags=re.S)


class Quoted(str):
    pass


def parse_sexpr(text, repairs=None):
    """Balanced, string-aware KiCad parser; rejects truncation and bad tokens."""
    roots, stack, pos = [], [], 0
    while pos < len(text):
        ch = text[pos]
        if ch.isspace():
            pos += 1
            continue
        if ch == "#":  # KiCad custom-rule comments
            end = text.find("\n", pos)
            pos = len(text) if end < 0 else end + 1
            continue
        if ch == "(":
            node = []
            (stack[-1] if stack else roots).append(node)
            stack.append(node)
            pos += 1
        elif ch == ")":
            if not stack:
                raise ValueError("Unmatched closing parenthesis")
            stack.pop()
            pos += 1
        elif ch == '"':
            start = pos
            pos += 1
            while pos < len(text):
                if text[pos] == "\\":
                    pos += 2
                elif text[pos] == '"':
                    pos += 1
                    break
                else:
                    pos += 1
            else:
                raise ValueError("Unterminated quoted string")
            # Keep escapes verbatim: equivalence never erases string content.
            (stack[-1] if stack else roots).append(Quoted(text[start + 1:pos - 1]))
        else:
            start = pos
            while pos < len(text) and not text[pos].isspace() and text[pos] not in '()"':
                pos += 1
            if pos == start:
                raise ValueError("Invalid token")
            token = text[start:pos]
            # KiCad's parseTEARDROP_PARAMETERS permits a known child keyword
            # without an opening parenthesis. Mirror that narrow read syntax,
            # also recognized by KRT repair_bare_teardrop_tokens (#1149).
            # This changes the in-memory tree only; source bytes stay frozen.
            if stack and name(stack[-1]) in {"teardrop", "teardrops"} and token in TEARDROP_KEYS:
                child = []
                stack[-1].append(child)
                stack.append(child)
                if repairs is not None:
                    repairs.append(start)
            (stack[-1] if stack else roots).append(token)
    if stack:
        raise ValueError("Unclosed S-expression")
    return roots


def name(node):
    return str(node[0]) if isinstance(node, list) and node else ""


def field(node, key):
    return next((x for x in node[1:] if isinstance(x, list) and name(x) == key), None)


def canonical(node):
    if isinstance(node, list):
        # Cached zone fill is regenerated under unchanged zone definitions.
        values = [x for x in node if not (name(node) == "zone" and
                  isinstance(x, list) and name(x) in {"filled_polygon", "fill_segments"})]
        return tuple(canonical(x) for x in values)
    if isinstance(node, Quoted):
        return ("string", str(node))
    if NUMBER.fullmatch(node):
        value = Decimal(node)
        return ("number", str(value.normalize()) if value else "0")
    return ("atom", node)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sources(path):
    return [path] + [path.with_suffix(ext) for ext in SIDECARS if path.with_suffix(ext).is_file()]


def source_hashes(path):
    return {str(p): digest(p) for p in sources(path)}


def rule_support(board):
    """Enumerate the complete authored DRU, not only KRT parser notes.

    v0.1 understands global/direct-layer minimum clearance rules. Everything
    else remains explicit unsupported until a search implementation and test
    exist. Native rule compilation is witnessed separately by the worker.
    """
    board = Path(board)
    result = {"supported": True, "reasons": [], "rules": [],
              "native_compilation_required": board.with_suffix(".kicad_dru").is_file(),
              "supported_dru_subset": "minimum clearance; global or direct layer clause; no object conditions",
              "critical_ignored_checks": [], "strict_supplement_required": False}
    pro = board.with_suffix(".kicad_pro")
    if not pro.is_file():
        result["reasons"].append("Matching .kicad_pro is required to preserve authored rules")
    else:
        try:
            project = json.loads(pro.read_text(encoding="utf-8"))
            severities = project.get("board", {}).get("design_settings", {}).get("rule_severities", {})
            result["critical_ignored_checks"] = sorted(key for key, value in severities.items()
                if key in CRITICAL_DRC_CHECKS and str(value).lower() == "ignore")
            result["strict_supplement_required"] = bool(result["critical_ignored_checks"])
        except (ValueError, TypeError, AttributeError) as exc:
            result["reasons"].append("Invalid project rule settings: " + str(exc))
    dru = board.with_suffix(".kicad_dru")
    if dru.is_file():
        try:
            nodes = parse_sexpr(dru.read_text(encoding="utf-8"))
            versions = [node for node in nodes if name(node) == "version"]
            if len(versions) != 1 or versions[0] != ["version", "1"]:
                result["reasons"].append("Custom rules require exactly one supported (version 1)")
            for node in nodes:
                if name(node) == "version":
                    continue
                if name(node) != "rule" or len(node) < 3 or not isinstance(node[1], Quoted):
                    result["reasons"].append("Unknown/malformed custom-rule top-level expression: " + name(node))
                    continue
                row = {"name": semantic_string(node[1]), "supported": True, "reasons": []}
                seen, constraints = Counter(), []
                for entry in node[2:]:
                    kind = name(entry)
                    seen[kind] += 1
                    if kind == "constraint":
                        constraints.append(entry)
                        if len(entry) != 3 or entry[1] != "clearance":
                            row["reasons"].append("Unsupported constraint: " + str(entry[1] if len(entry) > 1 else "missing"))
                            continue
                        bound = entry[2]
                        if not isinstance(bound, list) or len(bound) != 2 or bound[0] != "min":
                            row["reasons"].append("Only a single minimum clearance bound is supported")
                        elif not re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)(?:mm|mil|in)", str(bound[1])):
                            row["reasons"].append("Unsupported clearance value/unit: " + str(bound[1]))
                    elif kind == "layer":
                        if len(entry) != 2 or not re.fullmatch(r"(?:F\.Cu|B\.Cu|In[1-9]\d?\.Cu|outer|inner)", str(entry[1])):
                            row["reasons"].append("Unsupported layer clause")
                        elif isinstance(entry[1], Quoted) and str(entry[1]) in {"inner", "outer"}:
                            # Native parseLayer distinguishes keyword tokens from
                            # quoted layer names; KRT's parser otherwise merges them.
                            row["reasons"].append("inner/outer must be unquoted KiCad layer keywords")
                    elif kind == "condition":
                        expression = semantic_string(entry[1]) if len(entry) == 2 else ""
                        row["reasons"].append("Object conditions are not modeled exactly by KRT's layer map: " + expression)
                    elif kind == "severity":
                        if len(entry) != 2 or entry[1] not in {"error", "warning"}:
                            row["reasons"].append("Unsupported/ignored physical-rule severity")
                    else:
                        row["reasons"].append("Unknown rule field: " + (kind or repr(entry)))
                if not constraints:
                    row["reasons"].append("Rule contains no supported constraint")
                if any(count > 1 for key, count in seen.items()):
                    row["reasons"].append("Duplicate rule fields require unsupported precedence handling")
                row["supported"] = not row["reasons"]
                result["rules"].append(row)
                result["reasons"].extend("Rule " + row["name"] + ": " + reason for reason in row["reasons"])
        except (ValueError, TypeError, IndexError) as exc:
            result["reasons"].append("Custom-rule syntax could not be validated: " + str(exc))
    result["supported"] = not result["reasons"]
    return result


def _drc_diagnostics(stdout, stderr, *, allow_unmasked_trace=False):
    """Conservatively reject reported early stops, even with exit 0 + JSON.

    KiCad 10.0.4 RunTests returns void: a failed cache run returns early and a
    failed provider breaks its loop. The CLI can still write a report. Logs
    are an additional negative witness. Separate successful-return traces
    provide the positive witness for all pinned providers.
    Unknown stderr is deliberately not treated as a harmless warning.
    """
    failure = re.compile(
        r"\b(?:fatal|errors?|failed|failure|abort(?:ed|ing)?|cancel(?:led|ed|ling|ing)?|"
        r"interrupted|exception|assert(?:ion)?|traceback|segmentation\s+fault|"
        r"timed?\s*out|timeout|incomplete|crash(?:ed)?|terminat(?:ed|ing))\b|"
        r"\b(?:could\s+not|cannot|unable\s+to|not\s+completed?|stopped\s+early)\b|"
        r"错误|錯誤|失败|失敗|中止|取消|异常|異常|未完成|已终止|已終止|超时|逾時",
        re.IGNORECASE,
    )
    # These exact diagnostic classes occur on the supported macOS install.
    # Do not whitelist arbitrary Fontconfig warnings or all font errors.
    fontconfig = re.compile(
        r'Fontconfig warning: "[^"\n]+", line \d+: '
        r"(?:invalid attribute '[^'\n]+'|invalid constant used\s*:\s*[^\n]*)\Z"
    )
    font_fallback = re.compile(
        r"(?:.*?:\s*)?(?:找不到字体 '[^'\n]*'; 用 '[^'\n]*' 代替。|"
        r"Font '[^'\n]*' not found; substituting '[^'\n]*'\.?|"
        r"Could not find font '[^'\n]*'; substituting '[^'\n]*'\.?)\Z"
    )
    saved_report = re.compile(r"(?:Saved DRC Report to |保存 DRC 报告到 )[^\n]+\Z")
    duplicate_image = re.compile(r".*?Debug: Adding duplicate image handler for '[^'\n]+'\Z")
    mask = r"(?:\(KICAD_DRC_PROFILE\) )?" if allow_unmasked_trace else r"\(KICAD_DRC_PROFILE\) "
    trace = re.compile(r".*?Trace: " + mask + r"DRC "
                       r"(?:provider '([^'\n]+)' )?took \d+(?:\.\d+)? ms\Z")
    findings, benign, trace_events = [], 0, []
    for stream, text in (("stdout", stdout), ("stderr", stderr)):
        for number, raw_line in enumerate((text or "").splitlines(), 1):
            line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw_line).strip()
            if not line:
                continue
            if fontconfig.fullmatch(line) or font_fallback.fullmatch(line):
                benign += 1
                continue
            if duplicate_image.fullmatch(line):
                continue
            matched_trace = trace.fullmatch(line)
            if stream == "stderr" and matched_trace:
                trace_events.append({"provider": matched_trace.group(1), "line": number,
                                     "text": raw_line})
                continue
            # Output paths may contain words such as "error" or "timeout".
            if stream == "stdout" and saved_report.fullmatch(line):
                continue
            marker = failure.search(line)
            if marker or stream == "stderr":
                findings.append({"stream": stream, "line": number, "text": raw_line,
                                 "reason": "failure marker: " + marker.group(0) if marker
                                 else "unrecognized stderr diagnostic"})
    return {"checked": stdout is not None and stderr is not None,
            "findings": findings, "benign_font_diagnostic_count": benign,
            "trace_events": trace_events,
            "limitation": "Ordinary CLI output alone cannot witness silent provider early returns; "
                          "the acceptance path additionally requires every pinned successful-return trace"}


def drc_coverage(report, *, stdout=None, stderr=None, require_provider_trace=False,
                 covered_ignored_checks=(), capacity_evidence=None):
    """Reject absent severities, critical ignores, truncation and bad logs.

    Supplying both log streams enables diagnostic checks; measure always does
    so. JSON-only callers retain their prior API and get checked=false.
    """
    reasons = []
    if report.get("kicad_version") != SUPPORTED_KICAD:
        reasons.append("DRC completeness adapter is not verified for KiCad " + str(report.get("kicad_version")))
    if set(report.get("included_severities", [])) != {"error", "warning", "exclusion"}:
        reasons.append("DRC did not include error, warning and exclusion severities")
    ignored = [row.get("key") for row in report.get("ignored_checks", []) if isinstance(row, dict)]
    critical = sorted(set(ignored) & CRITICAL_DRC_CHECKS)
    covered = sorted(set(critical) & set(covered_ignored_checks))
    uncovered = sorted(set(critical) - set(covered))
    reasons.extend("Required physical check was ignored without complete supplemental validation: " + key for key in uncovered)
    counts = Counter()
    for section in ("violations", "unconnected_items", "schematic_parity"):
        rows = report.get(section, [])
        if not isinstance(rows, list):
            reasons.append("Invalid DRC section: " + section)
            continue
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("type"), str):
                reasons.append("DRC finding has no interpretable type")
            else:
                counts[row["type"]] += 1
    limits = {key: 499 if key in DRC_EXTENDED_LIMIT_TYPES else 199 for key in counts}
    if capacity_evidence is not None:
        from pcb_local.drc_complete import REPORT_CAPACITY, SOURCE_COMMIT, PATCH_SHA256
        if (capacity_evidence.get('per_type')!=REPORT_CAPACITY or capacity_evidence.get('extended')!=REPORT_CAPACITY
                or capacity_evidence.get('source_commit')!=SOURCE_COMMIT or capacity_evidence.get('patch_sha256')!=PATCH_SHA256
                or not re.fullmatch('[0-9a-f]{64}',capacity_evidence.get('manifest_sha256',''))):
            raise VerificationIncomplete('Invalid bound native DRC capacity evidence')
        limits={key:REPORT_CAPACITY for key in counts}
    saturated = {key: {"reported": count, "limit": limits[key]}
                 for key, count in counts.items() if count >= limits[key]}
    reasons.extend("DRC type reached its completeness boundary: " + key for key in saturated)
    diagnostics = _drc_diagnostics(stdout, stderr, allow_unmasked_trace=capacity_evidence is not None)
    if (stdout is None) != (stderr is None):
        reasons.append("Both DRC stdout and stderr are required for diagnostic checks")
    if diagnostics["findings"]:
        reasons.append("DRC emitted abnormal or unrecognized diagnostics; full run is not witnessed")
    events = diagnostics["trace_events"]
    provider_counts = Counter(row["provider"] for row in events if row["provider"] is not None)
    completions = [row for row in events if row["provider"] is None]
    missing = sorted(DRC_EXPECTED_PROVIDERS - provider_counts.keys())
    unexpected = sorted(provider_counts.keys() - DRC_EXPECTED_PROVIDERS)
    duplicates = sorted(key for key, value in provider_counts.items() if value != 1)
    complete_last = len(completions) == 1 and bool(events) and events[-1] is completions[0]
    trace_verified = not (missing or unexpected or duplicates) and complete_last
    if require_provider_trace and not trace_verified:
        reasons.append("Native successful-return traces do not witness exactly all 27 KiCad 10.0.4 DRC providers and final completion")
    provider_trace = {"required": require_provider_trace, "verified": trace_verified,
                      "expected": sorted(DRC_EXPECTED_PROVIDERS), "counts": dict(provider_counts),
                      "missing": missing, "unexpected": unexpected, "duplicates": duplicates,
                      "completion_count": len(completions), "completion_is_last": complete_last,
                      "scope": "Each registered provider returned true; authored ignored/inapplicable checks may still be skipped inside it"}
    return {"complete": not reasons, "reasons": reasons, "critical_ignored_checks": critical,
            "supplemented_ignored_checks": covered, "uncovered_critical_ignored_checks": uncovered,
            "type_counts": dict(counts), "saturated_types": saturated,
            "diagnostics": diagnostics, "provider_trace": provider_trace,
            "capacity_evidence": capacity_evidence,
            "limit_policy": "Identity-bound native capacity; equality remains incomplete" if capacity_evidence else "KiCad 10.0.4 per-type 199; clearance and unconnected_items 499; equality is conservatively incomplete",
            "source": "https://github.com/KiCad/kicad-source-mirror/blob/10.0.4/pcbnew/drc/drc_engine.cpp#L907"}


def has_zones(path):
    """Detect authored or footprint zones even when their fill cache is empty."""
    def visit(node):
        return isinstance(node,list) and (name(node)=='zone' or any(visit(x) for x in node if isinstance(x,list)))
    return any(visit(node) for node in parse_sexpr(Path(path).read_text(encoding='utf-8')))


def read_board(path):
    repairs = []
    roots = parse_sexpr(path.read_text(encoding="utf-8"), repairs=repairs)
    if len(roots) != 1 or name(roots[0]) != "kicad_pcb":
        raise ValueError("Expected one complete kicad_pcb document")
    root = roots[0]
    net_names = {str(x[1]): semantic_string(x[2]) for x in root[1:]
                 if name(x) == "net" and len(x) >= 3}
    net_names.setdefault("0", "")
    routing, identities, static = {}, {}, Counter()
    missing_field_uuids = []

    def identity(node, footprint=None):
        uuid = field(node, "uuid") or field(node, "tstamp")
        if not uuid or len(uuid) < 2:
            raise ValueError("Missing stable UUID on " + name(node))
        stored_uuid = str(uuid[1])
        uid = stored_uuid
        if footprint is not None:
            fp_uuid = field(footprint, "uuid") or field(footprint, "tstamp")
            fp_at, at = field(footprint, "at"), field(node, "at")
            if not fp_uuid:
                raise ValueError("Pad lacks stable footprint identity")
            fp_at = fp_at or ["at", "0", "0"]
            at = at or ["at", "0", "0"]
            fx, fy, px, py = map(float, [fp_at[1], fp_at[2], at[1], at[2]])
            angle = math.radians(float(fp_at[3]) if len(fp_at) > 3 else 0)
            x = round((fx + px * math.cos(angle) + py * math.sin(angle)) * 1e6)
            y = round((fy - px * math.sin(angle) + py * math.cos(angle)) * 1e6)
            uid = "pad:{}:{}:{}:{}:{}".format(fp_uuid[1], stored_uuid, semantic_string(node[1]), x, y)
        if uid in identities:
            raise ValueError("Duplicate UUID: " + uid)
        net = field(node, "net")
        if net is None:
            net_name = ""
        elif isinstance(net[1], Quoted):
            net_name = semantic_string(net[1])
        else:
            net_name = net_names.get(str(net[1]))
            if net_name is None and len(net) > 2:
                net_name = semantic_string(net[2])
            if net_name is None:
                raise ValueError("Unresolved net for " + uid)
        identities[uid] = {"kind": name(node), "uuid": stored_uuid, "net": net_name}
        return uid

    for node in root[1:]:
        key = name(node)
        if not key:
            raise ValueError("Invalid board child")
        if key in ROUTING:
            routing[identity(node)] = node
        else:
            if key == "footprint":
                fp_uuid = field(node, "uuid") or field(node, "tstamp")
                for child in node[1:]:
                    if name(child) == "pad":
                        identity(child, node)
                    elif name(child) in {"property", "fp_text"} and len(child) > 2:
                        field_name = semantic_string(child[1])
                        field_name = {"reference": "Reference", "value": "Value"}.get(field_name, field_name)
                        if field_name in {"Reference", "Value"} and fp_uuid and not (field(child, "uuid") or field(child, "tstamp")):
                            missing_field_uuids.append((str(fp_uuid[1]), field_name))
            if key not in META:
                static[canonical(node)] += 1
    if not identities:
        raise ValueError("No identifiable conductive objects")
    def stored_ids(node):
        if not isinstance(node, list):
            return set()
        found = {str(node[1])} if name(node) in {"uuid", "tstamp"} and len(node) > 1 else set()
        for child in node:
            if isinstance(child, list):
                found.update(stored_ids(child))
        return found
    return {"routing": routing, "identities": identities, "static": static,
            "stored_uuids": stored_ids(root),
            "missing_field_uuids": missing_field_uuids,
            "bare_teardrop_children_read": len(repairs)}


def rule_content(board, suffix):
    path = board.with_suffix(suffix)
    if not path.exists():
        return None
    if suffix == ".kicad_pro":
        return json.loads(path.read_text(encoding="utf-8"))
    return canonical(parse_sexpr(path.read_text(encoding="utf-8")))


def protection(before, after, before_path, after_path, case):
    old, new = before["routing"], after["routing"]
    removed = sorted(set(old) - set(new))
    changed = sorted(uid for uid in set(old) & set(new)
                     if canonical(old[uid]) != canonical(new[uid]))
    added = sorted(set(new) - set(old))
    policy = permissions(case)
    modification = contract(case)
    authorized = set(modification["routing_uuids"])
    allowed_nets = {case["target_net"], *modification["nets"]}
    partial_errors=[];fixed_crossing_tails=set()
    if case.get('partial_segments'):
        from pcb_local.partial_protection import check as check_partial
        partial_errors=check_partial(before,after,case)
        if not partial_errors:
            fixed_crossing_tails={c['uuid'] for p in case['partial_segments'] if p.get('schema_version')==2
                                  and p['parent_uuid'] in removed for c in (p['children'][0],p['children'][-1])}
    invalid_modifications = []
    for uid in authorized:
        node = old.get(uid)
        locked = field(node, "locked") if node else None
        if (node is None or name(node) != "segment" or "locked" in node
                or (locked and (len(locked) == 1 or locked[1] != "no"))
                or before["identities"][uid]["net"] not in modification["nets"]):
            invalid_modifications.append(uid)
    scope_errors, new_length = [], 0.0
    new_vias = [uid for uid in added if name(new[uid]) == "via"]
    xmin, ymin, xmax, ymax = map(float, case["region_mm"])
    if not all(math.isfinite(x) for x in (xmin, ymin, xmax, ymax)) or xmin >= xmax or ymin >= ymax:
        raise ValueError("Invalid declared region")
    for uid in added + changed:
        node = new[uid]
        errors = []
        if name(node) == "via":
            if not policy["max_new_vias"]:
                errors.append("new vias not authorized")
            at, size, drill, layers = (field(node, x) for x in ("at", "size", "drill", "layers"))
            if any(x in node for x in ("blind", "micro")) or field(node, "padstack"):
                errors.append("only ordinary through vias permitted")
            if not all((at, size, drill, layers)) or len(at) != 3 or len(size) != 2 or len(drill) != 2:
                errors.append("malformed new via")
            else:
                x, y, diameter, hole = map(float, [at[1], at[2], size[1], drill[1]])
                if not all(math.isfinite(v) for v in (x, y, diameter, hole)) or not 0 < hole < diameter:
                    errors.append("invalid via dimensions")
                if list(map(str, layers[1:])) != ["F.Cu", "B.Cu"]:
                    errors.append("only full-board through-via span permitted")
                if (x - diameter / 2 < xmin - 1e-9 or x + diameter / 2 > xmax + 1e-9 or
                        y - diameter / 2 < ymin - 1e-9 or y + diameter / 2 > ymax + 1e-9):
                    errors.append("via copper envelope outside allowed region")
        elif name(node) == "arc":
            # Additive only: this never authorizes converting an original track.
            # Whole-circle copper containment is deliberately conservative and
            # uses exact rational arithmetic, not the three points' bounding box.
            from pcb_local.arc_geometry import contained
            keys = [name(x) for x in node[1:]]
            required = {"start": 3, "mid": 3, "end": 3, "width": 2, "layer": 2}
            if (uid not in added or modification["mode"] != "none" or
                    any(k not in {"start", "mid", "end", "width", "layer", "net", "uuid", "tstamp"} for k in keys) or
                    len(keys) != len(set(keys)) or
                    any(not field(node, k) or len(field(node, k)) != size for k, size in required.items())):
                errors.append("only ordinary additive three-point arcs permitted")
            else:
                try:
                    inside, geometry = contained(field(node, "start")[1:], field(node, "mid")[1:],
                        field(node, "end")[1:], field(node, "width")[1], case["region_mm"])
                    if not inside:
                        errors.append("arc whole-circle copper envelope outside allowed region")
                    if str(field(node, "layer")[1]) not in policy["route_layers"]:
                        errors.append("wrong layer")
                    new_length += geometry["length_mm"]
                except (ValueError, TypeError, ArithmeticError) as exc:
                    errors.append("invalid arc geometry: " + str(exc))
        elif name(node) != "segment":
            errors.append("only new straight segments and authorized through vias permitted")
        else:
            start, end, width, layer = (field(node, x) for x in ("start", "end", "width", "layer"))
            if not all((start, end, width, layer)):
                errors.append("malformed new segment")
            else:
                x1, y1, x2, y2, w = map(float, [start[1], start[2], end[1], end[2], width[1]])
                if not all(math.isfinite(x) for x in (x1, y1, x2, y2, w)) or w <= 0:
                    errors.append("invalid segment coordinates or width")
                if str(layer[1]) not in policy["route_layers"]:
                    errors.append("wrong layer")
                if uid not in fixed_crossing_tails and (min(x1, x2) - w / 2 < xmin - 1e-9 or max(x1, x2) + w / 2 > xmax + 1e-9 or
                        min(y1, y2) - w / 2 < ymin - 1e-9 or max(y1, y2) + w / 2 > ymax + 1e-9):
                    errors.append("copper envelope outside allowed region")
                new_length += math.hypot(x2 - x1, y2 - y1)
        if after["identities"][uid]["net"] not in allowed_nets:
            errors.append("wrong net")
        if uid in changed and after["identities"][uid]["net"] != before["identities"][uid]["net"]:
            errors.append("modified track changed net identity")
        if errors:
            scope_errors.append({"uuid": uid, "errors": errors})
    rules = {ext: rule_content(before_path, ext) == rule_content(after_path, ext)
             for ext in (".kicad_pro", ".kicad_dru")}
    static_removed = sum((before["static"] - after["static"]).values())
    static_added = sum((after["static"] - before["static"]).values())
    checks = {("unauthorized_routing_unchanged" if authorized else "existing_routing_unchanged"): not ((set(removed) | set(changed)) - authorized),
              "modification_permission_valid": not invalid_modifications,
              "modified_track_budget_respected": len(set(removed) | set(changed)) <= modification["max_modified_tracks"],
              "nonrouting_definitions_unchanged": static_removed == static_added == 0,
              "rules_unchanged": all(rules.values()),
              "project_rules_available": before_path.with_suffix(".kicad_pro").is_file(),
              "additions_within_permission": not scope_errors,
              "new_via_budget_respected": len(new_vias) <= policy["max_new_vias"]}
    if case.get('partial_segments'):
        checks['partial_fixed_tails_preserved']=not partial_errors
    return {"checks": checks, "partial_segment_errors":partial_errors, "removed_routing": removed, "changed_routing": changed,
            "added_routing": added, "scope_errors": scope_errors,
            "static_removed_count": static_removed, "static_added_count": static_added,
            "rule_files_equal": rules, "new_track_length_mm": new_length,
            "new_via_count": len(new_vias), "permissions": policy,
            "modification": modification, "invalid_modifications": invalid_modifications}


def run_command(command, folder, label, deadline, *, env_overrides=None):
    start = time.monotonic()
    timed_out = False
    try:
        remaining = deadline - start
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, 0)
        environment = dict(os.environ)
        environment.update(env_overrides or {})
        p = subprocess.run(command, capture_output=True, text=True, timeout=remaining, env=environment)
        code, stdout, stderr = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out, code = True, 124
        stdout, stderr = exc.stdout or b"", exc.stderr or b""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
    row = {"command": command, "returncode": code, "timed_out": timed_out,
           "seconds": time.monotonic() - start, "env_overrides": env_overrides or {}}
    (folder / (label + ".stdout.log")).write_text(stdout)
    (folder / (label + ".stderr.log")).write_text(stderr)
    (folder / (label + ".command.json")).write_text(json.dumps(row, indent=2) + "\n")
    return row


def copy_library_context(board, stage, fallback):
    """Copy project tables and their KIPRJMOD resources; never copy rules from fallback."""
    copied = {}
    used_fallback = []
    missing = []
    for table in ("fp-lib-table", "sym-lib-table"):
        source = board.parent / table
        if not source.is_file() and (fallback.parent / table).is_file():
            source = fallback.parent / table
            used_fallback.append(table)
        if not source.is_file():
            continue
        shutil.copy2(source, stage / table)
        copied[table] = digest(source)
        text = source.read_text(encoding="utf-8")
        for relative in re.findall(r'\$\{KIPRJMOD\}(?:/([^"\n]*))?', text):
            relative_path = Path(relative)
            if not relative_path.parts or relative_path.is_absolute() or ".." in relative_path.parts:
                raise ValueError("Unsupported project-relative library path: " + relative)
            origin = source.parent / relative_path
            if not origin.exists():
                alternative = fallback.parent / relative_path
                if alternative.exists():
                    origin = alternative
                    used_fallback.append(relative)
                else:
                    # An absent upstream library is a source limitation, not
                    # a staging loss. Preserve the table and let full KiCad
                    # DRC report it under identical before/after context.
                    missing.append(relative)
                    continue
            destination = stage / relative_path
            resolved_origin, resolved_destination = origin.resolve(), destination.resolve()
            if resolved_destination == resolved_origin or resolved_origin in resolved_destination.parents:
                raise ValueError("Library staging would copy a resource into itself: " + str(origin))
            destination.parent.mkdir(parents=True, exist_ok=True)
            if origin.is_dir():
                shutil.copytree(origin, destination, dirs_exist_ok=True)
                for item in origin.rglob("*"):
                    if item.is_file():
                        copied[str(relative_path / item.relative_to(origin))] = digest(item)
            elif origin.is_file():
                shutil.copy2(origin, destination)
                copied[str(relative_path)] = digest(origin)
            else:
                raise ValueError("Unsupported project library object: " + str(origin))
    result = {"files_sha256": copied, "fallback_from_before": used_fallback,
              "missing_source_libraries": sorted(set(missing))}
    (stage / "library-context.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def _supplemental_drc(board, directory, args, checks):
    """Enable ignored checks only in an extra project; keep authored DRC too.

    Some severity keys activate implicit rules, so this is an independent
    gate whose findings are compared before/after. It never replaces the
    original authored-rule run or changes the source project's settings.
    """
    directory.mkdir()
    target = directory / "board.kicad_pcb"
    shutil.copy2(board, target)
    for suffix in SIDECARS:
        source = board.with_suffix(suffix)
        if source.is_file():
            shutil.copy2(source, target.with_suffix(suffix))
    context = copy_library_context(board, directory, args.before)
    original_project = rule_content(target, ".kicad_pro")
    if not isinstance(original_project, dict):
        raise VerificationIncomplete("Supplemental DRC requires the authored project settings")
    project = json.loads(json.dumps(original_project))
    severities = project.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rule_severities", {})
    changes = {key: {"before": severities.get(key, "KiCad default ignore"), "after": "error"}
               for key in sorted(checks)}
    severities.update({key: "error" for key in checks})
    target.with_suffix(".kicad_pro").write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n")
    expected_rules = {suffix: rule_content(target, suffix) for suffix in (".kicad_pro", ".kicad_dru")}
    command = [args.kicad_cli, "pcb", "drc", "--format", "json", "--severity-all",
               "--all-track-errors", "--refill-zones", "--save-board", "--output",
               str(directory / "drc.json"), str(target)]
    from pcb_local.drc_complete import load_runtime, environment, capacity_trace
    native_runtime=load_runtime(args.kicad_cli)
    run = run_command(command, directory, "drc", args.deadline,
        env_overrides={"KICAD_ENABLE_WXTRACE": "1", "WXTRACE": "KICAD_DRC_PROFILE", **environment(native_runtime)})
    if run["returncode"] not in (0, 5) or not (directory / "drc.json").is_file():
        raise VerificationIncomplete("Supplemental full DRC did not complete; see " + str(directory))
    report = json.loads((directory / "drc.json").read_text())
    if not all(isinstance(report.get(key), list) for key in ("violations", "unconnected_items")):
        raise VerificationIncomplete("Supplemental DRC JSON schema is incomplete")
    clean_stderr,capacity=capacity_trace((directory/'drc.stderr.log').read_text(encoding='utf-8',errors='replace'),native_runtime)
    if load_runtime(args.kicad_cli)!=native_runtime:raise VerificationIncomplete('Native DRC runtime changed during supplemental check')
    coverage = drc_coverage(report, capacity_evidence=capacity,
        stdout=(directory / "drc.stdout.log").read_text(encoding="utf-8", errors="replace"),
        stderr=clean_stderr,
        require_provider_trace=True)
    (directory / "drc-coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n")
    if any(expected_rules[key] != rule_content(target, key) for key in expected_rules):
        raise VerificationIncomplete("Supplemental DRC modified settings beyond the declared severity overrides")
    evidence = {"checks_enabled": sorted(checks), "severity_overrides": changes,
                "drc": report, "coverage": coverage, "drc_run": run,
                "refilled_board": str(target), "refilled_sha256": digest(target),
                "library_context": context,
                "policy": "Independent additional gate; original authored-rule DRC remains mandatory. "
                          "Only listed severity settings differ; no numeric constraints or user source files change."}
    (directory / "supplement.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    return evidence


def measure(board, label, args, raw):
    board = Path(board)
    stage = args.out_dir / label
    stage.mkdir()
    dst = stage / "board.kicad_pcb"
    shutil.copy2(board, dst)
    for suffix in SIDECARS:
        src = board.with_suffix(suffix)
        if src.is_file():
            shutil.copy2(src, dst.with_suffix(suffix))
    context = copy_library_context(board, stage, args.before)
    rules_before = {suffix: rule_content(dst, suffix) for suffix in (".kicad_pro", ".kicad_dru")}
    command = [args.kicad_cli, "pcb", "drc", "--format", "json", "--severity-all",
               "--all-track-errors", "--refill-zones", "--save-board", "--output",
               str(stage / "drc.json"), str(dst)]
    from pcb_local.drc_complete import load_runtime, environment, capacity_trace
    native_runtime=load_runtime(args.kicad_cli)
    drc_run = run_command(command, stage, "drc", args.deadline,
                          env_overrides={"KICAD_ENABLE_WXTRACE": "1", "WXTRACE": "KICAD_DRC_PROFILE", **environment(native_runtime)})
    if drc_run["returncode"] not in (0, 5) or not (stage / "drc.json").is_file():
        raise RuntimeError(label + ": KiCad full DRC did not complete; see command/log files")
    report = json.loads((stage / "drc.json").read_text())
    if not all(isinstance(report.get(key), list) for key in ("violations", "unconnected_items")):
        raise ValueError(label + ": unsupported/incomplete KiCad DRC JSON schema")
    stdout = (stage / "drc.stdout.log").read_text(encoding="utf-8", errors="replace")
    stderr = (stage / "drc.stderr.log").read_text(encoding="utf-8", errors="replace")
    stderr,capacity=capacity_trace(stderr,native_runtime)
    if load_runtime(args.kicad_cli)!=native_runtime:raise VerificationIncomplete('Native DRC runtime changed during check')
    coverage = drc_coverage(report, stdout=stdout, stderr=stderr, require_provider_trace=True,capacity_evidence=capacity)
    supplement = None
    if coverage["critical_ignored_checks"]:
        supplement = _supplemental_drc(dst, stage / "supplemental-drc", args, coverage["critical_ignored_checks"])
        if supplement["coverage"]["complete"]:
            coverage = drc_coverage(report, stdout=stdout, stderr=stderr, require_provider_trace=True,
                                    covered_ignored_checks=supplement["checks_enabled"],capacity_evidence=capacity)
        else:
            coverage["reasons"].append("Supplemental DRC is incomplete: " + repr(supplement["coverage"]["reasons"]))
    coverage["supplemental_drc_required"] = supplement is not None
    coverage["supplemental_drc_complete"] = supplement["coverage"]["complete"] if supplement else None
    (stage / "drc-coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n")
    if any(rules_before[ext] != rule_content(dst, ext) for ext in rules_before):
        raise RuntimeError(label + ": DRC/refill modified authored project rules")
    snapshot_command = [args.kicad_python, str(SNAPSHOT_WORKER), str(dst), str(stage / "connectivity.json")]
    if dst.with_suffix(".kicad_dru").is_file():
        snapshot_command += ["--rule-check-output", str(stage / "native-rule-check.rpt")]
    snapshot_run = run_command(snapshot_command, stage, "connectivity", args.deadline)
    if snapshot_run["returncode"] != 0 or not (stage / "connectivity.json").is_file():
        raise RuntimeError(label + ": native connectivity snapshot did not complete")
    snapshot = json.loads((stage / "connectivity.json").read_text())
    if dst.with_suffix(".kicad_dru").is_file() and not snapshot.get("rule_compilation", {}).get("successful"):
        raise VerificationIncomplete(label + ": custom-rule compilation was not witnessed")
    if snapshot.get("version") != report.get("kicad_version"):
        raise VerificationIncomplete(label + ": native worker and CLI versions differ")
    mismatched = [uid for uid, value in raw["identities"].items()
                  if uid not in snapshot["items"] or
                  any(snapshot["items"][uid][key] != value[key] for key in ("kind", "net"))]
    extras = sorted(set(snapshot["items"]) - set(raw["identities"]))
    if mismatched or extras:
        raise RuntimeError(label + ": refill/load changed routing or pad identity/net: "
                           + repr({"mismatch": mismatched[:20], "extra": extras[:20]}))
    absent_fields = set(raw.get("missing_field_uuids", []))
    generated_uuids = sorted(row["uuid"] for row in snapshot.get("reference_value_fields", [])
        if (row["footprint_uuid"], row["field_name"]) in absent_fields and row["uuid"] not in raw["stored_uuids"])
    return {"drc": report, "snapshot": snapshot, "drc_run": drc_run,
            "snapshot_run": snapshot_run, "refilled_sha256": digest(dst), "library_context": context,
            "coverage": coverage, "rule_support": rule_support(board),
            "supplemental_drc": supplement,
            "refilled_board": str(dst), "known_generated_field_uuids": generated_uuids}


def finding_key(finding, stored_uuids=None, generated_field_uuids=()):
    """Conservative exact pairing; description retains actual violation values."""
    value = dict(finding)
    if isinstance(value.get("items"), list):
        items = []
        for original in value["items"]:
            item = dict(original)
            if stored_uuids is not None and item.get("uuid") not in stored_uuids and item.get("uuid") in generated_field_uuids:
                # KiCad generates UUIDs for older reference/value fields that
                # have none in the source file. Only these absent UUIDs lose
                # identity weight; exact position, item description, rule,
                # severity and measured numbers remain in the strict key.
                item.pop("uuid", None)
                item["identity_basis"] = "source_missing_uuid_exact_position_and_description"
            items.append(item)
        value["items"] = sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def drc_delta(before, after, before_uuids=None, after_uuids=None,
              before_generated_field_uuids=(), after_generated_field_uuids=()):
    metadata_lists = {"ignored_checks", "included_severities"}
    sections = sorted({key for data in (before, after) for key, value in data.items()
                       if isinstance(value, list) and key not in metadata_lists | {"unconnected_items"}})
    result = {}
    for section in sections:
        old = Counter(finding_key(row, before_uuids, before_generated_field_uuids) for row in before.get(section, []))
        new = Counter(finding_key(row, after_uuids, after_generated_field_uuids) for row in after.get(section, []))
        result[section] = {"before_count": sum(old.values()), "after_count": sum(new.values()),
                           "new_or_changed": [json.loads(key) for key in (new - old).elements()],
                           "resolved": [json.loads(key) for key in (old - new).elements()]}
    same_coverage = all(before.get(key) == after.get(key) for key in metadata_lists)
    return {"no_new_or_changed_findings": same_coverage and all(not row["new_or_changed"] for row in result.values()),
            "sections": result,
            "same_drc_coverage": same_coverage,
            "ignored_checks": after.get("ignored_checks", []),
            "included_severities": after.get("included_severities", []),
            "unconnected_items_before": len(before["unconnected_items"]),
            "unconnected_items_after": len(after["unconnected_items"]),
            "unconnected_policy": "Preserved in raw DRC reports; acceptance uses native component inclusion and explicit target pads, not ratsnest edge matching."}


def connectivity_delta(before, after, case):
    if case.get("target_endpoints"):
        from pcb_local.endpoints import validate as validate_bound_endpoints
        validate_bound_endpoints(case,before)
    evidence = witness(before, case)
    selected = set(evidence["selected"])
    splits, missing = [], []
    for component in before["components"]:
        retained = set(component["items"]) - selected
        absent = [uid for uid in retained if uid not in after["component_of"]]
        missing.extend(absent)
        parts = {after["component_of"][uid] for uid in retained if uid in after["component_of"]}
        if len(parts) > 1:
            splits.append({"net": component["net"], "items": component["items"], "output_components": sorted(parts)})
    targets = case.get("target_item_ids")
    if targets is None:
        target_uuids = case["target_pad_uuids"]
        if len(target_uuids) < 2 or len(set(target_uuids)) != len(target_uuids):
            raise ValueError("Target must identify at least two distinct pad UUIDs")
        targets = []
        for uuid in target_uuids:
            matches = [uid for uid, row in after["items"].items() if row.get("uuid") == uuid and row["kind"] == "pad"]
            if len(matches) != 1:
                raise ValueError("Target pad UUID is missing or ambiguous: " + uuid)
            targets.append(matches[0])
    if len(targets) < 2 or len(set(targets)) != len(targets):
        raise ValueError("Target must identify at least two distinct original conductive items")
    valid = all(uid in before["items"] and uid in after["items"] and
                before["items"][uid]["net"] == case["target_net"] and after["items"][uid]["net"] == case["target_net"] for uid in targets)
    connected = valid and len({after["component_of"][uid] for uid in targets}) == 1
    target_components = {after["component_of"][uid] for uid in targets if uid in after["component_of"]}
    allowed_components = set(target_components)
    allowed_components.update(after["component_of"][uid] for uid in evidence["anchors"] if uid in after["component_of"])
    detached_additions = [uid for uid, row in after["items"].items()
                          if (uid not in before["items"] or uid in selected) and row["kind"] in {"segment", "via", "arc"}
                          and after["component_of"][uid] not in allowed_components]
    return {"existing_components_preserved": not (splits or missing),
            "target_connected": connected, "target_pad_identities_valid": valid,
            "added_copper_connected_to_target": not detached_additions,
            "detached_added_items": sorted(detached_additions),
            "target_item_ids": targets,
            "missing_items": missing, "split_components": splits,
            "before_component_count": len(before["components"]),
            "after_component_count": len(after["components"]),
            "modification_witness": evidence,
            "scope": "All unchanged original conductive identities and witnessed original electrical relations; explicitly authorized tracks may be replaced."}


def verify(before, after, task, out_dir, kicad_cli=CLI_DEFAULT,
           kicad_python=PYTHON_DEFAULT, timeout=120, *, original=None, geometry_python=None):
    """Verify an immutable candidate; task accepts the historical case keys.

    target_item_ids may replace target_pad_uuids for unambiguous native
    conductive identities. The output directory must not already exist.
    Return the persisted report; this function never exits the caller.
    """
    started = time.monotonic()
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Verification timeout must be positive and finite")
    args = SimpleNamespace(before=Path(before).resolve(), after=Path(after).resolve(),
        out_dir=Path(out_dir).resolve(), original=Path(original).resolve() if original else None,
        case_id=task.get("id", "local-task"), kicad_cli=str(kicad_cli), kicad_python=str(kicad_python),
        deadline=started + timeout)
    case = json.loads(json.dumps(task))
    args.out_dir.mkdir(parents=True, exist_ok=False)
    result = {"schema_version": 1, "case_id": args.case_id, "accepted": False,
              "verification_complete": False, "checks": {}, "errors": [],
              "verifier_sha256": digest(Path(__file__)),
              "snapshot_worker_sha256": digest(SNAPSHOT_WORKER),
              "support_module_sha256": {n: digest(Path(__file__).with_name(n)) for n in ("task_policy.py", "modification.py", "discovery.py", "endpoints.py", "drc_complete.py", "arc_geometry.py")},
              "source_hashes_before": {}, "limitations": [
                  "Full physical DRC under unchanged authored settings; rules explicitly ignored in the project remain ignored. Schematic parity is not requested.",
                  "All zone fills require exact native geometry equality except explicitly selected zone/layer changes inside the fixed ROI; no area tolerance is accepted.",
                  "Exact DRC matching can conservatively reject equivalent findings with different anchors/descriptions."]}
    code = 2
    try:
        result["case"] = case
        for path in (args.before, args.after, args.original):
            if path is not None:
                result["source_hashes_before"].update(source_hashes(path))
        old, new = read_board(args.before), read_board(args.after)
        result["read_compatibility"] = {"before_bare_teardrop_children": old["bare_teardrop_children_read"],
                                        "after_bare_teardrop_children": new["bare_teardrop_children_read"],
                                        "policy": "KiCad optional opening parenthesis for known direct teardrop children, interpreted in memory only."}
        result["protection"] = protection(old, new, args.before, args.after, case)
        result["checks"].update(result["protection"]["checks"])
        result["rule_support"] = {label: rule_support(path) for label, path in
                                   (("before", args.before), ("after", args.after))}
        result["checks"]["rules_supported"] = all(row["supported"] for row in result["rule_support"].values())
        if not result["checks"]["rules_supported"]:
            result["status"] = "unsupported"
            raise VerificationIncomplete("Rules outside supported search/verification contract: " +
                repr({key: value["reasons"] for key, value in result["rule_support"].items() if not value["supported"]}))
        needs_fill_witness=case.get('zone_refill') is not None or has_zones(args.before) or has_zones(args.after)
        if needs_fill_witness:
            from pcb_local.zone_refill import contract as refill_contract
            refill_contract(case)
            if not geometry_python:
                raise VerificationIncomplete('Boards with zones require the pinned geometry runtime for independent fill protection, even without refill permission')
        measured_before = measure(args.before, "before", args, old)
        measured_after = measure(args.after, "after", args, new)
        if needs_fill_witness:
            worker = Path(__file__).with_name('zone_fill_witness.py')
            result['support_module_sha256'].update({n: digest(Path(__file__).with_name(n)) for n in ('zone_fill_witness.py', 'zone_refill.py', 'geometry_search.py')})
            extracts = {}
            for label, measured in (('before', measured_before), ('after', measured_after)):
                output = args.out_dir / ('zone-fill-' + label + '.json')
                run = run_command([args.kicad_python, str(worker), 'extract', '--board', measured['refilled_board'],
                    '--sha256', measured['refilled_sha256'], '--output', str(output)], args.out_dir, 'zone-fill-' + label, args.deadline)
                if run['returncode'] or not output.is_file():
                    raise VerificationIncomplete('Native zone fill extraction failed: ' + label)
                extracts[label] = output
            case_path = args.out_dir / 'zone-refill-task.json'
            case_path.write_text(json.dumps(case))
            output = args.out_dir / 'zone-fill-witness.json'
            run = run_command([str(geometry_python), str(worker), 'compare', '--before', str(extracts['before']),
                '--after', str(extracts['after']), '--task', str(case_path), '--output', str(output)],
                args.out_dir, 'zone-fill-compare', args.deadline)
            witness_result = json.loads(output.read_text()) if output.is_file() else {}
            result['zone_refill'] = witness_result
            if run['returncode'] not in (0, 1) or witness_result.get('status') != 'verified':
                raise VerificationIncomplete('Zone fill scope witness incomplete: ' + str(witness_result.get('reason', witness_result)))
            if witness_result.get('source_sha256') != {'before': measured_before['refilled_sha256'], 'after': measured_after['refilled_sha256']}:
                raise VerificationIncomplete('Zone fill witness is not bound to the native measurements')
            if set(witness_result.get('checks', {})) != {'zone_refill_scope_preserved'}:
                raise VerificationIncomplete('Missing explicit zone fill scope check')
            result['checks'].update(witness_result['checks'])
        result["drc_coverage"] = {"before": measured_before["coverage"], "after": measured_after["coverage"]}
        result["checks"]["drc_coverage_complete"] = all(row["complete"] for row in result["drc_coverage"].values())
        result["drc"] = drc_delta(measured_before["drc"], measured_after["drc"], old["stored_uuids"], new["stored_uuids"],
            measured_before.get("known_generated_field_uuids", ()), measured_after.get("known_generated_field_uuids", ()))
        supplements = {key: row.get("supplemental_drc") for key, row in
                       (("before", measured_before), ("after", measured_after))}
        result["supplemental_drc"] = supplements
        result["checks"]["supplemental_drc_no_new_or_changed"] = not any(supplements.values())
        if all(supplements.values()):
            extra = drc_delta(supplements["before"]["drc"], supplements["after"]["drc"],
                old["stored_uuids"], new["stored_uuids"],
                measured_before.get("known_generated_field_uuids", ()),
                measured_after.get("known_generated_field_uuids", ()))
            result["supplemental_drc_delta"] = extra
            result["checks"]["supplemental_drc_no_new_or_changed"] = (
                supplements["before"]["checks_enabled"] == supplements["after"]["checks_enabled"]
                and extra["no_new_or_changed_findings"])
        result["connectivity"] = connectivity_delta(measured_before["snapshot"], measured_after["snapshot"], case)
        input_target = connectivity_delta(measured_before["snapshot"], measured_before["snapshot"], case)
        result["checks"].update({"full_drc_no_new_or_changed": result["drc"]["no_new_or_changed_findings"],
                                "existing_components_preserved": result["connectivity"]["existing_components_preserved"],
                                "added_copper_connected_to_target": result["connectivity"]["added_copper_connected_to_target"],
                                "input_target_disconnected": not input_target["target_connected"],
                                "library_context_unchanged": all(measured_before["library_context"][key] == measured_after["library_context"][key] for key in ("files_sha256", "missing_source_libraries")),
                                "target_connected": result["connectivity"]["target_connected"]})
        result["library_context"] = {"before": measured_before["library_context"], "after": measured_after["library_context"]}
        if measured_before["library_context"]["missing_source_libraries"]:
            result["limitations"].append("Unverified library-definition matching for missing upstream project libraries; embedded board geometry and full authored-rule DRC remain checked. See library_context.missing_source_libraries and raw DRC.")
        result["tool_versions"] = {"native_kicad": measured_after["snapshot"]["version"],
                                   "cli_report": measured_after["drc"].get("kicad_version")}
        result["artifacts"] = {"candidate": str(args.after),
            "refilled_candidate": measured_after["refilled_board"],
            "refilled_candidate_sha256": measured_after["refilled_sha256"]}
        result["checks"]["allowed_layer_exists"] = bool(permissions(case, measured_before["snapshot"]["copper_layers"]))
        if args.original is not None:
            control = measure(args.original, "original", args, read_board(args.original))
            result["original_control"] = {"target": connectivity_delta(control["snapshot"], control["snapshot"], case),
                                           "drc_violation_count": len(control["drc"]["violations"]),
                                           "unconnected_count": len(control["drc"]["unconnected_items"]),
                                           "library_context": control["library_context"]}
            result["checks"]["original_target_connected"] = result["original_control"]["target"]["target_connected"]
            result["drc_coverage"]["original"] = control["coverage"]
            result["checks"]["drc_coverage_complete"] &= control["coverage"]["complete"]
        if not result["checks"]["drc_coverage_complete"]:
            raise VerificationIncomplete("Full DRC evidence incomplete: " + repr({key: value["reasons"]
                for key, value in result["drc_coverage"].items() if not value["complete"]}))
        result["verification_complete"] = True
        code = 0 if all(result["checks"].values()) else 1
    except Exception as exc:
        result["errors"].append(type(exc).__name__ + ": " + str(exc))
    finally:
        after_hashes = {}
        for board in (args.before, args.after, args.original):
            if board is not None:
                for path in sources(board):
                    after_hashes[str(path)] = digest(path) if path.is_file() else None
        result["source_hashes_after"] = after_hashes
        intact = after_hashes == result["source_hashes_before"] and bool(after_hashes)
        result["checks"]["source_files_untouched"] = intact
        if not intact:
            result["errors"].append("Source file hashes changed or no input was measured")
            code = 2
        result["seconds"] = time.monotonic() - started
        result["total_timeout_seconds"] = timeout
        result["checks"]["verification_within_budget"] = result["seconds"] <= timeout
        if result["seconds"] > timeout:
            result["verification_complete"] = False
            result["errors"].append("Total validation budget exhausted")
            code = 2
        result["accepted"] = code == 0 and result["verification_complete"]
        result["exit_code"] = code
        result.setdefault("status", "accepted" if result["accepted"] else
                          "rejected" if result["verification_complete"] else "verification_incomplete")
        (args.out_dir / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main(argv=None):
    """Historical regression CLI, kept as a thin adapter to verify()."""
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("before", "after", "case-manifest", "out-dir"):
        parser.add_argument("--" + arg, type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--original", type=Path)
    parser.add_argument("--kicad-cli", default=os.environ.get("KICAD_CLI", CLI_DEFAULT))
    parser.add_argument("--kicad-python", default=os.environ.get("KICAD_PYTHON", PYTHON_DEFAULT))
    parser.add_argument("--geometry-python", help="Pinned runtime for explicit zone refill geometry witness")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args(argv)
    candidates = json.loads(args.case_manifest.read_text())["cases"]
    matches = [case for case in candidates if case["id"] == args.case_id]
    if len(matches) != 1:
        parser.error("Case ID must resolve exactly once")
    result = verify(args.before, args.after, matches[0], args.out_dir, args.kicad_cli,
                    args.kicad_python, args.timeout, original=args.original, geometry_python=args.geometry_python)
    print(json.dumps({"case": args.case_id, "accepted": result["accepted"], "complete": result["verification_complete"],
                          "failed_checks": [k for k, v in result["checks"].items() if not v],
                          "errors": result["errors"], "report": str(args.out_dir / "verification.json")}, ensure_ascii=False))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
