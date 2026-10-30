#!/usr/bin/env python3
"""Inspect or reproduce the pinned KRT runtime (read-only unless --execute).

Locks and patches come from this script's delivery bundle. --root selects the
workspace containing KiCadRoutingTools, the default venv and setup logs. No
routing, cloud service, board download, Git reset, staging or commit is done.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_command(argv, cwd=ROOT, timeout=30):
    result = subprocess.run([str(arg) for arg in argv], cwd=cwd,
                            capture_output=True, text=True, timeout=timeout)
    check(result.returncode == 0, 'Command failed: ' + repr(argv) + '\n' + result.stderr[-2000:])
    return result.stdout.strip()


def tracked_diff_bytes(krt):
    command = ['git', '-c', 'core.quotePath=false', '-c', 'diff.renames=false',
               '-C', str(krt), 'diff', '--binary', '--no-ext-diff', '--no-textconv',
               '--src-prefix=a/', '--dst-prefix=b/', 'HEAD', '--']
    result = subprocess.run(command, capture_output=True, timeout=30)
    check(result.returncode == 0, 'Cannot inspect tracked source diff: ' + result.stderr.decode(errors='replace')[-2000:])
    return result.stdout


def load_catalog(bundle=ROOT):
    lock = json.loads((bundle / 'source-lock.json').read_text())
    pinned = next(item for item in lock['sources'] if item['path'] == 'KiCadRoutingTools')
    state = lock['runtime_patch_state']
    manifest_path = bundle / state['manifest']
    check(sha(manifest_path) == state['manifest_sha256'], 'Patch manifest differs from source-lock.json')
    manifest = json.loads(manifest_path.read_text())
    check(manifest['upstream']['commit'] == pinned['commit'] == state['base_commit'], 'Patch base commit disagrees with source lock')
    check(manifest['upstream']['origin'] == pinned['origin'], 'Patch source origin disagrees with source lock')
    for name, spec in manifest['patch_sets'].items():
        if spec['patch']:
            patch = bundle / spec['patch']
            check(patch.is_file() and sha(patch) == spec['sha256'], 'Recorded patch is missing or changed: ' + name)
    return pinned, manifest


def runtime_source_files(krt):
    """Include ignored source too; exclude only build products and bytecode."""
    paths = [p for p in krt.iterdir() if p.is_file() and p.suffix in ('.py', '.pyi', '.so', '.pyd')]
    for dirname in ('py_router', 'py_placer', 'rust_router'):
        base = krt / dirname
        if not base.exists():
            continue
        for directory, dirs, files in os.walk(base):
            for name in dirs:
                check(not (Path(directory) / name).is_symlink(), 'Symlinked runtime source directory is not supported')
            dirs[:] = [name for name in dirs if name != '__pycache__' and not (dirname == 'rust_router' and Path(directory) == base and name == 'target')]
            for name in files:
                path = Path(directory) / name
                if path.suffix in ('.pyc', '.pyo'):
                    continue
                if dirname == 'rust_router' and Path(directory) == base and name.startswith('grid_router.') and path.suffix in ('.so', '.pyd'):
                    continue  # The imported extension has its own runtime hash gate.
                paths.append(path)
    return sorted(set(paths))


def verify_source(krt, patch_set, bundle=ROOT):
    pinned, manifest = load_catalog(bundle)
    check(patch_set in manifest['patch_sets'], 'Unknown patch set: ' + patch_set)
    spec = manifest['patch_sets'][patch_set]
    head = read_command(['git', '-C', krt, 'rev-parse', 'HEAD'])
    check(head == pinned['commit'], 'KRT HEAD differs from source-lock.json; no reset/checkout will be performed')
    diff = tracked_diff_bytes(krt)
    check(hashlib.sha256(diff).hexdigest() == spec['tracked_diff_sha256'],
          'Tracked source diff does not exactly match patch set ' + patch_set + '; refusing unrelated, partial or staged-added modifications')
    for name, expected in spec['files'].items():
        path = krt / name
        check(path.is_file() and not path.is_symlink() and sha(path) == expected,
              'Recorded patched file is missing or changed: ' + name)
    tracked = set(read_command(['git', '-C', krt, 'ls-files', '-z']).split('\0'))
    allowed = set(spec['added_files'])
    sources = runtime_source_files(krt)
    extra = [str(p.relative_to(krt)) for p in sources if str(p.relative_to(krt)) not in tracked | allowed]
    check(not extra, 'Unaudited runtime source files (including ignored files): ' + ', '.join(extra[:20]))
    for path in sources:
        check(not path.is_symlink(), 'Symlinked runtime source file is not supported: ' + str(path))
    upstream = manifest['upstream']
    check(sha(krt / upstream['license_path']) == upstream['license_sha256'], 'Upstream MIT license text changed')
    hashes = {str(p.relative_to(krt)): sha(p) for p in sources}
    return {'source_head': head, 'patch_set': patch_set, 'recorded_patch': spec['patch'],
            'recorded_patch_sha256': spec['sha256'], 'tracked_diff_sha256': spec['tracked_diff_sha256'],
            'patched_file_hashes': spec['files'], 'runtime_source_files': hashes,
            'runtime_source_count': len(hashes),
            'runtime_source_sha256': hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()}


def compact_audit(audit):
    return {key: value for key, value in audit.items() if key != 'runtime_source_files'}


def inspect_python(executable):
    code = "import json,sys; print(json.dumps({'version':list(sys.version_info[:3]),'prefix':sys.prefix,'base_prefix':sys.base_prefix,'executable':sys.executable}))"
    result = json.loads(read_command([executable, '-B', '-c', code]))
    check(result['version'][:2] == [3, 12], 'Python 3.12 is required; got ' + str(result['version']))
    return result


def inspect_environment(python, expected, venv, krt):
    if not python.exists():
        return {'exists': False, 'packages_match': False}
    interpreter = inspect_python(python)
    check(Path(interpreter['prefix']).resolve() == venv, 'Existing interpreter is not the requested independent venv')
    check(interpreter['prefix'] != interpreter['base_prefix'], 'Refusing to install into a global Python')
    code = """import importlib.metadata as m,json,sys,hashlib
names=json.loads(sys.argv[1]); result={}
for name in names:
 try: result[name]=m.version(name)
 except m.PackageNotFoundError: result[name]=None
sys.path.insert(0,sys.argv[2])
try:
 import grid_router
 binary={'version':getattr(grid_router,'__version__',None),'path':grid_router.__file__,'sha256':hashlib.sha256(open(grid_router.__file__,'rb').read()).hexdigest()}
except Exception as error: binary={'error':repr(error)}
print(json.dumps({'packages':result,'binary':binary}))
"""
    result = json.loads(read_command([python, '-B', '-c', code, json.dumps(list(expected)), krt / 'rust_router']))
    result.update(exists=True, interpreter=interpreter, packages_match=result['packages'] == expected)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT, help='Workspace root; locks/patches remain in this delivery bundle')
    parser.add_argument('--python', default='python3.12')
    parser.add_argument('--venv', type=Path, help='Independent venv (default: ROOT/.venv-krt)')
    parser.add_argument('--run-dir', type=Path, help='New log directory; never overwritten')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--rebuild', action='store_true', help='Rebuild even when the observed extension hash matches')
    parser.add_argument('--allow-recorded-patch', action='store_true', help='Accept only the exact selected recorded patch, including added runtime files')
    parser.add_argument('--patch-set', choices=('v0.8.9','v0.8.8', 'v0.1', 'teardrop', 'teardrop-with-test', 'pristine'),
                        help='Default with --allow-recorded-patch: v0.8.9; otherwise pristine')
    args = parser.parse_args()
    root = args.root.resolve(); krt = root / 'KiCadRoutingTools'
    check(args.allow_recorded_patch or args.patch_set in (None, 'pristine'), '--patch-set requires --allow-recorded-patch unless pristine')
    selected = args.patch_set or ('v0.8.9' if args.allow_recorded_patch else 'pristine')
    audit = verify_source(krt, selected)
    runtime = json.loads((ROOT / 'runtime-lock.json').read_text())
    cargo_lock = krt / 'rust_router/Cargo.lock'
    check(sha(cargo_lock) == runtime['cargo_lock_sha256'], 'Cargo.lock differs from runtime lock')
    requirements = ROOT / 'requirements-krt.lock.txt'; expected = {}
    for line in requirements.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'): continue
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)', line.strip())
        check(match is not None, 'Requirements must use exact name==version pins')
        expected[match.group(1)] = match.group(2)
    expected['pip'] = runtime['packages']['pip']
    check(expected == runtime['packages'], 'Requirements and runtime package pins disagree')
    host_python = shutil.which(args.python)
    check(host_python is not None, 'Python executable not found: ' + args.python)
    host_info = inspect_python(host_python)
    venv = (args.venv or root / '.venv-krt').resolve()
    check(venv != root and venv != krt and krt not in venv.parents and venv not in krt.parents, 'The venv must be independent of the source checkout')
    check(not venv.exists() or (venv / 'pyvenv.cfg').is_file(), 'Refusing to replace a non-venv directory')
    python = venv / 'bin/python'; existing = inspect_environment(python, expected, venv, krt)
    binary = existing.get('binary', {})
    binary_ok = (binary.get('version') == runtime['rust_module_version'] and binary.get('sha256') == runtime['rust_module_sha256']
                 and Path(binary.get('path', '/')).resolve().parent == (krt / 'rust_router').resolve())
    build_needed = args.rebuild or not binary_ok
    plan = {**compact_audit(audit), 'workspace_root': str(root), 'bundle_root': str(ROOT), 'python': host_info,
            'venv': str(venv), 'existing': existing, 'create_venv': not existing['exists'],
            'install_pinned_dependencies': not existing['packages_match'], 'build_from_source': build_needed,
            'reuse_observed_binary': binary_ok and not args.rebuild, 'build_jobs': 2,
            'limitations': 'Package versions and Cargo.lock are pinned; wheel archive hashes are not. New source builds may have different hashes by platform/toolchain; only the observed hash skips a subsequent build.'}
    check(verify_source(krt, selected) == audit, 'Source changed during inspection')
    print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
    if not args.execute: return 0
    if build_needed:
        check(shutil.which('cargo') and shutil.which('rustc'), 'Rust toolchain must already be installed')
        check(read_command(['rustc', '--version']) == runtime['rustc'], 'Rust compiler differs from runtime-lock.json')
    logs = args.run_dir.resolve() if args.run_dir else root / 'runs/bootstrap' / ('setup-krt-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    logs.mkdir(parents=True, exist_ok=False)
    (logs / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2)+'\n')
    (logs / 'source-fingerprint.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2)+'\n')
    def run(name, command, timeout, cwd=root, env=None):
        argv = [sys.executable, '-B', str(ROOT / 'scripts/run_logged.py'), '--run-dir', str(logs/name), '--cwd', str(cwd), '--timeout', str(timeout)]
        for key,value in (env or {}).items(): argv.extend(['--env', key+'='+value])
        result = subprocess.run(argv+['--']+[str(arg) for arg in command])
        check(result.returncode == 0, 'Setup stage failed; inspect '+str(logs/name))
    if not existing['exists']: run('create-venv', [host_python, '-m', 'venv', venv], 120)
    if not existing['packages_match']:
        flags = ['--disable-pip-version-check','--retries','2','--timeout','20','--only-binary',':all:']
        run('pin-pip', [python,'-m','pip','install',*flags,'pip=='+expected['pip']], 180)
        run('install-dependencies', [python,'-m','pip','install',*flags,'--no-deps','-r',requirements], 600)
    run('pip-check', [python,'-m','pip','check'], 60)
    if build_needed:
        run('cargo-fetch-locked', ['cargo','fetch','--locked'], 600, krt/'rust_router', {'CARGO_BUILD_JOBS':'2'})
        run('build-source', [python,'build_router.py','--from-source'], 1200, krt,
            {'CARGO_BUILD_JOBS':'2','PYO3_PYTHON':str(python),'PYTHONUNBUFFERED':'1','CARGO_NET_OFFLINE':'true','CARGO_TARGET_DIR':str(krt/'rust_router/target')})
    run('krt-help', [python,'-B','py_router/route.py','--help'], 60, krt)
    final = inspect_environment(python, expected, venv, krt)
    check(final['packages_match'], 'Installed package versions differ from lock')
    check(final.get('binary',{}).get('version') == runtime['rust_module_version'], 'Built extension import/version check failed')
    check(sha(cargo_lock) == runtime['cargo_lock_sha256'], 'Cargo.lock changed during setup')
    check(verify_source(krt, selected) == audit, 'Source changed during setup')
    report = {**compact_audit(audit), 'requirements_sha256':sha(requirements), 'cargo_lock_sha256':sha(cargo_lock),
              'environment':final, 'source_unchanged':True, 'log_directory':str(logs),
              'finished_at':datetime.now(timezone.utc).isoformat(), 'routing_verified':False}
    (logs/'setup-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2)); return 0

if __name__ == '__main__':
    try: sys.exit(main())
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('ERROR: '+str(error),file=sys.stderr); sys.exit(1)
