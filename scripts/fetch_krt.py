#!/usr/bin/env python3
"""Fetch pinned sparse KRT source and apply one recorded patch set.

No datasets/board fixtures are checked out, no runtime is built, and no Git
changes are staged or committed. Existing destination directories are refused.
--source uses a local Git cache; --root selects a new workspace while locks and
patches are read from this script's delivery bundle.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from setup_krt import ROOT, check, compact_audit, load_catalog, read_command, verify_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--source', type=Path, help='Local Git checkout/bare cache containing the pinned commit')
    parser.add_argument('--patch-set', choices=('v0.8.9','v0.8.8','v0.1','teardrop','teardrop-with-test','pristine'), default='v0.8.9')
    parser.add_argument('--run-dir', type=Path, help='New log directory; existing directories are refused')
    args = parser.parse_args()
    pinned, catalog = load_catalog()
    root = args.root.resolve(); destination = root / 'KiCadRoutingTools'
    check(not destination.exists() and not destination.is_symlink(), 'Refusing to overwrite existing destination: '+str(destination))
    check(not root.exists() or root.is_dir(), 'Workspace root is not a directory')
    source = args.source.resolve() if args.source else None
    if source:
        check(source.is_dir(), 'Local Git source does not exist: '+str(source))
        read_command(['git','-C',source,'cat-file','-e',pinned['commit']+'^{commit}'])
    acquisition = source.as_uri() if source else pinned['origin']
    patterns = catalog['runtime_sparse_checkout_patterns']
    check(patterns == ['/*','!/kicad_files/','!/tests/fixtures/','!/awx/'], 'Unexpected sparse source scope; review the runtime manifest')
    logs = args.run_dir.resolve() if args.run_dir else root/'runs/fetch-krt'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    check(not logs.exists(), 'Refusing to overwrite fetch log directory: '+str(logs))
    root.mkdir(parents=True,exist_ok=True)
    logs.mkdir(parents=True,exist_ok=False)
    plan = {'workspace_root':str(root),'bundle_root':str(ROOT),'destination':str(destination),
            'upstream_origin':pinned['origin'],'acquisition_source':str(source) if source else pinned['origin'],
            'source_commit':pinned['commit'],'patch_set':args.patch_set,
            'sparse_checkout_patterns':patterns,'build_requested':False,'routing_requested':False,
            'source_license':'MIT','contribution_notice':catalog['contributions']}
    (logs/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n')
    def run(name, command, timeout=60):
        argv=[sys.executable,'-B',str(ROOT/'scripts/run_logged.py'),'--run-dir',str(logs/name),
              '--cwd',str(root),'--timeout',str(timeout),'--',*[str(v) for v in command]]
        completed=subprocess.run(argv)
        check(completed.returncode==0,'Fetch stage failed; partial destination/logs retained: '+str(logs/name))
    try:
        run('init',['git','init','--quiet',destination])
        run('origin',['git','-C',destination,'remote','add','origin',acquisition])
        run('promisor',['git','-C',destination,'config','remote.origin.promisor','true'])
        run('filter',['git','-C',destination,'config','remote.origin.partialclonefilter','blob:none'])
        if source:
            # Enable blob filtering only for this child upload-pack invocation;
            # never change the user's cache configuration or use Git alternates.
            run('local-filter',['git','-C',destination,'config','remote.origin.uploadpack',
                                'git -c uploadpack.allowFilter=true upload-pack'])
        run('fetch',['git','-c','protocol.file.allow=always','-C',destination,'fetch',
                     '--no-tags','--filter=blob:none','--depth=1','origin',pinned['commit']],600)
        run('sparse-enable',['git','-C',destination,'config','core.sparseCheckout','true'])
        run('sparse-mode',['git','-C',destination,'config','core.sparseCheckoutCone','false'])
        sparse_file=destination/'.git/info/sparse-checkout'
        sparse_file.parent.mkdir(parents=True,exist_ok=True)
        sparse_file.write_text('\n'.join(patterns)+'\n')
        run('checkout',['git','-c','protocol.file.allow=always','-C',destination,'checkout','--detach',pinned['commit']],600)
        check(not any((destination/path).exists() for path in ('kicad_files','tests/fixtures','awx')), 'Excluded board fixtures appeared in checkout')
        board_files = [str(p.relative_to(destination)) for p in destination.rglob('*')
                       if p.is_file() and p.suffix in ('.kicad_pcb','.kicad_pro','.kicad_sch','.kicad_mod','.kicad_sym','.brd','.sch','.dsn','.ses')]
        check(not board_files, 'Unexpected board data in sparse source checkout: '+', '.join(board_files[:10]))
        pristine=verify_source(destination,'pristine')
        (logs/'pristine-fingerprint.json').write_text(json.dumps(pristine,ensure_ascii=False,indent=2)+'\n')
        spec=catalog['patch_sets'][args.patch_set]
        if spec['patch']:
            patch=ROOT/spec['patch']
            run('patch-check',['git','-C',destination,'apply','--check',patch])
            run('patch-apply',['git','-C',destination,'apply',patch])
        # All sparse runtime blobs now live in the new object store. Restore
        # the documented upstream URL; no reference to the local cache remains.
        if source:
            run('upstream-origin',['git','-C',destination,'remote','set-url','origin',pinned['origin']])
            run('clear-local-filter',['git','-C',destination,'config','--unset','remote.origin.uploadpack'])
        check(not (destination/'.git/objects/info/alternates').exists(), 'Unexpected dependency on an external Git object store')
        check(not read_command(['git','-C',destination,'diff','--cached','--name-only']), 'Fetch must not stage changes')
        final=verify_source(destination,args.patch_set)
        (logs/'source-fingerprint.json').write_text(json.dumps(final,ensure_ascii=False,indent=2)+'\n')
        result={**plan,**compact_audit(final),'status':'succeeded','log_directory':str(logs),
                'excluded_fixtures_absent':True,'changes_staged':False,'commits_created':False,
                'finished_at':datetime.now(timezone.utc).isoformat()}
        (logs/'fetch-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except Exception as error:
        (logs/'failure.json').write_text(json.dumps({'error':str(error),'partial_destination':str(destination),'log_directory':str(logs)},ensure_ascii=False,indent=2)+'\n')
        raise

if __name__=='__main__':
    try:sys.exit(main())
    except (RuntimeError,OSError,ValueError,KeyError,subprocess.SubprocessError) as error:
        print('ERROR: '+str(error),file=sys.stderr);sys.exit(1)
