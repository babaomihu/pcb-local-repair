"""Offline, inspect-only PCB change overview. Never changes acceptance or boards.

The SVG is a review aid, not a KiCad renderer: pads are size/position envelopes,
zone outlines are authored boundaries (not refilled copper), and neither image
nor report status authorizes adoption. No external assets or runtime packages.
"""
import base64
import hashlib
import html
import json
import math
from pathlib import Path

from .validation import canonical, field, name, parse_sexpr, semantic_string


COLORS = {'unchanged': '#8493a8', 'added': '#20c997', 'removed': '#ff6b75',
          'changed_before': '#ffae57', 'changed_after': '#bb93ff', 'zone': '#567191'}
LIMITATION = ('几何概览，不是 KiCad 精确渲染：焊盘显示位置/尺寸包络，铺铜仅显示区域边界，'
              '不显示重填铜岛或间距。对象颜色不证明电气连接或 DRC 通过。')


def _number(value):
    n = float(value)
    if not math.isfinite(n) or abs(n) > 1e7:
        raise ValueError('Invalid or out-of-range visual coordinate')
    return n


def _point(node, key='at'):
    row = field(node, key)
    if row is None or len(row) < 3:
        raise ValueError('Missing visual coordinate: ' + key)
    return [_number(row[1]), _number(row[2])]


def _bounds(points, margin=0):
    xs, ys = zip(*points)
    return [min(xs)-margin, min(ys)-margin, max(xs)+margin, max(ys)+margin]


def _uuid(node, fallback):
    row = field(node, 'uuid') or field(node, 'tstamp')
    return str(row[1]) if row else fallback


def _board(path, native=None):
    """Read display geometry; native pad positions take precedence when present."""
    path = Path(path)
    data = path.read_bytes()
    if len(data) > 100 * 1024 * 1024:
        raise ValueError('Visual board exceeds 100 MiB display limit')
    roots = parse_sexpr(data.decode('utf-8'))
    if len(roots) != 1 or name(roots[0]) != 'kicad_pcb':
        raise ValueError('Visual input is not one complete KiCad board')
    root = roots[0]
    layer_rows = field(root, 'layers') or []
    layers = [semantic_string(x[1]) for x in layer_rows[1:] if isinstance(x, list) and len(x)>1 and str(x[1]).endswith('.Cu')]
    nets = {str(x[1]): semantic_string(x[2]) for x in root[1:] if name(x)=='net' and len(x)>2}
    native_pads = {}
    for row in (native or {}).get('items', {}).values():
        if row.get('kind')=='pad':
            native_pads[(row['uuid'], row.get('footprint_uuid'))] = row
    objects = []
    warnings = []

    def net(node):
        row = field(node, 'net')
        if not row: return ''
        return semantic_string(row[2]) if len(row)>2 else nets.get(str(row[1]), semantic_string(row[1]))

    def on_layers(node):
        multi = field(node, 'layers')
        single = field(node, 'layer')
        values = [semantic_string(v) for v in multi[1:]] if multi else [semantic_string(single[1])] if single else []
        if '*.Cu' in values: return list(layers)
        if name(node)=='via' and len(values)==2 and all(x in layers for x in values):
            lo, hi = sorted(layers.index(x) for x in values)
            return layers[lo:hi+1]
        return [x for x in values if x in layers or x=='Edge.Cuts']

    for index, node in enumerate(root[1:]):
        kind = name(node)
        uid = _uuid(node, 'display-'+str(index))
        if kind in ('segment', 'arc', 'via'):
            row = dict(uuid=uid, key=uid, kind=kind, net=net(node), layers=on_layers(node), signature=repr(canonical(node)))
            if kind=='via':
                row.update(center=_point(node), diameter=_number(field(node,'size')[1]), drill=_number(field(node,'drill')[1]))
                row['bounds'] = _bounds([row['center']],row['diameter']/2)
            else:
                row.update(start=_point(node,'start'), end=_point(node,'end'), width=_number(field(node,'width')[1]))
                points=[row['start'],row['end']]
                if kind=='arc':
                    row['mid']=_point(node,'mid');points.append(row['mid'])
                    # Circle bounds also cover the major arc, not just its three points.
                    circle=_arc_circle(row['start'],row['mid'],row['end'])
                    row['circle']=circle
                    if circle:points=[[circle[0]-circle[2],circle[1]-circle[2]],[circle[0]+circle[2],circle[1]+circle[2]]]
                row['bounds']=_bounds(points,row['width']/2)
            objects.append(row)
        elif kind=='footprint':
            fp_at = field(node,'at') or ['at','0','0'];fx,fy=map(_number,fp_at[1:3]);angle=math.radians(_number(fp_at[3]) if len(fp_at)>3 else 0)
            reference=next((semantic_string(v[2]) for v in node[1:] if name(v) in ('property','fp_text') and len(v)>2 and str(v[1]).lower()=='reference'),'')
            for pi,pad in enumerate(v for v in node[1:] if name(v)=='pad'):
                puid=_uuid(pad,uid+'-pad-'+str(pi));at=field(pad,'at') or ['at','0','0'];px,py=map(_number,at[1:3])
                position=[fx+px*math.cos(angle)+py*math.sin(angle),fy-px*math.sin(angle)+py*math.cos(angle)]
                native_pad=native_pads.get((puid,uid))
                if native_pad:position=list(map(_number,native_pad['position_mm']))
                size=field(pad,'size');size=list(map(_number,size[1:3])) if size else [.3,.3]
                # A circumscribed circle is an orientation-independent pad envelope.
                radius=math.hypot(*size)/2
                objects.append(dict(uuid=puid,key=uid+':'+puid+':'+str(pi),kind='pad',net=net(pad),layers=on_layers(pad),
                    center=position,size=size,radius=radius,shape=str(pad[3]),reference=reference,number=semantic_string(pad[1]),
                    bounds=_bounds([position],radius),signature=repr((canonical(pad),position,reference))))
        elif kind in ('zone','gr_line','gr_rect','gr_poly','gr_arc'):
            visible_layers=on_layers(node)
            if not visible_layers:continue
            polygons=[]
            if kind=='zone':
                for polygon in (x for x in node[1:] if name(x)=='polygon'):
                    pts=field(polygon,'pts') or []
                    points=[list(map(_number,x[1:3])) for x in pts[1:] if name(x)=='xy']
                    if points:polygons.append(points)
            elif kind=='gr_line':polygons=[[_point(node,'start'),_point(node,'end')]]
            elif kind=='gr_rect':
                a,b=_point(node,'start'),_point(node,'end');polygons=[[a,[b[0],a[1]],b,[a[0],b[1]],a]]
            elif kind=='gr_poly':
                pts=field(node,'pts') or [];polygons=[[list(map(_number,x[1:3])) for x in pts[1:] if name(x)=='xy']]
            elif kind=='gr_arc':
                # This display-only path is deliberately a polyline approximation.
                polygons=[[_point(node,'start'),_point(node,'mid'),_point(node,'end')]]
                warnings.append('板框圆弧在概览中以三点折线表示；以 KiCad 为准。')
            points=[point for polygon in polygons for point in polygon]
            if points:objects.append(dict(uuid=uid,key=uid,kind='zone' if kind=='zone' else 'outline',net=net(node),layers=visible_layers,
                polygons=polygons,bounds=_bounds(points),signature=repr(canonical(node))))
    if len(objects)>200000:raise ValueError('Visual object count exceeds bounded display scope')
    return dict(path=str(path),sha256=hashlib.sha256(data).hexdigest(),layers=layers,objects=objects,warnings=sorted(set(warnings)))


def _arc_circle(a,b,c):
    ax,ay=a;bx,by=b;cx,cy=c
    d=2*(ax*(by-cy)+bx*(cy-ay)+cx*(ay-by))
    if abs(d)<1e-12:return None
    aa=ax*ax+ay*ay;bb=bx*bx+by*by;cc=cx*cx+cy*cy
    x=(aa*(by-cy)+bb*(cy-ay)+cc*(ay-by))/d;y=(aa*(cx-bx)+bb*(ax-cx)+cc*(bx-ax))/d
    return [x,y,math.hypot(ax-x,ay-y)]


def build_model(report, *, before=None, candidate=None):
    """Build a JSON-safe overview from a final report; never infer acceptance."""
    native=report.get('preflight',{}).get('measurement',{}).get('snapshot',{})
    before=Path(before or report['source_board'])
    old=_board(before,native);new=None
    candidate=candidate or report.get('candidate')
    if candidate and Path(candidate).is_file():new=_board(candidate)
    warnings=list(old['warnings'])+(new['warnings'] if new else [])
    expected=report.get('source_hashes',{}).get(str(before))
    if expected and old['sha256']!=expected:warnings.append('原板已不同于报告绑定版本；此概览不能作为验收凭据。')
    if new:
        expected=report.get('candidate_hashes',{}).get(str(Path(candidate)))
        if expected and new['sha256']!=expected:warnings.append('候选板已不同于报告绑定版本；此概览不能作为验收凭据。')
    oldmap={o['key']:o for o in old['objects']};newmap={o['key']:o for o in new['objects']} if new else oldmap
    objects=[]
    for key in sorted(oldmap.keys()|newmap.keys()):
        left,right=oldmap.get(key),newmap.get(key)
        if left and right and left['signature']==right['signature']:objects.append(dict(left,change='unchanged',side='both'))
        else:
            if left:objects.append(dict(left,change='changed_before' if right else 'removed',side='before'))
            if right:objects.append(dict(right,change='changed_after' if left else 'added',side='after'))
    for i,obj in enumerate(objects):obj.pop('signature');obj['display_id']='item-'+str(i)
    layers=list(dict.fromkeys(old['layers']+(new['layers'] if new else [])))
    issues=[]
    diagnosis=report.get('diagnosis',{})
    for item in diagnosis.get('zone_fill_changes',[]):
        if item.get('scope_preserved') is not False:
            continue
        raw=item.get('unauthorized_change_bounds_mm');bounds=None
        try:
            if not isinstance(raw,list) or len(raw)!=4 or any(type(x) not in (int,float) for x in raw):
                raise ValueError('Missing or invalid bounds')
            bounds=list(map(_number,raw))
            if bounds[0]>bounds[2] or bounds[1]>bounds[3]:
                raise ValueError('Reversed bounds')
        except (ValueError,TypeError):
            bounds=None
            warnings.append('未授权铺铜变化缺少有效定位范围；不伪造位置：'+str(item.get('uuid','')))
        issues.append(dict(uuid=str(item.get('uuid','')),label='未授权铺铜变化：'+str(item.get('uuid','')),
            description='变化包络，不是实际铜形；范围内并非处处变化。面积仅作诊断，不能作为接受容差。',
            layers=[item['layer']] if isinstance(item.get('layer'),str) else [],bounds=bounds,
            kind='zone_refill_scope',strategy=item.get('strategy'),
            unauthorized_change_area_mm2=item.get('unauthorized_change_area_mm2'),
            authorized_local_refill=item.get('authorized_local_refill')))
    for item in diagnosis.get('pns_progress',[]):
        pos=item.get('last_active_observation',{}).get('head_mm')
        issues.append(dict(uuid='',label='PNS 实际停点 '+str(item['attempt_index']),
            description='{}；距请求终点 {} mm。游标位置不是电气接通或物理不可达证明。'.format(item['stage'],item.get('distance_to_target_mm')),
            layers=[],bounds=_bounds([list(map(_number,pos))],.4) if pos else None,kind='pns_cursor_observation'))
    for item in diagnosis.get('objects',[]):
        layer=item.get('layer');layer=layer if isinstance(layer,list) else [layer] if layer else []
        bounds=item.get('bounds_mm');position=item.get('position_mm')
        if not bounds and position:bounds=_bounds([list(map(_number,position))],.4)
        issues.append(dict(uuid=str(item.get('uuid','')),label='相关对象：'+str(item.get('uuid','')),
                           description=str(item.get('reason',item.get('evidence_kind',''))),layers=layer,bounds=bounds,kind='possible_obstacle'))
    for finding in diagnosis.get('drc_findings',[]):
        for item in finding.get('items',[]):
            pos=item.get('pos');point=[pos['x'],pos['y']] if isinstance(pos,dict) and 'x' in pos and 'y' in pos else pos
            issues.append(dict(uuid=str(item.get('uuid','')),label='DRC '+str(finding.get('type','')),
                               description=str(finding.get('description','')),layers=[],bounds=_bounds([list(map(_number,point))],.4) if point else None,kind='drc'))
    for source in diagnosis.get('baseline_drc',[]):
        for finding in source.get('findings',[]):
            for item in finding.get('items',[]):
                bounds=None
                pos=item.get('pos')
                if source.get('coordinate_units')=='mm':
                    try:
                        if not isinstance(pos,dict):raise ValueError('No named mm position')
                        bounds=_bounds([[_number(pos['x']),_number(pos['y'])]],.4)
                    except (KeyError,ValueError,TypeError,OverflowError):
                        pass
                issues.append(dict(uuid=str(item.get('uuid','')),label='原板已有 DRC / '+str(source['scope'])+' / '+str(finding['type']),
                    description=str(finding['description'])+'；原板记录，不是候选新增违规。',
                    layers=[],bounds=bounds,kind='baseline_drc'))
    roi=report.get('task',{}).get('region_mm')
    if roi:roi=list(map(_number,roi))
    allbounds=[o['bounds'] for o in objects]+([roi] if roi else [])
    bounds=_bounds([[b[0],b[1]] for b in allbounds]+[[b[2],b[3]] for b in allbounds],1) if allbounds else [0,0,10,10]
    return dict(schema_version=1,status=str(report.get('status','unknown')),accepted_record=report.get('status')=='accepted' and report.get('accepted') is True,
        reason=str(report.get('reason','')),summary=str(diagnosis.get('summary','')),limitation=LIMITATION,
        adoption_available=False,before=dict(path=old['path'],sha256=old['sha256']),candidate=dict(path=new['path'],sha256=new['sha256']) if new else None,
        layers=layers,objects=objects,issues=issues,roi=roi,bounds=bounds,warnings=warnings,
        route_quality=report.get('route_quality'),
        baseline_drc=[{k:v for k,v in row.items() if k!='findings'} for row in diagnosis.get('baseline_drc',[])],
        change_counts={key:sum(o['change']==key for o in objects) for key in COLORS if key!='zone'})


_STYLE = '''body{margin:0;background:#101820;color:#e9eef4;font:14px system-ui,sans-serif}header{padding:16px 20px;border-bottom:1px solid #405166}h1{font-size:22px;margin:0 0 8px}.warning{color:#ffca80}.accepted{color:#55dfb0}.failed{color:#ff969d}button,select,input{font:inherit;background:#1e2b39;color:#edf5ff;border:1px solid #60718a;border-radius:5px;padding:6px;margin:3px}button{cursor:pointer}button:hover{border-color:#fff}main{display:grid;grid-template-columns:minmax(0,1fr) 360px;height:70vh;min-height:450px}#canvas{width:100%;height:100%;background:#15212d;touch-action:none}aside{overflow:auto;border-left:1px solid #405166;padding:10px}.row{display:block;width:100%;text-align:left;word-break:break-all;font-size:12px}#detail{white-space:pre-wrap;overflow-wrap:anywhere;background:#1b2c3d;padding:10px}footer{padding:12px 20px;overflow-wrap:anywhere;font-size:12px}.legend{display:flex;gap:14px;flex-wrap:wrap;margin:10px 0}.swatch{display:inline-block;width:16px;height:4px;vertical-align:middle;margin-right:5px}label{white-space:nowrap}#hint{margin:7px 0;color:#b4c8da}@media(max-width:800px){main{grid-template-columns:1fr;height:auto}#canvas{height:65vh}aside{max-height:40vh}}'''
_JS = r'''"use strict";
const model=JSON.parse(document.getElementById('model').textContent), NS='http://www.w3.org/2000/svg';
const svg=document.getElementById('canvas'), layer=document.getElementById('layer'), view=document.getElementById('view');
const colors={unchanged:'#8493a8',added:'#20c997',removed:'#ff6b75',changed_before:'#ffae57',changed_after:'#bb93ff'};
let box=model.bounds.slice(),selected=null;
function make(tag,attrs,text){const e=document.createElementNS(NS,tag);for(const [k,v]of Object.entries(attrs||{}))e.setAttribute(k,String(v));if(text!==undefined)e.textContent=text;return e;}
function setBox(b){const cx=(b[0]+b[2])/2,cy=(b[1]+b[3])/2,w=Math.max(b[2]-b[0],1),h=Math.max(b[3]-b[1],1);box=[cx-w/2,cy-h/2,cx+w/2,cy+h/2];svg.setAttribute('viewBox',`${box[0]} ${box[1]} ${w} ${h}`);}
function fit(b){const m=Math.max((b[2]-b[0])*.12,(b[3]-b[1])*.12,.7);setBox([b[0]-m,b[1]-m,b[2]+m,b[3]+m]);}
function visible(o){return (!layer.value||o.layers.includes(layer.value)||o.layers.includes('Edge.Cuts')) && (view.value==='both'||o.side==='both'||o.side===view.value) && (document.getElementById('unchanged').checked||o.change!=='unchanged') && (document.getElementById('pads').checked||o.kind!=='pad');}
function focusObject(o,description){selected=o.uuid;const hit=model.objects.find(x=>x.uuid===o.uuid&&x.side!=='before')||model.objects.find(x=>x.uuid===o.uuid);const data=o.kind==='zone_refill_scope'?o:(hit||o);const preferred=(o.layers||[]).find(x=>model.layers.includes(x));if(preferred)layer.value=preferred;if(hit&&hit.side!=='both'&&view.value!==hit.side)view.value='both';fit(o.bounds||data.bounds||model.bounds);document.getElementById('detail').textContent=(description||'对象')+'\n'+JSON.stringify(data,null,2);draw();}
function shape(o){const common={stroke:colors[o.change],fill:'none','stroke-linecap':'round','stroke-linejoin':'round'};let e;
 if(o.kind==='segment')e=make('line',{...common,x1:o.start[0],y1:o.start[1],x2:o.end[0],y2:o.end[1],'stroke-width':o.width});
 else if(o.kind==='arc'&&o.circle){const [x,y,r]=o.circle;let a=Math.atan2(o.start[1]-y,o.start[0]-x),b=Math.atan2(o.mid[1]-y,o.mid[0]-x),c=Math.atan2(o.end[1]-y,o.end[0]-x);const tau=Math.PI*2,positive=v=>(v%tau+tau)%tau;const sweep=positive(b-a)<=positive(c-a)?1:0;const span=sweep?positive(c-a):positive(a-c);e=make('path',{...common,d:`M ${o.start} A ${r} ${r} 0 ${span>Math.PI?1:0} ${sweep} ${o.end}`,'stroke-width':o.width});}
 else if(o.kind==='arc')e=make('polyline',{...common,points:[o.start,o.mid,o.end].map(p=>p.join(',')).join(' '),'stroke-width':o.width});
 else if(o.kind==='via'){e=make('g');e.append(make('circle',{...common,cx:o.center[0],cy:o.center[1],r:(o.diameter+o.drill)/4,'stroke-width':Math.max(.04,(o.diameter-o.drill)/2)}));e.append(make('circle',{cx:o.center[0],cy:o.center[1],r:o.drill/2,fill:'#101820'}));}
 else if(o.kind==='pad')e=make('circle',{...common,cx:o.center[0],cy:o.center[1],r:o.radius,'stroke-width':.055,'stroke-dasharray':'.12 .07'});
 else{e=make('g');for(const p of o.polygons||[])e.append(make(o.kind==='zone'?'polygon':'polyline',{...common,points:p.map(v=>v.join(',')).join(' '),'stroke-width':o.kind==='zone'?.045:.08,'stroke-dasharray':o.kind==='zone'?'.3 .15':'none',opacity:o.kind==='zone'?.6:1}));}
 e.setAttribute('data-display-id',o.display_id);if(o.uuid===selected)e.setAttribute('filter','url(#highlight)');e.addEventListener('click',event=>{event.stopPropagation();focusObject(o);});e.append(make('title',{},`${o.change} ${o.kind} ${o.net} ${o.uuid}`));return e;}
function draw(){svg.replaceChildren();const defs=make('defs'),filter=make('filter',{id:'highlight',x:'-50%',y:'-50%',width:'200%',height:'200%'});filter.append(make('feDropShadow',{'dx':0,'dy':0,'stdDeviation':.08,'flood-color':'#fff'}));defs.append(filter);svg.append(defs);
 if(model.roi){const [a,b,c,d]=model.roi;svg.append(make('rect',{x:a,y:b,width:c-a,height:d-b,fill:'none',stroke:'#6bcaff','stroke-width':.06,'stroke-dasharray':'.4 .2'}));}
 for(const o of model.objects)if(visible(o))svg.append(shape(o));
 for(const i of model.issues){if(!i.bounds||layer.value&&i.layers.length&&!i.layers.includes(layer.value))continue;const [a,b,c,d]=i.bounds;const e=make('rect',{x:a-.1,y:b-.1,width:Math.max(c-a,.2)+.2,height:Math.max(d-b,.2)+.2,fill:'none',stroke:(i.kind==='drc'||i.kind==='baseline_drc'||i.kind==='zone_refill_scope')?'#ff4646':'#ffe582','stroke-width':.08,'stroke-dasharray':'.15 .1'});e.addEventListener('click',()=>focusObject(i,i.label+' '+i.description));svg.append(e);}
 updateList();}
function updateList(){const list=document.getElementById('list');list.replaceChildren();const query=document.getElementById('search').value.toLowerCase();const rows=[...model.issues.map(i=>({item:i,label:i.label+' '+i.description})),...model.objects.filter(o=>o.change!=='unchanged'&&visible(o)).map(o=>({item:o,label:`${o.change} ${o.kind} ${o.net} ${o.uuid}`}))].filter(x=>x.label.toLowerCase().includes(query));document.getElementById('count').textContent=`相关对象/修改项 ${rows.length}，显示前 ${Math.min(rows.length,300)} 项；可用 UUID 或网络筛选。`;
 for(const row of rows.slice(0,300)){const button=document.createElement('button');button.className='row';button.textContent=row.label;button.addEventListener('click',()=>focusObject(row.item,row.label));list.append(button);}}
for(const l of model.layers){const option=document.createElement('option');option.value=l;option.textContent=l;layer.append(option);}
const preferred=model.roi&&model.objects.find(o=>o.change==='added');if(preferred&&preferred.layers.length)layer.value=preferred.layers[0];
for(const id of ['layer','view','unchanged','pads'])document.getElementById(id).addEventListener('change',draw);
document.getElementById('search').addEventListener('input',updateList);document.getElementById('fit').addEventListener('click',()=>fit(model.bounds));document.getElementById('roi').addEventListener('click',()=>fit(model.roi||model.bounds));
svg.addEventListener('wheel',event=>{event.preventDefault();const scale=event.deltaY>0?1.2:1/1.2,cx=(box[0]+box[2])/2,cy=(box[1]+box[3])/2,w=(box[2]-box[0])*scale,h=(box[3]-box[1])*scale;if(w<.05||w>1e7)return;setBox([cx-w/2,cy-h/2,cx+w/2,cy+h/2]);},{passive:false});
let drag=null;svg.addEventListener('pointerdown',e=>{if(e.button!==0)return;drag={x:e.clientX,y:e.clientY,b:box.slice()};svg.setPointerCapture(e.pointerId);});svg.addEventListener('pointermove',e=>{if(!drag)return;const rect=svg.getBoundingClientRect(),scale=Math.max((drag.b[2]-drag.b[0])/rect.width,(drag.b[3]-drag.b[1])/rect.height),dx=(e.clientX-drag.x)*scale,dy=(e.clientY-drag.y)*scale;setBox([drag.b[0]-dx,drag.b[1]-dy,drag.b[2]-dx,drag.b[3]-dy]);});svg.addEventListener('pointerup',()=>drag=null);svg.addEventListener('pointercancel',()=>drag=null);
fit(model.roi||model.bounds);draw();
'''


def render_html(model):
    """Escape all board-controlled content; only the fixed script may execute."""
    payload=json.dumps(model,ensure_ascii=False,allow_nan=False).replace('&','\\u0026').replace('<','\\u003c').replace('>','\\u003e').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    script_hash=base64.b64encode(hashlib.sha256(_JS.encode()).digest()).decode()
    csp="default-src 'none'; script-src 'sha256-"+script_hash+"'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; connect-src 'none'"
    status='已验收记录（保存前仍需 CLI 重新核对）' if model['accepted_record'] else '未通过验收／仅供诊断，不可采纳'
    warning=''.join('<p class="warning">'+html.escape(w)+'</p>' for w in model['warnings'])
    if model.get('route_quality'):
        from .review_summary import quality_text
        warning += '<p>'+html.escape(quality_text(model['route_quality']))+'</p><p>'+html.escape(model['route_quality']['limitation'])+'</p>'
    from .diagnostics import baseline_text
    baseline=baseline_text(model.get('baseline_drc',[]))
    if baseline:warning += '<section aria-label="原板已有DRC"><h2>原板检查记录</h2><p>'+html.escape(baseline).replace('\n','<br>')+'</p></section>'
    legend=''.join('<span><i class="swatch" style="background:'+color+'"></i>'+label+'</span>' for key,label,color in [('unchanged','原有未变',COLORS['unchanged']),('added','新增',COLORS['added']),('removed','删除',COLORS['removed']),('changed_before','改变前',COLORS['changed_before']),('changed_after','改变后',COLORS['changed_after'])])
    return '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="'+html.escape(csp,quote=True)+'"><title>PCB Local 修复对比</title><style>'+_STYLE+'</style></head><body><header><h1>PCB Local 修复对比</h1><div class="'+('accepted' if model['accepted_record'] else 'failed')+'">'+html.escape(model['status']+' — '+status)+'</div><p>'+html.escape(model['summary'] or model['reason'])+'</p><p class="warning">'+html.escape(model['limitation'])+'</p>'+warning+'<div class="legend">'+legend+'</div><label>层 <select id="layer"><option value="">全部铜层</option></select></label><label>对比 <select id="view"><option value="both">叠加前后</option><option value="before">修复前</option><option value="after">候选</option></select></label><label><input type="checkbox" id="unchanged" checked>原有对象</label><label><input type="checkbox" id="pads" checked>焊盘包络</label><button id="fit">整板范围</button><button id="roi">授权局部范围</button><div id="hint">滚轮缩放，拖动平移，点击对象或右侧条目定位。黄色框仅为可能相关对象，红框为报告中的 DRC 位置或未授权铺铜变化包络；包络不是实际铜形。没有自动保存或采纳操作。</div></header><main><svg id="canvas" role="img" aria-label="PCB 修复前后几何概览"></svg><aside><input id="search" placeholder="筛选 UUID / 网络 / DRC" aria-label="筛选对象"><p id="count"></p><div id="list"></div><pre id="detail">点击对象查看身份和几何信息。</pre></aside></main><footer>原板：'+html.escape(model['before']['path'])+'<br>候选：'+html.escape(model['candidate']['path'] if model['candidate'] else '没有候选；显示原板与失败位置')+'<br>此文件为离线只读展示，不替代原生连接性和完整板级 DRC 验收。完整精确铜形请在 KiCad 独立候选板中审查。</footer><script id="model" type="application/json">'+payload+'</script><script>'+_JS+'</script></body></html>\n'


def write_visual_report(report, output, *, before=None, candidate=None):
    """Write a new inspect-only HTML report; return path/hash/counts for result.json.

    Caller should catch a display error and record it without accepting a board
    that was rejected. No report or source file is edited by this function.
    """
    model=build_model(report,before=before,candidate=candidate)
    output=Path(output)
    output.parent.mkdir(parents=True,exist_ok=True)
    data=render_html(model).encode('utf-8')
    with output.open('xb') as stream:stream.write(data)
    return dict(path=str(output.absolute()),sha256=hashlib.sha256(data).hexdigest(),
                schema_version=1,object_count=len(model['objects']),issue_count=len(model['issues']),
                change_counts=model['change_counts'],warnings=model['warnings'],
                display_only=True,adoption_available=False,limitations=LIMITATION)
