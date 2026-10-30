"""Audited import of a bounded old KiCad format into an independent project.

Import is not a repair and never authorizes adoption into the original board.
Original content and rules remain immutable; unsupported conversion is refused.
"""
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

from .jobs import Job, Halt, write_json
from .workspace import source_files, load_config, runtime_fingerprint, check_output_location
from .validation import parse_sexpr, field, name, canonical, Quoted, rule_support
from .scope_candidate import _serialize

EXIT = {'complete': 0, 'unsupported': 20, 'protection_violation': 31,
        'verification_incomplete': 40, 'error': 50, 'timed_out': 124, 'cancelled': 130}
IDENTIFIED = {'module', 'footprint', 'pad', 'segment', 'via', 'gr_line', 'gr_circle',
              'gr_text', 'fp_line', 'fp_circle', 'fp_text'}
TOP = {'version', 'host', 'generator', 'general', 'page', 'paper', 'title_block',
       'layers', 'setup', 'net', 'net_class', 'module', 'footprint', 'segment', 'via',
       'gr_line', 'gr_circle', 'gr_text'}
FP = {'layer', 'tedit', 'tstamp', 'uuid', 'at', 'descr', 'tags', 'path', 'attr',
      'autoplace_cost90', 'autoplace_cost180', 'solder_mask_margin', 'solder_paste_margin',
      'solder_paste_ratio', 'clearance', 'zone_connect', 'thermal_width', 'thermal_gap',
      'pad', 'fp_line', 'fp_circle', 'fp_text', 'model'}


def normalized_uuid(value):
    value = str(value)
    if re.fullmatch(r'[0-9A-Fa-f]{1,8}', value):
        result = uuid.UUID(int=int(value, 16))
        if not result.int:
            raise ValueError('Null stored identity is ambiguous')
        return str(result)
    try:
        result = uuid.UUID(value)
        if not result.int:
            raise ValueError('Null stored identity is ambiguous')
        return str(result)
    except ValueError:
        raise ValueError('Invalid stored identity; refusing to replace it silently')


def strip_identity(node):
    if not isinstance(node, list):
        return node
    return [strip_identity(x) for x in node if name(x) not in {'uuid', 'tstamp'}]


def prepare(text):
    """Add only deterministic identities; retain old grammar/version and geometry."""
    roots = parse_sexpr(text)
    if len(roots) != 1 or name(roots[0]) != 'kicad_pcb':
        raise ValueError('Expected one complete PCB')
    original = roots[0]
    version = field(original, 'version')
    if not version or len(version) != 2 or not str(version[1]).isdigit():
        raise ValueError('Missing board format version')
    if not 20171130 <= int(version[1]) < 20201115:
        raise ValueError('Import supports the KiCad 5 S-expression subset only (20171130..20201114)')
    if any(name(x) == 'zone' for x in original):
        raise ValueError('Legacy zones require a separate fill-semantics proof; best-effort zone conversion is not accepted')
    unknown = sorted({name(x) for x in original[1:] if isinstance(x, list)} - TOP)
    if unknown:
        raise ValueError('Unsupported legacy board objects: ' + ', '.join(unknown))
    for item in (x for x in original if name(x) in {'segment', 'via'}):
        status = field(item, 'status')
        if status and (len(status) != 2 or not re.fullmatch('0+', str(status[1]))):
            raise ValueError('Nonzero legacy status flags have no witnessed modern protection semantics')
    for fp in (x for x in original if name(x) in {'module', 'footprint'}):
        unknown = sorted({name(x) for x in fp[1:] if isinstance(x, list)} - FP)
        if unknown:
            raise ValueError('Unsupported legacy footprint objects: ' + ', '.join(unknown))
        if any(name(x) == 'tags' and any('net tie' in str(v).lower() for v in x[1:]) for x in fp):
            raise ValueError('Legacy implicit net-tie semantics require a separate proof')
        for pad in (x for x in fp if name(x) == 'pad'):
            if len(pad) < 4 or str(pad[2]) not in {'smd', 'thru_hole', 'np_thru_hole', 'connect'} or str(pad[3]) not in {'circle', 'rect', 'oval'}:
                raise ValueError('Only ordinary circular, rectangular and oval legacy pads are supported')
    result = copy.deepcopy(original)
    identity_map, seen = [], set()
    source_sha256 = hashlib.sha256(text.encode('utf-8')).hexdigest()

    def walk(node, path):
        if not isinstance(node, list):
            return
        if name(node) in IDENTIFIED:
            fields = [x for x in node if name(x) in {'uuid', 'tstamp'}]
            if len(fields) > 1 or (fields and len(fields[0]) != 2):
                raise ValueError('Ambiguous identity at ' + repr(path))
            old = str(fields[0][1]) if fields else None
            identity = normalized_uuid(old) if old else str(uuid.uuid5(uuid.NAMESPACE_URL,
                'pcb-local-legacy:' + source_sha256 + ':' + '/'.join(map(str, path))))
            if identity in seen:
                raise ValueError('Duplicate legacy identity; original mapping is ambiguous')
            seen.add(identity)
            if fields:
                fields[0][1] = identity
            else:
                node.append(['tstamp', identity])
            identity_map.append({'ast_path': path, 'kind': name(node), 'old_identity': old,
                                 'new_uuid': identity, 'generated': old is None})
        for index, child in enumerate(node):
            if isinstance(child, list):
                walk(child, path + [index])
    walk(result, [])
    if not identity_map or not any(x['kind'] == 'pad' for x in identity_map):
        raise ValueError('No witnessed legacy pads')
    if canonical(strip_identity(original)) != canonical(strip_identity(result)):
        raise ValueError('Identity enrichment changed authored content')
    output = _serialize(result) + '\n'
    if canonical(parse_sexpr(output)[0]) != canonical(result):
        raise ValueError('Identity serialization is not lossless')
    return output, {'schema_version': 1, 'source_sha256': source_sha256,
                    'source_version': int(version[1]), 'identity_map': identity_map,
                    'authored_content_except_identity_equal': True,
                    'scope': 'Only identity fields; no version, module, rule or geometry rewrite'}


def native_semantics(text):
    """Compare full native serialization, retaining order within geometric paths."""
    roots = parse_sexpr(text)
    if len(roots) != 1 or name(roots[0]) != 'kicad_pcb':
        raise ValueError('Expected one native serialized PCB')
    def transform(node):
        if not isinstance(node, list):
            return canonical(node)
        children = [x for x in node[1:] if name(x) not in {'uuid', 'tstamp'}
                    and not (name(node) == 'kicad_pcb' and name(x) in {'generator', 'generator_version'})]
        if name(node) == 'layers':
            # KiCad 10's first reload adds these two built-in display aliases.
            # IDs, standard layer names, types, order and every custom alias
            # remain exact. This does not alter copper geometry or layer names.
            aliases = {'F.SilkS': 'F.Silkscreen', 'B.SilkS': 'B.Silkscreen'}
            children = [x[:3] if isinstance(x,list) and len(x)==4
                and aliases.get(str(x[1]))==str(x[3]) else x for x in children]
        values = [transform(x) for x in children]
        # Native object iteration may follow UUID ordering. Polygon vertex,
        # layer-stack and every other nested sequence is deliberately unsorted.
        if name(node) in {'kicad_pcb', 'footprint'}:
            atoms = [transform(x) for x in children if not isinstance(x, list)]
            nodes = sorted((transform(x) for x in children if isinstance(x, list)), key=repr)
            values = atoms + nodes
        return (name(node), tuple(values))
    return transform(roots[0])


def check_imported(report):
    """Revalidate a successful import before a UI selects its independent board."""
    from .delivery import plain_path
    from .workspace import sha256
    if (report.get('command') != 'import-legacy' or report.get('status') != 'complete'
            or report.get('accepted') is not False or report.get('exit_code') != 0):
        raise ValueError('Only a completed, audited import can be selected')
    folder = plain_path(report['task_dir'])
    source, imported = plain_path(report['source_board']), plain_path(report['imported_board'])
    if imported != folder/'imported/board.kicad_pcb':
        raise ValueError('Imported board is not the audited output location')
    if source_files(source) != report['source_hashes'] or source_files(imported) != report['imported_hashes']:
        raise ValueError('Original or imported project changed after import')
    conversion = report.get('conversion', {})
    converted = plain_path(conversion.get('converted', ''))
    if converted != folder/'audit/converted/board.kicad_pcb':
        raise ValueError('Conversion board is not the audited intermediate')
    if source_files(converted) != conversion.get('converted_hashes'):
        raise ValueError('Audited conversion changed after verification')
    if relative_hashes(imported, report['imported_hashes']) != relative_hashes(converted, conversion['converted_hashes']):
        raise ValueError('Imported project is not byte-identical to the audited conversion')
    if not report.get('audit_hashes'):
        raise ValueError('Import audit evidence changed or is invalid')
    for name, digest in report['audit_hashes'].items():
        if Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('Import audit evidence path is invalid')
        if sha256(plain_path(folder/name)) != digest:
            raise ValueError('Import audit evidence changed')
    for key in ('conversion', 'verification'):
        record = json.loads((folder/'audit'/(key+'.json')).read_text())
        if record != report[key] or record.get('status') != 'complete' or not record.get('checks') or not all(record['checks'].values()):
            raise ValueError('Import proof is incomplete: ' + key)
    if report['verification'].get('conversion_sha256') != report['audit_hashes'].get('audit/conversion.json'):
        raise ValueError('Verification is not bound to the conversion proof')
    for name,digest in report['verification'].get('evidence_hashes', {}).items():
        if report['audit_hashes'].get('audit/'+name) != digest:
            raise ValueError('Published evidence differs from the verified evidence')
    if sha256(imported.parent/'pcb-local-import.json') != report.get('import_receipt_sha256'):
        raise ValueError('Import origin receipt changed')
    return report


def relative_hashes(board, hashes):
    return {str(Path(path).relative_to(board.parent)): digest for path,digest in hashes.items()}


def workflow(args):
    job = Job(args.out_dir, parent_check=getattr(args, '_parent_check', None))
    report = {'schema_version': 1, 'command': 'import-legacy', 'status': 'error',
              'accepted': False, 'candidate': None, 'imported_board': None,
              'task_dir': str(job.folder), 'reason': '',
              'policy': 'Independent imported project only; this is not a routing success or adoption permission'}
    source, hashes, config, fingerprint = None, None, None, None
    try:
        config = load_config(args.config)
        fingerprint = runtime_fingerprint(config)
        if fingerprint != config.get('source_fingerprint'):
            raise Halt('unsupported', 'Configured runtime identity changed')
        source = Path(args.board).expanduser().resolve()
        report['source_board'] = str(source)
        if source.suffix != '.kicad_pcb' or not source.is_file():
            raise Halt('unsupported', 'Input must be an existing KiCad board')
        check_output_location(source, job.folder)
        hashes = source_files(source, check=job.check)
        report['source_hashes'] = hashes
        support = rule_support(source)
        if not support['supported']:
            raise Halt('unsupported', '; '.join(support['reasons']))
        try:
            _, manifest = prepare(source.read_text(encoding='utf-8'))
        except ValueError as exc:
            raise Halt('unsupported', str(exc))
        write_json(job.folder/'identity-plan.json', manifest)
        worker = Path(__file__).with_name('legacy_worker.py')
        code = job.run('legacy-convert', [config['kicad_python'], str(worker), 'convert',
            '--board', str(source), '--out-dir', str(job.folder/'audit')], timeout=60)
        result_path = job.folder/'audit/conversion.json'
        conversion = json.loads(result_path.read_text()) if result_path.is_file() else {}
        from .workspace import sha256
        frozen_conversion = sha256(result_path) if result_path.is_file() else None
        report['conversion'] = conversion
        log = (job.folder/'logs/legacy-convert/output.log').read_text(errors='replace')
        if code or conversion.get('status') != 'complete':
            raise Halt('unsupported', conversion.get('reason', 'Native conversion failed; see logs'))
        if re.search(r'\b(?:warning|error|failed|assert|exception)\b|警告|错误|失敗|失败', log, re.I):
            raise Halt('unsupported', 'Native conversion reported a warning/error; no best-effort import is accepted')
        code = job.run('legacy-verify', [sys.executable, str(worker), 'verify',
            '--board', str(source), '--out-dir', str(job.folder/'audit'),
            '--kicad-cli', config['kicad_cli'], '--kicad-python', config['kicad_python']], timeout=120)
        audit_path = job.folder/'audit/verification.json'
        verification = json.loads(audit_path.read_text()) if audit_path.is_file() else {}
        frozen_verification = sha256(audit_path) if audit_path.is_file() else None
        report['verification'] = verification
        if code or verification.get('status') != 'complete':
            raise Halt('verification_incomplete', verification.get('reason', 'Import proof is incomplete'))
        converted = job.folder/'audit/converted/board.kicad_pcb'
        def frozen_proof():
            if (source_files(converted) != conversion['converted_hashes']
                    or sha256(result_path) != frozen_conversion
                    or sha256(audit_path) != frozen_verification
                    or verification.get('conversion_sha256') != frozen_conversion):
                raise Halt('protection_violation', 'Audited conversion or proof changed before publication')
            for relative, expected in verification.get('evidence_hashes', {}).items():
                if Path(relative).is_absolute() or '..' in Path(relative).parts or sha256(job.folder/'audit'/relative) != expected:
                    raise Halt('protection_violation', 'Native import audit evidence changed')
            if not verification.get('evidence_hashes'):
                raise Halt('verification_incomplete', 'Native import audit did not freeze its evidence')
        frozen_proof()
        job.check()
        if source_files(source) != hashes:
            raise Halt('protection_violation', 'Original project changed during import')
        imported = job.folder/'imported'
        # Publish only a completely audited, byte-identical copy. No file is
        # labeled a repair candidate; delivery.accepted_result rejects this job.
        from .workspace import copy_project
        board, _ = copy_project(converted, imported)
        frozen_proof()
        imported_hashes = source_files(board)
        if relative_hashes(board, imported_hashes) != relative_hashes(converted, conversion['converted_hashes']):
            raise Halt('protection_violation', 'Published import differs from its audited conversion')
        report.update(status='complete', imported_board=str(board), imported_hashes=imported_hashes,
                      reason='Legacy import audited; inspect the independent imported project before selecting repair tasks')
        write_json(imported/'pcb-local-import.json', {'schema_version': 1, 'original_board': str(source),
            'original_hashes': hashes, 'audit': str(job.folder), 'identity_plan': str(job.folder/'identity-plan.json'),
            'not_a_repair': True, 'automatic_adoption_to_original': False})
        from .workspace import sha256
        report['audit_hashes'] = {str(path.relative_to(job.folder)):sha256(path)
            for path in (job.folder/'audit').rglob('*') if path.is_file()}
        report['audit_hashes']['identity-plan.json'] = sha256(job.folder/'identity-plan.json')
        report['import_receipt_sha256'] = sha256(imported/'pcb-local-import.json')
        frozen_proof()
    except Halt as exc:
        report.update(status=exc.status, reason=str(exc))
    except Exception as exc:
        report.update(status='error', reason=type(exc).__name__ + ': ' + str(exc))
    finally:
        try:
            if hashes is not None:
                report['source_files_untouched'] = source_files(source) == hashes
                if not report['source_files_untouched']:
                    raise ValueError('Original project changed during import')
            if config is not None and fingerprint is not None:
                report['runtime_untouched'] = runtime_fingerprint(config) == fingerprint
                if not report['runtime_untouched']:
                    raise ValueError('Pinned runtime changed during import')
            if report['status'] == 'complete':
                job.check()
        except Halt as exc:
            report.update(status=exc.status, reason=str(exc))
        except Exception as exc:
            report.update(status='protection_violation', reason=str(exc))
        if report['status'] != 'complete':
            report['imported_board'] = None
        report['exit_code'] = EXIT[report['status']]
        if report['status'] == 'complete':
            try:
                check_imported(report)
            except Exception as exc:
                report.update(status='protection_violation', reason=str(exc), imported_board=None,
                              exit_code=EXIT['protection_violation'])
        write_json(job.folder/'result.json', report)
        job.finish(report['status'])
    return report
