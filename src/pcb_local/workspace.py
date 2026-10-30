"""Immutable input snapshots and explicit local runtime configuration."""
import hashlib
import json
from pathlib import Path
import re
import shutil

SIDECARS = (".kicad_pro", ".kicad_dru", ".kicad_prl", ".design-brief.json")


MAX_CONTEXT_BYTES = 256 * 1024 * 1024
MAX_CONTEXT_FILES = 10000


def sha256(path, check=None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            if check:
                check()
            digest.update(block)
    return digest.hexdigest()


def source_files(board, check=None):
    paths = [board] + [board.with_suffix(ext) for ext in SIDECARS if board.with_suffix(ext).is_file()]
    for table in ("fp-lib-table", "sym-lib-table"):
        table_path = board.parent / table
        if table_path.is_file():
            paths.append(table_path)
            for relative in re.findall(r'\$\{KIPRJMOD\}/([^"\n]+)', table_path.read_text(encoding="utf-8")):
                part = Path(relative)
                if not part.parts or part.is_absolute() or ".." in part.parts:
                    raise ValueError("Unsupported project-relative library path: " + relative)
                library = board.parent / part
                if library.is_dir():
                    for item in library.rglob("*"):
                        if check:
                            check()
                        if item.is_file():
                            paths.append(item)
                            if len(paths) > MAX_CONTEXT_FILES:
                                raise ValueError("Project context exceeds 10000-file v0.1 limit")
                elif library.is_file():
                    paths.append(library)
    if len(paths) > MAX_CONTEXT_FILES or sum(p.stat().st_size for p in paths) > MAX_CONTEXT_BYTES:
        raise ValueError("Project context exceeds v0.1 limit (10000 files / 256 MiB)")
    return {str(p): sha256(p, check) for p in paths}


def assert_unchanged(board, hashes):
    if source_files(board) != hashes:
        raise ValueError("Source board/project/context changed during the task; results cannot be accepted")


def check_output_location(board, output):
    for table in ("fp-lib-table", "sym-lib-table"):
        source = board.parent / table
        if not source.is_file():
            continue
        for relative in re.findall(r'\$\{KIPRJMOD\}/([^"\n]+)', source.read_text(encoding="utf-8")):
            origin = (board.parent / relative).resolve()
            if origin == output or origin in output.parents:
                raise ValueError("Task output must not be inside a source library: " + str(origin))


def copy_project(board, destination):
    from .validation import copy_library_context
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    target = destination / "board.kicad_pcb"
    shutil.copy2(board, target)
    for suffix in SIDECARS:
        source = board.with_suffix(suffix)
        if source.is_file():
            shutil.copy2(source, target.with_suffix(suffix))
    context = copy_library_context(board, destination, board)
    return target, context


def load_config(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_config(data)


def validate_config(data):
    for key in ("krt_root", "router_python", "kicad_cli", "kicad_python"):
        if key not in data:
            raise ValueError("Missing runtime setting: " + key)
        # Resolving a virtualenv's Python symlink changes sys.prefix and loses
        # its installed packages. Keep executable paths inside their venv.
        path = Path(data[key]).expanduser()
        data[key] = str(path.resolve() if key == "krt_root" else path.absolute())
        if not Path(data[key]).exists():
            raise ValueError("Runtime path does not exist: " + key + " = " + data[key])
    if not (Path(data["krt_root"]) / "py_router/route.py").is_file():
        raise ValueError("krt_root does not contain py_router/route.py")
    if "pns" in data:
        pns = data["pns"]
        for key in ("python", "module_dir"):
            pns[key] = str(Path(pns[key]).expanduser().absolute())
            if not Path(pns[key]).exists():
                raise ValueError("PNS runtime path does not exist: " + key)
        module = Path(pns["module_dir"]) / "kicad_rl_router.so"
        if not module.is_file() or sha256(module) != pns.get("module_sha256"):
            raise ValueError("PNS module differs from the pinned compiled runtime")
    from .drc_complete import load_runtime
    native_runtime=load_runtime(data['kicad_cli'])
    if native_runtime and data.get('drc_runtime_manifest_sha256')!=native_runtime['manifest_sha256']:
        raise ValueError('Complete DRC runtime manifest differs from configured identity; configure explicitly')
    if not native_runtime and data.get('drc_runtime_manifest_sha256'):
        raise ValueError('Configured complete DRC runtime manifest is missing')
    data.setdefault("verified_kicad_version", "10.0.4")
    return data


def runtime_fingerprint(config):
    root = Path(config["krt_root"])
    paths = list((root / "py_router").rglob("*.py"))
    paths += list((root / "rust_router").glob("grid_router.*"))
    result = {str(p.relative_to(root)): sha256(p) for p in sorted(paths) if p.is_file()}
    if "pns" in config:
        module = Path(config["pns"]["module_dir"])/"kicad_rl_router.so"
        result["__pns__/module"] = sha256(module)
    if config.get('drc_runtime_manifest_sha256'):
        from .drc_complete import load_runtime
        native=load_runtime(config['kicad_cli'])
        if not native or native['manifest_sha256']!=config['drc_runtime_manifest_sha256']:
            raise ValueError('Complete DRC runtime changed')
        result['__drc__/manifest']=native['manifest_sha256']
    return result
