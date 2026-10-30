"""Identity-bound native KiCad 10.0.4 extended DRC report collection.

Only report capacity changes. Native providers, authored rules, severities,
timeouts and the requirement to witness all successful providers stay intact.
The ordinary installed KiCad path keeps its existing 199/499 fail-closed gate.
"""
import hashlib
import json
from pathlib import Path
import re

SOURCE_COMMIT = 'f7414d419cae5df2d00e7eaacb16fc0e803799bc'
PATCH_SHA256 = '17cd7317c8ad9c1d239a37e279caccd851e660929f4f93dd0853850dbba138f4'
REPORT_CAPACITY = 2147483647
MANIFEST_NAME = 'pcb-local-drc-runtime.json'
TRACE_TEXT = 'PCB_LOCAL_DRC_REPORT_CAPACITY per_type=2147483647 extended=2147483647'
TRACE = re.compile(r'.*?Trace: (?:\(KICAD_DRC_PROFILE\) )?' + re.escape(TRACE_TEXT) + r'\Z')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_runtime(cli):
    """Validate the manifest adjacent to the explicitly configured CLI.

    No search, PATH fallback or automatic binary replacement is performed.
    Missing manifest means ordinary KiCad; a present but invalid one is fatal.
    """
    cli = Path(cli).expanduser().absolute()
    manifest_path = cli.with_name(MANIFEST_NAME)
    if not manifest_path.exists():
        return None
    record = json.loads(manifest_path.read_text())
    if (record.get('schema_version') != 1 or record.get('source_commit') != SOURCE_COMMIT
            or record.get('patch_sha256') != PATCH_SHA256 or record.get('kicad_version') != '10.0.4'
            or record.get('per_type_capacity') != REPORT_CAPACITY
            or record.get('extended_capacity') != REPORT_CAPACITY):
        raise ValueError('Unsupported complete-DRC runtime identity or capacity')
    root = Path(record['runtime_root']).resolve()
    files = record.get('files', {})
    expected_cli = root / record['cli_relative']
    if not files or expected_cli.resolve() != cli.resolve() or record['cli_relative'] not in files:
        raise ValueError('Complete-DRC manifest does not identify the requested CLI')
    if not any(name.endswith('/_pcbnew.kiface') for name in files):
        raise ValueError('Complete-DRC manifest omits the native PCB provider module')
    for relative, digest in files.items():
        path = root / relative
        if (Path(relative).is_absolute() or '..' in Path(relative).parts
                or not path.resolve().is_relative_to(root) or not path.is_file()
                or not re.fullmatch(r'[0-9a-f]{64}', digest) or sha256(path) != digest):
            raise ValueError('Complete-DRC runtime file changed or escaped its root: ' + relative)
    for filename, digest in record.get('external_files', {}).items():
        path = Path(filename)
        if (not path.is_absolute() or not path.is_file()
                or not re.fullmatch(r'[0-9a-f]{64}', digest) or sha256(path) != digest):
            raise ValueError('Complete-DRC linked runtime dependency changed: ' + filename)
    return dict(record, manifest=str(manifest_path), manifest_sha256=sha256(manifest_path),
                verified=True)


def environment(runtime):
    return {'KICAD_RUN_FROM_BUILD_DIR': '1'} if runtime else {}


def capacity_trace(stderr, runtime):
    """Separate the pinned capacity witness from ordinary native diagnostics.

    A marker alone never authorizes larger report counts. All files must have
    passed load_runtime and exactly one marker must identify this invocation.
    """
    lines = stderr.splitlines()
    marker_lines = [line for line in lines if 'PCB_LOCAL_DRC_REPORT_CAPACITY' in line]
    if runtime is None:
        if marker_lines:
            raise ValueError('Untrusted DRC report-capacity marker without a pinned runtime')
        return stderr, None
    if runtime.get('verified') is not True:
        raise ValueError('Complete-DRC runtime was not verified')
    if len(marker_lines) != 1 or not TRACE.fullmatch(marker_lines[0].strip()):
        raise ValueError('Native DRC report capacity was not witnessed exactly once')
    remaining = '\n'.join(line for line in lines if line != marker_lines[0])
    evidence = dict(per_type=REPORT_CAPACITY, extended=REPORT_CAPACITY,
                    manifest_sha256=runtime['manifest_sha256'], source_commit=SOURCE_COMMIT,
                    patch_sha256=PATCH_SHA256, native_trace=marker_lines[0],
                    policy='Only native diagnostic report capacity changes; rules, providers and time budgets remain unchanged')
    return remaining, evidence
