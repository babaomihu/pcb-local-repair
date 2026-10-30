"""Conservative signal scope shared by discovery and modification permissions.

Authored class labels are evidence of exclusions, not proof of ordinary signal
intent. An ordinary-single-ended declaration cannot override known exclusions.
"""
import re


def net_restriction(net, names, netclasses=()):
    if not net or any(c in net for c in ('\n', '\r', ',')):
        return 'empty or ambiguous KRT net-selector syntax'
    leaf = net.rsplit('/', 1)[-1]
    if re.fullmatch(r'(?i)(?:[+\-]?\d+(?:[vV.]\d+)?V?|GND[\w.\-+]*|[AD]?V(?:CC|DD|SS)[\w.\-+]*|VBAT[\w.\-+]*|VBUS[\w.\-+]*|POWER[\w.\-+]*)', leaf):
        return 'power/ground names require a power-routing policy not provided by ordinary-single-ended repair'
    for ending, other in (('_P','_N'),('_N','_P'),('+','-'),('-','+')):
        if net.endswith(ending) and net[:-len(ending)]+other in names:
            return 'a complementary pair name was found; differential routing is outside ordinary-single-ended scope'
    for nc in netclasses:
        if not isinstance(nc, dict):
            continue
        labels=[nc.get('name',''), *nc.get('members',[])]
        for label in labels:
            words=re.sub(r'([a-z])([A-Z])',r'\1 \2',str(label)).lower()
            tokens=set(re.findall(r'[a-z]+|[\u4e00-\u9fff]+',words))
            compact=''.join(re.findall(r'[a-z]+',str(label).lower()))
            power=bool(tokens & {'power','pwr','ground','gnd','supply','电源','地','供电'}) or any(x in str(label) for x in ('电源','供电'))
            controlled=bool(tokens & {'differential','diff','diffpair','ddr','highspeed','lengthmatched','timing','差分','高速','等长','时序'}) or compact.endswith('diff') or any(x in str(label) for x in ('差分','高速','等长','时序')) or compact in {'highspeed','lengthmatched','lengthmatching','timingcontrolled','differentialpair'}
            if power or controlled:
                return 'Authored netclass %r indicates %s; ordinary-single-ended repair cannot authorize it' % (label,'power/ground' if power else 'differential/high-speed/timing-controlled routing')
    return None


def restrictions(snapshot):
    classes={}
    for row in snapshot['items'].values():
        net=row.get('net','')
        if net:classes.setdefault(net,[]).append(row.get('netclass'))
    names=set(classes)
    result = {net:net_restriction(net,names,values) for net,values in classes.items()}
    # Pin metadata can carry the design intent even for anonymous net names.
    # Check every pad on the net, including pads outside the selected region.
    for row in snapshot['items'].values():
        net = row.get('net', '')
        if row.get('kind') != 'pad' or net not in result or result[net]:
            continue
        pin_type = str(row.get('pin_type', '')).strip().lower()
        function = str(row.get('pin_function', '')).strip()
        shield = re.fullmatch(r'(?i)(?:SHIELD|CHASSIS|EARTH|PE|[APDS]?GND)[\d_+\-]*', function)
        if pin_type in {'power_in', 'power_out'} or shield:
            result[net] = ('Authored pin %s.%s (%s, %s) indicates power/ground/shield; '
                           'ordinary-single-ended repair cannot authorize it') % (
                               row.get('reference', '?'), row.get('number', '?'),
                               function or 'unnamed', pin_type or 'untyped')
    return result


def require_signal(snapshot, net):
    reasons=restrictions(snapshot)
    if net not in reasons or reasons[net]:
        raise ValueError('Unsupported signal scope: '+net+': '+(reasons.get(net) or 'net absent'))
