"""Safety regression mechanisms; no PCB dataset and no routing benchmarks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pcb_local.discovery import discover, select_task, validate_endpoints
from pcb_local.jobs import Job, Halt
from pcb_local.workspace import source_files, assert_unchanged


class DiscoveryTests(unittest.TestCase):
    def snapshot(self):
        items = {str(i): {"kind": "pad", "uuid": "pad-" + str(i), "net": "DATA",
                         "reference": "U" + str(i), "number": "1", "position_mm": [float(i), 1.],
                         "copper_layers": ["F.Cu"]} for i in range(4)}
        return {"copper_layers": ["F.Cu", "B.Cu"], "items": items,
                "components": [{"net": "DATA", "items": [str(i)], "pads": [str(i)]} for i in range(4)],
                "component_of": {str(i): i for i in range(4)}}

    def test_local_net_and_explicit_pair_have_distinct_scope(self):
        native = self.snapshot()
        listing = discover(native, [-.5, 0, 2.5, 2], "F.Cu")
        self.assertEqual(listing["supported_target_count"], 2)
        net = select_task(listing, net="DATA")
        pair = select_task(listing, target=listing["targets"][0]["id"])
        self.assertEqual(len(net["target_pad_uuids"]), 3)
        self.assertEqual(len(pair["target_pad_uuids"]), 2)
        self.assertTrue(validate_endpoints(pair, native))

    def test_outside_roi_endpoint_rejected(self):
        native = self.snapshot()
        listing = discover(native, [-.5, 0, 2.5, 2], "F.Cu")
        task = select_task(listing, net="DATA", pads=["pad-0", "pad-3"])
        with self.assertRaisesRegex(ValueError, "outside"):
            validate_endpoints(task, native)

    def test_ambiguous_pad_ids_are_not_offered(self):
        native = self.snapshot()
        native["items"]["0"]["uuid"] = native["items"]["1"]["uuid"]
        listing = discover(native, [-1, 0, 5, 2], "F.Cu")
        self.assertEqual(listing["supported_target_count"], 1)
        self.assertEqual(listing["nets"][0]["eligible_pad_count"], 2)


class SourceTests(unittest.TestCase):
    def test_new_rule_file_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            board = Path(temp) / "input.kicad_pcb"
            board.write_text("(kicad_pcb)")
            hashes = source_files(board)
            board.with_suffix(".kicad_dru").write_text("(version 1)")
            with self.assertRaisesRegex(ValueError, "changed"):
                assert_unchanged(board, hashes)

    def test_library_contents_are_frozen(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            board = folder / "input.kicad_pcb"
            board.write_text("(kicad_pcb)")
            (folder / "fp-lib-table").write_text('(fp_lib_table (lib (uri "${KIPRJMOD}/local.pretty")))')
            (folder / "local.pretty").mkdir()
            item = folder / "local.pretty/a.kicad_mod"
            item.write_text("before")
            hashes = source_files(board)
            item.write_text("after")
            with self.assertRaisesRegex(ValueError, "changed"):
                assert_unchanged(board, hashes)


class SupervisorTests(unittest.TestCase):
    def test_timeout_terminates_process_group_and_leaves_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            job = Job(Path(temp) / "task", timeout=10)
            start = time.monotonic()
            try:
                with self.assertRaises(Halt) as raised:
                    job.run("child", [sys.executable, "-c", "import time;time.sleep(30)"], timeout=.2)
                self.assertEqual(raised.exception.status, "timed_out")
            finally:
                job.finish("timed_out")
            state = json.loads((job.folder / "logs/child/command.json").read_text())
            self.assertEqual(state["exit_code"], 124)
            self.assertLess(time.monotonic() - start, 3)
            with self.assertRaises(ProcessLookupError):
                os.kill(state["pid"], 0)

    def test_cancellation_before_dispatch_starts_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            job = Job(Path(temp) / "task")
            (job.folder / "cancel.request").touch()
            try:
                with self.assertRaises(Halt) as raised:
                    job.run("never", [sys.executable, "-c", "raise SystemExit(0)"])
                self.assertEqual(raised.exception.status, "cancelled")
                self.assertFalse((job.folder / "logs/never").exists())
            finally:
                job.finish("cancelled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
