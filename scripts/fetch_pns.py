#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fetch only three pinned engine source trees; no demos, QA boards or training code."""
import argparse,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--destination',required=True,type=Path);p.add_argument('--run-dir',required=True,type=Path)
 p.add_argument('--source',type=Path,help='Optional local PCBWorld-Engine Git source cache (including its two initialized source submodules)')
 a=p.parse_args();dest=a.destination.resolve();logs=a.run_dir.resolve()
 if dest.exists() or logs.exists():raise ValueError('Destination and log directory must both be new')
 logs.mkdir(parents=True);lock=json.loads((ROOT/'source-lock.json').read_text());records=[]
 def run(label,argv,timeout=600):
  code=subprocess.call([sys.executable,str(ROOT/'scripts/run_logged.py'),'--run-dir',str(logs/label),'--timeout',str(timeout),'--',*map(str,argv)])
  if code:raise RuntimeError('Fetch failed; partial source retained: '+label)
 try:
  for index,relative in enumerate(['','kicad-python','kicad-python/kicad']):
   key='PCBWorld/engine'+('/'+relative if relative else '');spec=next(s for s in lock['sources'] if s['path']==key);path=dest/relative
   path.mkdir(parents=True,exist_ok=True)
   if any(path.iterdir()):raise ValueError('Expected empty source submodule path: '+str(path))
   origin=(a.source.resolve()/relative).as_uri() if a.source else spec['origin'];prefix=str(index)
   run(prefix+'-init',['git','init','--quiet',path]);run(prefix+'-origin',['git','-C',path,'remote','add','origin',origin])
   if a.source:run(prefix+'-local-filter',['git','-C',path,'config','remote.origin.uploadpack','git -c uploadpack.allowFilter=true upload-pack'])
   run(prefix+'-fetch',['git','-c','protocol.file.allow=always','-C',path,'fetch','--no-tags','--filter=blob:none','--depth=1','origin',spec['commit']])
   patterns=['/*','!/demos/','!/qa/data/','!/examples/']
   patterns+=['!*'+ext for ext in ('.kicad_pcb','.kicad_pro','.kicad_sch','.brd','.sch','.dsn','.ses')]
   run(prefix+'-sparse',['git','-C',path,'config','core.sparseCheckout','true'])
   (path/'.git/info/sparse-checkout').write_text('\n'.join(patterns)+'\n')
   run(prefix+'-checkout',['git','-c','protocol.file.allow=always','-C',path,'checkout','--detach',spec['commit']])
   actual=subprocess.check_output(['git','-C',str(path),'rev-parse','HEAD'],text=True).strip()
   if actual!=spec['commit']:raise ValueError('Checkout pin mismatch')
   records.append(dict(path=relative or '.',origin=spec['origin'],commit=actual,sparse_patterns=patterns))
   (logs/'fetch-result.json').write_text(json.dumps(dict(status='fetching',sources=records),indent=2)+'\n')
  boards=[str(p.relative_to(dest)) for p in dest.rglob('*') if '.git' not in p.parts and p.is_file() and p.suffix in ('.kicad_pcb','.kicad_pro','.kicad_sch','.brd','.sch','.dsn','.ses')]
  if boards:raise ValueError('Unexpected board files in sparse source: '+repr(boards[:10]))
  (logs/'fetch-result.json').write_text(json.dumps(dict(status='fetched',sources=records,board_files=[]),indent=2)+'\n');return 0
 except Exception as exc:
  (logs/'fetch-result.json').write_text(json.dumps(dict(status='failed',sources=records,error=str(exc)),indent=2)+'\n');raise
if __name__=='__main__':raise SystemExit(main())
