"""Restore witnessed neighbour components after a bounded local rip-up.

All work is on disposable candidates, under the parent's generation deadline.
This is candidate construction; the original full-board gate remains required.
"""
import copy
import json
import math
from pathlib import Path

from .endpoints import catalog
from .engine import command
from .jobs import Halt
from .modification import witness
from .task_policy import permissions
from .validation import read_board,name
from .workspace import source_files


def restore(job,config,baseline,candidate,task,native,report,run,profile):
    try:
        return _restore(job,config,baseline,candidate,task,native,report,run,profile)
    except Exception as exc:
        if 'neighbour_restoration' in report:
            log=report['neighbour_restoration'];log.update(status=exc.status if isinstance(exc,Halt) else 'error',reason=str(exc))
            if log['steps'] and log['steps'][-1].get('status') == 'running':
                log['steps'][-1].update(status=log['status'], reason=str(exc))
            path=job.folder/'neighbour-restoration/restoration.json'
            if path.parent.is_dir():path.write_text(json.dumps(log,indent=2)+'\n')
        raise


def _restore(job, config, baseline, candidate, task, native, report, run, profile):
    spec = witness(native, task)
    groups = [[a for a in spec['anchors'] if native['component_of'][a] == component]
              for component in spec['components']]
    # The accepted branch must merge original components at each step. Extra
    # branches are bounded separately, inside the existing generation deadline.
    depth_bound = sum(max(0, len(group) - 1) for group in groups)
    alternative_limit = 8
    log = dict(original_anchor_groups=groups, steps=[], status='running',
               search=dict(policy='nearest-first bounded fixed-anchor backtracking',
                           extra_attempt_allowance=alternative_limit,
                           max_attempts=depth_bound + alternative_limit,
                           max_depth=depth_bound, backtracks=0),
               policy='Original fixed anchor components; original ROI/rules/permissions and shared generation deadline')
    report['neighbour_restoration'] = log
    folder = job.folder / 'neighbour-restoration'
    folder.mkdir()

    def save():
        (folder / 'restoration.json').write_text(json.dumps(log, indent=2) + '\n')

    save()
    before = read_board(baseline)
    policy = permissions(task)
    measurements = 0

    def checked_run(board, hashes, label, argv, **kwargs):
        # Even a timed-out command may have modified its source. Such output
        # must never become a reusable branch or be hidden by another attempt.
        try:
            return run(label, argv, **kwargs)
        finally:
            if source_files(board) != hashes:
                raise Halt('protection_violation', 'Neighbour operation changed its input')

    def measure(board, parent=None):
        nonlocal measurements
        index = measurements
        measurements += 1
        hashes = source_files(board)
        path = folder / ('native-%d.json' % index)
        code = checked_run(board, hashes, 'restore-measure-%d' % index,
                           [config['kicad_python'], str(Path(__file__).with_name('native_snapshot.py')),
                            str(board), str(path)])
        if code or not path.is_file():
            raise Halt('verification_incomplete', 'Could not measure neighbour restoration candidate')
        try:
            current = json.loads(path.read_text())
            components = current['component_of']
            if not isinstance(components, dict) or not isinstance(current['items'], dict):
                raise ValueError('Invalid native object/component inventory')
        except (ValueError, KeyError, TypeError) as exc:
            raise Halt('verification_incomplete', 'Invalid neighbour native measurement: ' + str(exc)) from exc
        if any(a not in components or a not in current['items'] for group in groups for a in group):
            raise Halt('protection_violation', 'Original fixed restoration anchor disappeared')
        if parent is not None:
            previous = parent['native']['component_of']
            # A lower aggregate score must not conceal a broken connection in
            # another original component. Each old partition may only merge.
            for group in groups:
                destinations = {}
                for anchor in group:
                    old = previous[anchor]
                    if old in destinations and destinations[old] != components[anchor]:
                        raise Halt('protection_violation', 'Neighbour proposal split a preserved anchor connection')
                    destinations[old] = components[anchor]
            if any(current['items'][a] != parent['native']['items'][a]
                   for group in groups for a in group):
                raise Halt('protection_violation', 'Neighbour proposal changed a fixed anchor')
        unresolved = [g for g in groups if len({components[a] for a in g}) > 1]
        score = sum(len({components[a] for a in group}) - 1 for group in groups)
        if parent is not None and score >= parent['score']:
            # A proposed route without the promised merge is not an ordinary
            # search miss. Stop and retain the evidence instead of hiding it.
            raise Halt('no_progress', 'Neighbour proposal did not merge original anchor components')
        copper = read_board(board)
        new_vias = sum(name(row) == 'via' for uid, row in copper['routing'].items()
                       if uid not in before['routing'])
        if new_vias > policy['max_new_vias']:
            raise Halt('protection_violation', 'Target candidate exceeded original via budget')
        endpoints = list(catalog(current, task['allowed_layer']).values())
        choices = []
        for group in unresolved:
            available = [e for e in endpoints if e['item_id'] in group and
                         task['region_mm'][0] <= e['position_mm'][0] <= task['region_mm'][2] and
                         task['region_mm'][1] <= e['position_mm'][1] <= task['region_mm'][3]]
            for j, a in enumerate(available):
                for b in available[j + 1:]:
                    if components[a['item_id']] != components[b['item_id']]:
                        choices.append((math.dist(a['position_mm'], b['position_mm']),
                                        a['item_id'], b['item_id'], a['selector'], b['selector'], a, b))
        choices.sort(key=lambda row: row[:5])
        return dict(board=board, hashes=hashes, native=current, score=score,
                    choices=choices, next_choice=0, new_vias=new_vias)

    root = measure(candidate)
    stack = [root]
    attempts = 0
    failures = []
    while stack:
        node = stack[-1]
        if source_files(node['board']) != node['hashes']:
            raise Halt('protection_violation', 'Restoration branch changed before reuse')
        if node['score'] == 0:
            log.update(status='proposed', all_anchor_groups_connected=True,
                       selected_branch=[str(frame['board']) for frame in stack],
                       selected_attempts=[frame['attempt'] for frame in stack[1:]])
            save()
            return node['board']
        if not node['choices']:
            failures.append('unsupported')
        if node['next_choice'] >= len(node['choices']):
            stack.pop()
            if stack:
                log['search']['backtracks'] += 1
            save()
            continue
        if attempts >= depth_bound + alternative_limit:
            raise Halt('no_progress', 'Bounded neighbour restoration alternatives exhausted; no infeasibility claim')
        if len(stack) > depth_bound:
            raise Halt('no_progress', 'Bounded neighbour restoration did not merge all anchor groups')
        *_, a, b = node['choices'][node['next_choice']]
        node['next_choice'] += 1
        index = attempts
        attempts += 1
        child = copy.deepcopy(task)
        for key in ('modification', 'requested_modification_nets', 'partial_segments',
                    'zone_refill', 'target_pad_uuids'):
            child.pop(key, None)
        child.update(id=task['id'] + '-restore-%d' % index, target_net=a['net'],
                     target_endpoints=[a, b], target_item_ids=[a['item_id'], b['item_id']],
                     permissions=dict(policy, max_new_vias=policy['max_new_vias'] - node['new_vias']))
        stage = folder / ('step-%d' % index)
        stage.mkdir()
        output = stage / 'board.kicad_pcb'
        argv, env = command(config, node['board'], output, child,
                            stage / 'route-summary.json', profile, node['native'])
        step = dict(endpoints=[a, b], input=str(node['board']), output=str(output),
                    depth=len(stack), parent_attempt=node.get('attempt'), status='running')
        log['steps'].append(step)
        log['search']['attempts'] = attempts
        save()
        code = checked_run(node['board'], node['hashes'], 'restore-route-%d' % index,
                           argv, cwd=config['krt_root'], env=env)
        summary = stage / 'route-summary.json'
        try:
            result = json.loads(summary.read_text()) if summary.exists() else {}
            if not isinstance(result, dict):
                raise ValueError('Expected object')
        except (ValueError, TypeError) as exc:
            raise Halt('verification_incomplete', 'Invalid neighbour routing summary') from exc
        step['summary'] = result
        status = result.get('status')
        if status in {'protection_violation', 'verification_incomplete', 'timed_out', 'cancelled', 'error'}:
            step['status'] = status
            save()
            raise Halt(status, result.get('reason', 'Neighbour router stopped'))
        if code or not output.is_file():
            # Only ordinary search/representation misses admit a different
            # original pair. Unexpected execution failure is not a search miss.
            status = 'unsupported' if code == 2 and status == 'unsupported' else 'no_progress' if code == 0 else 'error'
            step['status'] = status
            failures.append(status)
            save()
            if status == 'error':
                raise Halt(status, 'Neighbour router exited unexpectedly: ' + str(code))
            continue
        step['status'] = 'proposed'
        save()
        next_node = measure(output, node)
        next_node['attempt'] = index
        stack.append(next_node)
    status = 'unsupported' if failures and all(x == 'unsupported' for x in failures) else 'no_progress'
    raise Halt(status, 'No tested fixed-anchor restoration branch succeeded; no infeasibility claim')
