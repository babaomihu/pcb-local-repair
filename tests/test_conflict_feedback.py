"""Ordering evidence is advisory; these tests do not claim physical routing."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pcb_local.auto_strategy import plans, repair
from pcb_local.conflict_feedback import collect, focused_tasks
from pcb_local.joint_repair import JointCoordinator
from pcb_local.jobs import Halt
from pcb_local.workspace import copy_project
from test_modification import snapshot
from test_scope_candidate import BASE, RULES, segment


class ConflictFeedbackTests(unittest.TestCase):
    def test_native_identity_binding_and_drc_priority(self):
        report={'preflight':{'measurement':{'snapshot':{'items':{'a':{'net':'N'}}}}},
            'pns':{'direct_path_obstacles':{'items':[{'uuid':'a'},{'uuid':'unknown'}]}},
            'verification':{'verification_complete':True,'drc':{'sections':{'violations':{
                'new_or_changed':[{'items':[{'uuid':'a'},{'uuid':'unknown'}]}]}}}}}
        result=collect(report)
        self.assertEqual([r['uuid'] for r in result['objects']],['a'])
        self.assertEqual(result['objects'][0]['kinds'],['direct_corridor_hint','native_drc_participant'])
        self.assertFalse(result['physical_impossibility_proven'])
        report['verification']['verification_complete']=False
        self.assertEqual(collect(report)['objects'][0]['kinds'],['direct_corridor_hint'])

    def coordinator(self):
        c=JointCoordinator(['a','b','c'],'auto')
        def frame(uid,index):
            return dict(index=index,board=uid+'-baseline',target_id=uid,snapshot={},changed_uuids=[uid+'-trace'],
                pool={'selected_fingerprint':uid+'-one','candidates':[{'fingerprint':uid+'-two','status':'pending'}]},strategies={})
        c.frames=[frame('a',1),frame('b',2)]
        return c

    def test_relevant_earlier_frame_precedes_unrelated_latest_frame(self):
        c=self.coordinator();c.last_conflicts={'a-trace':{'kinds':['direct_corridor_hint']}}
        c.conflict_history={'a-trace':2}
        self.assertEqual(c.backtrack({}),(1,'a-baseline'))
        row=c.evidence['backtracks'][0]
        self.assertEqual(row['conflict_objects'],['a-trace'])
        self.assertEqual(row['discarded_suffix'],['a','b'])
        self.assertEqual(c.max_backtracks,8)

    def test_no_feedback_preserves_reverse_order(self):
        self.assertEqual(self.coordinator().backtrack({}),(2,'b-baseline'))

    def test_joint_planning_charges_trigger_target_generation_and_total(self):
        c=self.coordinator();now=[100.]
        with patch('pcb_local.joint_repair.time.monotonic',side_effect=lambda:now[0]):
            with c.planning('c','failure-ranking'):now[0]+=3
            c.budgets['c'].charge('generation',56)
            with self.assertRaises(Halt):
                with c.planning('c','failure-ranking'):now[0]+=2
        self.assertEqual(c.budgets['c'].used['generation'],61)
        self.assertEqual(c.budgets['c'].used['total'],5)
        self.assertEqual(c.budgets['a'].used['generation'],0)
        self.assertEqual(c.evidence['planning'][-1]['target_id'],'c')

    def test_actual_drc_precedes_corridor_and_budget_is_not_reset(self):
        c=self.coordinator();c.last_conflicts={'a-trace':{'kinds':['native_drc_participant']},'b-trace':{'kinds':['direct_corridor_hint']}}
        c.conflict_history={'b-trace':100}
        self.assertEqual(c.backtrack({}),(1,'a-baseline'))
        c=self.coordinator();c.last_conflicts={'a-trace':{'kinds':['native_drc_participant']}}
        c.budgets['a'].charge('generation',60)
        self.assertEqual(c.backtrack({}),(2,'b-baseline'))

    def native(self):
        data=snapshot()
        for row in data['items'].values():row['netclass']={'track_width_mm':.2,'clearance_mm':.2}
        # Target chord crosses the N component; unrelated U stays far away.
        data['items']['t1']['position_mm']=[1,-1]
        data['items']['t2']['position_mm']=[1,1]
        for uid,old in [('u1','n1'),('u2','n2'),('up1','p1'),('up2','p2')]:
            row=copy.deepcopy(data['items'][old]);row.update(uuid=uid,net='U')
            for key in ('position_mm','start_mm','end_mm'):
                if key in row:row[key]=[row[key][0],row[key][1]+5]
            data['items'][uid]=row;data['component_of'][uid]=3
        data['components'].append({'items':['u1','u2','up1','up2'],'net':'U'})
        task=dict(target_net='T',target_pad_uuids=['t1','t2'],allowed_layer='F.Cu',region_mm=[-2,-2,8,8],
            modification=dict(schema_version=1,mode='ripup',routing_uuids=['n1','n2','u1','u2'],nets=['N','U'],max_modified_tracks=4))
        return data,task

    def test_focused_permission_is_anchored_original_component_subset(self):
        data,task=self.native();original=copy.deepcopy(task)
        focused=focused_tasks(data,task)
        self.assertEqual(len(focused),1)
        _,child,proof=focused[0]
        self.assertEqual(child['modification']['routing_uuids'],['n1','n2'])
        self.assertEqual(child['modification']['max_modified_tracks'],2)
        self.assertEqual(proof['retained_fixed_uuids'],['u1','u2'])
        self.assertEqual(task,original)
        self.assertEqual(child['region_mm'],task['region_mm'])
        data['items']['n1']['locked']=True
        self.assertEqual(focused_tasks(data,task),[])

    def test_feedback_can_select_non_chord_component_without_authorizing_other_objects(self):
        data,task=self.native()
        result=focused_tasks(data,task,{'objects':[{'uuid':'u1','kinds':['native_drc_participant']},{'uuid':'other','kinds':['native_drc_participant']}]})
        self.assertEqual(result[0][1]['modification']['routing_uuids'],['u1','u2'])
        self.assertEqual(len(result),2)
        task['partial_segments']=[{'not':'supported by this selector'}]
        self.assertEqual(focused_tasks(data,task),[])

    def test_all_focused_plans_are_permission_subsets_and_full_plan_remains(self):
        data,task=self.native()
        branches=plans(task,True,data)
        self.assertEqual(branches[-1][0],'ripup')
        self.assertEqual(branches[-1][2]['modification'],task['modification'])
        focused=[child for label,_,child in branches if '-focused-' in label]
        self.assertEqual(len(focused),1)
        for child in focused:
            self.assertLess(set(child['modification']['routing_uuids']),set(task['modification']['routing_uuids']))
            self.assertEqual(child['region_mm'],task['region_mm'])
            self.assertEqual(child['modification']['mode'],'ripup')

    def test_actual_failure_adds_authorized_non_chord_subset_before_hint(self):
        data,task=self.native()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);board=root/'board.kicad_pcb'
            board.write_text(BASE);board.with_suffix('.kicad_pro').write_text(json.dumps(RULES))
            folder=root/'job';folder.mkdir();(folder/'netclasses.native.json').write_text('{}')
            job=SimpleNamespace(folder=folder,check=lambda:None)
            report={'preflight':{'measurement':{'snapshot':data}}};tried=[]
            def generate(job,config,source,child,*args,**kwargs):
                tried.append((job.label,copy.deepcopy(child)))
                result,_=copy_project(source,job.folder/'candidate')
                result.write_text(BASE[:-1]+segment('proposal')+')')
                return result
            def validate(job,*args,**kwargs):
                if job.label=='add-only':
                    return 1,{'verification_complete':True,'accepted':False,'checks':{'full_drc_no_new_or_changed':False},
                        'drc':{'sections':{'violations':{'new_or_changed':[{'items':[{'uuid':'u1'}]}]}}}}
                return 0,{'verification_complete':True,'accepted':True}
            with patch('pcb_local.auto_strategy.generate',side_effect=generate),patch('pcb_local.auto_strategy.validate',side_effect=validate):
                repair(job,{},board,task,data,report,'quick')
            self.assertEqual([label for label,_ in tried[:2]],['add-only','geometry'])
            self.assertEqual(tried[2][1]['modification']['routing_uuids'],['u1','u2'])
            self.assertTrue(tried[2][0].startswith('ripup-focused-'))
            self.assertEqual(len(tried),3)
            self.assertEqual(report['automatic_strategy']['generation_budget_seconds'],60)
            self.assertEqual(board.read_text(),BASE)


if __name__=='__main__':unittest.main()
