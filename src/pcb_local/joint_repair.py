"""Bounded candidate-branch backtracking for a small selected repair group.

Every branch is an ordinary fully validated chain. Backtracking discards a
tentative chain suffix; it never authorizes deleting copper from an accepted
input. This is candidate search, not a negotiated-congestion routing kernel.
"""
import copy
from contextlib import contextmanager
import math
import json
from pathlib import Path
import time

from .jobs import Halt
from .priority_graph import PriorityGraph


class JointBudget:
    """One original target's cumulative budget, shared across all branches."""
    limits = {'generation': 60., 'verification': 120., 'total': 240.}

    def __init__(self):
        self.used = {stage: 0. for stage in self.limits}

    def remaining(self, stage):
        return max(0., self.limits[stage] - self.used[stage])

    def charge(self, stage, seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError('Invalid cumulative joint budget charge')
        self.used[stage] += seconds

    def check(self):
        if self.remaining('total') <= 0:
            raise Halt('timed_out', 'Original target cumulative whole-task budget exhausted')

    def available(self):
        return all(self.remaining(stage) > .5 for stage in self.limits)


class JointCoordinator:
    """Keep immutable branch reports and choose a different earlier route."""
    max_backtracks = 8

    def __init__(self, ids, strategy):
        if not 2 <= len(ids) <= 4:
            raise Halt('unsupported', 'Joint candidate backtracking supports 2–4 selected targets')
        if strategy not in ('pns', 'auto'):
            raise Halt('unsupported', 'Joint candidate backtracking requires PNS or auto candidate pools')
        self.budgets = {uid: JointBudget() for uid in ids}
        self.exclusions = {}
        self.frames = []
        self.best = None
        self.failures = {}
        self.conflict_history = {}
        self.last_conflicts = {}
        self.last_failure_target = None
        self.last_priority_objects = set()
        self.priority = PriorityGraph.empty(ids)
        self.priority_seen = {self.priority.key()}
        self.priority_pending = []
        self.initial = None
        self.original_inspection = None
        self.repair_templates = {}
        self.probed_targets = set()
        self.last_task = None
        self.nested_work_seconds = 0.
        self.planning_checks = []
        self.evidence = dict(schema_version=4, policy='native-conflict-priority-and-candidate-backtracking-v4',
            target_ids=list(ids), visits=[], backtracks=[], budgets={},
            priority_states=[], priority_relations=self.priority.evidence(),
            max_backtracks=self.max_backtracks, status='running',
            limitations='Native DRC conflicts may branch target priorities; every changed order restarts from the original board with no reused candidate chain. Corridor hints only rank ordinary backtracking. Untried strategies are not validated candidates. Regeneration consumes the original per-target budget. No fallback after protection/incomplete-validation/cancellation failures.')

    @contextmanager
    def planning(self, uid, operation):
        """Charge the triggering target for planning outside its workflow call."""
        start=time.monotonic();nested_before=self.nested_work_seconds;failed=False
        def check_remaining():
            elapsed=max(0.,time.monotonic()-start-(self.nested_work_seconds-nested_before))
            budget=self.budgets[uid]
            if elapsed >= budget.remaining('generation'):
                raise Halt('timed_out','Original target generation budget exhausted during joint planning')
            if elapsed >= budget.remaining('total'):
                raise Halt('timed_out','Original target whole-task budget exhausted during joint planning')
        self.planning_checks.append(check_remaining)
        try:
            yield
        except BaseException:
            failed=True
            raise
        finally:
            self.planning_checks.pop()
            # Nested workflow calls charge their own generation/verification
            # and whole-task time. Only orchestration outside them is planning.
            elapsed=max(0.,time.monotonic()-start-(self.nested_work_seconds-nested_before))
            budget=self.budgets[uid]
            budget.charge('generation',elapsed);budget.charge('total',elapsed)
            self.evidence.setdefault('planning',[]).append(dict(target_id=uid,operation=operation,
                generation_seconds=elapsed,total_seconds=elapsed))
            self.evidence['budgets']={key:dict(limits=value.limits,used=dict(value.used)) for key,value in self.budgets.items()}
            if not failed:
                budget.check()
                if budget.used['generation']>budget.limits['generation']:
                    raise Halt('timed_out','Original target generation budget exhausted during joint planning')

    def check_planning(self, parent_check):
        parent_check()
        for check in self.planning_checks:
            check()

    @staticmethod
    def snapshot(report):
        return copy.deepcopy({key: report[key] for key in
                              ('items', 'accepted_chain', 'candidate', 'execution_order')})

    def before(self, report, index, board):
        checkpoint = dict(index=index, board=str(board), snapshot=self.snapshot(report))
        if self.initial is None:
            if index != 1:
                raise ValueError('Joint priority baseline must precede the first target')
            self.initial = copy.deepcopy(checkpoint)
        return checkpoint

    def exclusion_key(self, uid, board):
        # A failed suffix in one execution order must not suppress a route
        # when a different order changes the downstream dependency context.
        return uid, str(board), tuple(self.priority.order())

    def excluded(self, uid, board):
        return set(self.exclusions.get(self.exclusion_key(uid, board), set()))

    def call(self, workflow, args, uid):
        budget = self.budgets[uid]
        budget.check()
        if getattr(args, 'command', None) == 'repair' and not getattr(args, '_priority_probe', False):
            self.repair_templates[uid] = workflow, copy.copy(args)
        args._shared_budget = budget
        args._candidate_exclusions = self.excluded(uid, args.board)
        parent_check = args._parent_check
        def check():
            parent_check()
            budget.check()
        args._parent_check = check
        start = time.monotonic()
        try:
            code = workflow(args)
        finally:
            seconds = time.monotonic() - start
            budget.charge('total', seconds)
            self.nested_work_seconds += seconds
            self.evidence['budgets'] = {key: dict(limits=value.limits, used=dict(value.used))
                                        for key, value in self.budgets.items()}
        budget.check()
        if any(budget.used[stage] > budget.limits[stage] for stage in ('generation', 'verification')):
            raise Halt('timed_out', 'Original target cumulative stage budget exceeded')
        if (code == 0 and getattr(args, 'command', None) == 'inspect' and self.initial is not None
                and str(args.board) == self.initial['board'] and self.original_inspection is None):
            self.original_inspection = Path(args.out_dir) / 'result.json'
        return code

    def record(self, checkpoint, report, child):
        uid=report['items'][checkpoint['index']-1]['target']['id']
        with self.planning(uid,'record-candidate-or-failure-feedback'):
            return self._record(checkpoint,report,child)

    def _record(self, checkpoint, report, child):
        index = checkpoint['index']
        uid = report['items'][index-1]['target']['id']
        pool = child.get('candidate_pool') or {}
        strategies = child.get('strategy_pool') or {}
        self.evidence['visits'].append(dict(target_id=uid, source_board=checkpoint['board'],
            task_dir=child.get('task_dir'), status=child['status'],
            selected_fingerprint=pool.get('selected_fingerprint') or strategies.get('selected_candidate_fingerprint'),
            selected_strategy=strategies.get('selected_token'),
            distinct_candidates=len(pool.get('candidates', [])) or int(bool(strategies.get('selected_candidate_fingerprint'))),
            untried_strategies=sum(row.get('status')=='untried' for row in strategies.get('branches',[]))))
        if child['status'] == 'accepted' and child.get('accepted') and child.get('exit_code') == 0:
            from .conflict_feedback import changed_objects
            self.frames.append(dict(checkpoint, target_id=uid, pool=copy.deepcopy(pool),strategies=copy.deepcopy(strategies),
                changed_uuids=changed_objects(child), candidate=child.get('candidate'),
                candidate_hashes=copy.deepcopy(child.get('candidate_hashes')),
                additive_fixed=bool(child.get('verification', {}).get('verification_complete')) and
                    not child.get('task', {}).get('modification') and not child.get('task', {}).get('zone_refill')))
            resolved = sum(row['status'] in ('accepted', 'already_connected') for row in report['items'])
            if self.best is None or resolved > self.best['resolved']:
                self.best = dict(resolved=resolved, snapshot=self.snapshot(report))
        else:
            self.last_failure_target=uid
            self.last_task=copy.deepcopy(child.get('task'))
            self.priority_witness_kind = 'complete_native_drc_conflict'
            self.failures[uid] = copy.deepcopy(report['items'][index-1])
            from .conflict_feedback import collect
            feedback=collect(child)
            if child.get('runtime',{}).get('conflict_feedback',True) is False:
                feedback=dict(feedback,objects=[],guidance_disabled=True)
            self.last_conflicts={row['uuid']:row for row in feedback['objects']}
            self.last_priority_objects = self.priority_conflicts(child) & set(self.last_conflicts)
            for obj in self.last_conflicts:
                self.conflict_history[obj]=self.conflict_history.get(obj,0)+1
            self.evidence['visits'][-1]['conflict_feedback']=feedback
            self.evidence['visits'][-1]['priority_conflict_objects']=sorted(self.last_priority_objects)

    def _probe_independent_conflict(self, report):
        """One bounded independent candidate can witness a blocked route pair.

        Only attempted after ordinary candidate backtracking is exhausted. The
        probe counts against the SAME target budgets and eight-backtrack cap.
        Its accepted result is never appended to the current candidate chain.
        """
        uid = self.last_failure_target
        if (uid in self.probed_targets or uid not in self.repair_templates or
                self.original_inspection is None or not self.frames or not self.last_task or
                self.last_task.get('modification') or self.last_task.get('zone_refill') or
                len(self.evidence['backtracks']) + 2 > self.max_backtracks or
                not all(b.available() for b in self.budgets.values())):
            return False
        eligible = [f for f in self.frames if f.get('additive_fixed') and
                    set(f.get('changed_uuids', [])) & set(self.last_conflicts)]
        if not eligible: return False
        self.probed_targets.add(uid)
        workflow, template = self.repair_templates[uid]
        args = copy.copy(template)
        args.board = Path(self.initial['board']); args.inspection = self.original_inspection
        args.out_dir = Path(template.out_dir).parent / ('priority-probe-%02d-repair' % len(self.probed_targets))
        args._priority_probe = True
        event = dict(target_id=uid, alternative_kind='priority_probe', source_board=str(args.board),
            task_dir=str(args.out_dir), acceptance_scope='Independent conflict witness only; never part of the current accepted chain')
        self.evidence['backtracks'].append(event)
        code = self.call(workflow, args, uid)
        child = json.loads((args.out_dir/'result.json').read_text())
        event['status'] = child['status']; event['exit_code'] = code
        if child['status'] not in ('accepted', 'no_progress', 'unsupported', 'rejected'):
            raise Halt(child['status'], 'Independent priority probe stopped: ' + child.get('reason', child['status']))
        if code or child['status'] != 'accepted' or not child.get('accepted') or child.get('task') != self.last_task:
            event['reason'] = 'No independently accepted identical task'; return False
        verify = child.get('verification', {})
        if not verify.get('accepted') or not verify.get('verification_complete') or not verify.get('checks') or not all(verify['checks'].values()):
            raise Halt('verification_incomplete', 'Priority probe lacks complete native acceptance')
        from .workspace import source_files
        from .scope_candidate import read_board
        from .priority_graph import copper_crossings
        candidate = Path(child['candidate'])
        if source_files(candidate) != child.get('candidate_hashes'):
            raise Halt('protection_violation', 'Priority probe candidate changed before conflict witness')
        check = lambda: self.check_planning(args._parent_check)
        check()
        probe = read_board(candidate)
        check()
        probe_ids = verify.get('protection', {}).get('added_routing', [])
        conflicts = []
        for frame in eligible:
            check()
            previous = Path(frame['candidate'])
            if source_files(previous) != frame['candidate_hashes']:
                raise Halt('protection_violation', 'Accepted prior route changed before priority witness')
            conflicts.extend(copper_crossings(read_board(previous), frame['changed_uuids'], probe, probe_ids, check=check))
        event['copper_crossings'] = conflicts
        event['native_probe_report'] = str(args.out_dir/'result.json')
        event['physical_impossibility_proven'] = False
        if not conflicts: return False
        self.last_priority_objects = {c['left_uuid'] for c in conflicts}
        self.priority_witness_kind = 'independently_native_accepted_copper_intersection'
        self._queue_priority(report)
        return True

    @staticmethod
    def priority_conflicts(child):
        """Only complete DRC-only rejections qualify, never corridor guesses.

        Protection, connectivity and scope must already have succeeded. The
        batch driver also stops those failures before calling the coordinator.
        This extra guard keeps the relation builder conservative in isolation.
        """
        required = {'existing_components_preserved', 'rules_unchanged',
                    'source_files_untouched', 'additions_within_permission'}
        drc_checks = {'full_drc_no_new_or_changed', 'supplemental_drc_no_new_or_changed'}
        found = set()
        contexts = [child] + [a.get('details', {}) for a in
            child.get('automatic_strategy', {}).get('attempts', [])
            if a.get('status') in ('rejected', 'no_progress')]
        for context in contexts:
            verify = context.get('verification', {})
            checks = verify.get('checks', {})
            if (not verify.get('verification_complete') or verify.get('accepted') or
                    not required <= checks.keys() or
                    any(value is not True for key, value in checks.items() if key not in drc_checks) or
                    not any(checks.get(key) is False for key in drc_checks)):
                continue
            for scope in ('drc', 'supplemental_drc'):
                for section in verify.get(scope, {}).get('sections', {}).values():
                    for issue in section.get('new_or_changed', []):
                        found.update(i['uuid'] for i in issue.get('items', []) if isinstance(i.get('uuid'), str))
        return found

    def _queue_priority(self, report):
        if self.initial is None or self.last_failure_target is None or not self.last_priority_objects:
            return
        failure = self.last_failure_target
        # An exact DRC participant must belong to an earlier accepted route.
        frames = [f for f in self.frames if
            set(f.get('changed_uuids', [])) & self.last_priority_objects]
        for frame in reversed(frames):
            earlier = frame['target_id']
            # Preserve the explored orientation as a resumable branch. Its
            # immutable validated prefix is reusable only in its SAME order.
            # The opposite orientation always discards the entire chain.
            for higher, lower in ((earlier, failure), (failure, earlier)):
                state = self.priority.extend(higher, lower)
                if state is None:
                    self.evidence.setdefault('priority_pruned', []).append(dict(
                        higher=higher, lower=lower, reason='cycle_or_existing_relation'))
                    continue
                if state.key() in self.priority_seen:
                    self.evidence.setdefault('priority_pruned', []).append(dict(
                        higher=higher, lower=lower, reason='duplicate_relation_state'))
                    continue
                self.priority_seen.add(state.key())
                current_order = [r['target']['id'] for r in report['items']]
                resume = state.order() == current_order
                branch = dict(state=state, higher=higher, lower=lower,
                    conflict_objects=sorted(set(frame.get('changed_uuids', [])) & self.last_priority_objects),
                    resume=resume, snapshot=self.snapshot(report) if resume else None,
                    frames=copy.deepcopy(self.frames) if resume else None)
                branch['witness_kind'] = getattr(self, 'priority_witness_kind', 'complete_native_drc_conflict')
                self.priority_pending.append(branch)
                self.evidence['priority_states'].append(dict(higher=higher, lower=lower,
                    relations=state.evidence(), order=state.order(), reuse_same_order=resume,
                    conflict_objects=branch['conflict_objects']))

    def _priority_backtrack(self, report):
        while self.priority_pending:
            branch = self.priority_pending.pop()
            state = branch['state']
            if branch['resume']:
                # A suspended unchanged context can resume ordinary candidate
                # backtracking; any altered ordering never enters this path.
                self.priority = state
                report.update(copy.deepcopy(branch['snapshot']))
                self.frames = copy.deepcopy(branch['frames'])
                self.evidence['priority_relations'] = state.evidence()
                retry = self._candidate_backtrack(report)
                if retry is not None:
                    return retry
                continue
            if not all(b.available() for b in self.budgets.values()):
                self.evidence.setdefault('priority_pruned', []).append(dict(
                    higher=branch['higher'], lower=branch['lower'], reason='original_target_budget_exhausted'))
                continue
            self.priority = state
            discarded = [f['target_id'] for f in self.frames]
            report.update(copy.deepcopy(self.initial['snapshot']))
            by_id = {r['target']['id']: r for r in report['items']}
            order = state.order()
            report['items'] = [by_id[uid] for uid in order]
            report['execution_order'] = order
            self.frames = []
            self.evidence['priority_relations'] = state.evidence()
            self.evidence.pop('termination', None)
            self.evidence['backtracks'].append(dict(
                target_id=branch['higher'], source_board=self.initial['board'],
                alternative_kind='priority', higher=branch['higher'], lower=branch['lower'],
                conflict_objects=branch['conflict_objects'], selection_reason=branch['witness_kind'],
                order=order, discarded_suffix=discarded, retained_route_witnesses=0))
            return 1, self.initial['board']
        return None

    def backtrack(self, report):
        uid=self.last_failure_target or (self.frames[-1]['target_id'] if self.frames else None)
        if uid is None:return self._backtrack(report)
        with self.planning(uid,'rank-and-select-backtrack'):
            return self._backtrack(report)

    def _backtrack(self, report):
        if len(self.evidence['backtracks']) >= self.max_backtracks:
            self.evidence['termination'] = 'branch_limit'
            return None
        self._queue_priority(report)
        # Newly witnessed conflicts get priority over trying another full
        # ordering's route pool. The pending explored orientation remains.
        if self.priority_pending and not self.priority_pending[-1]['resume']:
            retry = self._priority_backtrack(report)
            if retry is not None:
                return retry
        retry = self._candidate_backtrack(report)
        if retry is not None:
            return retry
        if not self.priority_pending and self._probe_independent_conflict(report):
            return self._priority_backtrack(report)
        return self._priority_backtrack(report)

    def _candidate_backtrack(self, report):
        def score(pos):
            matched=set(self.frames[pos].get('changed_uuids',[])) & set(self.last_conflicts)
            observed=sum('native_drc_participant' in self.last_conflicts[u]['kinds'] for u in matched)
            return observed, bool(matched), sum(self.conflict_history.get(u,0) for u in matched), pos
        ranked=sorted(range(len(self.frames)),key=score,reverse=True)
        for pos in ranked:
            frame = self.frames[pos]
            uid, board, pool = frame['target_id'], frame['board'], frame['pool']
            strategies=frame.get('strategies',{})
            selected = pool.get('selected_fingerprint') or strategies.get('selected_candidate_fingerprint')
            if not selected or not self.budgets[uid].available():
                continue
            excluded = self.exclusions.setdefault(self.exclusion_key(uid, board), set())
            excluded.add(selected)
            excluded.update(row['fingerprint'] for row in pool.get('candidates', [])
                            if row.get('status') in ('rejected', 'no_progress') and row.get('fingerprint'))
            # Do not repeat earlier failed strategy probes on the identical
            # baseline. These tokens do not authorize a route or a mutation.
            excluded.update(row['token'] for row in strategies.get('branches',[])
                            if row.get('token') and row.get('status') in
                            ('unsupported','no_progress','rejected','timed_out','error','excluded_branch'))
            alternatives = [row['fingerprint'] for row in pool.get('candidates', [])
                            if row.get('fingerprint') and row['fingerprint'] not in excluded]
            kind='candidate'
            if not alternatives and strategies.get('selected_token'):
                excluded.add(strategies['selected_token'])
                alternatives=[row['token'] for row in strategies.get('branches',[])
                              if row.get('status')=='untried' and row.get('token') and row['token'] not in excluded]
                kind='strategy'
            if not alternatives:
                continue
            self.evidence['backtracks'].append(dict(target_id=uid, source_board=board,
                excluded_fingerprints=sorted(excluded), alternatives=alternatives,
                alternative_kind=kind,
                conflict_objects=sorted(set(frame.get('changed_uuids',[])) & set(self.last_conflicts)),
                selection_reason='native_conflict_or_corridor_relevance' if score(pos)[:2]!=(0,False) else 'reverse_order_fallback',
                conflict_score=list(score(pos)[:-1]),
                discarded_suffix=[f['target_id'] for f in self.frames[pos:]]))
            report.update(copy.deepcopy(frame['snapshot']))
            self.frames = self.frames[:pos]
            return frame['index'], board
        self.evidence['termination'] = 'no_remaining_candidate_within_budget'
        return None

    def finish(self, report):
        """Keep the best verified partial chain when all safe branches fail."""
        resolved = sum(row['status'] in ('accepted', 'already_connected') for row in report['items'])
        if self.best is not None and resolved < self.best['resolved']:
            report.update(copy.deepcopy(self.best['snapshot']))
            for index, row in enumerate(report['items']):
                if row['status'] not in ('accepted', 'already_connected'):
                    report['items'][index] = copy.deepcopy(self.failures.get(row['target']['id'], row))
            self.evidence['restored_best_verified_partial'] = True
        done = all(row['status'] in ('accepted', 'already_connected') for row in report['items'])
        self.evidence['status'] = 'all_selected_resolved' if done else 'partial'
