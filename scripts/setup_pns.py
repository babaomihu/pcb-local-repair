#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the pinned independent PCBWorld-Engine on the verified macOS toolchain.

Requires an existing pinned engine checkout (see source-lock.json). No board
collections are downloaded, no user project is opened, no GUI is launched.
The build script's destructive rsync is restricted to a verified build copy.
"""
import argparse,hashlib,json,os,shlex,shutil,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--engine-root',required=True,type=Path);p.add_argument('--build-dir',required=True,type=Path)
 p.add_argument('--venv-dir',required=True,type=Path);p.add_argument('--run-dir',required=True,type=Path)
 p.add_argument('--python',type=Path,default=Path(sys.executable));p.add_argument('--reuse-build',action='store_true')
 a=p.parse_args();engine=a.engine_root.resolve();build=a.build_dir.resolve();venv=a.venv_dir.absolute();logs=a.run_dir.resolve()
 if logs.exists():raise ValueError('Run log directory must be new')
 logs.mkdir(parents=True);report={'status':'running','engine_root':str(engine),'build_dir':str(build),'venv_dir':str(venv),'source_pins':{}}
 def save(): (logs/'setup-result.json').write_text(json.dumps(report,indent=2)+'\n')
 def read(argv):return subprocess.check_output(list(map(str,argv)),text=True).strip()
 def run(label,argv,timeout,env=None):
  command=[sys.executable,str(ROOT/'scripts/run_logged.py'),'--run-dir',str(logs/label),'--timeout',str(timeout)]
  for k,v in (env or {}).items():command+=['--env',k+'='+v]
  command+=['--',*map(str,argv)]
  if subprocess.call(command):raise RuntimeError('Failed stage '+label+'; see '+str(logs/label))
 try:
  if sys.platform!='darwin':raise ValueError('This build adapter is verified only on macOS arm64; upstream Linux build is not validated here')
  if any(any(c.isspace() for c in str(path)) for path in (engine,build,venv)):
   raise ValueError('Upstream build script requires engine/build/venv paths without whitespace')
  lock=json.loads((ROOT/'source-lock.json').read_text())
  for relative in ['', 'kicad-python','kicad-python/kicad']:
   key='PCBWorld/engine'+('/'+relative if relative else '')
   spec=next(s for s in lock['sources'] if s['path']==key);path=engine/relative
   commit=read(['git','-C',path,'rev-parse','HEAD'])
   if commit!=spec['commit'] or read(['git','-C',path,'status','--porcelain','--untracked-files=no']):
    raise ValueError('Engine source differs from pinned clean checkout: '+key)
   report['source_pins'][key]=commit
  wx=shutil.which('wx-config') or shutil.which('wx-config-3.2')
  if not wx and Path('/opt/homebrew/opt/wxwidgets@3.2/bin/wx-config-3.2').is_file():wx='/opt/homebrew/opt/wxwidgets@3.2/bin/wx-config-3.2'
  cmake=shutil.which('cmake');ninja=shutil.which('ninja')
  if not all([wx,cmake,ninja]):raise ValueError('Install cmake, ninja and wxwidgets@3.2; see docs/高级修复-v0.2.md')
  if not build.exists():build.mkdir(parents=True)
  elif any(build.iterdir()):
   if not a.reuse_build:raise ValueError('Existing build requires explicit --reuse-build')
   cache=build/'CMakeCache.txt';stamp=build/'pcbnew/python/rl/ENGINE_CPP_HASH'
   expected=read(['bash',engine/'tools/cpp_content_hash.sh',engine/'kicad-patches'])
   if not cache.is_file() or str(build/'kicad_src') not in cache.read_text() or not stamp.is_file() or stamp.read_text().strip()!=expected:
    raise ValueError('Refusing to reuse an unverified build tree')
  if not venv.exists():run('venv',[a.python,'-m','venv',venv],90)
  if not (venv/'pyvenv.cfg').is_file():raise ValueError('PNS interpreter must be an explicit virtual environment')
  python=venv/'bin/python'
  if read([python,'-c','import sys;print("%d.%d"%sys.version_info[:2])'])!='3.12':raise ValueError('Verified PNS build uses Python 3.12')
  run('python-dependency',[python,'-m','pip','install','pybind11==2.13.6'],180)
  wrappers=logs/'tools';wrappers.mkdir();(wrappers/'wx-config').symlink_to(wx)
  for filename,text in {
   'ninja':'#!/bin/sh\nexec '+shlex.quote(ninja)+' -j2 "$@"\n',
   'pcb-local-cmake':'#!/bin/sh\ncase "$1" in --version) exec '+shlex.quote(cmake)+' "$@";; esac\nexec '+shlex.quote(cmake)+' "$@" -DwxWidgets_CONFIG_EXECUTABLE='+shlex.quote(wx)+' -DBOOST_ROOT=/opt/homebrew/opt/boost@1.85 -DKICAD_INSTALL_DEMOS=OFF\n'}.items():
   path=wrappers/filename;path.write_text(text);path.chmod(0o755)
  env={'PATH':str(wrappers)+':'+os.environ['PATH'],'CMAKE':'pcb-local-cmake','BUILD_DIR':str(build),'VENV_DIR':str(venv)}
  report['environment']=env;save();run('build',['bash',engine/'build_rl_router.sh'],5400,env)
  module=build/'pcbnew/python/rl/kicad_rl_router.so'
  run('import',[python,'-c','import sys;sys.path.insert(0,sys.argv[1]);import kicad_rl_router;print("PNS import OK")',module.parent],30)
  report.update(status='built',module_sha256=hashlib.sha256(module.read_bytes()).hexdigest(),module_dir=str(module.parent),python=str(python),
                boundary='Fresh or verified incremental build; import is not routing acceptance; no original projects touched')
  save();print(json.dumps(report));return 0
 except Exception as exc:
  report.update(status='failed',error=type(exc).__name__+': '+str(exc));save();print(report['error'],file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
