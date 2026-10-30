"""Bounded strategy portfolio under one immutable user authorization.

Each attempt starts from the same frozen board, never a failed proposal. The
first fully verified candidate wins. Search and verification budgets are shared.
"""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import time
from .jobs import Halt, write_json
from .layer_stack import supported_stack
from .modification import contract, witness
from .task_policy import permissions
from .workspace import source_files
from .validation import read_board
from .candidate_pipeline import generate, validate, outcome
from .candidate_pool import fingerprint


class AttemptJob:
    """Namespaced logs, with the parent's deadline and cancellation authority."""
    def __init__(self, parent, label):
        self.parent=parent;self.label=label;self.folder=parent.folder/'strategies'/label
        self.folder.mkdir(parents=True,exist_ok=False)
    def check(self):self.parent.check()
    def run(self,label,argv,**kwargs):return self.parent.run(self.label+'-'+label,argv,**kwargs)


def plans(task, configured_pns, native=None):
    """Return only permission subsets. Never infer permission from congestion."""
    spec=contract(task);policy=permissions(task)
    single={'schema_version':1,'route_layers':[task['allowed_layer']],'max_new_vias':0,'via_type':'through'}
    rows=[('add-only','krt',single,'none'),('geometry','geometry',single,'none')]
    # Native line/via endpoints can use KRT's explicit endpoint overrides;
    # these are distinct candidates from the visibility-graph geometry path.
    if configured_pns:
        rows.append(('push' if spec['mode'] in ('shove','shove+ripup') else 'pns-fixed','pns',single,
                     'shove' if spec['mode'] in ('shove','shove+ripup') else 'none'))
    if spec['mode'] in ('ripup','shove+ripup'):
        rows.append(('ripup','krt',single,'ripup'))
    if policy['route_layers']!=single['route_layers'] or policy['max_new_vias']:
        rows.append(('multilayer','krt',policy,'ripup' if spec['mode'] in ('ripup','shove+ripup') else 'none'))
        if len(policy['route_layers']) > 1:
            rows.append(('geometry-multilayer','geometry-multilayer',policy,'none'))
    if task.get('zone_refill'):
        rows=[(label+'-local-refill' if strategy=='pns' else label, strategy, allowed, mode) for label,strategy,allowed,mode in rows]
        if task['zone_refill'].get('schema_version')==2:
            from .zone_refill import layer_contract
            layer_contract(task)
            if spec['mode'] != 'none':raise ValueError('Layered refill currently requires fixed original tracks/vias')
            # KRT retains fixed fill obstacles; the independent geometry branch
            # may use the selected local fill permission. BOTH candidates keep
            # the same layered native refill witness and shared budgets.
            rows=[('geometry-multilayer-local-refill','geometry-multilayer',policy,'none'),
                  ('multilayer','krt',policy,'none')]
    result=[]
    for label,strategy,allowed,mode in rows:
        child=copy.deepcopy(task);child['permissions']=copy.deepcopy(allowed)
        if mode=='none':child.pop('modification',None);child.pop('requested_modification_nets',None)
        else:child['modification']['mode']=mode
        if strategy!='pns' or mode=='none':child.pop('partial_segments',None)
        if native and mode!='none':
            from .conflict_feedback import focused_tasks
            for key,focused,proof in focused_tasks(native,child):
                result.append((label+'-focused-'+key,strategy,focused))
        result.append((label,strategy,child))
    return result


def strategy_token(board, frozen, task, child, label, strategy, config, profile):
    """Stable across disposable snapshots, bound to exact input and authority.

    This names a lazy strategy branch, not a generated or verified candidate.
    Prefixing it keeps branch exclusions distinct from copper fingerprints.
    """
    context=sorted((Path(path).relative_to(board.parent).as_posix(), digest)
                   for path,digest in frozen.items())
    value=[context,task,child,label,strategy,profile,config.get('source_fingerprint'),
           config.get('verified_kicad_version'),config.get('drc_runtime_manifest_sha256')]
    return 'strategy-v1:'+hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def _repair(job,config,board,task,native,report,profile,generation_budget=60,verification_budget=120,started=None):
    started=time.monotonic() if started is None else started
    frozen=source_files(board);original=read_board(board)
    portfolio=plans(task,bool(config.get('pns')),native if config.get('conflict_feedback',True) else None)
    ledger=dict(status='running',generation_budget_seconds=generation_budget,verification_budget_seconds=verification_budget,
                generation_seconds=0.0,verification_seconds=0.0,attempts=[],
                policy='Fixed input/ROI/rules/authorization; each strategy is a permission subset; only a fully verified proposal can be returned')
    rejected_candidates={}
    report['automatic_strategy']=ledger
    branches=[dict(name=label,strategy=strategy,status='untried',
                   token=strategy_token(board,frozen,task,child,label,strategy,config,profile))
              for label,strategy,child in portfolio]
    by_name={row['name']:row for row in branches}
    branch_pool=dict(schema_version=1,kind='lazy-strategy-branches',branches=branches,
                     policy='Untried branches are search options, not candidates or successes; regeneration and validation consume the original target budget')
    report['strategy_pool']=branch_pool
    exclusions=set(report.get('candidate_exclusions',[]))
    def charge_orchestration():
        # Every non-validation second, including focused planning, feedback,
        # proof reconstruction and serialization, uses the original generation
        # allocation. Validation is measured separately and subtracted once.
        ledger['generation_seconds']=max(ledger['generation_seconds'],
            time.monotonic()-started-ledger['verification_seconds'])
    def save():
        from .conflict_feedback import collect
        report['conflict_feedback']=collect(report)
        write_json(job.folder/'conflict-feedback.json',report['conflict_feedback'])
        for row in ledger['attempts']:
            branch=by_name[row['name']];branch['status']=row['status']
            if 'candidate_task_fingerprint' in row:
                branch['candidate_fingerprint']=row['candidate_task_fingerprint']
        charge_orchestration()
        write_json(job.folder/'strategy-report.json',ledger)
        write_json(job.folder/'strategy-pool.json',branch_pool)
    def immutable():
        job.check()
        if source_files(board)!=frozen:raise Halt('protection_violation','Automatic strategy changed its frozen baseline')
        charge_orchestration()
    save()
    index=0
    while index<len(portfolio):
        immutable()
        if ledger['generation_seconds']>=generation_budget or ledger['verification_seconds']>=verification_budget:
            ledger['status']='timed_out';save();raise Halt('timed_out','Automatic shared budget exhausted')
        label,strategy,child=portfolio[index]
        # A preceding native rejection may identify a component that was not
        # on the original straight chord. Add only witnessed original subsets,
        # at most two per full branch including any preplanned corridor hints.
        if config.get('conflict_feedback',True) and child.get('modification'):
            from .conflict_feedback import collect, focused_tasks
            feedback=collect(report)
            if feedback['objects']:
                base_label=label.split('-focused-')[0]
                full_child=next(value for name,_,value in portfolio if name==base_label)
                existing={name for name,_,_ in portfolio}
                room=max(0,2-sum(name.startswith(base_label+'-focused-') for name in existing))
                additions=[]
                if room:
                    for key,focused,proof in focused_tasks(native,full_child,feedback):
                        new_label=base_label+'-focused-'+key
                        if new_label in existing:continue
                        additions.append((new_label,strategy,focused));existing.add(new_label)
                        branch=dict(name=new_label,strategy=strategy,status='untried',
                            token=strategy_token(board,frozen,task,focused,new_label,strategy,config,profile))
                        branches.append(branch);by_name[new_label]=branch
                        if len(additions)>=room:break
                if additions:
                    portfolio[index:index]=additions
                    save();continue
        index+=1
        immutable()
        if ledger['generation_seconds']>=generation_budget or ledger['verification_seconds']>=verification_budget:
            ledger['status']='timed_out';save();raise Halt('timed_out','Automatic shared budget exhausted')
        row=dict(name=label,strategy=strategy,status='starting',task=child);ledger['attempts'].append(row)
        if '-focused-' in label:
            row['permission_selection']=dict(selected_uuids=child['modification']['routing_uuids'],
                retained_fixed_uuids=sorted(set(contract(task)['routing_uuids'])-set(child['modification']['routing_uuids'])),
                reason='Witnessed original component selected by native corridor or earlier failure participants; relevance, not blockage proof',
                failure_feedback=report.get('conflict_feedback',{}))
        row['strategy_token']=by_name[label]['token']
        if row['strategy_token'] in exclusions:
            row.update(status='excluded_branch',reason='This exact strategy branch was already explored in the selected group')
            save();continue
        if strategy=='pns' and native.get('copper_layers') is not None and not supported_stack(native['copper_layers']):
            row.update(status='unsupported',skip_kind='geometry',skip_reason='native_stack',reason='Native copper-layer stack is outside witnessed standard 2/4/6/8-layer PNS support')
            save();continue
        sub=AttemptJob(job,label);row['directory']=str(sub.folder)
        try:
            witness(native,child)
        except ValueError as exc:
            row.update(status='unsupported',reason='This strategy cannot use the authorized track set: '+str(exc));save();continue
        write_json(sub.folder/'task.json',child);write_json(sub.folder/'task-manifest.json',{'cases':[child]})
        shutil.copy2(job.folder/'netclasses.native.json',sub.folder/'netclasses.native.json')
        detail={'preflight':report['preflight'], 'candidate_exclusions':report.get('candidate_exclusions',[])};row['details']=detail
        left=generation_budget-ledger['generation_seconds']
        # Reserve time for later distinct strategies. The first cheap/no-motion
        # attempt gets at most 12 s; the final eligible strategy may use the rest.
        budget=(min(12,left) if label=='add-only' else left if index==len(portfolio)
                else min(12,left) if label=='geometry' else min(24,left/(len(portfolio)-index+1)))
        row['generation_allocation_seconds']=budget;start=time.monotonic()
        try:
            candidate=generate(sub,config,board,child,native,detail,strategy,profile,budget=budget)
        except (Halt,ValueError) as exc:
            status=exc.status if isinstance(exc,Halt) else 'unsupported'
            row.update(status=status,reason=str(exc));immutable()
            if status in ('cancelled','protection_violation','verification_incomplete'):raise
            continue
        finally:
            charge_orchestration();save()
        immutable();row['candidate']=str(candidate)
        if ledger['generation_seconds']>generation_budget:raise Halt('timed_out','Automatic cumulative generation budget exceeded')
        parsed=read_board(candidate)
        if parsed==original:
            row.update(status='no_progress',reason='Candidate has no board changes; no acceptance claimed');save();continue
        signature=fingerprint(board,candidate,child)
        row['candidate_task_fingerprint']=signature
        if signature in exclusions and not detail.get('candidate_pool'):
            row.update(status='excluded_branch',reason='This exact protected proposal was already explored, including across engines')
            save();continue
        if signature in rejected_candidates and not detail.get('candidate_pool'):
            previous=rejected_candidates[signature]
            row.update(status=previous['status'],reason='Identical board semantics and permission task already failed complete validation',skip_reason='duplicate_verified_candidate',prior_attempt=previous['name'])
            save();continue
        left=verification_budget-ledger['verification_seconds']
        if left<=0:raise Halt('timed_out','Automatic shared verification budget exhausted')
        start=time.monotonic()
        try:
            from .candidate_pool import validate_candidates
            candidate,code,verification=validate_candidates(sub,config,board,candidate,child,detail,budget=left,validator=validate)
            row['candidate']=str(candidate)
            detail['verification']=verification
            status,reason=outcome(code,verification);row.update(status=status,reason=reason)
        except Halt as exc:
            row.update(status=exc.status,reason=str(exc))
            for key in ('candidate','candidate_pool','candidate_construction','verification','verification_directory'):
                if key in detail:report[key]=detail[key]
            raise
        finally:
            ledger['verification_seconds']+=time.monotonic()-start;save()
        immutable()
        if ledger['verification_seconds']>verification_budget:raise Halt('timed_out','Automatic cumulative verification budget exceeded')
        if status in ('protection_violation','verification_incomplete'):
            raise Halt(status,reason)
        if status in ('rejected','no_progress'):rejected_candidates[signature]=dict(name=label,status=status)
        if status=='accepted':
            # The public report points to the actual verifier and its original
            # candidate hashes; never copy an unverified file into its place.
            # A PNS pool may select a later proposal after rejecting its first.
            signature=detail.get('candidate_pool',{}).get('selected_fingerprint') or fingerprint(board,candidate,child)
            row['candidate_task_fingerprint']=signature
            branch_pool.update(selected_token=by_name[label]['token'],selected_candidate_fingerprint=signature)
            ledger.update(status='accepted',selected=label);save()
            for key,value in detail.items():
                if key not in ('preflight','verification'):report[key]=value
            report['verification_directory']=detail.get('verification_directory',str(sub.folder/'verification'))
            return candidate,code,verification
        save()
    statuses={r['status'] for r in ledger['attempts']}
    status='unsupported' if statuses=={'unsupported'} else 'timed_out' if 'timed_out' in statuses else 'rejected' if 'rejected' in statuses else 'error' if statuses=={'error'} else 'no_progress'
    ledger['status']=status;save()
    raise Halt(status,'No strategy produced a fully accepted repair; see strategy-report.json')


def repair(job,config,board,task,native,report,profile,generation_budget=60,verification_budget=120):
    started=time.monotonic()
    try:
        result=_repair(job,config,board,task,native,report,profile,generation_budget,verification_budget,started)
    except Halt as exc:
        if 'automatic_strategy' in report:
            report['automatic_strategy']['status']=exc.status
            report['automatic_strategy']['reason']=str(exc)
            write_json(job.folder/'strategy-report.json',report['automatic_strategy'])
        raise
    finally:
        if 'automatic_strategy' in report:
            ledger=report['automatic_strategy']
            ledger['generation_seconds']=max(ledger['generation_seconds'],
                time.monotonic()-started-ledger['verification_seconds'])
            ledger['generation_accounting']='All portfolio wall time excluding separately charged validation; includes planning and feedback'
            write_json(job.folder/'strategy-report.json',ledger)
    if ledger['generation_seconds']>generation_budget or ledger['verification_seconds']>verification_budget:
        ledger.update(status='timed_out',reason='Automatic shared budget exhausted during orchestration')
        write_json(job.folder/'strategy-report.json',ledger)
        raise Halt('timed_out',ledger['reason'])
    return result
