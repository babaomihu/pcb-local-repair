"""Read-only PNS cursor diagnostics, never a candidate acceptance gate.

Move's boolean does not prove arrival. Record the actual placer head before
fix_route can cancel the session. Neither exact cursor arrival nor distance
proves electrical contact, which remains the native verifier's responsibility.
"""
import math


def observe(router, target, layer, expected_net, phase):
    row = {'phase': phase, 'requested_cursor_mm': list(target),
           'intended_layer': layer, 'expected_net_code': expected_net}
    try:
        head = list(router.get_route_head())
        net = int(router.get_current_net_code())
        if len(head) != 3 or not all(math.isfinite(float(v)) for v in [*head, *target]):
            raise ValueError('Non-finite or malformed native route head')
        if int(head[2]) != head[2]:
            raise ValueError('Non-integral native route layer')
        row.update(status='idle' if head[2] < 0 else 'observed',
                   head_mm=None if head[2] < 0 else head[:2],
                   head_layer=int(head[2]), active_net_code=net)
        if head[2] >= 0:
            row.update(distance_to_cursor_mm=math.dist(head[:2], target),
                       cursor_exact_nm=all(round(head[i]*1e6) == round(target[i]*1e6) for i in (0, 1)),
                       layer_matches=int(head[2]) == layer, net_matches=net == expected_net)
    except Exception as exc:
        # Optional observations must not replace the engine's result or abort
        # a potentially valid proposal. Native geometry/connectivity still gate it.
        row.update(status='unavailable', reason=type(exc).__name__ + ': ' + str(exc))
    return row


def summarize(attempt):
    observations = attempt.get('progress', [])
    live = [row for row in observations if row.get('status') == 'observed']
    result = {'observations': len(observations),
              'interpretation': 'Observed cursor/commit state only; not proof of physical impossibility or electrical contact'}
    if live:
        last = live[-1]
        result['last_active_observation'] = last
        result['distance_to_target_mm'] = math.dist(last['head_mm'], attempt['end'])
    if not attempt.get('started'):
        stage = 'start_rejected'
    elif attempt.get('committed'):
        stage = 'proposal_generated_requires_native_validation'
    elif not live:
        stage = 'progress_unavailable' if any(r.get('status') == 'unavailable' for r in observations) else 'no_active_trace_observed'
    else:
        if not last['net_matches']:
            stage = 'active_net_mismatch'
        elif not last['layer_matches']:
            stage = 'active_layer_mismatch'
        elif last['phase'].startswith('waypoint') and not last['cursor_exact_nm']:
            stage = 'waypoint_not_reached'
        elif not all(round(last['head_mm'][i]*1e6) == round(attempt['end'][i]*1e6) for i in (0, 1)):
            stage = 'endpoint_not_reached'
        else:
            stage = 'commit_rejected_after_cursor_arrival'
    result['stage'] = stage
    return result
