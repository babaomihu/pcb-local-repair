"""Saved-task filtering only; no active-board serialization or acceptance."""
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace as N
import sys
import types
import unittest
from unittest.mock import Mock, patch

spec=importlib.util.spec_from_file_location('native_selection',Path(__file__).resolve().parents[1]/'kicad_plugin/pcb_local_launcher/native_selection.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class NativeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.path=str(Path('/tmp/saved.kicad_pcb').resolve())
        point=lambda x,y:N(x=x,y=y)
        self.track=N(GetClass=lambda:'PCB_TRACK',m_Uuid=N(AsString=lambda:'track'),
            GetNetname=lambda:'SIGNAL',GetLayerSet=lambda:N(CuStack=lambda:[0]),
            GetPosition=lambda:point(1000000,2000000),IsLocked=lambda:False,
            GetStart=lambda:point(1000000,2000000),GetEnd=lambda:point(3000000,2000000),GetWidth=lambda:200000)
        self.active=N(GetFileName=lambda:self.path,GetEnabledLayers=lambda:N(CuStack=lambda:[0,2]),
            GetStandardLayerName=lambda v:{0:'F.Cu',2:'B.Cu'}[v])
        self.api=N(Version=lambda:'10.0.4',GetBoard=lambda:self.active,GetCurrentSelection=lambda:[self.track])
        self.row=dict(uuid='track',kind='segment',net='SIGNAL',copper_layers=['F.Cu'],position_mm=[1,2],
            locked=False,start_mm=[1,2],end_mm=[3,2],width_mm=.2)
        self.report=dict(status='complete',source_files_untouched=True,source_board=self.path,
            preflight=dict(measurement=dict(snapshot=dict(copper_layers=['F.Cu','B.Cu'],items={'track':self.row}))),
            discovery=dict(allowed_layer='F.Cu',targets=[dict(id='matching',supported=True,endpoints=[dict(uuid='track')]),
                dict(id='unrelated',supported=True,pad_uuids=['another']),dict(id='unsupported',supported=False,pad_uuids=['track'])]))
        self.check=Mock()

    def call(self):return module.saved_target_selection(self.api,self.report,self.path,'F.Cu',self.check)

    def test_only_existing_matching_targets_and_no_save_claim(self):
        previous=copy.deepcopy(self.report)
        value=self.call()
        self.assertEqual(value['target_ids'],['matching']);self.assertFalse(value['active_board_saved_state_verified'])
        self.assertTrue(value['endpoint_binding_only']);self.assertEqual(self.report,previous)
        self.assertEqual(self.check.call_count,2)

    def test_changed_position_width_net_or_layer_rejects(self):
        mutations={'position_mm':[1.000001,2],'width_mm':.200001,'net':'OTHER','copper_layers':['B.Cu'],'locked':True}
        for key,value in mutations.items():
            with self.subTest(key=key):
                old=self.row[key];self.row[key]=value
                with self.assertRaises(ValueError):self.call()
                self.row[key]=old

    def test_other_active_board_and_unknown_version_reject(self):
        self.active.GetFileName=lambda:'/tmp/other.kicad_pcb'
        with self.assertRaises(ValueError):self.call()
        self.api.Version=lambda:'10.0.5'
        with self.assertRaises(ValueError):self.call()

    def test_stale_context_rejects_before_reading_selection(self):
        self.api.GetCurrentSelection=Mock(side_effect=AssertionError('must not read'))
        self.check.side_effect=ValueError('stale context')
        with self.assertRaisesRegex(ValueError,'stale context'):self.call()
        self.api.GetCurrentSelection.assert_not_called()

    def test_race_during_read_rejects(self):
        self.check.side_effect=[None,ValueError('changed during read')]
        with self.assertRaisesRegex(ValueError,'changed during read'):self.call()

    def test_empty_unsupported_and_ambiguous_selection_reject(self):
        self.api.GetCurrentSelection=lambda:[]
        with self.assertRaises(ValueError):self.call()
        self.api.GetCurrentSelection=lambda:[N(GetClass=lambda:'FOOTPRINT')]
        with self.assertRaises(ValueError):self.call()
        self.api.GetCurrentSelection=lambda:[self.track]
        self.report['preflight']['measurement']['snapshot']['items']['duplicate']=dict(self.row)
        with self.assertRaises(ValueError):self.call()

    def test_no_eligible_match_never_expands_to_whole_net(self):
        self.report['discovery']['targets']=self.report['discovery']['targets'][1:]
        with self.assertRaises(ValueError):self.call()


class NativeSelectionDialogTests(unittest.TestCase):
    def setUp(self):
        plugin=Path(__file__).resolve().parents[1]/'kicad_plugin/pcb_local_launcher'
        self.bridge=Mock();self.selection=Mock()
        package=types.ModuleType('native_selection_test_launcher');package.__path__=[str(plugin)]
        package.process=self.bridge
        selection_module=types.ModuleType('native_selection_test_launcher.native_selection')
        selection_module.saved_target_selection=self.selection
        self.modules={'wx':N(Frame=object),'pcbnew':Mock(),
            'native_selection_test_launcher':package,
            'native_selection_test_launcher.native_selection':selection_module}
        spec=importlib.util.spec_from_file_location('native_selection_test_launcher.dialog',plugin/'dialog.py')
        dialog=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,self.modules):spec.loader.exec_module(dialog)
        frame=object.__new__(dialog.LauncherFrame)
        frame.busy=Mock(return_value=False);frame.control_pending=False
        frame.inspected={'status':'complete'};frame.config={'cli_python':'fake'}
        frame.board=Mock();frame.board.GetPath.return_value='/tmp/saved.kicad_pcb'
        frame.layer=Mock();frame.layer.GetStringSelection.return_value='F.Cu'
        frame.targets=[dict(id='target-a'),dict(id='target-b')]
        frame.target=Mock();frame.mode=Mock();frame.status=Mock();frame.fail=Mock()
        frame.refill_zones=['zone-a'];frame.refill_target_ids=['target-b'];frame.refill_button=Mock()
        frame.reviewed_recommendations=['old-proposal'];frame.describe_region=Mock()
        self.frame=frame

    def invoke(self):
        with patch.dict(sys.modules,self.modules):self.frame.select_from_editor(None)

    def test_success_uses_existing_ids_and_revokes_target_specific_permissions(self):
        def selected(api,inspected,path,layer,check_disk):
            check_disk()
            return dict(target_ids=['target-a','never-listed'],notice='仅磁盘版本；未保存修改不参与')
        self.selection.side_effect=selected
        self.invoke()
        self.frame.target.SetCheckedItems.assert_called_once_with([0])
        self.frame.mode.SetSelection.assert_called_once_with(0)
        self.assertEqual(self.frame.reviewed_recommendations,[])
        self.assertEqual(self.frame.refill_zones,[]);self.assertEqual(self.frame.refill_target_ids,[])
        self.frame.describe_region.assert_called_once_with()
        self.frame.status.SetValue.assert_called_once_with('仅磁盘版本；未保存修改不参与')
        self.bridge.source_still_matches.assert_called_once_with(self.frame.config,self.frame.inspected)
        self.frame.fail.assert_not_called()

    def test_source_failure_preserves_selection_and_existing_permissions(self):
        self.bridge.source_still_matches.side_effect=ValueError('磁盘工程已变化')
        self.selection.side_effect=lambda api,inspected,path,layer,check_disk:check_disk()
        self.invoke()
        self.frame.target.SetCheckedItems.assert_not_called()
        self.frame.mode.SetSelection.assert_not_called()
        self.assertEqual(self.frame.refill_zones,['zone-a'])
        self.assertEqual(self.frame.reviewed_recommendations,['old-proposal'])
        self.frame.fail.assert_called_once_with('磁盘工程已变化')
        self.bridge.launch.assert_not_called()

    def test_busy_pending_or_uninspected_disables_button_and_skips_native_read(self):
        f=self.frame
        for name in ('auto_region','net','inspect','advanced','from_selection','repair','cancel','report','open','save_as','adopt'):
            setattr(f,name,Mock())
        f.inputs=[];f.task=None;f.finished=True;f.cancel_requested=Mock()
        f.cancel_requested.is_set.return_value=False
        for busy,pending,inspected in [(True,False,{'status':'complete'}),(False,True,{'status':'complete'}),(False,False,None)]:
            f.busy.return_value=busy;f.control_pending=pending;f.inspected=inspected
            self.invoke();f.enable_controls();f.from_selection.Enable.assert_called_with(False)
        self.selection.assert_not_called()
        f.busy.return_value=False;f.control_pending=False;f.inspected={'status':'complete'}
        f.enable_controls();f.from_selection.Enable.assert_called_with(True)


if __name__=='__main__':unittest.main()
