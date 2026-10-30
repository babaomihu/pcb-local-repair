"""Conservative search projection of the already validated DRU subset.

This is not a substitute for KiCad rule compilation or final DRC. Original
rules are never rewritten. Unknown fields cannot be silently dropped.
"""
import hashlib
from decimal import Decimal, ROUND_CEILING
import math
from pathlib import Path
import re

from .validation import field, name, parse_sexpr, rule_support


def resolve(board, layer, stack, *, conservative_stack=False):
    if not stack or len(stack)!=len(set(stack)) or layer not in stack:
        raise ValueError('Custom clearance requires an explicit native copper stack')
    path=Path(board).with_suffix('.kicad_dru')
    evidence=rule_support(board)
    if not evidence['supported']:
        raise ValueError('Unsupported custom rule projection: '+'; '.join(evidence['reasons']))
    minima={s:0.0 for s in stack};matched={s:None for s in stack}
    data=path.read_bytes() if path.is_file() else None
    if data is not None:
        for rule in parse_sexpr(data.decode('utf-8')):
            if name(rule)!='rule':continue
            selector=field(rule,'layer')
            selector=str(selector[1]) if selector else None
            value=str(field(rule,'constraint')[2][1])
            m=re.fullmatch(r'(\d+(?:\.\d*)?|\.\d+)(mm|mil|in)',value)
            if not m:raise ValueError('Invalid custom clearance dimension')
            # Native coordinates are integer nm. Round search floors outward
            # so solver serialization cannot truncate a stricter constraint.
            exact=Decimal(m[1])*{'mm':Decimal(1),'mil':Decimal('.0254'),'in':Decimal('25.4')}[m[2]]
            mm=float(exact.quantize(Decimal('.000001'),rounding=ROUND_CEILING))
            if not math.isfinite(mm):raise ValueError('Nonfinite custom clearance')
            for copper in stack:
                applies=(selector is None or selector==copper or
                         selector=='outer' and copper in ('F.Cu','B.Cu') or
                         selector=='inner' and copper not in ('F.Cu','B.Cu'))
                if applies:minima[copper]=mm;matched[copper]=str(rule[1])
    return dict(authored_rules_sha256=hashlib.sha256(data).hexdigest() if data is not None else None,
                effective_layer_minima_mm=minima,selected_layer_min_mm=minima[layer],
                search_minimum_mm=max(minima.values()) if conservative_stack else minima[layer],
                winning_rules=matched,conservative_stack=conservative_stack,
                policy='Last matching authored minimum per layer; search uses a conservative floor with original board/class/own constraints. Original rules remain authoritative for final native acceptance.')
