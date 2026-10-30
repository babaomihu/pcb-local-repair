"""Candidate generation and full validation shared by explicit and auto modes."""
import json
from pathlib import Path
import sys
import time
from .engine import command as engine_command
from .jobs import Halt
from .workspace import source_files

PACKAGE = Path(__file__).resolve().parent


def generate(job, config, board, task, native, report, strategy, profile, budget=60):
    if strategy == 'pns':
        from .pns_backend import generate as pns_generate
        return pns_generate(job,config,board,task,native,report,budget=budget)
    frozen = source_files(board)
    deadline = time.monotonic()+budget
    def run(label, args, **kw):
        left=deadline-time.monotonic()
        if left <= 0:raise Halt('timed_out','Shared candidate generation budget exhausted')
        return job.run(label,args,timeout=left,**kw)
    def fallback_without_candidate(reason):
        from .task_policy import permissions
        policy=permissions(task)
        if strategy!='krt+geometry' or task.get('modification') or policy['route_layers']!=[task['allowed_layer']] or policy['max_new_vias']:
            return None
        if source_files(board)!=frozen:raise Halt('protection_violation','Engine changed input before geometry fallback')
        from .geometry_fallback import supplement
        report['krt_fallback_reason']=reason
        result=supplement(job,config,board,board,task,native,report,deadline,independent=True)
        if time.monotonic()>deadline:raise Halt('timed_out','Shared candidate generation budget exhausted')
        return result
    if strategy == 'geometry-multilayer':
        from .geometry_fallback import supplement
        candidate=supplement(job,config,board,board,task,native,report,deadline,independent=True,multilayer=True)
        if time.monotonic()>deadline:raise Halt('timed_out','Multilayer geometry generation budget exhausted')
        return candidate
    if strategy == 'geometry':
        from .geometry_fallback import supplement
        from .task_policy import permissions
        if task.get('modification') or permissions(task)['route_layers']!=[task['allowed_layer']] or permissions(task)['max_new_vias']:
            raise Halt('unsupported','Independent geometric routing requires single-layer add-only permission')
        candidate=supplement(job,config,board,board,task,native,report,deadline,independent=True)
        if time.monotonic()>deadline:raise Halt('timed_out','Independent geometry generation budget exhausted')
        return candidate
    candidate_dir=job.folder/'engine-output';candidate_dir.mkdir()
    candidate=candidate_dir/'board.kicad_pcb';engine_input=board
    if task.get('modification'):
        from .reroute import prepare
        engine_input=prepare(board,task,job.folder/'reroute-transaction')
        report['routing_transaction']=dict(prepared_board=str(engine_input),rollback_board=str(board),state='proposal_only',
            policy='Disposable baseline copy; only independently accepted proposals advance')
    argv,env=engine_command(config,engine_input,candidate,task,candidate_dir/'route-summary.json',profile,native)
    code=run('routing',argv,cwd=config['krt_root'],env=env)
    if (candidate_dir/'local-target-request.json').is_file():
        report['candidate_generator']='krt-local-endpoints'
        summary_path=candidate_dir/'route-summary.json'
        report['local_target_routing']=json.loads(summary_path.read_text()) if summary_path.exists() else {'status':'missing_report'}
        if code==2 and report['local_target_routing'].get('status')=='unsupported':
            fallback=fallback_without_candidate(report['local_target_routing'].get('reason','Local KRT model unsupported'))
            if fallback is not None:return fallback
            raise Halt('unsupported',report['local_target_routing'].get('reason','Local KRT endpoint model unsupported'))
    if code:raise Halt('error','Router exited with code '+str(code)+'; output is not eligible for acceptance')
    if not candidate.is_file():
        fallback=fallback_without_candidate('KRT produced no candidate board')
        if fallback is not None:return fallback
        raise Halt('no_progress','Router produced no candidate board')
    if task.get('target_endpoints') and task.get('modification'):
        from .krt_restore import restore
        candidate=restore(job,config,board,candidate,task,native,report,run,profile)
    if source_files(board)!=frozen:raise Halt('protection_violation','Engine changed the frozen input snapshot')
    scope_dir=job.folder/'scoping'
    code=run('scope-candidate',[sys.executable,str(PACKAGE/'scope_candidate.py'),'--before',str(board),
        '--raw-after',str(candidate),'--task',str(job.folder/'task.json'),'--out-dir',str(scope_dir)])
    path=scope_dir/'scope-report.json';scoped=json.loads(path.read_text()) if path.exists() else {}
    report['candidate_construction']=scoped
    if code or scoped.get('status')!='ready':
        raise Halt('protection_violation' if code==1 else 'error','; '.join(scoped.get('errors',[])) or 'Scoped candidate construction failed')
    candidate=Path(scoped['candidate_board'])
    if source_files(candidate)!=scoped['candidate_hashes']:raise Halt('protection_violation','Constructed candidate changed before verification')
    report['candidate_generator']='krt-local-endpoints' if report.get('local_target_routing') else 'krt'
    if task.get('modification'):
        report['geometry_fallback']=dict(status='unsupported',reason='Single-target geometric supplement cannot restore multiple modified nets')
    elif strategy=='krt+geometry':
        from .geometry_fallback import supplement
        candidate=supplement(job,config,board,candidate,task,native,report,deadline)
    if time.monotonic()>deadline:raise Halt('timed_out','Shared candidate generation budget exhausted')
    return candidate


def validate(job, config, board, candidate, task, budget=120):
    frozen=source_files(candidate)
    argv=[sys.executable,str(PACKAGE/'validation.py'),'--before',str(board),'--after',str(candidate),
        '--case-manifest',str(job.folder/'task-manifest.json'),'--case-id',task['id'],
        '--out-dir',str(job.folder/'verification'),'--kicad-cli',config['kicad_cli'],'--kicad-python',config['kicad_python']]
    argv += ['--timeout', str(budget)]
    argv += ['--geometry-python', config['router_python']]
    code=job.run('validation',argv,timeout=budget)
    result_path=job.folder/'verification/verification.json'
    if not result_path.is_file():raise Halt('verification_incomplete','Verifier did not produce its complete report')
    result=json.loads(result_path.read_text())
    if source_files(candidate)!=frozen:raise Halt('protection_violation','Candidate changed during validation')
    for path,expected in frozen.items():
        if Path(path).parent==candidate.parent and Path(path).name.startswith('board.') and result.get('source_hashes_after',{}).get(path)!=expected:
            raise Halt('protection_violation','Verifier did not bind the expected candidate file: '+path)
    return code,result


def outcome(code, verification):
    if not verification.get('verification_complete') or code==2:
        return 'verification_incomplete','; '.join(verification.get('errors',[])) or 'Independent verification incomplete'
    if verification.get('accepted') and code==0:
        return 'accepted','Target connected; independent native and protection checks all passed'
    failed=[key for key,value in verification.get('checks',{}).items() if not value]
    protected=set(verification.get('protection',{}).get('checks',{})) | {'existing_components_preserved','source_files_untouched','zone_refill_scope_preserved'}
    status='protection_violation' if protected.intersection(failed) else 'no_progress' if failed==['target_connected'] else 'rejected'
    return status,'Candidate failed acceptance: '+', '.join(failed)
