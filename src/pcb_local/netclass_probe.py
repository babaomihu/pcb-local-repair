#!/usr/bin/env python3
"""Compare project net-class values with KiCad's native effective net classes.

Reads only: no routing, refill, save, or DRC. The native reader runs in an
isolated process because pcbnew needs wx application state on macOS.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
NATIVE = '/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3'
FIELDS = {'clearance': 'Clearance', 'track_width': 'TrackWidth',
          'via_diameter': 'ViaDiameter', 'via_drill': 'ViaDrill',
          'diff_pair_width': 'DiffPairWidth', 'diff_pair_gap': 'DiffPairGap'}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def native_read(board_path, names):
    import wx
    app = wx.App(False)
    import pcbnew
    board = pcbnew.LoadBoard(str(board_path))
    if board is None:
        raise RuntimeError('KiCad did not load the board')
    settings = board.GetDesignSettings().m_NetSettings
    project = json.loads(board_path.with_suffix('.kicad_pro').read_text())
    declared = project.get('net_settings') or {}
    class_names = {c['name'] for c in (declared.get('classes') or []) if c.get('name')}
    class_names.update(p['netclass'] for p in (declared.get('netclass_patterns') or [])
                       if p.get('netclass'))
    for assigned in (declared.get('netclass_assignments') or {}).values():
        class_names.update([assigned] if isinstance(assigned, str) else assigned)
    if not names:
        names = sorted(str(n) for n in board.GetNetsByName().keys() if str(n))
    result = {}
    for name in names:
        nc = settings.GetEffectiveNetClass(name)
        values = {field: getattr(nc, 'Get' + stem)() / 1e6
                  for field, stem in FIELDS.items()}
        parents = {}
        for field, stem in FIELDS.items():
            parent = getattr(nc, 'Get' + stem + 'Parent', lambda: None)()
            parents[field] = str(parent.GetName()) if parent else None
        members = sorted(c for c in class_names if c != 'Default'
                         and nc.ContainsNetclassWithName(c))
        result[name] = {'effective_name': str(nc.GetName()), 'memberships': members,
                        'values': values, 'property_parents': parents}
    # Keep this SWIG map alive: non-default keys can be wxString wrappers
    # owned by this specific map, and cannot index a newly returned map.
    native_classes = board.GetAllNetClasses()
    return {'version': pcbnew.Version(), 'nets': result,
            'class_priorities': {str(k): native_classes[k].GetPriority() for k in native_classes}}


def compare_krt(board, native, krt_root):
    sys.path.insert(0, str(krt_root / 'py_router'))
    from design_rules import DesignRules
    from list_nets import (read_design_rules, net_class_memberships,
                           net_clearance_map_by_id, net_track_width_map_by_id)
    names = {i: name for i, name in enumerate(native['nets'], 1)}
    pcb = SimpleNamespace(nets={i: SimpleNamespace(name=n) for i, n in names.items()},
                          groups={}, board_info=SimpleNamespace(copper_layers=[]))
    rules = DesignRules.from_project(pcb, str(board))
    legacy = read_design_rules(str(board))
    memberships = net_class_memberships(str(board), names, legacy)
    clearances = net_clearance_map_by_id(str(board), names, legacy)
    widths = net_track_width_map_by_id(str(board), names, legacy)
    default = legacy.get('classes', {}).get('Default', {})
    results, mismatches = {}, []
    for nid, name in names.items():
        values = {field: rules.class_value(nid, field) for field in FIELDS}
        maps = {'clearance': clearances.get(nid, default.get('clearance')),
                'track_width': widths.get(nid, default.get('track_width'))}
        row = {'design_rules': values, 'routing_maps': maps,
               'memberships': sorted(rules.memberships.get(nid, ())),
               'routing_memberships': sorted(memberships.get(nid, ()))}
        expected_members = native['nets'][name]['memberships']
        for channel in ('memberships', 'routing_memberships'):
            actual = [member for member in row[channel] if member != 'Default']
            if actual != expected_members:
                mismatches.append({'net': name, 'channel': channel,
                                   'field': 'memberships', 'krt': actual,
                                   'native': expected_members})
        for channel in ('design_rules', 'routing_maps'):
            for field, value in row[channel].items():
                want = native['nets'][name]['values'][field]
                if value is None or abs(value - want) > 1e-9:
                    mismatches.append({'net': name, 'channel': channel,
                                       'field': field, 'krt': value, 'native': want})
        results[name] = row
    return {'nets': results, 'mismatches': mismatches, 'notes': rules.notes,
            'passed': not mismatches and not rules.notes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', required=True, type=Path)
    parser.add_argument('--net', action='append', default=[])
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--native-python', default=NATIVE)
    parser.add_argument('--krt-root', type=Path, default=ROOT / 'KiCadRoutingTools')
    parser.add_argument('--native-worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    board, output = args.board.resolve(), args.output.resolve()
    if args.native_worker:
        try:
            result = native_read(board, args.net)
            output.write_text(json.dumps(result, indent=2) + '\n')
            code = 0
        except Exception as exc:
            output.write_text(json.dumps({'error': f'{type(exc).__name__}: {exc}'}) + '\n')
            code = 2
        # Do not let macOS wx/pcbnew shutdown invalidate a completed read.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    if not board.is_file() or not board.with_suffix('.kicad_pro').is_file():
        parser.error('a saved board and its sibling .kicad_pro are required')
    output.parent.mkdir(parents=True, exist_ok=True)
    native_path = output.with_suffix('.native.json')
    paths = [board, board.with_suffix('.kicad_pro'), board.with_suffix('.kicad_dru')]
    before = {str(path): digest(path) for path in paths}
    command = [args.native_python, '-B', str(Path(__file__).resolve()),
               '--native-worker', '--board', str(board), '--output', str(native_path)]
    for name in args.net:
        command.extend(['--net', name])
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
    output.with_suffix('.native.stdout.log').write_text(completed.stdout)
    output.with_suffix('.native.stderr.log').write_text(completed.stderr)
    report = {'board': str(board), 'source_hashes': before,
              'native_command': command, 'native_exit_code': completed.returncode,
              'krt_source_hashes': {name: digest(args.krt_root.resolve() / 'py_router' / name)
                                    for name in ('design_rules.py', 'list_nets.py', 'netclass_resolver.py')},
              'scope': 'Net-class matching and effective class properties only; custom DRC rules are not evaluated here.'}
    if completed.returncode == 0:
        native = json.loads(native_path.read_text())
        report['native'] = native
        try:
            report['krt'] = compare_krt(board, native, args.krt_root.resolve())
        except Exception as exc:
            report['krt'] = {'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
    else:
        report['native_error'] = (json.loads(native_path.read_text()).get('error')
                                  if native_path.is_file() else
                                  f'native probe exited {completed.returncode}; see native logs')
    report['inputs_unchanged'] = before == {str(path): digest(path) for path in paths}
    report['passed'] = bool(completed.returncode == 0 and report.get('krt', {}).get('passed')
                            and report['inputs_unchanged'])
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'output': str(output),
                      'mismatches': len(report.get('krt', {}).get('mismatches', [])),
                      'error': report.get('krt', {}).get('error') or report.get('native_error')}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
