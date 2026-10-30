#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Small launcher bridge checks; no KiCad imports, installation or GUI actions."""
import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("launcher_bridge", ROOT / "pcb_local_launcher/process.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class BridgeChecks(unittest.TestCase):
    def test_failed_task_explanation_is_visible_and_never_accepted(self):
        wx = types.SimpleNamespace(Frame=object)
        package = types.ModuleType('test_diagnostic_launcher'); package.process = bridge
        spec = importlib.util.spec_from_file_location('test_diagnostic_launcher.dialog', ROOT / 'pcb_local_launcher/dialog.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'wx': wx, 'test_diagnostic_launcher': package}):
            spec.loader.exec_module(module)
        frame = object.__new__(module.LauncherFrame)
        frame.task = dict(folder=self.root, kind='repair', process=Mock())
        frame.task['process'].poll.return_value = 30
        frame.status = Mock(); frame.cancel_requested = threading.Event()
        frame.enable_controls = Mock(); frame.maybe_close = Mock()
        diagnosis = dict(summary='候选违反原设计规则，已拒绝', suggestion='查看完整报告',
                         objects=[dict(uuid='actual-object-id', net='N', layer='In1.Cu')])
        (self.root / 'result.json').write_text(json.dumps(dict(status='rejected', accepted=False, diagnosis=diagnosis)))
        frame.finish(dict(status='rejected', accepted=False))
        visible = '\n'.join(c.args[0] for c in frame.status.AppendText.call_args_list)
        self.assertIn(diagnosis['summary'], visible)
        self.assertIn('actual-object-id', visible)
        self.assertIn('In1.Cu', visible)
        self.assertFalse(frame.task['accepted'])
        frame.enable_controls.assert_called_once()

    def test_exact_recommendations_and_queue_order_reach_cli(self):
        inspection=self.root/'inspect.json';inspection.write_text('{}')
        inspected={'modification_recommendations':[dict(id='proposal',target_id='one')]}
        with patch.object(bridge,'source_still_matches'), patch.object(bridge.subprocess,'Popen',return_value=Mock()):
            task=bridge.launch(self.config,'batch',self.selection,['one'],threading.Event(),inspected,inspection,None,['proposal'],'constrained-first')
            argv=task['argv']
            self.assertIn('--accept-recommendation',argv);self.assertIn('proposal',argv)
            self.assertIn('constrained-first',argv);self.assertIn('auto',argv)
            self.assertNotIn('--modify-nets',argv);self.assertNotIn('--shove-nets',argv)
            with self.assertRaises(ValueError):bridge.launch(self.config,'batch',self.selection,['other'],threading.Event(),inspected,inspection,None,['proposal'])

    def test_recommendation_modes_are_explicit(self):
        inspection=self.root/'inspect.json';inspection.write_text('{}')
        inspected={'modification_recommendations':[dict(id='proposal',target_id='one')]}
        with patch.object(bridge,'source_still_matches'), patch.object(bridge.subprocess,'Popen',return_value=Mock()):
            for mode in ('shove','ripup','shove+ripup'):
                task=bridge.launch(self.config,'repair',self.selection,('--target','one'),threading.Event(),inspected,inspection,
                                   recommendations=['proposal'],recommendation_mode=mode)
                argv=task['argv'];self.assertEqual(argv[argv.index('--recommendation-mode')+1],mode)
                self.assertNotIn('--modify-nets',argv)
            with self.assertRaises(ValueError):
                bridge.launch(self.config,'repair',self.selection,('--target','one'),threading.Event(),inspected,inspection,
                              recommendations=['proposal'],recommendation_mode='anything')

    def test_comparison_hash_is_required_even_for_failed_task(self):
        import hashlib
        html=self.root/'comparison.html';html.write_text('<html>offline</html>')
        report=dict(status='rejected',visualization=dict(path=str(html),sha256=hashlib.sha256(html.read_bytes()).hexdigest()))
        (self.root/'result.json').write_text(json.dumps(report))
        with patch.object(bridge.subprocess,'Popen') as opened:
            bridge.open_comparison(dict(folder=self.root))
            opened.assert_called_once()
        html.write_text('changed')
        with self.assertRaises(ValueError):bridge.open_comparison(dict(folder=self.root))

    def test_auto_requires_explicit_combined_net_authorization(self):
        inspected={'preflight':{'measurement':{'snapshot':{'copper_layers':['F.Cu','B.Cu'],
            'items':{'x':{'kind':'segment','net':'N'}}}}},'discovery':{'nets':[{'net':'N','supported':True}]}}
        # Use the bridge's own discovery format from the existing fixture.
        from unittest.mock import patch
        options=dict(route_layers=['F.Cu'],max_new_vias=0,reroute_nets=[],max_modified_tracks=0,strategy='auto')
        with patch.object(bridge,'advanced_choices',return_value=(['F.Cu','B.Cu'],['N'])):
            args=bridge.advanced_arguments(options,inspected,'F.Cu')
            self.assertIn('auto',args);self.assertNotIn('--modify-nets',args)
            args=bridge.advanced_arguments(dict(options,reroute_nets=['N'],max_modified_tracks=5),inspected,'F.Cu')
            self.assertIn('--modify-nets',args);self.assertNotIn('--reroute-nets',args)

    def test_explicit_advanced_permissions_reach_batch_and_default_stays_empty(self):
        inspected = {"preflight": {"measurement": {"snapshot": {
            "copper_layers": ["F.Cu", "B.Cu"], "items": {
                "a": {"kind": "segment", "net": "N", "locked": False}}}}},
            "discovery": {"nets": [{"net": "N", "supported": True}]}}
        options = dict(route_layers=["F.Cu", "B.Cu"], max_new_vias=2,
                       reroute_nets=["N"], max_modified_tracks=5, strategy="krt")
        self.assertEqual(bridge.advanced_arguments(None, inspected, "F.Cu"), [])
        args = bridge.advanced_arguments(options, inspected, "F.Cu")
        self.assertEqual(args, ["--route-layers", "F.Cu", "B.Cu", "--max-new-vias", "2",
                                "--reroute-nets", "N", "--max-modified-tracks", "5"])
        for change in [dict(route_layers=["F.Cu"]), dict(max_modified_tracks=0),
                       dict(reroute_nets=["OTHER"]), dict(max_new_vias=-1)]:
            with self.assertRaises(ValueError):
                bridge.advanced_arguments(dict(options, **change), inspected, "F.Cu")
        shove = dict(options, route_layers=["F.Cu"], max_new_vias=0, strategy="pns")
        self.assertIn("--shove-nets", bridge.advanced_arguments(shove, inspected, "F.Cu"))
        self.assertIn("--candidate-strategy", bridge.advanced_arguments(shove, inspected, "F.Cu"))
        with self.assertRaises(ValueError):
            bridge.advanced_arguments(dict(options, strategy="pns"), inspected, "F.Cu")
        inspection = self.root / "inspect.json"; inspection.write_text('{}')
        with patch.object(bridge, "source_still_matches"), patch.object(bridge.subprocess, "Popen", return_value=Mock()):
            task = bridge.launch(self.config, "batch", self.selection, ["one"], threading.Event(),
                                 inspected, inspection, options)
            self.assertEqual(task["argv"][-len(args):], args)

    def test_legacy_import_is_background_independent_and_never_a_repair(self):
        with patch.object(bridge.subprocess,'Popen',return_value=Mock()):
            task=bridge.launch(self.config,'import-legacy',self.selection,None,threading.Event())
        self.assertIn('import-legacy',task['argv'])
        for flag in ('--roi','--auto-region','--layer','--signal-kind','--original-closed'):
            self.assertNotIn(flag,task['argv'])
        folder=task['folder'];(folder/'imported').mkdir(parents=True)
        board=folder/'imported/board.kicad_pcb';board.write_text('(kicad_pcb)')
        task['process'].poll.return_value=0
        state=dict(status='complete',import_verified=True,accepted=False,imported_board=str(board))
        with patch.object(bridge,'query',return_value=state):
            self.assertEqual(bridge.imported_project(self.config,task),str(board.resolve()))
        for change in (dict(status='stale_result'),dict(import_verified=False),dict(accepted=True),dict(imported_board=str(self.board))):
            with patch.object(bridge,'query',return_value=dict(state,**change)),self.assertRaises(ValueError):
                bridge.imported_project(self.config,task)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.board = self.root / "含 空格 [0].kicad_pcb"
        self.board.write_text("(kicad_pcb)")
        self.board.with_suffix(".kicad_pro").write_text("{}")
        self.config = {"cli_python": "/test/venv/bin/python", "runtime_config": "/test/runtime.json",
                       "pcbnew_app": "/test/pcbnew.app", "tasks_root": str(self.root / "tasks")}
        self.selection = bridge.parameters(str(self.board), [0, 0, 10, 10], "F.Cu")

    def test_native_api_scope_and_python39_syntax(self):
        for path in (ROOT / "pcb_local_launcher").glob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "pcbnew":
                    self.assertIn(node.attr, {"ActionPlugin", "Version"})

    def test_invalid_roi_and_missing_rules_reject(self):
        for roi in ([0, 0, float("nan"), 2], [0, 0, 0, 2], [1, 0, 0, 2]):
            with self.assertRaises(ValueError):
                bridge.parameters(self.board, roi, "F.Cu")
        self.board.with_suffix(".kicad_pro").unlink()
        with self.assertRaises(ValueError):
            bridge.parameters(self.board, [0, 0, 2, 2], "F.Cu")

    def test_automatic_parameters_need_no_coordinates_and_argv_has_no_roi(self):
        selection = bridge.parameters(self.board, ["", "", "", ""], "F.Cu", auto_region=True)
        self.assertNotIn("roi", selection)
        with patch.object(bridge.subprocess, "Popen", return_value=Mock()) as popen:
            bridge.launch(self.config, "inspect", selection, None, threading.Event())
        argv = popen.call_args.args[0]
        self.assertIn("--auto-region", argv)
        self.assertNotIn("--roi", argv)

    def test_automatic_repair_cannot_request_a_whole_net(self):
        selection = bridge.parameters(self.board, [], "F.Cu", auto_region=True)
        inspection = self.root / "inspect.json"
        inspection.write_text('{}')
        with patch.object(bridge, "source_still_matches"), patch.object(bridge.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "连接目标"):
                bridge.launch(self.config, "repair", selection, ("--net", "DATA"), threading.Event(), {}, inspection)
            popen.assert_not_called()

    def test_cancel_before_launch_does_not_create_task(self):
        event = threading.Event(); event.set()
        with patch.object(bridge.subprocess, "Popen") as popen:
            self.assertIsNone(bridge.launch(self.config, "inspect", self.selection, None, event))
            popen.assert_not_called()
        self.assertFalse(Path(self.config["tasks_root"]).exists())

    def test_batch_keeps_order_and_requires_inspection(self):
        inspection = self.root / "inspect.json"
        inspection.write_text('{}')
        selection = bridge.parameters(self.board, [], "F.Cu", auto_region=True)
        with patch.object(bridge, "source_still_matches"), patch.object(bridge.subprocess, "Popen", return_value=Mock()) as popen:
            task = bridge.launch(self.config, "batch", selection, ["first", "second"], threading.Event(), {}, inspection)
            argv = task["argv"]
            index = argv.index("--targets")
            self.assertEqual(argv[index+1:index+3], ["first", "second"])
            self.assertIn("--auto-region", argv)
            self.assertNotIn("--roi", argv)
            for target in ([], ["same", "same"]):
                with self.assertRaises(ValueError):
                    bridge.launch(self.config, "batch", selection, target, threading.Event(), {}, inspection)
            with self.assertRaises(ValueError):
                bridge.launch(self.config, "batch", selection, ["one"], threading.Event())

    def test_delivery_commands_use_explicit_paths_and_confirmation(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({"status": "exported"}), "")
        with patch.object(bridge.subprocess, "run", return_value=completed) as run:
            bridge.deliver(self.config, self.root / "task", destination=self.root / "新 结果")
            self.assertEqual(run.call_args.args[0][-2:], ["--destination", str(self.root / "新 结果")])
            self.assertIn("export-result", run.call_args.args[0])
        with patch.object(bridge.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"status":"adopted"}', '')) as run:
            bridge.deliver(self.config, self.root / "task", adopt=True)
            self.assertEqual(run.call_args.args[0][-1], "--original-closed")
        with patch.object(bridge.subprocess, "run", return_value=subprocess.CompletedProcess([], 50, '', 'stale')):
            with self.assertRaises(RuntimeError): bridge.deliver(self.config, self.root, adopt=True)
        for kwargs in ({}, {"adopt": True, "destination": self.root}):
            with self.assertRaises(ValueError): bridge.deliver(self.config, self.root, **kwargs)

    def test_save_buttons_cancel_and_reopen_flow_without_wx_window(self):
        # Exercise event handlers with inert wx doubles; no window/app is opened.
        wx = types.SimpleNamespace(Frame=object, YES=1, NO=2, ID_OK=3, YES_NO=4,
             NO_DEFAULT=8, ICON_WARNING=16, FD_OPEN=32, FD_FILE_MUST_EXIST=64,
             DD_DEFAULT_STYLE=128, DD_DIR_MUST_EXIST=256)
        package = types.ModuleType("test_launcher"); package.process = bridge
        spec = importlib.util.spec_from_file_location("test_launcher.dialog", ROOT / "pcb_local_launcher/dialog.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"wx": wx, "test_launcher": package}):
            spec.loader.exec_module(module)
        frame = object.__new__(module.LauncherFrame)
        frame.task = {"folder": self.root, "accepted": True}
        frame.config = self.config; frame.cancel_requested = threading.Event()
        frame.busy = lambda: False; frame.control_pending = False
        frame.status = Mock(); frame.background = Mock(); frame.enable_controls = Mock(); frame.fail = Mock()
        (self.root / "result.json").write_text(json.dumps({"source_board": str(self.board)}))
        wx.MessageBox = Mock(return_value=wx.NO)
        frame.save_result(True)
        frame.background.assert_not_called()
        wx.MessageBox.return_value = wx.YES
        frame.save_result(True)
        action = frame.background.call_args.args[0]
        with patch.object(bridge, "deliver", return_value={"status": "adopted"}) as deliver:
            action()
            deliver.assert_called_once_with(self.config, self.root, destination=None, adopt=True)
        frame.control_pending = False; frame.task = None; frame.background.reset_mock()
        dialog = Mock(); dialog.ShowModal.return_value = wx.ID_OK
        dialog.GetPath.return_value = str(self.root / "result.json")
        manager = Mock(); manager.__enter__ = Mock(return_value=dialog); manager.__exit__ = Mock(return_value=False)
        wx.FileDialog = Mock(return_value=manager)
        frame.save_result(True)
        wx.FileDialog.assert_called_once()
        with patch.object(bridge, "deliver") as deliver:
            frame.background.call_args.args[0]()
            deliver.assert_called_once_with(self.config, self.root, destination=None, adopt=True)
        frame.control_pending = False
        frame.task = {"folder": self.root, "accepted": True}
        dialog.GetPath.return_value = str(self.root)
        wx.DirDialog = Mock(return_value=manager)
        frame.save_result(False)
        with patch.object(bridge, "deliver") as deliver:
            frame.background.call_args.args[0]()
            args, kwargs = deliver.call_args
            self.assertEqual(args, (self.config, self.root))
            self.assertFalse(kwargs["adopt"])
            self.assertEqual(kwargs["destination"].parent, self.root)
            self.assertFalse(kwargs["destination"].exists())

    def test_batch_opens_only_final_accepted_child(self):
        task = {"folder": self.root / "queue", "kind": "batch", "process": Mock()}
        task["process"].poll.return_value = 0
        candidate = task["folder"] / "02-repair/scoping/candidate/board.kicad_pcb"
        candidate.parent.mkdir(parents=True); candidate.write_text('(kicad_pcb)')
        (task["folder"] / "result.json").write_text(json.dumps({"accepted_chain": [{"result": str(task["folder"] / "02-repair/result.json")}]}))
        state = {"status": "accepted", "accepted": True, "candidate": str(candidate)}
        with patch.object(bridge, "query", return_value=state), patch.object(bridge.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            bridge.open_candidate(self.config, task)
            self.assertEqual(run.call_args.args[0][-1], str(candidate.resolve()))
        with patch.object(bridge, "query", return_value=dict(state, status="stale_result", accepted=False)):
            with self.assertRaises(ValueError): bridge.open_candidate(self.config, task)
        geometric = task["folder"] / "02-repair/geometry-search/candidate/board.kicad_pcb"
        geometric.parent.mkdir(parents=True); geometric.write_text('(kicad_pcb)')
        state["candidate"] = str(geometric)
        with patch.object(bridge, "query", return_value=state), patch.object(bridge.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            bridge.open_candidate(self.config, task)
            self.assertEqual(run.call_args.args[0][-1], str(geometric.resolve()))

    def test_batch_opens_auto_geometry_child_with_same_gate(self):
        task = {"folder": self.root / "queue", "kind": "batch", "process": Mock()}
        task["process"].poll.return_value = 0
        candidate = task["folder"] / "02-repair/scoping/candidate/board.kicad_pcb"
        candidate.parent.mkdir(parents=True); candidate.write_text('(kicad_pcb)')
        (task["folder"] / "result.json").write_text(json.dumps({"accepted_chain": [{"result": str(task["folder"] / "02-repair/result.json")}]}))
        state = {"status": "accepted", "accepted": True, "candidate": str(candidate)}
        with patch.object(bridge, "query", return_value=state), patch.object(bridge.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            bridge.open_candidate(self.config, task)
            self.assertEqual(run.call_args.args[0][-1], str(candidate.resolve()))
        with patch.object(bridge, "query", return_value=dict(state, status="stale_result", accepted=False)):
            with self.assertRaises(ValueError): bridge.open_candidate(self.config, task)
        geometric = task["folder"] / "02-repair/strategies/geometry/geometry-search/candidate/board.kicad_pcb"
        geometric.parent.mkdir(parents=True); geometric.write_text('(kicad_pcb)')
        state["candidate"] = str(geometric)
        with patch.object(bridge, "query", return_value=state), patch.object(bridge.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            bridge.open_candidate(self.config, task)
            self.assertEqual(run.call_args.args[0][-1], str(geometric.resolve()))

    def test_argv_keeps_exact_net_and_nonexistent_task_directory(self):
        inspection = self.root / "inspect/result.json"
        inspection.parent.mkdir()
        inspection.write_text('{}')
        inspected = {"source_board": str(self.board), "source_hashes": {}}
        with patch.object(bridge, "source_still_matches") as recheck:
            with patch.object(bridge.subprocess, "Popen", return_value=Mock()) as popen:
                task = bridge.launch(self.config, "repair", self.selection, ("--net", "-BUS[0] /中文"),
                                     threading.Event(), inspected, inspection)
            recheck.assert_called_once_with(self.config, inspected)
        argv = popen.call_args.args[0]
        self.assertIn("--net=-BUS[0] /中文", argv)
        self.assertEqual(argv[argv.index("--inspection") + 1], str(inspection.resolve()))
        self.assertIn(str(self.board.resolve()), argv)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertFalse(task["folder"].exists())
        self.assertTrue(task["log"].is_file())

    def test_repair_without_inspection_binding_never_starts(self):
        with patch.object(bridge.subprocess, "Popen") as popen:
            with self.assertRaises(ValueError):
                bridge.launch(self.config, "repair", self.selection, ("--net", "DATA"), threading.Event())
            popen.assert_not_called()

    def test_nonzero_status_cannot_smuggle_accepted(self):
        bad = subprocess.CompletedProcess([], 1, json.dumps({"status": "accepted", "accepted": True}), "failure")
        with patch.object(bridge.subprocess, "run", return_value=bad):
            with self.assertRaises(RuntimeError):
                bridge.query(self.config, "status", self.root)

    def test_open_requires_fresh_status_and_separate_instance(self):
        task = {"folder": self.root / "task", "process": Mock(), "accepted": True}
        task["process"].poll.return_value = 0
        candidate = task["folder"] / "candidate/board.kicad_pcb"
        candidate.parent.mkdir(parents=True); candidate.write_text("(kicad_pcb)")
        with patch.object(bridge, "query", return_value={"status": "stale_result", "accepted": False}):
            with patch.object(bridge.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    bridge.open_candidate(self.config, task)
                run.assert_not_called()
        state = {"status": "accepted", "accepted": True, "candidate": str(candidate)}
        with patch.object(bridge, "query", return_value=state) as query:
            with patch.object(bridge.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                bridge.open_candidate(self.config, task)
                query.assert_called_once_with(self.config, "status", task["folder"])
                self.assertEqual(run.call_args.args[0], ["/usr/bin/open", "-n", "-a", "/test/pcbnew.app", "--args", str(candidate.resolve())])
        task["process"].poll.return_value = 1
        with patch.object(bridge, "query", return_value=state):
            with self.assertRaises(ValueError):
                bridge.open_candidate(self.config, task)

    def test_changed_inspection_identity_rejects(self):
        inspected = {"source_board": str(self.board), "source_hashes": {str(self.board): "old"}}
        changed = subprocess.CompletedProcess([], 0, json.dumps({str(self.board): "new"}), "")
        with patch.object(bridge.subprocess, "run", return_value=changed):
            with self.assertRaises(ValueError):
                bridge.source_still_matches(self.config, inspected)

    def test_scope_candidate_allowed_but_raw_outputs_never_open(self):
        task = {"folder": self.root / "task", "process": Mock(), "accepted": True}
        task["process"].poll.return_value = 0
        for relative, allowed in (("scoping/candidate/board.kicad_pcb", True),
                                  ("geometry-search/candidate/board.kicad_pcb", True),
                                  ("strategies/geometry/geometry-search/candidate/board.kicad_pcb", True),
                                  ("strategies/geometry/geometry-search/raw-engine/board.kicad_pcb", False),
                                  ("strategies/geometry/scoping/candidate/board.kicad_pcb", False),
                                  ("strategies/geometry-multilayer-local-refill/geometry-search/candidate/board.kicad_pcb", True),
                                  ("strategies/geometry-multilayer-local-refill/scoping/candidate/board.kicad_pcb", False),
                                  ("geometry-search/raw-engine/board.kicad_pcb", False),
                                  ("engine-output/board.kicad_pcb", False),
                                  ("scoping/raw-engine/board.kicad_pcb", False)):
            with self.subTest(relative=relative):
                candidate = task["folder"] / relative
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_text("(kicad_pcb)")
                state = {"status": "accepted", "accepted": True, "candidate": str(candidate)}
                with patch.object(bridge, "query", return_value=state):
                    with patch.object(bridge.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                        if allowed:
                            bridge.open_candidate(self.config, task)
                            self.assertEqual(run.call_args.args[0][-1], str(candidate.resolve()))
                        else:
                            with self.assertRaises(ValueError):
                                bridge.open_candidate(self.config, task)
                            run.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
