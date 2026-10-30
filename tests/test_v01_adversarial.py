# SPDX-License-Identifier: GPL-3.0-or-later
"""Adversarial orchestration controls; these are not native PCB acceptance tests.

Only the native/engine boundaries are mocked for the first group. The real
workflow, copies, hashes, final reporting and state transitions remain active.
The second group starts small Python processes, exercises the actual CLI
cancel/status commands, and cleans up every process it creates. No boards,
network, KiCad GUI, routing engine or dataset are needed.
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pcb_local import cli
from pcb_local.jobs import Halt, Job
from pcb_local.workspace import source_files
from pcb_local.discovery import discover_automatic


def native_pair():
    items = {str(i): {"kind": "pad", "uuid": "pad-" + str(i), "net": "DATA",
                     "reference": "U" + str(i), "number": "1", "position_mm": [float(i), 1.],
                     "copper_layers": ["F.Cu"]} for i in range(2)}
    return {"version": "10.0.4", "copper_layers": ["F.Cu"], "items": items,
            "components": [{"net": "DATA", "items": [str(i)], "pads": [str(i)]} for i in range(2)],
            "component_of": {str(i): i for i in range(2)}}


class FrozenEvidenceTests(unittest.TestCase):
    def exercise(self, mutation=None, automatic=False):
        """Return a real workflow result after one controlled boundary attack."""
        with tempfile.TemporaryDirectory(prefix="pcb-local-adversarial-") as temp:
            folder = Path(temp)
            source_dir = folder / "source"
            source_dir.mkdir()
            source = source_dir / "source.kicad_pcb"
            source.write_text('(kicad_pcb (version 20241229) (layers (0 "F.Cu" signal)) (net 1 "DATA") '
                '(footprint "x" (at 0 1) (uuid "fp-0") (pad "1" smd circle (at 0 0) (net 1 "DATA") (uuid "pad-0"))) '
                '(footprint "x" (at 1 1) (uuid "fp-1") (pad "1" smd circle (at 0 0) (net 1 "DATA") (uuid "pad-1"))))\n')
            source.with_suffix(".kicad_pro").write_text('{"board": {}}\n')
            original_hashes = source_files(source)
            task_dir = folder / "task"
            args = argparse.Namespace(command="repair", out_dir=task_dir,
                config=folder / "unused-config.json", board=source, roi=[-1, 0, 3, 2],
                layer="F.Cu", net="DATA", target=None, pads=None, profile="baseline",
                signal_kind="ordinary-single-ended")
            if automatic:
                listing = discover_automatic(native_pair(), "F.Cu")
                args.auto_region = True
                args.roi = None
                args.net = None
                args.target = listing["targets"][0]["id"]
                args.inspection = folder / "inspection.json"
                if mutation == "automatic_region":
                    listing["targets"][0]["region_mm"] = [-100, -100, 100, 100]
                args.inspection.write_text(json.dumps({"status": "complete", "source_board": str(source.resolve()),
                    "source_hashes": source_files(source.resolve()), "discovery": listing}))
            fingerprint = {"mock-router.py": "pinned-test-double"}
            config = {"source_fingerprint": fingerprint, "krt_root": str(folder),
                      "router_python": sys.executable, "kicad_python": sys.executable,
                      "kicad_cli": sys.executable, "verified_kicad_version": "10.0.4"}
            calls = []
            real_run = Job.run
            render = cli.report_text
            def renderer(report):
                if mutation == "report_error":
                    raise TypeError("injected report serialization failure")
                return render(report)

            def preflight(_job, _config, _board, report):
                report["preflight"] = {"complete": True, "measurement": {
                    "coverage": {"complete": True, "reasons": []}}}
                return native_pair()

            def run(job, label, _argv, **_kwargs):
                calls.append(label)
                before = job.folder / "input/board.kicad_pcb"
                candidate = job.folder / ("engine-output/board.kicad_pcb" if label == "routing" else "scoping/candidate/board.kicad_pcb")
                if label == "routing":
                    if mutation == "input_board":
                        before.write_text("(kicad_pcb (modified_reference yes))\n")
                    elif mutation == "input_rules":
                        before.with_suffix(".kicad_pro").write_text('{"board": {"relaxed": true}}\n')
                    shutil.copy2(before, candidate)
                    shutil.copy2(before.with_suffix(".kicad_pro"), candidate.with_suffix(".kicad_pro"))
                    if mutation not in ("input_board", "input_rules"):
                        text = candidate.read_text().rstrip()
                        candidate.write_text(text[:-1] + ' (segment (start 0 1) (end 1 1) (width 0.2) (layer "F.Cu") (net 1) (uuid "new-line")))\n')
                    return 0
                if label == "validation":
                    output = job.folder / "verification"
                    output.mkdir()
                    record = {"accepted": True, "verification_complete": True,
                              "source_hashes_after": {**source_files(before), **source_files(candidate)},
                              "checks": {"target_connected": True}, "errors": [],
                              "protection": {"checks": {"existing_routing_unchanged": True}},
                              "limitations": ["TEST DOUBLE: no physical verification was run"]}
                    (output / "verification.json").write_text(json.dumps(record))
                    if mutation == "candidate_board":
                        candidate.write_text("(kicad_pcb (unverified_copper yes))\n")
                    elif mutation == "candidate_rules":
                        candidate.with_suffix(".kicad_pro").write_text('{"board": {"relaxed": true}}\n')
                    elif mutation == "candidate_deleted":
                        candidate.unlink()
                    elif mutation == "candidate_context_invalid":
                        (candidate.parent / "fp-lib-table").write_text(
                            '(fp_lib_table (lib (uri "${KIPRJMOD}/../outside.pretty")))')
                    return 0
                # Actual bounded standard-library copy workers still run;
                # only routing and physical verification are test doubles.
                return real_run(job, label, _argv, **_kwargs)

            with mock.patch.object(cli, "load_config", return_value=config), \
                 mock.patch.object(cli, "runtime_fingerprint", return_value=fingerprint), \
                 mock.patch.object(cli, "preflight", side_effect=preflight), \
                 mock.patch("pcb_local.candidate_pipeline.engine_command", return_value=(["mock-engine"], {})), \
                 mock.patch.object(cli, "report_text", side_effect=renderer), \
                 mock.patch.object(Job, "run", new=run), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = cli.workflow(args)
            result = json.loads((task_dir / "result.json").read_text())
            state = json.loads((task_dir / "status.json").read_text())
            self.assertEqual(source_files(source), original_hashes, "An attack on copies reached the original")
            self.assertEqual(result["status"], state["status"], "Final result and task state disagree")
            return code, result, calls

    def test_control_can_accept_consistent_mocked_evidence(self):
        code, result, calls = self.exercise()
        self.assertEqual(code, 0, result.get("reason"))
        self.assertTrue(result["accepted"])
        self.assertEqual(calls[-4:], ["routing", "scope-candidate", "geometry-model", "validation"])
        self.assertEqual(result["geometry_fallback"]["status"], "unsupported")
        self.assertEqual(result["candidate_generator"], "krt")

    def test_automatic_task_uses_only_selected_endpoint_box(self):
        code, result, calls = self.exercise(automatic=True)
        self.assertEqual(code, 0, result.get("reason"))
        self.assertEqual(result["task"]["region_mm"], [-2., -1., 3., 3.])
        self.assertNotIn("region_mm", result["discovery"])
        self.assertIn("validation", calls)

    def test_modified_automatic_scope_is_rejected_before_routing(self):
        code, result, calls = self.exercise("automatic_region", automatic=True)
        self.assertEqual(code, 20)
        self.assertFalse(result["accepted"])
        self.assertNotIn("routing", calls)
        self.assertIn("region/target changed", result["reason"])

    def test_report_failure_cannot_leave_an_accepted_delivery(self):
        code, result, _ = self.exercise("report_error")
        self.assertEqual(code, 50)
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["accepted"])

    def test_report_handles_native_ignored_check_objects(self):
        text = cli.report_text({"status": "rejected", "task_dir": "test", "verification": {
            "drc": {"ignored_checks": [{"key": "track_not_centered_on_via", "description": "native object"}]},
            "checks": {}, "limitations": []}})
        self.assertIn("track_not_centered_on_via", text)

    def test_engine_cannot_rewrite_its_verification_baseline(self):
        for mutation in ("input_board", "input_rules"):
            with self.subTest(mutation=mutation):
                code, result, calls = self.exercise(mutation)
                self.assertEqual(code, 31)
                self.assertEqual(result["status"], "protection_violation")
                self.assertFalse(result["accepted"])
                self.assertNotIn("validation", calls, "Do not validate against the rewritten baseline")

    def test_post_verification_changes_cannot_be_rebranded_as_verified(self):
        for mutation in ("candidate_board", "candidate_rules", "candidate_deleted", "candidate_context_invalid"):
            with self.subTest(mutation=mutation):
                code, result, calls = self.exercise(mutation)
                self.assertIn("validation", calls)
                self.assertEqual(code, 31)
                self.assertEqual(result["status"], "protection_violation")
                self.assertFalse(result["accepted"])


GRANDCHILD = """
import json,os,signal,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp()}))
time.sleep(60)
"""


def parent_code(mode):
    return """
import os,subprocess,sys,time
from pathlib import Path
subprocess.Popen([sys.executable, '-c', %r, sys.argv[1]])
deadline=time.monotonic()+3
while not Path(sys.argv[1]).exists():
    if time.monotonic()>deadline: raise RuntimeError('grandchild did not start')
    time.sleep(.01)
%s
""" % (GRANDCHILD, "os._exit(0)" if mode == "exit" else "time.sleep(60)")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed orphan may remain briefly as a zombie until its new parent
    # reaps it. It cannot execute or write output and is not a live helper.
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=2)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


def await_condition(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.025)
    return bool(predicate())


def cleanup_group(pidfile):
    if pidfile.is_file():
        group = json.loads(pidfile.read_text())["pgid"]
        if group == os.getpgrp():
            raise AssertionError("Refusing to signal the test runner's process group")
        with contextlib.suppress(ProcessLookupError):
            os.killpg(group, signal.SIGKILL)


@unittest.skipUnless(os.name == "posix", "Process-group controls are currently POSIX-specific")
class ProcessTreeTests(unittest.TestCase):
    def test_timeout_kills_grandchild_that_ignores_term(self):
        with tempfile.TemporaryDirectory(prefix="pcb-local-tree-") as temp:
            folder = Path(temp)
            pidfile = folder / "grandchild.json"
            job = Job(folder / "task", timeout=10)
            started = time.monotonic()
            try:
                with self.assertRaises(Halt) as raised:
                    job.run("worker", [sys.executable, "-c", parent_code("wait"), str(pidfile)], timeout=.6)
                self.assertEqual(raised.exception.status, "timed_out")
                self.assertTrue(pidfile.is_file(), "The test must actually start a grandchild")
                pid = json.loads(pidfile.read_text())["pid"]
                self.assertTrue(await_condition(lambda: not alive(pid), 1))
                self.assertLess(time.monotonic() - started, 5)
            finally:
                cleanup_group(pidfile)
                job.finish("timed_out")

    def test_parent_exit_does_not_leave_a_background_helper(self):
        with tempfile.TemporaryDirectory(prefix="pcb-local-parent-exit-") as temp:
            folder = Path(temp)
            pidfile = folder / "grandchild.json"
            job = Job(folder / "task", timeout=10)
            try:
                code = job.run("worker", [sys.executable, "-c", parent_code("exit"), str(pidfile)], timeout=3)
                self.assertEqual(code, 0)
                pid = json.loads(pidfile.read_text())["pid"]
                self.assertTrue(await_condition(lambda: not alive(pid), 1), "A finished stage left its helper running")
            finally:
                cleanup_group(pidfile)
                job.finish("complete")

    def test_cli_cancel_running_tree_finishes_within_five_seconds_and_can_restart(self):
        with tempfile.TemporaryDirectory(prefix="pcb-local-cancel-") as temp:
            folder = Path(temp)
            task, pidfile = folder / "task", folder / "grandchild.json"
            env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
            supervisor = """
import sys
from pcb_local.jobs import Job,Halt
job=Job(sys.argv[1],timeout=20)
status='complete'
try:
    job.run('worker',[sys.executable,'-c',sys.argv[3],sys.argv[2]],timeout=15)
except Halt as exc:
    status=exc.status
finally:
    job.finish(status)
"""
            process = subprocess.Popen([sys.executable, "-B", "-c", supervisor,
                str(task), str(pidfile), parent_code("wait")], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                self.assertTrue(await_condition(pidfile.is_file, 3), "Running worker did not start")
                started = time.monotonic()
                cancelled = subprocess.run([sys.executable, "-B", "-m", "pcb_local", "cancel", str(task)],
                    env=env, capture_output=True, text=True, timeout=2)
                self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
                acknowledgement = json.loads(cancelled.stdout)
                self.assertTrue(acknowledgement.get("cancellation_requested"))
                process.wait(timeout=4)
                status = subprocess.run([sys.executable, "-B", "-m", "pcb_local", "status", str(task)],
                    env=env, capture_output=True, text=True, timeout=2)
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertEqual(json.loads(status.stdout)["status"], "cancelled")
                self.assertLess(time.monotonic() - started, 5)
                pid = json.loads(pidfile.read_text())["pid"]
                self.assertTrue(await_condition(lambda: not alive(pid), 1))
                second = folder / "second-task"
                restart = "from pcb_local.jobs import Job; import sys; j=Job(sys.argv[1]); j.run('ok',[sys.executable,'-c','pass']); j.finish('complete')"
                result = subprocess.run([sys.executable, "-B", "-c", restart, str(second)],
                    env=env, capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads((second / "status.json").read_text())["status"], "complete")
            finally:
                cleanup_group(pidfile)
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
