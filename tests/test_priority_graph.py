"""Priority policy bookkeeping, with separate real native gate evidence."""
import copy
import itertools
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from pcb_local.priority_graph import PriorityGraph, copper_crossings
from pcb_local.joint_repair import JointCoordinator
from pcb_local.jobs import write_json
from pcb_local.batch import check_chain
import test_batch


def rejected_verification(uuid='earlier-track'):
    return dict(verification_complete=True, accepted=False,
        checks=dict(existing_components_preserved=True, rules_unchanged=True,
                    source_files_untouched=True, additions_within_permission=True,
                    target_connected=True, full_drc_no_new_or_changed=False),
        drc={'sections': {'violations': {'new_or_changed': [{'items': [{'uuid': uuid}]}]}}})


class PriorityGraphTests(unittest.TestCase):
    def parsed_segment(self, a, b, net='A', layer='F.Cu'):
        from pcb_local.validation import parse_sexpr
        node = parse_sexpr('(segment (start %s %s) (end %s %s) (width .2) (layer "%s") (net 1) (uuid "s"))' % (*a, *b, layer))[0]
        return {'routing': {'s': node}, 'identities': {'s': {'net': net}}}

    def test_exact_integer_copper_witness_ignores_nonconflicts(self):
        a = self.parsed_segment((0,0), (10,0))
        b = self.parsed_segment((5,-1), (5,1), 'B')
        self.assertEqual(len(copper_crossings(a, ['s'], b, ['s'])), 1)
        for other in [self.parsed_segment((5,-1),(5,1),'A'),
                      self.parsed_segment((5,-1),(5,1),'B','B.Cu'),
                      self.parsed_segment((5,.000001),(5,1),'B'),
                      self.parsed_segment((11,0),(12,0),'B')]:
            self.assertEqual(copper_crossings(a, ['s'], other, ['s']), [])
        # Collinear overlap and endpoint contact are physical copper overlap.
        for pair in [((9,0),(11,0)), ((10,0),(11,0))]:
            self.assertEqual(len(copper_crossings(a,['s'],self.parsed_segment(*pair,'B'),['s'])),1)

    def test_transitive_closure_and_cycle_refusal(self):
        graph = PriorityGraph.empty(['a', 'b', 'c', 'd'])
        graph = graph.extend('c', 'a').extend('a', 'd')
        self.assertEqual(graph.evidence()['d'], ['a', 'c'])
        self.assertEqual(graph.order(), ['b', 'c', 'a', 'd'])
        for a, b in [('d', 'c'), ('c', 'd'), ('a', 'a')]:
            self.assertIsNone(graph.extend(a, b))

    def test_all_four_target_complete_orders_preserve_relations(self):
        for order in itertools.permutations('abcd'):
            graph = PriorityGraph.empty('abcd')
            for a, b in zip(order, order[1:]): graph = graph.extend(a, b)
            self.assertEqual(graph.order(), list(order))
            for a, b in zip(order, order[1:]): self.assertIsNone(graph.extend(b, a))

    def test_parent_graph_not_mutated_and_duplicate_closures_identical(self):
        root = PriorityGraph.empty('abc')
        one = root.extend('a', 'b').extend('b', 'c')
        two = root.extend('b', 'c').extend('a', 'b')
        self.assertEqual(one.key(), two.key())
        self.assertTrue(all(not x for x in root.ancestors))

    def test_copper_pair_search_can_cancel_during_large_inner_loop(self):
        from pcb_local.jobs import Halt
        left=self.parsed_segment((0,0),(10,0))
        base=self.parsed_segment((5,1),(5,2),'B')
        right={'routing':{str(i):base['routing']['s'] for i in range(300)},
               'identities':{str(i):{'net':'B'} for i in range(300)}}
        calls=[]
        def cancel():
            calls.append(None)
            if len(calls)==305: raise Halt('cancelled','cancelled during pair search')
        with self.assertRaises(Halt) as raised:
            copper_crossings(left,['s'],right,right['routing'],check=cancel)
        self.assertEqual(raised.exception.status,'cancelled')
        self.assertEqual(len(calls),305)

    def test_copper_witness_is_bounded_even_when_every_pair_intersects(self):
        base=self.parsed_segment((0,0),(10,0))
        left={'routing':{str(i):base['routing']['s'] for i in range(100)},
              'identities':{str(i):{'net':'A'} for i in range(100)}}
        right=copy.deepcopy(left)
        right['identities']={str(i):{'net':'B'} for i in range(100)}
        self.assertEqual(len(copper_crossings(left,left['routing'],right,right['routing'])),1)

    def test_planning_generation_and_total_deadlines_stop_before_geometry_returns(self):
        from pcb_local.jobs import Halt
        for stage in ('generation','total'):
            with self.subTest(stage=stage):
                coordinator=JointCoordinator(['a','b'],'pns')
                coordinator.budgets['a'].used[stage]=coordinator.budgets['a'].limits[stage]-.1
                with patch('pcb_local.joint_repair.time.monotonic',return_value=0.) as clock:
                    with self.assertRaises(Halt) as raised:
                        with coordinator.planning('a','bounded-geometry'):
                            clock.return_value=.2
                            coordinator.check_planning(lambda:None)
                self.assertEqual(raised.exception.status,'timed_out')
                self.assertEqual(coordinator.planning_checks,[])

    def test_changed_priority_does_not_reuse_downstream_failure_exclusions(self):
        coordinator = JointCoordinator(['a','b'], 'pns')
        coordinator.exclusions[coordinator.exclusion_key('a','/original')] = {'old-route'}
        self.assertEqual(coordinator.excluded('a','/original'), {'old-route'})
        coordinator.priority = coordinator.priority.extend('b','a')
        self.assertEqual(coordinator.excluded('a','/original'), set())
        # Returning to the unchanged execution context preserves its pruning.
        coordinator.priority = PriorityGraph.empty(['a','b']).extend('a','b')
        self.assertEqual(coordinator.excluded('a','/original'), {'old-route'})

    def test_nested_probe_charges_verification_only_once(self):
        from argparse import Namespace
        coordinator = JointCoordinator(['a', 'b'], 'pns')
        args = Namespace(command='repair', board=Path('/original'), out_dir=Path('/unused'),
                         _parent_check=lambda: None, _priority_probe=True)
        def workflow(call_args):
            call_args._shared_budget.charge('generation', 2.)
            call_args._shared_budget.charge('verification', 7.)
            return 0
        # Planning spans 12 s; nested full workflow spans 10 s. Only the
        # remaining 2 s is generation planning, never the validation's 7 s.
        with patch('pcb_local.joint_repair.time.monotonic', side_effect=[0., 1., 11., 11.5, 12.]):
            with coordinator.planning('a', 'probe'):
                coordinator.call(workflow, args, 'a')
                coordinator.check_planning(lambda: None)
        self.assertEqual(coordinator.budgets['a'].used,
                         {'generation': 4., 'verification': 7., 'total': 12.})
        self.assertEqual(coordinator.budgets['b'].used,
                         {'generation': 0., 'verification': 0., 'total': 0.})

    def test_only_complete_drc_conflict_can_create_priority(self):
        verify = rejected_verification()
        self.assertEqual(JointCoordinator.priority_conflicts({'verification': verify}), {'earlier-track'})
        for key in ['existing_components_preserved', 'rules_unchanged',
                    'source_files_untouched', 'additions_within_permission', 'target_connected']:
            bad = copy.deepcopy(verify); bad['checks'][key] = False
            self.assertEqual(JointCoordinator.priority_conflicts({'verification': bad}), set())
        for key in ['verification_complete', 'accepted']:
            bad = copy.deepcopy(verify); bad[key] = key == 'accepted'
            self.assertEqual(JointCoordinator.priority_conflicts({'verification': bad}), set())
        verify['checks'].pop('rules_unchanged')
        self.assertEqual(JointCoordinator.priority_conflicts({'verification': verify}), set())
        self.assertEqual(JointCoordinator.priority_conflicts({'pns': {'direct_path_obstacles': {
            'items': [{'uuid': 'earlier-track'}]}}}), set())


class PriorityBatchTests(unittest.TestCase):
    setUp = test_batch.BatchChecks.setUp
    run_batch = test_batch.BatchChecks.run_batch

    def fake_child(self, args):
        if args.command == 'repair':
            args._shared_budget.charge('generation', 2)
            args._shared_budget.charge('verification', 3)
            self.statuses[args.target] = ('rejected' if args.target == 'two' and
                '# one' in args.board.read_text() else 'accepted')
        code = test_batch.BatchChecks.fake_child(self, args)
        if args.command != 'repair': return code
        p = args.out_dir/'result.json'; result = json.loads(p.read_text())
        result['task_dir'] = str(args.out_dir)
        if result['accepted']:
            board = Path(result['candidate']); board.write_text(board.read_text() + '# ' + args.target + '\n')
            from pcb_local.workspace import source_files
            result['candidate_hashes'] = source_files(board)
            result['task'] = {'id': args.target}
            result['verification'] = dict(rejected_verification(), accepted=True)
            result['verification']['checks']['full_drc_no_new_or_changed'] = True
            result['verification']['drc'] = {}
            result['verification']['protection'] = {'added_routing': ['earlier-track']}
        else:
            result['task'] = {'id': args.target}
            result['verification'] = rejected_verification()
            result['preflight'] = {'measurement': {'snapshot': {'items': {'earlier-track': {'net': 'one'}}}}}
        write_json(p, result)
        return code

    def test_native_conflict_restarts_from_original_in_new_order(self):
        self.args.order = 'joint'; self.args.candidate_strategy = 'auto'
        code, report = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(report['counts'], {'accepted': 2})
        self.assertEqual(report['requested_order'], ['one', 'two'])
        self.assertEqual(report['execution_order'], ['two', 'one'])
        repairs = [a for a in self.calls if a.command == 'repair']
        self.assertEqual([a.target for a in repairs], ['one', 'two', 'two', 'one'])
        self.assertEqual(repairs[2].board, self.source)
        branch = report['joint_repair']['backtracks'][0]
        self.assertEqual(branch['alternative_kind'], 'priority')
        self.assertEqual(branch['retained_route_witnesses'], 0)
        self.assertEqual(report['joint_repair']['budgets']['one']['used']['verification'], 6)
        self.assertEqual(report['joint_repair']['max_backtracks'], 8)
        self.assertNotIn('01-repair', str(report['accepted_chain']))
        check_chain(report)

    def test_undemonstrated_corridor_conflict_cannot_reorder(self):
        original = self.fake_child
        def hint_only(args):
            code = original(args)
            if args.command == 'repair' and not self.statuses[args.target] == 'accepted':
                p = args.out_dir/'result.json'; result = json.loads(p.read_text())
                result.pop('verification'); result['pns'] = {'direct_path_obstacles': {'items': [{'uuid': 'earlier-track'}]}}
                result.pop('task')  # No safe identical task for an independent probe.
                write_json(p, result)
            return code
        self.fake_child = hint_only
        self.args.order = 'joint'; self.args.candidate_strategy = 'auto'
        code, report = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(report['counts'], {'accepted': 1, 'rejected': 1})
        self.assertEqual(report['execution_order'], ['one', 'two'])
        self.assertEqual(report['joint_repair']['priority_states'], [])

    def test_independent_probe_is_evidence_only_and_charged_to_same_target(self):
        original = self.fake_child
        def blocked(args):
            code = original(args)
            if args.command == 'repair' and self.statuses[args.target] == 'rejected':
                p=args.out_dir/'result.json'; result=json.loads(p.read_text())
                result['status']='no_progress';result['verification']['checks'].update(
                    full_drc_no_new_or_changed=True,target_connected=False)
                result['verification']['drc']={}
                result['pns']={'direct_path_obstacles': {'items': [{'uuid': 'earlier-track'}]}}
                write_json(p,result)
            return code
        self.fake_child=blocked
        self.args.order='joint';self.args.candidate_strategy='auto'
        # Geometry recognition itself is tested above; this checks orchestration.
        with patch('pcb_local.scope_candidate.read_board',return_value={}), patch('pcb_local.priority_graph.copper_crossings',return_value=[dict(left_uuid='earlier-track',right_uuid='probe-track')]):
            code,report=self.run_batch()
        self.assertEqual(code,0);self.assertEqual(report['counts'],{'accepted':2})
        self.assertEqual(report['execution_order'],['two','one'])
        events=report['joint_repair']['backtracks']
        self.assertEqual([e['alternative_kind'] for e in events],['priority_probe','priority'])
        self.assertNotIn('priority-probe',str(report['accepted_chain']))
        self.assertEqual(report['joint_repair']['budgets']['two']['used']['verification'],9)
        self.assertEqual(len([a for a in self.calls if a.command=='repair']),5)
        check_chain(report)

    def test_independent_probe_protection_failure_stops_group(self):
        original = self.fake_child
        def protected(args):
            code = original(args)
            if args.command != 'repair': return code
            path=args.out_dir/'result.json'; result=json.loads(path.read_text())
            if getattr(args, '_priority_probe', False):
                result.update(status='protection_violation', accepted=False,
                              reason='Original connectivity damaged in probe')
                write_json(path,result);return 40
            if self.statuses[args.target]=='rejected':
                result['status']='no_progress';result['verification']['checks'].update(
                    full_drc_no_new_or_changed=True,target_connected=False)
                result['verification']['drc']={}
                result['pns']={'direct_path_obstacles':{'items':[{'uuid':'earlier-track'}]}}
                write_json(path,result)
            return code
        self.fake_child=protected;self.args.order='joint';self.args.candidate_strategy='auto'
        code,report=self.run_batch()
        self.assertNotEqual(code,0)
        self.assertEqual(report['status'],'protection_violation')
        self.assertEqual([e['alternative_kind'] for e in report['joint_repair']['backtracks']],['priority_probe'])
        self.assertNotIn('priority-probe',str(report['accepted_chain']))

    def test_priority_restart_keeps_original_target_budget(self):
        original = self.fake_child
        def expensive(args):
            code = original(args)
            if args.command == 'repair' and args.target == 'two':
                args._shared_budget.charge('generation', 57.6)
            return code
        self.fake_child = expensive
        self.args.order = 'joint'; self.args.candidate_strategy = 'auto'
        code, report = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(report['execution_order'], ['one', 'two'])
        self.assertEqual(report['joint_repair']['backtracks'], [])
        self.assertIn('original_target_budget_exhausted', str(report['joint_repair']['priority_pruned']))


if __name__ == '__main__': unittest.main()
