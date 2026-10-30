"""Batch integration: fresh scheduling, one attempt and unchanged authority."""
import copy
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
from pcb_local.batch import batch_workflow, check_chain
from pcb_local.jobs import write_json, Halt
from pcb_local.workspace import source_files
from pcb_local.queue_coordination import choose_next
from test_queue_coordination import scenario, wall


class BatchCoordination(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.board=self.root/'board.kicad_pcb'
        self.board.write_text('(kicad_pcb)');self.board.with_suffix('.kicad_pro').write_text('{}')
        self.targets,self.native=scenario()
        # A third independent pending target exposes a genuine second choice.
        third=copy.deepcopy(self.targets[0]);third.update(id='third',net='C',region_mm=[18,8,32,16])
        third['pads']=[dict(uuid='c1',position_mm=[20.,12.]),dict(uuid='c2',position_mm=[30.,12.])]
        self.targets.append(third);self.native['routing_rules']['netclasses_mm']['C']=dict(track_width=.2,clearance=.1)
        for t in self.targets:
            t.update(supported=True,pad_uuids=[e['uuid'] for e in t['pads']])
            for e in t['pads']:
                self.native['items'][e['uuid']]=dict(kind='pad',uuid=e['uuid'],net=t['net'],position_mm=e['position_mm'],copper_layers=['F.Cu'])
        self.native['component_of']={k:k for k in self.native['items']}
        self.listing=dict(mode='automatic-regions',allowed_layer='F.Cu',automatic_region_policy={'name':'test'},targets=self.targets)
        self.previous=dict(status='complete',source_files_untouched=True,source_board=str(self.board),source_hashes=source_files(self.board),discovery=self.listing,preflight={'measurement':{'snapshot':self.native}})
        self.inspection=self.root/'inspection.json';write_json(self.inspection,self.previous)
        self.args=Namespace(command='batch',board=self.board,inspection=self.inspection,out_dir=self.root/'batch',config=self.root/'unused',targets=[t['id'] for t in self.targets],auto_region=True,roi=None,layer='F.Cu',order='coordinated',profile='external-pour-check',signal_kind='ordinary-single-ended',candidate_strategy='auto',route_layers=['F.Cu'],max_new_vias=0,shove_nets=['W'],max_modified_tracks=3)
        self.calls=[];self.inspections=0;self.mutate=None;self.decisions=[];self.cancel=False

    def fake_child(self,args):
        self.calls.append(args);args.out_dir.mkdir()
        if args.command=='inspect':
            self.inspections+=1;result=copy.deepcopy(self.previous)
            result.update(source_board=str(args.board),source_hashes=source_files(args.board))
            if self.mutate:self.mutate(result,self.inspections)
        else:
            destination=args.out_dir/'candidate/board.kicad_pcb';destination.parent.mkdir()
            destination.write_text(args.board.read_text()+'\n');destination.with_suffix('.kicad_pro').write_text('{}')
            result=dict(status='accepted',accepted=True,exit_code=0,source_board=str(args.board),source_hashes=source_files(args.board),candidate=str(destination),candidate_hashes=source_files(destination))
        write_json(args.out_dir/'result.json',result);return 0

    def scheduler(self,targets,native,layer,**kw):
        if self.cancel:raise Halt('cancelled','cancelled during coordination')
        chosen,evidence=choose_next(targets,native,layer,**kw)
        self.decisions.append((copy.deepcopy(targets),copy.deepcopy(native),chosen['id']))
        return chosen,evidence

    def run_batch(self):
        with patch('pcb_local.cli.workflow',side_effect=self.fake_child),patch('pcb_local.queue_coordination.choose_next',side_effect=self.scheduler):
            code=batch_workflow(self.args)
        return code,json.loads((self.args.out_dir/'result.json').read_text())

    def test_fresh_dynamic_choices_once_no_extra_inspection_and_same_authority(self):
        # First current snapshot removes all third-target templates so conflict
        # coordination picks narrow. The second fresh copper blocks flexible,
        # while the third channel becomes available; recomputation picks third.
        def mutate(result,round):
            native=result['preflight']['measurement']['snapshot']
            if round==1:
                native['items']['third-wall']=dict(kind='segment',uuid='third-wall',net='W',copper_layers=['F.Cu'],start_mm=[25,8],end_mm=[25,16],width_mm=.2)
            elif round==2:
                native['items']['flex-wall']=dict(kind='segment',uuid='flex-wall',net='W',copper_layers=['F.Cu'],start_mm=[-2,0],end_mm=[12,0],width_mm=.2)
        self.mutate=mutate;frozen=copy.deepcopy(self.targets);hashes=source_files(self.board)
        code,report=self.run_batch();self.assertEqual(code,0)
        repairs=[a for a in self.calls if a.command=='repair']
        self.assertEqual([a.target for a in repairs],['narrow','third','flexible'])
        self.assertEqual(len(repairs),len({a.target for a in repairs}))
        self.assertEqual(self.inspections,3);self.assertEqual(len(self.calls),6)
        self.assertEqual([d[2] for d in self.decisions],['narrow','third','flexible'])
        self.assertIn('flex-wall',self.decisions[1][1]['items'])
        self.assertNotIn('third-wall',self.decisions[1][1]['items'])
        self.assertEqual(report['execution_order'],[a.target for a in repairs])
        self.assertEqual({t['id']:t for t in frozen},{r['target']['id']:r['target'] for r in report['items']})
        self.assertEqual(self.targets,frozen);self.assertEqual(source_files(self.board),hashes)
        for a in repairs:
            self.assertEqual(a.shove_nets,['W']);self.assertEqual(a.max_modified_tracks,3)
            self.assertEqual(a.max_new_vias,0);self.assertEqual(a.route_layers,['F.Cu']);self.assertIsNone(a.roi)
        for decision in report['coordination']:
            self.assertEqual(decision['extra_attempts'],0);self.assertEqual(decision['extra_native_inspections'],0)
        check_chain(report)

    def test_cancel_during_coordination_starts_no_repair(self):
        self.cancel=True;code,report=self.run_batch()
        self.assertEqual(code,130);self.assertEqual(report['status'],'cancelled')
        self.assertEqual(len(self.calls),1);self.assertTrue(all(r['status']=='not_run' for r in report['items']))

    def test_changed_target_is_not_rescheduled_as_new_task(self):
        def mutate(result,round):
            result['discovery']['targets'][0]['region_mm']=[-3,-4,12,4]
        self.mutate=mutate;code,report=self.run_batch()
        self.assertEqual(code,0)
        self.assertNotIn('flexible',[a.target for a in self.calls if a.command=='repair'])
        self.assertEqual(next(r['status'] for r in report['items'] if r['target']['id']=='flexible'),'target_changed')


if __name__=='__main__':unittest.main()
