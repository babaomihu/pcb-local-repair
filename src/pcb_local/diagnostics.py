"""Evidence-linked explanations. Never decides acceptance or infers infeasibility."""

MESSAGES={
 'zone_refill_scope_violation':('目标已连通且 DRC 检查通过，但重填铜改变了未授权范围', '定位报告中的铺铜 UUID、铜层与变化包络；保留当前拒绝结果，检查填铜行为或候选路径，不扩大权限或忽略微小差异。'),
 'accepted':('修复已通过完整验收','打开独立候选板审查，可另存或按采纳流程保存。'),
 'no_supported_targets':('当前支持范围内没有待修复连接','检查目标层或查看未支持项；这不代表整板所有问题已消除。'),
 'inspection_complete':('检查完成','选择需要修复的连接，再明确允许的操作。'),
 'unsupported_geometry':('当前策略无法完整表示这些几何或层/孔结构','查看具体对象和限制；需增加建模支持，不能删除障碍或放宽规则。'),
 'permission_limit':('任务许可限制了可尝试的操作','先审查所列对象与修改范围；只有用户明确授权后才可建立不同权限的新任务。'),
 'search_scope_limit':('本次搜索达到规模或局部走廊限制，未得到候选','查看限制类型、实际规模和上限；保留规则、权限与预算，改进模型或搜索策略。该结果不证明物理上无法布通。'),
 'budget_exhausted':('本次预算已耗尽','保留日志定位耗时阶段；超时不证明物理上无法布通。'),
 'candidate_drc_rejected':('候选违反原设计规则，已拒绝','按列出的 DRC 类型、对象 UUID 和坐标检查候选，不采用该候选。'),
 'candidate_rejected':('候选未通过验收，已拒绝','查看具体失败检查项和策略报告，不采用该候选。'),
 'verification_incomplete':('无法完成可靠验收，不能接受候选','查看原生检查/覆盖错误；已有违规达到报告上限时先处理原工程问题，不关闭检查。'),
 'protected_state_violation':('保护或原连接关系检查失败，已停止接受','查看失败保护项和对象；先修复工具或任务边界问题。'),
 'search_not_found':('本次有界搜索没有找到通过验收的修复','查看尝试和直连走廊内可能相关的对象；这些证据不证明所有绕行路径都被阻断。'),
 'cancelled':('任务已取消','原工程未自动采纳；如需继续，请建立新任务。'),
 'execution_error':('执行失败','查看对应阶段日志，先排除运行时或输入错误。'),
 'unsupported_input':('输入或任务不在当前支持范围','查看原始原因；不要通过修改设计规则来绕过限制。')}



def baseline_drc(report):
    """Summarize existing input findings without merging independent DRC runs."""
    from collections import Counter
    preflight=report.get('preflight',{});measurement=preflight.get('measurement',{})
    supplement=measurement.get('supplemental_drc')
    if not isinstance(supplement,dict):supplement={}
    output=[]
    for scope,raw,coverage in [('authored',measurement.get('drc'),measurement.get('coverage',{})),
                                ('supplemental',supplement.get('drc'),supplement.get('coverage',{}))]:
        if not isinstance(raw,dict):
            continue
        counts={};records=[];valid=True
        for section in ('violations','unconnected_items','schematic_parity'):
            values=raw.get(section)
            counts[section]=len(values) if isinstance(values,list) else None
            if not isinstance(values,list):
                valid=False;continue
            for item in values:
                if not isinstance(item,dict):
                    valid=False;continue
                records.append(dict(section=section,type=str(item.get('type','unknown')),
                    severity=str(item.get('severity','unknown')),description=str(item.get('description','')),
                    items=[x for x in item.get('items',[]) if isinstance(x,dict)][:8]
                          if isinstance(item.get('items',[]),list) else []))
        output.append(dict(scope=scope,coordinate_units=raw.get('coordinate_units'),
            coverage_complete=preflight.get('complete') is True and coverage.get('complete') is True,
            records_complete=valid,section_counts=counts,
            total_records=sum(counts.values()) if valid else None,
            type_counts=dict(sorted(Counter(x['type'] for x in records).items())),
            severity_counts=dict(sorted(Counter(x['severity'] for x in records).items())),
            findings=records[:20],findings_truncated=len(records)>20,
            finding_limit=20,items_per_finding_limit=8))
    return output


def baseline_text(rows):
    if not rows:return ''
    lines=['原板已有 DRC 记录（原规则与补充检查可能重叠，数量不能相加；不表示新增候选违规）：']
    for row in rows:
        label='原规则' if row['scope']=='authored' else '补充检查'
        count=str(row['total_records']) if row['total_records'] is not None else '未完整记录'
        coverage='完整' if row['coverage_complete'] and row['records_complete'] else '未确认完整'
        types='、'.join(str(k)+'='+str(v) for k,v in row['type_counts'].items()) or '无已记录类型'
        lines.append('{}：{}条；覆盖{}；{}。'.format(label,count,coverage,types))
        if row['findings_truncated']:lines.append('对象定位仅列出前20条记录，每条最多8个对象；完整内容保留在原始DRC报告中。')
    return '\n'.join(lines)


def verification_outcome(verification):
    """Tri-state evidence: missing/invalid checks never become a pass."""
    checks=verification.get('checks',{})
    def truth(key):
        value=checks.get(key)
        return value if type(value) is bool else None
    drc_keys=('full_drc_no_new_or_changed','supplemental_drc_no_new_or_changed','drc_coverage_complete')
    drc=[truth(k) for k in drc_keys]
    return dict(complete=verification.get('verification_complete') is True,
                target_connected=truth('target_connected'),
                original_connections_preserved=truth('existing_components_preserved'),
                drc_passed=True if all(x is True for x in drc) else False if any(x is False for x in drc) else None,
                failed_checks=sorted(k for k,v in checks.items() if v is False),
                unknown_checks=sorted(k for k,v in checks.items() if type(v) is not bool))


def classify(status,reason='',verification=None,geometry=None):
    verification=verification or {};checks=verification.get('checks',{})
    if status=='accepted':return 'accepted'
    if status=='complete':return 'inspection_complete'
    if status=='verification_incomplete':return 'verification_incomplete'
    outcome=verification_outcome(verification)
    if (status in ('protection_violation','rejected') and outcome['complete']
            and outcome['target_connected'] is True and outcome['original_connections_preserved'] is True
            and outcome['drc_passed'] is True and not outcome['unknown_checks']
            and outcome['failed_checks']==['zone_refill_scope_preserved']):
        return 'zone_refill_scope_violation'
    if status=='protection_violation':return 'protected_state_violation'
    if status=='timed_out':return 'budget_exhausted'
    if status=='cancelled':return 'cancelled'
    if status=='rejected' and any(checks.get(k) is False for k in ('full_drc_no_new_or_changed','supplemental_drc_no_new_or_changed')):return 'candidate_drc_rejected'
    if status=='rejected':return 'candidate_rejected'
    if status=='no_progress':
        return 'search_scope_limit' if (geometry or {}).get('status')=='bounded_out' else 'search_not_found'
    if status=='unsupported':
        lower=reason.lower()
        if any(word in lower for word in ('permission','authorized track','modification','locked','outside roi')):return 'permission_limit'
        if any(word in lower for word in ('geometr','pad','copper','layer','stack','arc','drill','outline','hole','pns')):return 'unsupported_geometry'
        return 'unsupported_input'
    return 'execution_error'


def explain(report):
    status=report['status'];verification=report.get('verification',{})
    category=classify(status,report.get('reason',''),verification,report.get('geometry_fallback'))
    if status=='complete' and report.get('discovery',{}).get('supported_target_count')==0:category='no_supported_targets'
    attempts=[];objects=[];findings=[];limits=[];outlines=[];relevance=[];progress=[];fills=[];outcomes=[];search_limits=[]
    contexts=[('task',report)]
    for a in report.get('automatic_strategy',{}).get('attempts',[]):
        d=a.get('details',{});v=d.get('verification',{})
        c=classify(a['status'],a.get('reason',''),v,d.get('geometry_fallback'))
        if a.get('skip_kind'):c='permission_limit' if a.get('skip_kind')=='permission' else 'unsupported_geometry'
        attempts.append(dict(strategy=a['name'],status=a['status'],category=c,reason=a.get('reason',''),directory=a.get('directory'),skip_reason=a.get('skip_reason')))
        contexts.append((a['name'],d))
    for label,detail in contexts:
        geometry=detail.get('geometry_fallback',{})
        if geometry.get('status') in ('bounded_out','budget_exhausted'):
            search_limits.append(dict(strategy=label,status=geometry['status'],kind=geometry.get('limit_kind','unspecified'),details=geometry.get('limit_details',{}),reason=geometry.get('reason','')))
        pns=detail.get('pns',{})
        for index,attempt in enumerate(pns.get('worker',{}).get('attempts',[])):
            if attempt.get('progress_summary'):
                progress.append(dict(attempt['progress_summary'],strategy=label,attempt_index=index,
                    cursor_policy=attempt.get('cursor_policy'),direction=attempt.get('direction'),
                    target_mm=attempt.get('end')))
        for engine in (pns,detail.get('geometry_fallback',{})):
            if engine.get('outline_diagnostics'):
                outlines.append(dict(engine['outline_diagnostics'],strategy=label))
                for edge in engine['outline_diagnostics'].get('intersecting_edges',[]):
                    objects.append(dict(edge,layer='Edge.Cuts',evidence_kind='native_edge_envelope_intersects_roi',strategy=label))
        for layer,proof in pns.get('local_relevance',{}).items():
            relevance.append(dict(strategy=label,layer=layer,guard_mm=proof['guard_mm'],
                omitted_count=len(proof['omitted_uuids']),omitted_uuids=proof['omitted_uuids'],policy=proof['policy']))
        for obj in pns.get('unsupported_items',[]):objects.append(dict(obj,evidence_kind='unsupported_geometry',strategy=label))
        for obj in pns.get('direct_path_obstacles',{}).get('items',[]):objects.append(dict(obj,evidence_kind='direct_corridor_intersection',strategy=label))
        v=detail.get('verification',{})
        if v:
            outcomes.append(dict(verification_outcome(v),strategy=label))
        for row in v.get('zone_refill',{}).get('zones',[]):
            if not row.get('exact_equal',True):fills.append(dict(row,strategy=label))
        for scope in ('drc','supplemental_drc'):
            data=v.get(scope,{})
            for section,delta in data.get('sections',{}).items():
                for issue in delta.get('new_or_changed',[]):
                    findings.append(dict(strategy=label,scope=scope,section=section,type=issue.get('type'),description=issue.get('description'),items=issue.get('items',[])))
    selection=report.get('refill_operation_selection')
    if selection:
        for row in selection['tracks']:
            if row['decision']=='freeze':objects.append(dict(uuid=row['uuid'],net=row['net'],layer=report.get('task',{}).get('allowed_layer'),bounds_mm=row['influence_bounds_mm'],evidence_kind='refill_boundary_sensitive_frozen',strategy='operation_selection'))
        limits.append('为限制旧线移开后的区外回填，保守冻结了 {} 段授权原线；这不证明所有局部移动都不可行。'.format(len(selection['frozen_uuids'])))
    task=report.get('task',{})
    if task and status not in ('accepted','complete'):
        if not task.get('modification'):limits.append('旧走线全部受保护，未授权推挤或拆线。')
        permission=task.get('permissions',{})
        if not permission.get('max_new_vias',0):limits.append('未授权新增过孔。')
        limits.append('局部范围和授权在任务开始前固定；本次没有自动扩权。')
    # Keep object identities/coordinates, not log text masquerading as proof.
    unique={}
    for obj in objects:
        key=(obj.get('uuid'),str(obj.get('layer')),obj.get('evidence_kind'),obj.get('strategy'))
        unique[key]=obj
    # Auto mode keeps validation inside individual attempt details. Preserve
    # that evidence in the final explanation without altering the job status.
    if status=='rejected' and any(a['category']=='candidate_drc_rejected' for a in attempts):
        category='candidate_drc_rejected'
    if status=='unsupported' and attempts:
        categories={a['category'] for a in attempts}
        if categories in ({'unsupported_geometry'},{'permission_limit'}):
            category=next(iter(categories))
    errors=verification.get('errors',[]) or report.get('preflight',{}).get('errors',[])
    coverage=report.get('preflight',{}).get('measurement',{}).get('coverage',{})
    return dict(schema_version=1,category=category,summary=MESSAGES[category][0],suggestion=MESSAGES[category][1],
        original_reason=report.get('reason',''),attempts=attempts,objects=list(unique.values())[:64],
        drc_findings=findings[:64],drc_findings_total=len(findings),permission_limits=limits,
        verification_errors=errors,coverage_reasons=coverage.get('reasons',[]),
        evidence_limit='直连走廊相交对象只是可能相关的障碍；不是完整阻挡集合，不证明无法绕行。只有 DRC 项是该候选的实际违规。',
        physical_impossibility_proven=False,baseline_drc=baseline_drc(report),search_limits=search_limits,outline_diagnostics=outlines,local_relevance=relevance,pns_progress=progress,zone_fill_changes=fills,verification_outcomes=outcomes)


def markdown(d):
    lines=['','## 结果解释','',d['summary'],d['suggestion'],'',d['evidence_limit']]
    for row in d.get('search_limits',[]):
        lines.append('- 搜索限制 {} / {}：{}；{}。'.format(row['strategy'],row['kind'],row['reason'],row['details']))
    baseline=baseline_text(d.get('baseline_drc',[]))
    if baseline:lines += ['',baseline]
    labels={True:'通过',False:'未通过',None:'未确认'}
    for row in d.get('verification_outcomes',[]):
        lines.append('- 策略 {}：验收完整 {}；目标连接 {}；原连接保护 {}；完整/补充 DRC {}；失败检查 {}。'.format(
            row['strategy'],'是' if row['complete'] else '否',labels[row['target_connected']],
            labels[row['original_connections_preserved']],labels[row['drc_passed']],', '.join(row['failed_checks']) or '未记录'))
    lines += ['- '+s for s in d['permission_limits']]
    for item in d.get('zone_fill_changes',[]):
        lines.append('- 填铜 `{}` / {}：区域保护 {}，未授权变化 {} mm²，位置范围 {}；面积仅作诊断，接受依据为精确几何比较。'.format(item['uuid'],item['layer'],item['scope_preserved'],item['unauthorized_change_area_mm2'],item['unauthorized_change_bounds_mm']))
    for item in d.get('pns_progress',[]):
        last=item.get('last_active_observation',{})
        lines.append('- PNS 尝试 {} / {}：{}，实际停点 {}，距请求终点 {} mm；仅为游标观测，不能代替原生连通性验收或证明物理不可达。'.format(
            item['strategy'],item['attempt_index'],item['stage'],last.get('head_mm'),item.get('distance_to_target_mm')))
    for item in d.get('outline_diagnostics',[]):
        if item.get('status')=='analyzed':
            lines.append('- 板边诊断：ROI 内有 {} 个板材分量，涉及 {} 个原生板边对象；这不是全任务不可布通证明，未改变搜索模型。'.format(item['roi_board_components'],len(item['intersecting_edges'])))
    for item in d.get('local_relevance',[]):
        lines.append('- 层 {}：在求解副本中省略 {} 个有完整形状远离证明的固定铜对象，保护带 {} mm；原板和完整验收仍保留这些对象。'.format(item['layer'],item['omitted_count'],item['guard_mm']))
    for obj in d['objects']:
        lines.append('- 对象 `{}`，网络 `{}`，层 `{}`，位置/范围 `{}`；证据：{}'.format(obj.get('uuid'),obj.get('net'),obj.get('layer'),obj.get('position_mm',obj.get('bounds_mm')),obj['evidence_kind']))
    for finding in d['drc_findings']:
        lines.append('- DRC `{}`：{}'.format(finding['type'],finding['description']))
        for item in finding['items']:lines.append('  - `{}`，位置 `{}`：{}'.format(item.get('uuid'),item.get('pos'),item.get('description')))
    for a in d['attempts']:lines.append('- 策略 {}：{} / {}；{}'.format(a['strategy'],a['status'],a['category'],a['reason']))
    return '\n'.join(lines)+'\n'
