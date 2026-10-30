# SPDX-License-Identifier: GPL-3.0-or-later
"""Standard-library bridge to an installed CLI; no pcbnew/wx dependency."""
import json
import math
import os
import re
from pathlib import Path
import subprocess
import time
import uuid

TERMINAL = {"accepted", "complete", "no_progress", "unsupported", "rejected",
            "protection_violation", "verification_incomplete", "error", "timed_out",
            "cancelled", "interrupted", "stale_result"}


def config_read(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("cli_python", "runtime_config", "pcbnew_app", "tasks_root"):
        value = Path(config[key]).expanduser()
        if not value.is_absolute():
            raise ValueError("插件配置路径必须为绝对路径：" + key)
        config[key] = str(value)
        if key != "tasks_root" and not value.exists():
            raise ValueError("插件配置路径不存在：" + str(value))
    return config


def environment():
    # KiCad's bundled Python environment must not replace the CLI venv.
    env = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
        env.pop(key, None)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def prefix(config):
    return [config["cli_python"], "-B", "-m", "pcb_local"]


def parameters(board, roi, layer, auto_region=False):
    path = Path(board).expanduser().resolve()
    if path.suffix != ".kicad_pcb" or not path.is_file():
        raise ValueError("请主动选择一个已保存的 .kicad_pcb 文件")
    if not path.with_suffix(".kicad_pro").is_file():
        raise ValueError("磁盘板缺少同名 .kicad_pro，不能补造默认规则")
    if not layer:
        raise ValueError("必须指定铜层")
    if auto_region:
        return {"board": str(path), "auto_region": True, "layer": layer}
    values = [float(value) for value in roi]
    if (len(values) != 4 or not all(math.isfinite(x) for x in values)
            or values[0] >= values[2] or values[1] >= values[3]):
        raise ValueError("ROI 必须是有限的 xmin ymin xmax ymax，且宽、高大于零")
    if not layer:
        raise ValueError("必须指定铜层")
    return {"board": str(path), "roi": values, "layer": layer}


def query(config, command, task_dir, timeout=15):
    result = subprocess.run(prefix(config) + [command, str(task_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=environment(), timeout=timeout)
    if result.returncode:
        raise RuntimeError("CLI " + command + " 失败：" + (result.stderr or result.stdout)[-3000:])
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("CLI 没有返回状态对象")
    return value


def source_still_matches(config, inspected):
    # Uses the installed package's context enumeration, including rule-file
    # membership and project libraries; no board parser runs inside KiCad.
    code = ("import json,sys; from pathlib import Path; "
            "from pcb_local.workspace import source_files; "
            "print(json.dumps(source_files(Path(sys.argv[1]))))")
    result = subprocess.run([config["cli_python"], "-B", "-c", code, inspected["source_board"]],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=environment(), timeout=15)
    if result.returncode or json.loads(result.stdout) != inspected["source_hashes"]:
        raise ValueError("磁盘板、规则或项目库已变化，请重新检查目标")


def advanced_choices(inspected):
    native = inspected.get("preflight", {}).get("measurement", {}).get("snapshot", {})
    tracks = {row["net"] for row in native.get("items", {}).values()
              if row["kind"] == "segment" and not row.get("locked")}
    nets = sorted(row["net"] for row in inspected.get("discovery", {}).get("nets", [])
                  if row.get("supported") and row["net"] in tracks and not row["net"].startswith("-"))
    return list(native.get("copper_layers", [])), nets


def advanced_arguments(options, inspected, layer):
    if not options:
        return []
    if set(options) != {"route_layers", "max_new_vias", "reroute_nets", "max_modified_tracks", "strategy"}:
        raise ValueError("高级权限字段不完整")
    layers, nets = advanced_choices(inspected)
    chosen, via_limit = options["route_layers"], options["max_new_vias"]
    reroute, track_limit = options["reroute_nets"], options["max_modified_tracks"]
    if (not chosen or layer not in chosen or not set(chosen) <= set(layers)
            or len(chosen) != len(set(chosen)) or type(via_limit) is not int or not 0 <= via_limit <= 32
            or (via_limit and set(chosen) != set(layers))):
        raise ValueError("请选择有效铜层；通孔穿越的所有铜层都需要授权")
    if (not set(reroute) <= set(nets) or len(reroute) != len(set(reroute))
            or type(track_limit) is not int or not 0 <= track_limit <= 32
            or bool(reroute) != bool(track_limit)):
        raise ValueError("拆线重布需要明确选择普通信号网络及 1–32 段修改额度")
    if options["strategy"] not in ("krt", "pns", "auto"):
        raise ValueError("未知高级修复方法")
    if options["strategy"] == "pns" and (chosen != [layer] or via_limit):
        raise ValueError("当前推挤入口只支持所选单层且不新增过孔；跨层请使用补线/重布方式")
    argv = ["--route-layers", *chosen, "--max-new-vias", str(via_limit)]
    if options["strategy"] in ("pns", "auto"):
        argv += ["--candidate-strategy", options["strategy"]]
    if reroute:
        argv += ["--modify-nets" if options["strategy"] == "auto" else "--shove-nets" if options["strategy"] == "pns" else "--reroute-nets", *reroute, "--max-modified-tracks", str(track_limit)]
    return argv


def refill_choices(inspected, layer, targets, route_layers=None):
    """List only native-bound, foreign-net zones for all selected targets.

    This is a display/preflight filter, never a copper witness. The CLI rereads
    the board and applies the exact zone, ROI and original-connection checks.
    """
    if not targets or any(not t.get('supported') for t in targets):
        raise ValueError('局部重填铜需要选择本次检查支持的连接；原线端和已有普通通孔保持固定')
    nets={t['net'] for t in targets}
    native=inspected.get('preflight',{}).get('measurement',{}).get('snapshot',{})
    layers=route_layers or [layer]
    if route_layers and (layer not in layers or not set(layers)<=set(native.get('copper_layers',[]))):
        raise ValueError('重填铜层必须来自本次原生检查和明确的布线权限')
    native_rows=native.get('zone_refill_choices',[])
    ids=[z.get('uuid') for z in native_rows]
    if len(ids)!=len(set(ids)):
        raise ValueError('原生铺铜身份重复，请重新检查')
    native_by_id={z.get('uuid'):z for z in native_rows}
    rows=[];seen=set()
    for zone in inspected.get('zone_refill_choices',[]):
        uid=zone.get('uuid')
        if not isinstance(uid,str) or not uid or uid in seen or native_by_id.get(uid)!=zone:
            raise ValueError('铺铜列表与原生检查不一致，请重新检查')
        seen.add(uid)
        bounds=zone.get('bounds_mm',[])
        if (not isinstance(zone.get('net'),str) or not zone['net'] or zone['net'] in nets
                or not set(layers).intersection(zone.get('layers',[])) or zone.get('locked',False)
                or zone.get('source_owner_uuid') is not None or zone.get('is_rule_area',False)):
            continue
        if (len(bounds)!=4 or any(type(x) not in (int,float) or not math.isfinite(x) for x in bounds)
                or bounds[0]>=bounds[2] or bounds[1]>=bounds[3]):
            raise ValueError('铺铜范围无效，请重新检查')
        # Bounds can reject an unrelated zone, but cannot prove filled copper
        # intersects the ROI. That remains a native CLI gate.
        if any(t.get('region_mm') and not (bounds[0]<t['region_mm'][2] and bounds[2]>t['region_mm'][0]
                and bounds[1]<t['region_mm'][3] and bounds[3]>t['region_mm'][1]) for t in targets):
            continue
        rows.append(zone)
    return rows


def refill_arguments(zone_ids, inspected, layer, kind, target, advanced, recommendations, recommendation_mode):
    if not zone_ids:
        return []
    if (not isinstance(zone_ids,list) or any(not isinstance(u,str) or not u for u in zone_ids)
            or len(zone_ids)!=len(set(zone_ids))):
        raise ValueError('铺铜授权必须是明确、不重复的对象列表')
    if kind=='batch':
        ids=target
    elif kind=='repair' and target[0]=='--target':
        ids=[target[1]]
    else:
        raise ValueError('局部重填铜需要勾选检查发现的连接，不能按完整网络名隐式选择')
    listing=inspected.get('discovery',{}).get('targets',[])
    targets=[t for t in listing if t.get('id') in ids]
    if len(targets)!=len(ids) or len({t['id'] for t in targets})!=len(ids):
        raise ValueError('铺铜授权的目标不属于本次检查')
    if advanced:advanced_arguments(advanced,inspected,layer)
    layers=(advanced or {}).get('route_layers',[layer])
    choices=refill_choices(inspected,layer,targets,layers if advanced else None)
    if not set(zone_ids)<={z['uuid'] for z in choices}:
        raise ValueError('所选铺铜对某个目标不适用：检查网络、铜层、局部范围或锁定状态')
    strategy=(advanced or {}).get('strategy','auto' if recommendations else 'krt')
    if strategy not in ('pns','auto'):
        raise ValueError('局部重填铜需要明确选择推挤或自动组合方式')
    if advanced and (advanced['route_layers']!=[layer] or advanced['max_new_vias']):
        if strategy!='auto' or advanced['reroute_nets'] or recommendations:
            raise ValueError('跨层局部重填铜需要自动组合，并保持原走线/过孔固定；请取消邻网修改选项')
    if recommendations and recommendation_mode=='ripup':
        raise ValueError('局部重填铜与仅拆线重布建议不兼容；请选择明确允许推挤的操作')
    return [argument for uid in zone_ids for argument in ('--refill-zone',uid)]


def refill_summary(result):
    selection=result.get('refill_operation_selection')
    if not selection:
        summaries=[]
        for item in result.get('items',[]):
            if item.get('refill_operation_selection'):
                summaries.append('连接 '+str(item.get('target',{}).get('id','未知'))+'：\n'+
                                 refill_summary({'refill_operation_selection':item['refill_operation_selection']}))
        return '\n'.join(summaries)
    reasons={'old_clearance_corridor_may_refill_outside_roi':'移动后可能导致局部范围外重新填铜',
             'estimated_clearance_corridor_inside_roi':'预估间距范围位于授权区域内',
             'fixed_tails_with_movable_inset_middle':'两端保持固定，仅局部范围内向内缩进的中段允许移动'}
    frozen=selection.get('frozen_uuids',[])
    partial=set(selection.get('partial_uuids',[]))
    whole=set(selection.get('retained_uuids',[]))-partial
    text='铺铜操作筛选：整段冻结 %d 段，两端固定仅中段可动 %d 段，整段保留可动 %d 段；不改变原规则或授权范围。' % (len(frozen),len(partial),len(whole))
    affected=[row for row in selection.get('tracks',[]) if row.get('decision')!='retain']
    for row in affected[:20]:
        text+='\n对象 '+str(row.get('uuid'))+' / '+str(row.get('net',''))+'：'+reasons.get(row.get('reason'),str(row.get('reason','未提供原因')))
    if len(affected)>20:text+='\n其余 %d 个对象详见任务报告。'%(len(affected)-20)
    if selection.get('physical_impossibility_proven') is False:
        text+='\n固定表示当前策略保守避开该操作，不代表物理上无法布通。'
    return text


def launch(config, kind, selection, target, cancel_event, inspected=None, inspection_path=None, advanced=None, recommendations=None, order="selected", recommendation_mode="shove", refill_zones=None):
    if kind in ("repair", "batch") and (inspected is None or inspection_path is None or not Path(inspection_path).is_file()):
        raise ValueError("修复必须绑定本次检查的 result.json，请重新检查")
    if inspected is not None:
        source_still_matches(config, inspected)
    if cancel_event.is_set():
        return None
    root = Path(config["tasks_root"])
    root.mkdir(parents=True, exist_ok=True)
    folder = root / (time.strftime("%Y%m%d-%H%M%S") + "-" + kind + "-" + uuid.uuid4().hex[:10])
    scope = (["--auto-region"] if selection.get("auto_region")
             else ["--roi"] + [str(x) for x in selection["roi"]])
    if kind == "repair" and selection.get("auto_region") and target[0] != "--target":
        raise ValueError("自动范围模式需要选择一个发现的连接目标")
    argv = prefix(config) + [kind, "--config", config["runtime_config"],
        "--board", selection["board"]] + scope + [
        "--layer", selection["layer"], "--out-dir", str(folder)]
    if kind == 'import-legacy':
        argv = prefix(config) + [kind, '--config', config['runtime_config'],
                '--board', selection['board'], '--out-dir', str(folder)]
    if kind == "repair":
        option, value = target
        # equals protects an exact net beginning with '-' from option parsing.
        argv += [option + "=" + value, "--signal-kind", "ordinary-single-ended",
                 "--inspection", str(Path(inspection_path).resolve())]
    elif kind == "batch":
        if not isinstance(target, list) or not 1 <= len(target) <= 64 or len(set(target)) != len(target):
            raise ValueError("请勾选 1–64 个不同的待修复连接")
        argv += ["--targets"] + target + ["--signal-kind", "ordinary-single-ended",
                 "--inspection", str(Path(inspection_path).resolve())]
    if kind in ("repair", "batch"):
        argv += advanced_arguments(advanced, inspected, selection["layer"])
    if kind in ('repair','batch') and recommendations:
        allowed=inspected.get('modification_recommendations',[])
        chosen=[r for r in allowed if r['id'] in recommendations]
        target_ids=target if kind=='batch' else [target[1]]
        if len(chosen)!=len(recommendations) or len(set(recommendations))!=len(recommendations) or any(r['target_id'] not in target_ids for r in chosen):
            raise ValueError('修改建议不属于本次选择，请重新检查')
        if advanced and advanced.get('reroute_nets'):
            raise ValueError('建议对象权限不能与整网修改权限混用')
        if '--candidate-strategy' not in argv:argv += ['--candidate-strategy','auto']
        if recommendation_mode not in ('shove','ripup','shove+ripup'):raise ValueError('未知建议操作权限')
        if recommendation_mode!='shove' and any(p.get('scope')=='local_middle' for p in chosen):
            raise ValueError('区域内片段建议当前仅支持推挤；原两端保持固定')
        argv += ['--accept-recommendation',*recommendations,'--recommendation-mode',recommendation_mode]
    if kind in ('repair','batch'):
        argv += refill_arguments(refill_zones,inspected,selection['layer'],kind,target,advanced,recommendations,recommendation_mode)
    elif refill_zones:
        raise ValueError('铺铜授权只能用于修复任务')
    if kind=='batch':
        if order not in ('selected','constrained-first','coordinated','joint'):raise ValueError('未知顺序策略')
        if order=='joint' and not 2 <= len(target) <= 4:
            raise ValueError('联合修复请勾选 2–4 个连接；在原共享预算内回溯，不扩大修改权限')
        if order!='selected':argv += ['--order',order]
    log_path = root / (folder.name + ".launcher.log")
    with log_path.open("xb") as log:
        process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
            cwd=str(root), env=environment(), start_new_session=True)
    return {"kind": kind, "folder": folder, "process": process, "log": log_path,
            "selection": selection, "started": time.monotonic(), "argv": argv}


def open_candidate(config, task):
    # Fresh CLI status rechecks the source and candidate hash ledger. Never
    # enable opening from a cached result.json or an engine success message.
    state = query(config, "status", task["folder"])
    if task["process"].poll() != 0 or state.get("status") != "accepted" or state.get("accepted") is not True:
        raise ValueError("候选未处于重新核验的 accepted 状态，不能打开")
    candidate = Path(state["candidate"]).resolve()
    repair_outputs = ("scoping/candidate/board.kicad_pcb", "geometry-search/candidate/board.kicad_pcb")
    repair_outputs += tuple('pns-candidates/%d/board.kicad_pcb'%i for i in range(8))
    repair_outputs += tuple('strategies/'+strategy+'/'+relative
        for strategy in ('add-only','push','pns-fixed','push-local-refill','pns-fixed-local-refill','ripup','multilayer') for relative in repair_outputs)
    repair_outputs += ('strategies/geometry-multilayer-local-refill/geometry-search/candidate/board.kicad_pcb',)
    repair_outputs += ("strategies/geometry/geometry-search/candidate/board.kicad_pcb", "strategies/geometry-multilayer/geometry-search/candidate/board.kicad_pcb", "trace-quality/proposal/board.kicad_pcb")
    allowed = {(task['folder']/relative).resolve() for relative in (*repair_outputs,'candidate/board.kicad_pcb')}
    if task.get("kind") == "batch":
        # CLI status has just rechecked every accepted link and final identity.
        result = json.loads((task["folder"] / "result.json").read_text(encoding="utf-8"))
        chain = result.get("accepted_chain", [])
        if chain:
            child = Path(chain[-1]["result"]).resolve().parent
            if child.parent == task["folder"].resolve() and child.name.endswith("-repair"):
                allowed = {(child / relative).resolve() for relative in
                           repair_outputs}
    owner_folder=child if task.get('kind')=='batch' and chain else task['folder'].resolve()
    try:relative=candidate.relative_to(owner_folder).as_posix()
    except ValueError:relative=''
    focused=bool(re.fullmatch(r'strategies/(?:push|push-local-refill|ripup|multilayer)-focused-[0-9a-f]{12}/(?:scoping/candidate|pns-candidates/[0-7])/board\.kicad_pcb',relative))
    if focused:allowed.add(candidate)
    if candidate not in allowed or not candidate.is_file():
        raise ValueError("候选路径与本任务不一致")
    if focused or 'trace-quality' in candidate.relative_to(task['folder'].resolve()).parts or 'pns-candidates' in candidate.relative_to(task['folder'].resolve()).parts:
        # Keep the pool/verification policy in the installed CLI authority,
        # not in a second parser running in KiCad's older Python interpreter.
        # This checks the exact selected pool entry, independent native report,
        # full candidate/context hashes and every accepted batch link again.
        code = ('import json,sys; from pcb_local.delivery import accepted_result; '
                'print(json.dumps(accepted_result(sys.argv[1])))')
        proof = subprocess.run([config['cli_python'],'-B','-c',code,str(task['folder'])],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=environment(),timeout=30)
        if proof.returncode:
            raise ValueError('候选池的独立验收证据或文件已变化，不能打开：'+proof.stderr[-2000:])
        verified = json.loads(proof.stdout)
        if (verified.get('status')!='accepted' or verified.get('accepted') is not True
                or Path(verified.get('task_dir','')).resolve()!=task['folder'].resolve()
                or Path(verified.get('candidate','')).resolve()!=candidate):
            raise ValueError('候选池返回的已验收板与当前任务不一致')
    result = subprocess.run(["/usr/bin/open", "-n", "-a", config["pcbnew_app"],
        "--args", str(candidate)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=10)
    if result.returncode:
        raise RuntimeError("无法启动独立候选窗口：" + result.stderr)
    return state


def deliver(config, task_dir, *, destination=None, adopt=False):
    if adopt == (destination is not None):
        raise ValueError("请选择另存目录或采纳操作之一")
    argv = prefix(config) + ["adopt-result" if adopt else "export-result", str(task_dir)]
    argv += ["--original-closed"] if adopt else ["--destination", str(destination)]
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=environment(), timeout=120)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-4000:])
    value = json.loads(result.stdout)
    if value.get("status") not in {"exported", "adopted", "adopted_needs_review"}:
        raise ValueError("保存操作未返回可信的完成状态")
    return value


def open_comparison(task):
    import hashlib
    folder=Path(task['folder']).resolve()
    result=json.loads((folder/'result.json').read_text())
    visual=result.get('visualization',{})
    path=folder/'comparison.html'
    if path.is_symlink() or not path.is_file() or Path(visual.get('path','')).resolve()!=path or hashlib.sha256(path.read_bytes()).hexdigest()!=visual.get('sha256'):
        raise ValueError('可视化报告缺失或内容已变；不能打开未经绑定的文件')
    subprocess.Popen(['/usr/bin/open',str(path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    return str(path)


def imported_project(config, task):
    """Verify an independent imported project; never merge into editor memory."""
    state=query(config,'status',task['folder'])
    if (task.get('kind')!='import-legacy' or task['process'].poll()!=0
            or state.get('status')!='complete' or state.get('import_verified') is not True
            or state.get('accepted')):
        raise ValueError('旧格式导入尚未通过独立审计，不能选用其输出')
    board=Path(state['imported_board']).resolve()
    if board!=(task['folder']/'imported/board.kicad_pcb').resolve() or not board.is_file():
        raise ValueError('导入工程路径与本次任务不符')
    return str(board)
