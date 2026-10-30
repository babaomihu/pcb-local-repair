# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit plugin refill permissions; no wx windows or installed-plugin changes."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'kicad_plugin/pcb_local_launcher'
spec = importlib.util.spec_from_file_location('refill_plugin_bridge', PLUGIN / 'process.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class RefillBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.board = self.root / 'test board.kicad_pcb'
        self.board.write_text('(kicad_pcb)')
        self.board.with_suffix('.kicad_pro').write_text('{}')
        self.inspection = self.root / 'inspection.json'
        self.inspection.write_text('{}')
        self.config = dict(cli_python='/test/python', runtime_config='/test/runtime.json',
                           pcbnew_app='/test/pcbnew.app', tasks_root=str(self.root/'tasks'))
        self.selection = bridge.parameters(self.board, [], 'F.Cu', auto_region=True)
        self.options = dict(route_layers=['F.Cu'], max_new_vias=0, reroute_nets=[],
                            max_modified_tracks=0, strategy='auto')
        self.zones = [dict(uuid='zone-a', net='GND', layers=['F.Cu'], bounds_mm=[0,0,10,10],
                           local_clearance_mm=0.2, min_thickness_mm=0.2),
                      dict(uuid='zone-b', net='VCC', layers=['F.Cu'], bounds_mm=[1,1,9,9],
                           local_clearance_mm=0.3, min_thickness_mm=0.25)]
        self.targets = [dict(id='target-a',net='SIGNAL1',supported=True,region_mm=[2,2,8,8]),
                        dict(id='target-b',net='SIGNAL2',supported=True,region_mm=[3,3,7,7])]
        self.report = dict(zone_refill_choices=copy.deepcopy(self.zones),
                           discovery=dict(targets=self.targets, nets=[]),
                           preflight=dict(measurement=dict(snapshot=dict(
                               zone_refill_choices=self.zones,copper_layers=['F.Cu','B.Cu'],items={}))))

    def args(self, ids, **changes):
        kwargs=dict(inspected=self.report,layer='F.Cu',kind='batch',target=['target-a','target-b'],
                    advanced=self.options,recommendations=None,recommendation_mode='shove')
        kwargs.update(changes)
        return bridge.refill_arguments(ids,**kwargs)

    def test_empty_default_has_no_authority(self):
        self.assertEqual(self.args(None, advanced=None),[])
        self.assertEqual(self.args([], advanced=None),[])
        with patch.object(bridge,'source_still_matches'), patch.object(bridge.subprocess,'Popen',return_value=Mock()):
            task=bridge.launch(self.config,'batch',self.selection,['target-a'],threading.Event(),self.report,self.inspection)
        self.assertNotIn('--refill-zone',task['argv'])
        self.assertNotIn('--candidate-strategy',task['argv'])

    def test_all_targets_and_native_objects_bound(self):
        self.assertEqual(bridge.refill_choices(self.report,'F.Cu',self.targets),self.zones)
        self.targets[1]['net']='GND'
        self.assertEqual([z['uuid'] for z in bridge.refill_choices(self.report,'F.Cu',self.targets)],['zone-b'])
        with self.assertRaises(ValueError):self.args(['zone-a'])
        self.report['zone_refill_choices'][1]['min_thickness_mm']=0.01
        with self.assertRaisesRegex(ValueError,'原生检查'):self.args(['zone-b'])

    def test_unlocked_board_zones_only_on_layer_and_region(self):
        for mutation in (dict(locked=True),dict(source_owner_uuid='footprint'),dict(is_rule_area=True),
                         dict(layers=['B.Cu']),dict(bounds_mm=[20,20,30,30])):
            with self.subTest(mutation=mutation):
                report=copy.deepcopy(self.report)
                report['zone_refill_choices'][0].update(mutation)
                report['preflight']['measurement']['snapshot']['zone_refill_choices'][0].update(mutation)
                self.assertNotIn('zone-a',[z['uuid'] for z in bridge.refill_choices(report,'F.Cu',self.targets)])

    def test_targets_require_inspected_supported_connections(self):
        target=dict(self.targets[0],supported=False)
        with self.assertRaises(ValueError):bridge.refill_choices(self.report,'F.Cu',[target])
        # This filter offers choices, not acceptance. Native CLI binding still
        # checks every original segment/via identity, span and position.
        for kind in ('segment','via'):
            target=dict(self.targets[0],endpoints=[{'kind':'pad'},{'kind':kind}])
            self.assertEqual(bridge.refill_choices(self.report,'F.Cu',[target]),self.zones)
        with self.assertRaises(ValueError):self.args(['zone-a'],target=['unknown'])
        with self.assertRaises(ValueError):self.args(['zone-a'],kind='repair',target=('--net','SIGNAL1'))

    def test_joint_mode_is_explicit_bounded_and_preserves_permissions(self):
        with patch.object(bridge,'source_still_matches'),patch.object(bridge.subprocess,'Popen',return_value=Mock()):
            task=bridge.launch(self.config,'batch',self.selection,['target-a','target-b'],
                threading.Event(),self.report,self.inspection,order='joint')
        self.assertEqual(task['argv'][-2:],['--order','joint'])
        self.assertNotIn('--shove-nets',task['argv'])
        self.assertNotIn('--modify-nets',task['argv'])
        self.assertNotIn('--refill-zone',task['argv'])
        for ids in (['target-a'],['a','b','c','d','e']):
            with patch.object(bridge,'source_still_matches'),patch.object(bridge.subprocess,'Popen') as start:
                with self.assertRaisesRegex(ValueError,'2–4'):
                    bridge.launch(self.config,'batch',self.selection,ids,threading.Event(),
                        self.report,self.inspection,order='joint')
                start.assert_not_called()

    def test_engine_and_permission_combinations_rejected(self):
        for opts in (None, dict(self.options,strategy='krt'),dict(self.options,strategy='pns',route_layers=['F.Cu','B.Cu']),
                     dict(self.options,max_new_vias=1)):
            with self.subTest(options=opts),self.assertRaises(ValueError):self.args(['zone-a'],advanced=opts)
        with self.assertRaises(ValueError):self.args(['zone-a'],recommendations=['proposal'],recommendation_mode='ripup')
        for ids in (['missing'],['zone-a','zone-a'],'zone-a'):
            with self.assertRaises(ValueError):self.args(ids)

    def test_local_middle_recommendation_cannot_launch_with_ripup_permission(self):
        self.report['modification_recommendations']=[dict(id='reviewed-local',
            target_id='target-a',scope='local_middle')]
        for mode in ('ripup','shove+ripup'):
            with self.subTest(mode=mode),patch.object(bridge,'source_still_matches'),patch.object(bridge.subprocess,'Popen') as start:
                with self.assertRaisesRegex(ValueError,'仅支持推挤'):
                    bridge.launch(self.config,'repair',self.selection,('--target','target-a'),threading.Event(),
                        self.report,self.inspection,recommendations=['reviewed-local'],recommendation_mode=mode)
                start.assert_not_called()
        with patch.object(bridge,'source_still_matches'),patch.object(bridge.subprocess,'Popen',return_value=Mock()):
            task=bridge.launch(self.config,'repair',self.selection,('--target','target-a'),threading.Event(),
                self.report,self.inspection,recommendations=['reviewed-local'])
        self.assertEqual(task['argv'][-4:],['--accept-recommendation','reviewed-local','--recommendation-mode','shove'])
        self.assertNotIn('--modify-nets',task['argv'])
        self.assertNotIn('--local-track',task['argv'])

    def test_layered_refill_requires_explicit_layers_vias_and_no_track_motion(self):
        options=dict(self.options,route_layers=['F.Cu','B.Cu'],max_new_vias=2)
        for rows in (self.report['zone_refill_choices'],self.zones):rows[1]['layers']=['B.Cu']
        self.assertEqual(self.args(['zone-a','zone-b'],advanced=options),
                         ['--refill-zone','zone-a','--refill-zone','zone-b'])
        self.assertEqual([z['uuid'] for z in bridge.refill_choices(self.report,'F.Cu',self.targets)],['zone-a'])
        with self.assertRaises(ValueError):self.args(['zone-b'],advanced=self.options)
        with self.assertRaises(ValueError):self.args(['zone-a'],advanced=options,recommendations=['proposal'])

    def test_repair_and_batch_exact_argv_without_expanding_track_permissions(self):
        expected=['--refill-zone','zone-a','--refill-zone','zone-b']
        for strategy in ('pns','auto'):
            for kind,target in (('repair',('--target','target-a')),('batch',['target-a','target-b'])):
                with self.subTest(strategy=strategy,kind=kind),patch.object(bridge,'source_still_matches'),patch.object(bridge.subprocess,'Popen',return_value=Mock()) as popen:
                    task=bridge.launch(self.config,kind,self.selection,target,threading.Event(),self.report,
                        self.inspection,dict(self.options,strategy=strategy),refill_zones=['zone-a','zone-b'])
                    self.assertEqual(task['argv'][-4:],expected)
                    self.assertNotIn('--modify-nets',task['argv'])
                    self.assertNotIn('--shove-nets',task['argv'])
                    self.assertNotIn('--reroute-nets',task['argv'])
                    self.assertNotIn('--max-modified-tracks',task['argv'])
                    self.assertFalse(popen.call_args.kwargs.get('shell',False))

    def test_frozen_reasons_for_single_and_batch_and_no_physical_claim(self):
        selection=dict(frozen_uuids=['track-a'],retained_uuids=['track-b'],physical_impossibility_proven=False,
                       tracks=[dict(uuid='track-a',net='N',decision='freeze',reason='old_clearance_corridor_may_refill_outside_roi'),
                               dict(uuid='track-b',net='N',decision='retain',reason='inside')])
        single=bridge.refill_summary(dict(refill_operation_selection=selection))
        self.assertIn('track-a',single)
        self.assertIn('局部范围外',single)
        self.assertNotIn('track-b',single)
        self.assertIn('不代表物理上无法布通',single)
        batch=bridge.refill_summary(dict(items=[dict(target=dict(id='target-a'),refill_operation_selection=selection)]))
        self.assertIn('target-a',batch)
        self.assertIn(single,batch)

    def test_open_refill_auto_candidate_requires_fresh_accepted_status(self):
        task=dict(folder=self.root,process=Mock(),kind='repair')
        task['process'].poll.return_value=0
        for strategy in ('push-local-refill','pns-fixed-local-refill','untrusted'):
            candidate=self.root/'strategies'/strategy/'scoping/candidate/board.kicad_pcb'
            candidate.parent.mkdir(parents=True)
            candidate.write_text('(kicad_pcb)')
            state=dict(status='accepted',accepted=True,candidate=str(candidate))
            with patch.object(bridge,'query',return_value=state),patch.object(bridge.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'','')) as run:
                if strategy=='untrusted':
                    with self.assertRaises(ValueError):bridge.open_candidate(self.config,task)
                    run.assert_not_called()
                else:
                    bridge.open_candidate(self.config,task)
                    self.assertEqual(run.call_args.args[0][-1],str(candidate.resolve()))
            with patch.object(bridge,'query',return_value=dict(state,status='stale_result')),self.assertRaises(ValueError):
                bridge.open_candidate(self.config,task)

    def test_partial_segments_not_counted_as_whole_track_motion(self):
        selection=dict(frozen_uuids=['frozen'],retained_uuids=['partial','whole'],partial_uuids=['partial'],
                       tracks=[dict(uuid='partial',net='N',decision='partial',reason='fixed_tails_with_movable_inset_middle')])
        text=bridge.refill_summary(dict(refill_operation_selection=selection))
        self.assertIn('整段冻结 1 段',text)
        self.assertIn('两端固定仅中段可动 1 段',text)
        self.assertIn('整段保留可动 1 段',text)
        self.assertIn('对象 partial',text)
        self.assertIn('两端保持固定',text)

    def frame(self, selected, answer=1):
        dialog=Mock()
        dialog.__enter__=Mock(return_value=dialog)
        dialog.__exit__=Mock(return_value=False)
        dialog.ShowModal.return_value=answer
        dialog.GetSelections.return_value=selected
        wx=types.SimpleNamespace(Frame=object,ID_OK=1,MultiChoiceDialog=Mock(return_value=dialog))
        package=types.ModuleType('refill_test_launcher');package.process=bridge
        spec=importlib.util.spec_from_file_location('refill_test_launcher.dialog',PLUGIN/'dialog.py')
        module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'wx':wx,'refill_test_launcher':package}):spec.loader.exec_module(module)
        frame=object.__new__(module.LauncherFrame)
        frame.busy=lambda:False
        frame.inspected=self.report;frame.targets=self.targets
        frame.mode=Mock();frame.mode.GetSelection.return_value=0
        frame.target=Mock();frame.target.GetCheckedItems.return_value=[0,1]
        frame.layer=Mock();frame.layer.GetStringSelection.return_value='F.Cu'
        frame.refill_zones=[];frame.refill_target_ids=[]
        frame.advanced_options=None;frame.reviewed_recommendations=[];frame.recommendation_mode='shove'
        frame.advanced=Mock();frame.refill_button=Mock();frame.status=Mock();frame.fail=Mock()
        return frame,dialog

    def test_dialog_default_empty_and_explicit_selection_never_grants_old_line_motion(self):
        frame,dialog=self.frame([0])
        frame.configure_refill(None)
        frame.fail.assert_not_called()
        dialog.SetSelections.assert_called_once_with([])
        self.assertEqual(frame.refill_zones,['zone-a'])
        self.assertEqual(frame.refill_target_ids,['target-a','target-b'])
        self.assertEqual(frame.advanced_options,self.options)
        frame.clear_refill()
        self.assertEqual(frame.refill_zones,[])

    def test_dialog_cancel_no_selection_or_incompatible_strategy_has_no_permission_growth(self):
        for selected,answer in (([0],0),([],1)):
            frame,_=self.frame(selected,answer)
            frame.configure_refill(None)
            self.assertEqual(frame.refill_zones,[])
            self.assertIsNone(frame.advanced_options)
        frame,_=self.frame([0])
        frame.advanced_options=dict(self.options,strategy='krt')
        frame.configure_refill(None)
        frame.fail.assert_called_once()
        self.assertEqual(frame.refill_zones,[])
        self.assertEqual(frame.advanced_options['strategy'],'krt')

    def test_changing_target_selection_revokes_zone_authority(self):
        frame,_=self.frame([0])
        frame.configure_refill(None)
        self.assertEqual(frame.refill_zones,['zone-a'])
        frame.target.GetCheckedItems.return_value=[1]
        frame.auto_region=Mock();frame.auto_region.GetValue.return_value=True
        frame.region_summary=Mock();frame.GetChildren=Mock(return_value=[Mock()])
        frame.describe_region()
        self.assertEqual(frame.refill_zones,[])
        self.assertEqual(frame.refill_target_ids,[])


if __name__=='__main__':
    unittest.main(verbosity=2)
