#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a separately pinned KiCad 10.0.4 CLI with complete-report capacity.

Verified platform: macOS arm64. Requires existing Homebrew build libraries and
Python 3.12. Does not modify the installed KiCad or the routing engine. Source
fetching is sparse and excludes demos/qa; no board collection is downloaded.
"""
import argparse,hashlib,json,os,platform,shutil,subprocess,sys,sysconfig,tarfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from pcb_local.drc_complete import SOURCE_COMMIT,PATCH_SHA256,REPORT_CAPACITY,MANIFEST_NAME
SWIG_URL='https://downloads.sourceforge.net/project/swig/swig/swig-4.2.1/swig-4.2.1.tar.gz'
SWIG_SHA256='fa045354e2d048b2cddc69579e4256245d4676894858fcf0bab2290ecf59b7d8'


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def build_manifest(build,source,cli):
    files={str(p.relative_to(build)):sha(p) for p in (build/'kicad/KiCad.app').rglob('*')
           if p.is_file() and (p.suffix in ('.dylib','.kiface') or p==cli)}
    for module in ('_pcbnew.kiface','_eeschema.kiface'):
        if not any(k.endswith('/'+module) for k in files):raise ValueError('Native module was not built: '+module)
    # Record non-system linked libraries recursively. macOS dyld-cache system
    # libraries are supplied by the OS and are not copied or hashed as files.
    external={}; pending=[build/name for name in files]; seen=set()
    while pending:
        binary=pending.pop().resolve()
        if binary in seen:continue
        seen.add(binary)
        lines=subprocess.check_output(['otool','-L',str(binary)],text=True).splitlines()[1:]
        for line in lines:
            name=line.strip().split(' (compatibility version',1)[0]
            if not name.startswith('/') or name.startswith(('/usr/lib/','/System/')):continue
            path=Path(name)
            if not path.is_file():raise ValueError('Linked dependency is missing: '+name)
            if path.resolve().is_relative_to(build):continue
            external[str(path.resolve())]=sha(path);pending.append(path)
    result=dict(schema_version=1,source_commit=SOURCE_COMMIT,patch_sha256=PATCH_SHA256,
        kicad_version='10.0.4',per_type_capacity=REPORT_CAPACITY,extended_capacity=REPORT_CAPACITY,
        runtime_root=str(build),cli_relative=str(cli.relative_to(build)),files=files,external_files=external,
        source_root=str(source),scope='Report capacity plus diagnostic trace, empty-rpath quoting and disabled-IPC GUI event guards; all native DRC providers/rules remain authoritative')
    path=cli.with_name(MANIFEST_NAME);path.write_text(json.dumps(result,indent=2)+'\n');return path


def build_toolchain_record(build,logs,swig):
    commands={
        'cmake':['cmake','--version'], 'ninja':['ninja','--version'],
        'clang':['clang','--version'], 'swig':[str(swig),'-version'],
        'wxwidgets':['/opt/homebrew/bin/wx-config-3.2','--version'],
        'sdk':['xcrun','--show-sdk-version'],
        'brew':['/opt/homebrew/bin/brew','list','--versions','cmake','ninja','wxwidgets@3.2',
                'boost@1.85','opencascade','libngspice','protobuf','abseil','libgit2','cairo',
                'pixman','freetype','fontconfig','harfbuzz','zstd','unixodbc','pcre2'],
    }
    evidence={}
    for label,argv in commands.items():
        r=subprocess.run(argv,capture_output=True,text=True,timeout=30)
        evidence[label]=dict(argv=argv,returncode=r.returncode,stdout=r.stdout,stderr=r.stderr)
    cache=build/'CMakeCache.txt'
    result=dict(platform=platform.platform(),machine=platform.machine(),python=sys.version,
                python_executable=sys.executable,commands=evidence,
                cmake_cache_sha256=sha(cache),configure_log=str(logs/'configure/output.log'),
                limitation='Same macOS host and preinstalled development libraries; not a clean-OS or cross-platform build claim')
    path=logs/'toolchain.json';path.write_text(json.dumps(result,indent=2)+'\n');return path


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('source-root','build-dir','run-dir'):p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--proxy');p.add_argument('--reuse-build',action='store_true');p.add_argument('--jobs',type=int,default=2)
    p.add_argument('--swig-executable',type=Path,help='Existing SWIG 4.2.1; otherwise build the hash-pinned release privately')
    a=p.parse_args();source=a.source_root.absolute();build=a.build_dir.absolute();logs=a.run_dir.absolute()
    if logs.exists():raise ValueError('Run directory must be new')
    logs.mkdir(parents=True);report=dict(status='running',source=str(source),build=str(build));result_path=logs/'setup-result.json'
    def save():result_path.write_text(json.dumps(report,indent=2)+'\n')
    def run(label,argv,timeout=300,cwd=None):
        command=[sys.executable,str(ROOT/'scripts/run_logged.py'),'--run-dir',str(logs/label),'--timeout',str(timeout)]
        if cwd:command+=['--cwd',str(cwd)]
        code=subprocess.call(command+['--',*map(str,argv)])
        if code:raise RuntimeError('Failed stage '+label)
    def git(*args):return subprocess.check_output(['git','-C',str(source),*args],text=True).strip()
    try:
        if sys.platform!='darwin' or platform.machine()!='arm64' or sys.version_info[:2]!=(3,12):
            raise ValueError('Verified build requires macOS arm64 with Python 3.12')
        if a.jobs not in (1,2):raise ValueError('Use 1 or 2 build workers on the verified toolchain')
        patch=ROOT/'patches/kicad10-drc-report-capacity.patch'
        if sha(patch)!=PATCH_SHA256:raise ValueError('DRC source patch does not match the pinned adapter')
        if not source.exists():
            opts=['git']+(['-c','http.proxy='+a.proxy] if a.proxy else [])
            run('clone',opts+['clone','--filter=blob:none','--no-checkout','--depth','1','--single-branch','--branch','10.0.4','https://github.com/KiCad/kicad-source-mirror.git',source])
            if a.proxy:subprocess.check_call(['git','-C',str(source),'config','http.proxy',a.proxy])
            run('sparse',['git','-C',source,'sparse-checkout','set','--no-cone','/*','!/demos/','!/qa/'])
            run('checkout',['git','-C',source,'checkout',SOURCE_COMMIT],360)
        if git('rev-parse','HEAD')!=SOURCE_COMMIT:raise ValueError('Source commit mismatch')
        diff=subprocess.check_output(['git','-C',str(source),'diff','--binary'])
        if not diff:
            run('patch',['git','-C',source,'apply',patch])
            diff=subprocess.check_output(['git','-C',str(source),'diff','--binary'])
        if diff!=patch.read_bytes():raise ValueError('Source has changes outside the pinned minimal patch')
        if git('diff','--cached','--name-only'):raise ValueError('Unexpected staged source changes')
        if build.exists() and any(build.iterdir()):
            cache=build/'CMakeCache.txt'
            if not a.reuse_build or not cache.is_file() or 'CMAKE_HOME_DIRECTORY:INTERNAL='+str(source) not in cache.read_text():
                raise ValueError('Existing build requires --reuse-build and the same source root')
        ng=Path('/opt/homebrew/opt/libngspice/lib/libngspice.dylib')
        if not ng.is_file():raise ValueError('Install the shared libngspice development package')
        swig=a.swig_executable.absolute() if a.swig_executable else None
        if swig is None:
            archive=logs/'swig-4.2.1.tar.gz'
            command=['curl','--fail','--location','--connect-timeout','10','--max-time','180']
            if a.proxy:command+=['--proxy',a.proxy]
            run('swig-download',command+[SWIG_URL,'--output',archive],200)
            if sha(archive)!=SWIG_SHA256:raise ValueError('SWIG release archive checksum mismatch')
            folder=logs/'swig-source';folder.mkdir()
            with tarfile.open(archive) as tar:tar.extractall(folder,filter='data')
            swig_source=folder/'swig-4.2.1';prefix=logs/'swig-install'
            run('swig-configure',[swig_source/'configure','--prefix='+str(prefix),'--without-alllang','--with-pcre-prefix=/opt/homebrew'],120,swig_source)
            run('swig-build',['make','-j'+str(a.jobs)],300,swig_source)
            run('swig-install',['make','install'],60,swig_source)
            swig=prefix/'bin/swig'
        version=subprocess.check_output([str(swig),'-version'],text=True)
        if 'SWIG Version 4.2.1\n' not in version:raise ValueError('This source/toolchain needs pinned SWIG 4.2.1')
        report['swig']=dict(executable=str(swig),version='4.2.1',sha256=sha(swig),archive_url=SWIG_URL,archive_sha256=SWIG_SHA256)
        argv=['cmake','-S',source,'-B',build,'-G','Ninja','-DCMAKE_BUILD_TYPE=Release',
            '-DKICAD_SCRIPTING_WXPYTHON=OFF','-DKICAD_BUILD_I18N=OFF','-DKICAD_BUILD_QA_TESTS=OFF',
            '-DKICAD_INSTALL_DEMOS=OFF','-DKICAD_USE_SENTRY=OFF','-DKICAD_SPICE_QA=OFF',
            '-DKICAD_SIGNAL_INTEGRITY=OFF','-DKICAD_IPC_API=OFF','-DKICAD_UPDATE_CHECK=OFF',
            '-DCMAKE_IGNORE_PREFIX_PATH=/opt/anaconda3','-DwxWidgets_CONFIG_EXECUTABLE=/opt/homebrew/bin/wx-config-3.2',
            '-DBOOST_ROOT=/opt/homebrew/opt/boost@1.85','-DOCC_INCLUDE_DIR=/opt/homebrew/include/opencascade',
            '-DOCC_LIBRARY_DIR=/opt/homebrew/lib','-DPYTHON_EXECUTABLE='+sys.executable,
            '-DPYTHON_INCLUDE_DIR='+sysconfig.get_path('include'),
            '-DPYTHON_LIBRARY='+str(Path(sysconfig.get_config_var('LIBDIR'))/sysconfig.get_config_var('LDLIBRARY')),
            '-DSWIG_EXECUTABLE='+str(swig),
            '-DNGSPICE_LIBRARY='+str(ng),'-DNGSPICE_INCLUDE_DIR=/opt/homebrew/opt/libngspice/include','-Wno-dev']
        save();run('configure',argv,180)
        report['toolchain']=str(build_toolchain_record(build,logs,swig));save()
        run('build',['cmake','--build',build,'--parallel',a.jobs,'--target','kicad-cli','pcbnew_kiface','eeschema_kiface'],7200)
        cli=build/'kicad/KiCad.app/Contents/MacOS/kicad-cli'
        run('version',[cli,'--version'],30)
        manifest=build_manifest(build,source,cli)
        report.update(status='built',cli=str(cli),manifest=str(manifest),manifest_sha256=sha(manifest),
                      source_commit=SOURCE_COMMIT,patch_sha256=PATCH_SHA256,
                      validation='Build/identity only; native comparison and acceptance tests still required')
        save();print(json.dumps(report));return 0
    except Exception as exc:
        report.update(status='failed',error=type(exc).__name__+': '+str(exc));save();raise


if __name__=='__main__':raise SystemExit(main())
