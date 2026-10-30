"""Scope projection mechanisms; native DRC remains a separate required gate."""
import copy
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pcb_local.scope_candidate import build_scoped_candidate
from pcb_local.validation import parse_sexpr, read_board, semantic_string


BASE = '''(kicad_pcb (version 20241229) (generator "scope-test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (net 0 "") (net 1 "N") (net 2 "OTHER")
  (footprint "Fixture" (at 0 0) (layer "F.Cu") (uuid "fp1")
    (property "Reference" "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd circle (at 1 1) (size 1 1) (layers "F.Cu") (net 1 "N") (uuid "pad1")))
  (segment (start 1 1) (end 2 1) (width 0.2) (layer "F.Cu") (net 1) (uuid "old"))
)'''
RULES = {"board": {"design_settings": {"rules": {"min_clearance": 0.2}}}}
TASK = {"target_net": "N", "allowed_layer": "F.Cu", "region_mm": [0, 0, 10, 10]}


def segment(uid, start=(2, 1), end=(5, 1), width=0.2, layer="F.Cu", net=1):
    return '(segment (start %s %s) (end %s %s) (width %s) (layer "%s") (net %s) (uuid "%s"))' % (
        *start, *end, width, layer, net, uid)


class ScopeCandidate(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.before = self.write_project("input", BASE)

    def tearDown(self):
        self.temporary.cleanup()

    def write_project(self, name, text, rules=RULES):
        folder = self.root / name
        folder.mkdir()
        board = folder / "example.kicad_pcb"
        board.write_text(text, encoding="utf-8")
        board.with_suffix(".kicad_pro").write_text(json.dumps(rules), encoding="utf-8")
        return board

    def run_scope(self, additions=(), *, text=None, rules=RULES):
        source = text if text is not None else self.before.read_text()
        raw = self.write_project("raw", source[:-1] + "\n" + "\n".join(additions) + "\n)", rules)
        before_bytes, raw_bytes = self.before.read_bytes(), raw.read_bytes()
        result = build_scoped_candidate(self.before, raw, TASK, self.root / "scoped")
        self.assertEqual(self.before.read_bytes(), before_bytes)
        self.assertEqual(raw.read_bytes(), raw_bytes)
        self.assertEqual(Path(result["raw_engine_board"]).read_bytes(), raw_bytes)
        self.assertTrue(result["source_files_untouched"])
        self.assertTrue(result["requires_native_verification"])
        self.assertNotIn("accepted", result)
        self.assertEqual(json.loads(Path(result["report_path"]).read_text())["status"], result["status"])
        return result, raw

    def test_local_and_remote_segments_keep_only_local(self):
        result, _ = self.run_scope([segment("local"), segment("remote", (20, 20), (25, 20))])
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["retained_added_segments"], ["local"])
        self.assertEqual([row["uuid"] for row in result["discarded_segments"]], ["remote"])
        self.assertEqual(set(read_board(Path(result["candidate_board"]))["routing"]), {"old", "local"})

    def test_crossing_segment_is_discarded_whole_not_clipped(self):
        result, _ = self.run_scope([segment("crossing", (5, 1), (15, 1))])
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["retained_added_segments"], [])
        self.assertEqual(set(read_board(Path(result["candidate_board"]))["routing"]), {"old"})

    def test_full_width_envelope_must_fit(self):
        result, _ = self.run_scope([segment("edge", (2, 0.05), (5, 0.05), width=0.2)])
        self.assertEqual(result["discarded_count"], 1)
        self.assertEqual(result["retained_added_count"], 0)

    def test_inside_without_discard_preserves_exact_raw_bytes(self):
        result, raw = self.run_scope([segment("local")])
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(Path(result["candidate_board"]).read_bytes(), raw.read_bytes())

    def test_original_route_change_cannot_be_hidden(self):
        changed = BASE.replace('(end 2 1)', '(end 2.5 1)')
        result, _ = self.run_scope([segment("remote", (20, 20), (25, 20))], text=changed)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("existing_routing_unchanged", result["blocking_checks"])
        self.assertIsNone(result["candidate_board"])
        self.assertFalse((self.root / "scoped/candidate").exists())

    def test_static_footprint_change_cannot_be_hidden(self):
        result, _ = self.run_scope(text=BASE.replace('"R1"', '"R2"'))
        self.assertEqual(result["status"], "rejected")
        self.assertIn("nonrouting_definitions_unchanged", result["blocking_checks"])

    def test_rule_change_cannot_be_hidden(self):
        changed = copy.deepcopy(RULES)
        changed["board"]["design_settings"]["rules"]["min_clearance"] = 0.1
        result, _ = self.run_scope([segment("local")], rules=changed)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("rules_unchanged", result["blocking_checks"])

    def test_custom_rule_change_cannot_be_hidden(self):
        self.before.with_suffix(".kicad_dru").write_text('(version 1)(rule "c" (constraint clearance (min 0.2mm)))')
        result, _ = self.run_scope([segment("local")])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("rules_unchanged", result["blocking_checks"])

    def test_wrong_net_outside_roi_is_rejected_not_discarded(self):
        result, _ = self.run_scope([segment("foreign", (20, 20), (25, 20), net=2)])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("wrong net", result["invalid_additions"][0]["errors"])
        self.assertEqual(result["discarded_segments"], [])

    def test_wrong_layer_outside_roi_is_rejected_not_discarded(self):
        result, _ = self.run_scope([segment("back", (20, 20), (25, 20), layer="B.Cu")])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("wrong layer", result["invalid_additions"][0]["errors"])

    def test_new_via_outside_roi_is_rejected_not_discarded(self):
        via = '(via (at 20 20) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via"))'
        result, _ = self.run_scope([via])
        self.assertEqual(result["status"], "rejected")
        self.assertIsNone(result["candidate_board"])

    def test_new_arc_is_rejected(self):
        arc = '(arc (start 2 1) (mid 3 2) (end 5 1) (width 0.2) (layer "F.Cu") (net 1) (uuid "arc"))'
        result, _ = self.run_scope([arc])
        self.assertEqual(result["status"], "rejected")

    def test_quoted_escapes_and_bare_teardrop_semantics_survive(self):
        escaped = r'quote\"path\\tail'
        text = BASE.replace('"R1"', '"' + escaped + '"')
        text = text.replace('(size 1 1)', '(size 1 1) (teardrops (enabled yes)filter_ratio 0.9))')
        self.before.write_text(text, encoding="utf-8")
        self.assertEqual(read_board(self.before)["bare_teardrop_children_read"], 1)
        result, raw = self.run_scope([segment("local"), segment("remote", (20, 20), (25, 20))])
        self.assertEqual(result["status"], "ready", result)
        candidate = Path(result["candidate_board"])
        self.assertIn('"' + escaped + '"', candidate.read_text())
        self.assertEqual(read_board(candidate)["bare_teardrop_children_read"], 0)
        self.assertEqual(read_board(raw)["static"], read_board(candidate)["static"])
        def reference_value(node):
            if isinstance(node, list):
                if len(node) >= 3 and node[0] == "property" and node[1] == "Reference":
                    return semantic_string(node[2])
                for child in node:
                    value = reference_value(child)
                    if value is not None:
                        return value
            return None
        self.assertEqual(reference_value(parse_sexpr(candidate.read_text())),
                         reference_value(parse_sexpr(raw.read_text())))

    def test_absolute_script_entrypoint_returns_ready_and_rejected(self):
        script = Path(__file__).resolve().parents[1] / "src/pcb_local/scope_candidate.py"
        task = self.root / "task.json"; task.write_text(json.dumps(TASK))
        for label, addition, expected_code, expected_status in (
                ("ready", segment("local"), 0, "ready"),
                ("rejected", segment("foreign", net=2), 1, "rejected")):
            with self.subTest(label=label):
                raw = self.write_project(label, BASE[:-1] + addition + ")")
                output = self.root / (label + "-out")
                run = subprocess.run([sys.executable, str(script), "--before", str(self.before),
                    "--raw-after", str(raw), "--task", str(task), "--out-dir", str(output)],
                    capture_output=True, text=True, cwd=self.root, timeout=5)
                self.assertEqual(run.returncode, expected_code, run.stderr)
                self.assertEqual(json.loads((output / "scope-report.json").read_text())["status"], expected_status)

    def test_existing_output_directory_is_never_overwritten(self):
        raw = self.write_project("raw", BASE)
        output = self.root / "existing"; output.mkdir()
        sentinel = output / "keep.txt"; sentinel.write_text("keep")
        with self.assertRaises(FileExistsError):
            build_scoped_candidate(self.before, raw, TASK, output)
        self.assertEqual(sentinel.read_text(), "keep")


if __name__ == "__main__":
    unittest.main(verbosity=2)
