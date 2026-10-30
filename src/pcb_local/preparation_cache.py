"""Batch-local reuse of immutable, read-only net-class preparation.

This never caches native board inspection, refill, connectivity or candidate
DRC acceptance. Entries live in memory in one batch, are bounded, and retain
the original command as provenance rather than claiming it ran on a hit.
"""
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import time

from .jobs import Halt, write_json
from .workspace import source_files, sha256


_KRT_FILES = ('design_rules.py', 'list_nets.py', 'netclass_resolver.py')
_OUTPUT_SUFFIXES = ('.json', '.native.json', '.native.stdout.log', '.native.stderr.log')


class _CacheUnavailable(ValueError):
    pass


def _native_module_identity(config, check):
    # The supported native runtime is the bundled KiCad Python prefix. Do not
    # infer module identity from the Python launcher or version number alone.
    # Unknown layouts run the ordinary probe without caching.
    prefix = Path(config['kicad_python']).absolute().parent.parent
    wrappers = list(prefix.glob('lib/python*/site-packages/pcbnew.py'))
    if len(wrappers) != 1:
        raise _CacheUnavailable('Native pcbnew module location is not unambiguous')
    wrapper = wrappers[0]
    extensions = list(wrapper.parent.glob('_pcbnew*.so'))
    if len(extensions) != 1:
        raise _CacheUnavailable('Native pcbnew binary location is not unambiguous')
    # A PYTHONPATH shadow would invalidate the assumption about module lookup.
    for directory in os.environ.get('PYTHONPATH', '').split(os.pathsep):
        if not directory:
            continue
        folder = Path(directory).absolute()
        if folder != wrapper.parent and ((folder / 'pcbnew.py').exists() or
                                        (folder / 'pcbnew/__init__.py').exists()):
            raise _CacheUnavailable('Native pcbnew can be shadowed by PYTHONPATH')
    return {str(path): sha256(path, check) for path in [wrapper, *extensions]}


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _source_hashes(board, check):
    return {str(board.with_suffix(ext)): sha256(board.with_suffix(ext), check)
            if board.with_suffix(ext).is_file() else None
            for ext in ('.kicad_pcb', '.kicad_pro', '.kicad_dru')}


def _identity(board, config, check):
    check()
    files = source_files(board, check)
    context = board.parent / 'library-context.json'
    # Relative paths permit identical saved-project snapshots to share an entry.
    # The complete project context (including resolved local libraries) is bound.
    relative = {Path(p).relative_to(board.parent).as_posix(): h for p, h in files.items()}
    krt = {name: sha256(Path(config['krt_root']) / 'py_router' / name, check)
           for name in _KRT_FILES}
    executables = {name: sha256(Path(config[name]), check)
                   for name in ('router_python', 'kicad_python', 'kicad_cli')}
    value = dict(schema_version=1, files=relative, krt_netclass_sources=krt,
                 context_sha256=sha256(context, check) if context.is_file() else None,
                 source_fingerprint=config['source_fingerprint'],
                 verified_kicad_version=config['verified_kicad_version'],
                 drc_runtime_manifest=config.get('drc_runtime_manifest_sha256'),
                 executables=executables,
                 native_modules=_native_module_identity(config, check),
                 executable_paths={name: str(Path(config[name]).absolute()) for name in executables},
                 probe_sha256=sha256(Path(__file__).with_name('netclass_probe.py'), check),
                 environment_sha256=_digest(_json(dict(os.environ))))
    check()
    return _digest(_json(value)), files, krt


@dataclass(frozen=True)
class _Entry:
    outputs: tuple
    origin_board: str
    origin_probe: str
    elapsed_seconds: float

    @property
    def size(self):
        return sum(len(data) for _suffix, data, _sha in self.outputs)


class NetclassPreparationCache:
    """One explicit cache instance per batch; no disk/global/service cache."""

    def __init__(self, max_entries=8, max_bytes=16 * 1024 * 1024):
        if type(max_entries) is not int or max_entries < 1 or type(max_bytes) is not int or max_bytes < 1:
            raise ValueError('Preparation cache bounds must be positive integers')
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries = OrderedDict()

    def _load(self, entry, config, krt):
        blobs = {}
        for suffix, data, digest in entry.outputs:
            if suffix in blobs or suffix not in _OUTPUT_SUFFIXES or _digest(data) != digest:
                raise ValueError('Cached preparation payload identity mismatch')
            blobs[suffix] = data
        if set(blobs) != set(_OUTPUT_SUFFIXES):
            raise ValueError('Incomplete cached preparation outputs')
        report = json.loads(blobs['.json'])
        native = json.loads(blobs['.native.json'])
        if (report.get('passed') is not True or report.get('inputs_unchanged') is not True
                or report.get('native_exit_code') != 0 or report.get('krt', {}).get('passed') is not True
                or report.get('native') != native or native.get('version') != config['verified_kicad_version']
                or report.get('krt_source_hashes') != krt or report.get('board') != entry.origin_board):
            raise ValueError('Cached preparation is not a complete successful comparison')
        return blobs, report

    def run(self, job, config, board, probe, producer):
        """Run/reuse producer() -> int, materializing probe and native sidecars.

        Caller still runs native-preflight and checks the returned report in the
        usual way. Cancellation/protection exceptions propagate unchanged.
        """
        board, probe = Path(board).resolve(), Path(probe).resolve()
        started = time.monotonic()
        record = dict(schema_version=1, kind='netclass-preparation-only', status='starting',
                      candidate_validation_reused=False, native_preflight_reused=False)
        try:
            try:
                key, frozen, krt = _identity(board, config, job.check)
            except _CacheUnavailable as exc:
                record.update(status='bypass', reason=str(exc), executed_subprocess=True)
                return producer()
            record['identity_sha256'] = key
            initial_sources = _source_hashes(board, job.check)
            entry = self._entries.get(key)
            if entry is not None:
                try:
                    blobs, report = self._load(entry, config, krt)
                    old_sources = {str(Path(entry.origin_board).with_suffix(ext)): initial_sources[str(board.with_suffix(ext))]
                                   for ext in ('.kicad_pcb', '.kicad_pro', '.kicad_dru')}
                    if report.get('source_hashes') != old_sources:
                        raise ValueError('Cached source hash binding differs')
                except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                    self._entries.pop(key, None)
                    record['invalidated_reason'] = str(exc)
                    entry = None
            if entry is not None:
                job.check()
                if any(probe.with_suffix(s).exists() for s in _OUTPUT_SUFFIXES):
                    raise ValueError('Preparation output already exists; refusing to overwrite evidence')
                if _identity(board, config, job.check)[0] != key:
                    raise Halt('protection_violation', 'Preparation inputs/runtime changed before cache reuse')
                report.update(board=str(board), source_hashes=initial_sources,
                              preparation_cache=dict(reused=True, origin_board=entry.origin_board,
                                                     origin_probe=entry.origin_probe,
                                                     original_report_sha256=_digest(blobs['.json']),
                                                     identity_sha256=key,
                                                     native_command_is_historical=True))
                # Native command/exit evidence remains the actual original one;
                # only the read-only data's current input binding is rebased.
                probe.parent.mkdir(parents=True, exist_ok=True)
                for suffix in _OUTPUT_SUFFIXES[1:]:
                    job.check()
                    probe.with_suffix(suffix).write_bytes(blobs[suffix])
                write_json(probe, report)
                if source_files(board, job.check) != frozen:
                    raise Halt('protection_violation', 'Preparation input changed during reuse')
                self._entries.move_to_end(key)
                record.update(status='hit', origin_probe=entry.origin_probe,
                              original_computation_seconds=entry.elapsed_seconds,
                              executed_subprocess=False)
                return 0
            record.update(status='miss', executed_subprocess=True)
            compute_started = time.monotonic()
            code = producer()
            compute_seconds = time.monotonic() - compute_started
            job.check()
            if _identity(board, config, job.check)[0] != key:
                raise Halt('protection_violation', 'Preparation inputs/runtime changed during computation')
            if code:
                record.update(status='not_cached', reason='Producer did not succeed', producer_exit_code=code)
                return code
            try:
                outputs = tuple((s, probe.with_suffix(s).read_bytes()) for s in _OUTPUT_SUFFIXES)
                entry = _Entry(tuple((s, b, _digest(b)) for s, b in outputs), str(board), str(probe), compute_seconds)
                _blobs, report = self._load(entry, config, krt)
                if report.get('source_hashes') != initial_sources:
                    raise ValueError('Producer source hash binding differs')
            except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
                record.update(status='not_cached', reason=str(exc))
                return code
            if entry.size > self.max_bytes:
                record.update(status='not_cached', reason='Entry exceeds memory bound')
                return code
            self._entries[key] = entry
            while len(self._entries) > self.max_entries or sum(e.size for e in self._entries.values()) > self.max_bytes:
                self._entries.popitem(last=False)
            record.update(status='stored', payload_bytes=entry.size, computation_seconds=compute_seconds)
            return code
        except Halt as exc:
            record.update(status=exc.status, reason=str(exc))
            raise
        except BaseException as exc:
            record.update(status='error', reason=type(exc).__name__ + ': ' + str(exc))
            raise
        finally:
            record['elapsed_seconds'] = time.monotonic() - started
            record['entries'] = len(self._entries)
            record['memory_bytes'] = sum(e.size for e in self._entries.values())
            write_json(job.folder / 'preparation-cache.json', record)
