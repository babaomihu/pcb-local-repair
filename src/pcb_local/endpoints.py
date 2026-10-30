"""Stable native item endpoints; no synthetic pads are written to user boards."""
import math


def catalog(snapshot, layer, check=None):
    result = {}
    for key, row in snapshot['items'].items():
        if check:check()
        if layer not in row['copper_layers']:
            continue
        if row['kind'] == 'pad':
            points = [('center', row['position_mm'])]
        elif row['kind'] == 'segment':
            points = [('start', row['start_mm']), ('end', row['end_mm'])]
        elif row['kind'] == 'via' and row.get('standard_through') is True:
            points = [('center', row['position_mm'])]
        else:
            continue
        for selector, point in points:
            if len(point) != 2 or not all(math.isfinite(v) for v in point):
                raise ValueError('Invalid native endpoint coordinates')
            token = key if row['kind'] == 'pad' else key + ':' + selector
            result[token] = dict(item_id=key, uuid=row['uuid'], kind=row['kind'],
                selector=selector, position_mm=list(point), net=row['net'], layer=layer,
                reference=row.get('reference', '线段' if row['kind']=='segment' else '过孔'),
                number=row.get('number', row['uuid'][:8]+':'+selector), netclass=row.get('netclass'))
    return result


def validate(task, snapshot):
    available = catalog(snapshot, task['allowed_layer'])
    current = {(e['item_id'],e['selector']):e for e in available.values()}
    ends = task['target_endpoints']
    if len(ends)!=2 or len({e['item_id'] for e in ends})!=2:
        raise ValueError('Two distinct original endpoint objects required')
    x0,y0,x1,y1=task['region_mm'];components=set()
    for e in ends:
        actual=current.get((e['item_id'],e['selector']))
        if actual!=e or e['net']!=task['target_net']:
            raise ValueError('Selected native endpoint identity, geometry, layer or net changed')
        x,y=e['position_mm']
        if not (x0<=x<=x1 and y0<=y<=y1):raise ValueError('Endpoint outside authorized region')
        components.add(snapshot['component_of'][e['item_id']])
    if task.get('target_item_ids') != [e['item_id'] for e in ends]:
        raise ValueError('Target item identities disagree with endpoint binding')
    return len(components)>1
