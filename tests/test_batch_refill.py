import copy
import json
from argparse import Namespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_local.batch import batch_workflow, check_chain, bind_refill_selection
from pcb_local.jobs import write_json
from pcb_local.workspace import source_files


class BatchRefill(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.board = self.root / 'board.kicad_pcb'
        self.board.write_text('(kicad_pcb (net 1 "PLANE") (net 2 "SHIELD_FILL") '
            '(zone (net 1) (net_name "PLANE") (layer "F.Cu") (uuid "zone-a") '
            '(connect_pads (clearance .2)) (min_thickness .1) '
            '(polygon (pts (xy 0 0)(xy 9 0)(xy 9 9)(xy 0 9)))) '
            '(zone (net 2) (net_name "SHIELD_FILL") (layer "F.Cu") (uuid "zone-b") '
            '(connect_pads (clearance .2)) (min_thickness .1) '
            '(polygon (pts (xy 0 0)(xy 9 0)(xy 9 9)(xy 0 9)))))')
        self.board.with_suffix('.kicad_pro').write_text('{}')
        self.targets = [dict(id=n, net=n, pad_uuids=[n+'a', n+'b'],
            supported=True, region_mm=[0,0,5,5], pads=[]) for n in ('one','two')]
        self.zones = [dict(uuid=uid, net=net, layers=['F.Cu'], bounds_mm=[0,0,9,9],
                          local_clearance_mm=.2, min_thickness_mm=.1)
                      for uid,net in [('zone-a','PLANE'),('zone-b','SHIELD_FILL')]]
        self.native = dict(items={}, component_of={}, copper_layers=['F.Cu','B.Cu'],
            zone_refill_choices=copy.deepcopy(self.zones),
            routing_rules=dict(board_minima_mm={'clearance':.2},
                netclasses_mm={z['net']:{'clearance':.2, 'track_width':.3} for z in self.zones}))
        for target in self.targets:
            for index, uid in enumerate(target['pad_uuids']):
                self.native['items'][uid] = dict(kind='pad', uuid=uid, net=target['net'],
                    position_mm=[1+index*2,2], copper_layers=['F.Cu'])
                self.native['component_of'][uid] = uid
        self.listing = dict(mode='automatic-regions', allowed_layer='F.Cu',
            automatic_region_policy={'name':'test'}, targets=self.targets)
        self.inspection = self.root / 'inspection.json'
        self.previous = dict(status='complete', source_files_untouched=True,
            source_board=str(self.board), source_hashes=source_files(self.board),
            discovery=self.listing, zone_refill_choices=copy.deepcopy(self.zones),
            preflight={'measurement':{'snapshot':self.native}})
        write_json(self.inspection, self.previous)
        self.args = Namespace(command='batch', board=self.board, inspection=self.inspection,
            out_dir=self.root/'batch', config=self.root/'unused.json', targets=['one','two'],
            auto_region=True, roi=None, layer='F.Cu', profile='external-pour-check',
            signal_kind='ordinary-single-ended', candidate_strategy='pns',
            route_layers=None, max_new_vias=0, refill_zone=['zone-a','zone-b'])
        self.calls = []
        self.fresh_change = None
        self.child_change = None

    def fake_child(self, args):
        self.calls.append(args); args.out_dir.mkdir()
        if args.command == 'inspect':
            result = copy.deepcopy(self.previous)
            result.update(source_board=str(args.board), source_hashes=source_files(args.board))
            if self.fresh_change:
                self.fresh_change(result, args)
        else:
            candidate = args.out_dir/'candidate'/'board.kicad_pcb'
            candidate.parent.mkdir()
            candidate.write_text(args.board.read_text()+'\n')
            candidate.with_suffix('.kicad_pro').write_text('{}')
            result = dict(status='accepted', accepted=True, exit_code=0,
                source_board=str(args.board), source_hashes=source_files(args.board),
                candidate=str(candidate), candidate_hashes=source_files(candidate),
                task={'zone_refill':dict(schema_version=1,zone_uuids=list(args.refill_zone))},
                refill_operation_selection={'frozen_uuids':['old-line'], 'retained_uuids':[],
                    'tracks':[{'uuid':'old-line','reason':'boundary'}]})
            if self.child_change:
                self.child_change(result, args)
        write_json(args.out_dir/'result.json', result)
        return 0

    def run_batch(self):
        with patch('pcb_local.cli.workflow', side_effect=self.fake_child):
            code = batch_workflow(self.args)
        return code, json.loads((self.args.out_dir/'result.json').read_text())

    def save_previous(self):
        write_json(self.inspection, self.previous)

    def test_cli_batch_accepts_repeated_zone_arguments(self):
        from pcb_local.cli import main
        with patch('pcb_local.batch.batch_workflow', return_value=0) as call:
            self.assertEqual(main(['batch','--board',str(self.board),'--auto-region',
                '--layer','F.Cu','--out-dir',str(self.root/'unused'), '--inspection',str(self.inspection),
                '--targets','one','two','--signal-kind','ordinary-single-ended',
                '--candidate-strategy','pns','--refill-zone','zone-a','--refill-zone','zone-b']), 0)
        self.assertEqual(call.call_args.args[0].refill_zone, ['zone-a','zone-b'])

    def test_same_explicit_set_reaches_every_child_and_report(self):
        code, report = self.run_batch()
        self.assertEqual(code, 0)
        repairs = [a for a in self.calls if a.command == 'repair']
        self.assertEqual([a.refill_zone for a in repairs], [['zone-a','zone-b']]*2)
        self.assertTrue(all(a.refill_zone is None for a in self.calls if a.command == 'inspect'))
        self.assertEqual(report['zone_refill']['zone_uuids'], ['zone-a','zone-b'])
        self.assertEqual(len(report['accepted_chain']), 2)
        self.assertTrue(report['zone_refill_binding']['bounds_precheck_only'])
        self.assertTrue(report['zone_refill_binding']['native_filled_intersection_required_per_child'])
        self.assertEqual(report['items'][1]['refill_operation_selection']['frozen_uuids'], ['old-line'])
        self.assertEqual(json.loads((self.args.out_dir/'request.json').read_text())['zone_refill'], report['zone_refill'])
        check_chain(report)

    def test_all_targets_are_checked_before_any_child_starts(self):
        self.targets[1]['net'] = 'PLANE'
        for uid in self.targets[1]['pad_uuids']:
            self.native['items'][uid]['net'] = 'PLANE'
        self.save_previous()
        code, report = self.run_batch()
        self.assertEqual(code, 20); self.assertEqual(self.calls, [])
        self.assertIn('foreign', report['reason'])

    def test_malformed_endpoint_is_rejected_before_queue(self):
        self.targets[1]['endpoints'] = [{'kind':'via'}]
        self.save_previous(); code, report = self.run_batch()
        self.assertEqual(code, 20); self.assertEqual(self.calls, [])
        self.assertIn('two bound original endpoints', report['reason'])

    def test_native_bound_track_and_via_targets_can_select_refill(self):
        from pcb_local.endpoints import catalog
        target = self.targets[1]
        self.native['items']['track'] = dict(kind='segment', uuid='track', net=target['net'],
            start_mm=[1,2], end_mm=[2,2], copper_layers=['F.Cu'])
        self.native['items']['via'] = dict(kind='via', uuid='via', net=target['net'],
            position_mm=[3,2], copper_layers=['F.Cu','B.Cu'], standard_through=True)
        self.native['component_of'].update(track='track', via='via')
        available = catalog(self.native, 'F.Cu')
        target['endpoints'] = [available['track:end'], available['via:center']]
        target['pad_uuids'] = []
        binding = bind_refill_selection(self.args, self.previous, self.board, self.targets, ['zone-a'])
        self.assertEqual(binding['zone_uuids'], ['zone-a'])
        target['endpoints'][0]['position_mm'] = [2.001,2]
        with self.assertRaises(ValueError):
            bind_refill_selection(self.args, self.previous, self.board, self.targets, ['zone-a'])

    def test_missing_zone_and_duplicated_selection_are_rejected(self):
        self.args.refill_zone = ['zone-a','zone-a']
        code, report = self.run_batch()
        self.assertEqual(code, 20); self.assertEqual(self.calls, [])
        with self.assertRaises(ValueError):
            bind_refill_selection(self.args, self.previous, self.board, self.targets, ['missing'])

    def test_displayed_choice_must_match_bound_native_snapshot(self):
        self.previous['zone_refill_choices'][0]['local_clearance_mm'] = .1
        self.save_previous(); code, report = self.run_batch()
        self.assertEqual(code, 20); self.assertEqual(self.calls, [])

    def test_locked_authored_zone_cannot_be_forged_as_unlocked_choice(self):
        self.board.write_text(self.board.read_text().replace('(uuid "zone-a")','(uuid "zone-a") (locked yes)'))
        self.previous['source_hashes'] = source_files(self.board); self.save_previous()
        code, report = self.run_batch()
        self.assertEqual(code, 20); self.assertEqual(self.calls, [])
        self.assertIn('locked', report['reason'])

    def test_layer_bounds_and_new_via_policy_prechecks(self):
        for field, value in [('candidate_strategy','krt'),('max_new_vias',1),('route_layers',['F.Cu','B.Cu'])]:
            args = copy.deepcopy(self.args); setattr(args,field,value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                bind_refill_selection(args,self.previous,self.board,self.targets,['zone-a'])
        for key, value in [('layers',['B.Cu']),('bounds_mm',[6,6,8,8])]:
            report=copy.deepcopy(self.previous)
            for choices in (report['zone_refill_choices'],report['preflight']['measurement']['snapshot']['zone_refill_choices']):
                choices[0][key]=value
            with self.subTest(key=key), self.assertRaises(ValueError):
                bind_refill_selection(self.args,report,self.board,self.targets,['zone-a'])

    def test_layered_binding_has_exact_per_zone_layers(self):
        args=copy.deepcopy(self.args);args.candidate_strategy='auto'
        args.route_layers=['F.Cu','B.Cu'];args.max_new_vias=2
        report=copy.deepcopy(self.previous)
        report['preflight']['measurement']['snapshot']['copper_layers']=['F.Cu','B.Cu']
        for choices in (report['zone_refill_choices'],report['preflight']['measurement']['snapshot']['zone_refill_choices']):
            choices[1]['layers']=['B.Cu']
        bound=bind_refill_selection(args,report,self.board,self.targets,['zone-a','zone-b'])
        self.assertEqual(bound['permission'],dict(schema_version=2,zones=[
            dict(uuid='zone-a',layers=['F.Cu']),dict(uuid='zone-b',layers=['B.Cu'])]))

    def test_fill_bounds_can_change_without_definition_identity_drift(self):
        def change(report, args):
            report['zone_refill_choices'][0]['bounds_mm'] = [-1,-1,10,10]
            report['preflight']['measurement']['snapshot']['zone_refill_choices'][0]['bounds_mm'] = [-1,-1,10,10]
        self.fresh_change=change
        code, report=self.run_batch()
        self.assertEqual(code,0);self.assertEqual(report['batch_outcome'],'all_selected_resolved')

    def test_native_zone_rule_drift_stops_before_repair(self):
        def change(report,args):
            for choices in (report['zone_refill_choices'],report['preflight']['measurement']['snapshot']['zone_refill_choices']):
                choices[0]['min_thickness_mm']=.15
        self.fresh_change=change
        code,report=self.run_batch()
        self.assertEqual(code,31);self.assertFalse(report['accepted'])
        self.assertEqual(len(self.calls),1)

    def test_zone_disappearing_after_first_success_stops_without_dropping_it(self):
        def change(report,args):
            if args.out_dir.name.startswith('02'):
                report['zone_refill_choices']=report['zone_refill_choices'][1:]
                report['preflight']['measurement']['snapshot']['zone_refill_choices']=report['preflight']['measurement']['snapshot']['zone_refill_choices'][1:]
        self.fresh_change=change
        code,report=self.run_batch()
        self.assertEqual(code,31);self.assertFalse(report['accepted'])
        self.assertEqual(len(report['accepted_chain']),1)
        self.assertEqual(len([x for x in self.calls if x.command=='repair']),1)

    def test_child_cannot_silently_omit_refill_authorization(self):
        self.child_change=lambda report,args:report['task'].pop('zone_refill')
        code,report=self.run_batch()
        self.assertEqual(code,31);self.assertFalse(report['accepted'])
        self.assertEqual(len(report['accepted_chain']),0)
        self.assertEqual(report['items'][0]['status'],'protection_violation')

    def test_definition_hash_ignores_fill_cache_but_not_outline(self):
        baseline=bind_refill_selection(self.args,self.previous,self.board,self.targets,['zone-a'])
        text=self.board.read_text()
        self.board.write_text(text.replace('(uuid "zone-a")', '(uuid "zone-a") (filled_polygon (layer "F.Cu") (pts (xy 1 1)(xy 2 1)(xy 2 2)))'))
        self.assertEqual(baseline,bind_refill_selection(self.args,self.previous,self.board,self.targets,['zone-a']))
        self.board.write_text(text.replace('(xy 9 9)', '(xy 8 9)',1))
        self.assertNotEqual(baseline,bind_refill_selection(self.args,self.previous,self.board,self.targets,['zone-a']))


if __name__ == '__main__':
    unittest.main()
