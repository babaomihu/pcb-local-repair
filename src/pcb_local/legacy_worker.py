"""Isolated native legacy conversion and independent full-board audit workers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pcb_local.legacy_import import prepare, native_semantics
from pcb_local.workspace import copy_project, source_files, sha256
from pcb_local.jobs import write_json


def semantic_digest(path):
    return hashlib.sha256(repr(native_semantics(path.read_text())).encode()).hexdigest()


def effective_rules(board):
    settings = board.GetDesignSettings()
    values = {}
    for attribute in ('m_MinClearance', 'm_TrackMinWidth', 'm_CopperEdgeClearance',
                      'm_HoleClearance', 'm_HoleToHoleMin', 'm_ViasMinSize',
                      'm_ViasMinAnnularWidth', 'm_MinThroughDrill', 'm_MicroViasMinSize',
                      'm_MicroViasMinDrill', 'm_SolderMaskExpansion', 'm_SolderMaskMinWidth',
                      'm_MinConn', 'm_MinGrooveWidth', 'm_MinResolvedSpokes',
                      'm_MinSilkTextHeight', 'm_MinSilkTextThickness', 'm_SilkClearance',
                      'm_SolderMaskToCopperClearance', 'm_SolderPasteMargin',
                      'm_SolderPasteMarginRatio', 'm_MaxError', 'm_UseHeightForLengthCalcs',
                      'm_AllowSoldermaskBridgesInFPs', 'm_CapVias', 'm_CoverViasBack',
                      'm_CoverViasFront', 'm_FillVias', 'm_PlugViasBack', 'm_PlugViasFront',
                      'm_TentViasBack', 'm_TentViasFront', 'm_ZoneKeepExternalFillets'):
        value = getattr(settings, attribute)
        if not isinstance(value, (bool, int, float)):
            raise ValueError('Unrecognized native rule field: ' + attribute)
        values[attribute] = value
    values['drc_epsilon'] = settings.GetDRCEpsilon()
    names = {str(p.GetNetname()) for fp in board.GetFootprints() for p in fp.Pads()}
    names.update(str(t.GetNetname()) for t in board.GetTracks())
    classes = {}
    for name in sorted(names):
        nc = settings.m_NetSettings.GetEffectiveNetClass(name)
        classes[name] = {key: int(getattr(nc, method)()) for key, method in (
            ('track_width', 'GetTrackWidth'), ('clearance', 'GetClearance'),
            ('via_diameter', 'GetViaDiameter'), ('via_drill', 'GetViaDrill'),
            ('microvia_diameter', 'GetuViaDiameter'), ('microvia_drill', 'GetuViaDrill'),
            ('diff_pair_width', 'GetDiffPairWidth'), ('diff_pair_gap', 'GetDiffPairGap'),
            ('diff_pair_via_gap', 'GetDiffPairViaGap'))}
        classes[name]['name'] = str(nc.GetName())
        classes[name]['tuning_profile'] = str(nc.GetTuningProfile())
        classes[name]['members'] = sorted(str(key) for key in board.GetAllNetClasses()
                                         if nc.ContainsNetclassWithName(key))
    return {'board_settings': values, 'netclasses': classes,
            'drc_exclusions': sorted(str(x) for x in settings.m_DrcExclusions),
            'drc_exclusion_comments': dict(settings.m_DrcExclusionComments)}


def convert(args):
    import wx
    app = wx.App(False)
    import pcbnew
    if pcbnew.Version() != '10.0.4':
        raise ValueError('Legacy conversion is witnessed only for KiCad 10.0.4')
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=False)
    original, _ = copy_project(args.board, out/'original')
    prepared, _ = copy_project(args.board, out/'prepared')
    enriched, manifest = prepare(original.read_text())
    prepared.write_text(enriched)
    write_json(out/'identity-map.json', manifest)
    before = pcbnew.LoadBoard(str(original))
    stabilized = pcbnew.LoadBoard(str(prepared))
    if before is None or stabilized is None:
        raise ValueError('Native reader rejected the original/prepared board')
    rules_before, rules_stable = effective_rules(before), effective_rules(stabilized)
    baseline, _ = copy_project(original, out/'original-native')
    converted, _ = copy_project(prepared, out/'converted')
    # SaveBoard's default also rewrites .kicad_pro from legacy in-memory
    # defaults. Explicitly skip settings; authored context bytes are immutable.
    if not pcbnew.SaveBoard(str(baseline), before, True) or not pcbnew.SaveBoard(str(converted), stabilized, True):
        raise ValueError('Native board-only save failed')
    loaded = pcbnew.LoadBoard(str(converted))
    rules_after = effective_rules(loaded)
    roundtrip, _ = copy_project(converted, out/'roundtrip')
    if not pcbnew.SaveBoard(str(roundtrip), loaded, True):
        raise ValueError('Native board-only roundtrip save failed')
    # GetSeverity returns an opaque SWIG SEVERITY* in KiCad 10.0.4; int()
    # exposes its address, not its value. Export native in-memory settings
    # only into dedicated evidence probes. Never rewrite input/import context.
    # This compares all effective severities, even when no violation exists.
    for label, native, rules in (('original', before, rules_before),
                                 ('prepared', stabilized, rules_stable),
                                 ('reloaded', loaded, rules_after)):
        probe = out/'effective-settings'/label/'board.kicad_pcb'
        probe.parent.mkdir(parents=True, exist_ok=False)
        if not pcbnew.SaveBoard(str(probe), native, False):
            raise ValueError('Native settings evidence could not be exported')
        effective = json.loads(probe.with_suffix('.kicad_pro').read_text())
        severities = effective['board']['design_settings']['rule_severities']
        if not severities:
            raise ValueError('Native severity export is empty')
        rules['effective_drc_severities'] = severities
    fingerprints = {key: semantic_digest(path) for key, path in (
        ('original_native', baseline), ('stabilized_native', converted), ('reloaded_native', roundtrip))}
    from pcb_local.validation import read_board
    parsed = read_board(converted)
    planned = {row['new_uuid'] for row in manifest['identity_map']}
    checks = {'identity_only_source_transform': manifest['authored_content_except_identity_equal'],
              'native_full_serialization_equal': len(set(fingerprints.values())) == 1,
              'effective_rules_equal': rules_before == rules_stable == rules_after,
              'all_conductive_identities_planned': all(row['uuid'] in planned for row in parsed['identities'].values()),
              'converted_has_conductive_objects': bool(parsed['identities'])}
    def context(path):
        return {str(Path(p).relative_to(path.parent)):h for p,h in source_files(path).items() if Path(p)!=path}
    checks['context_bytes_equal'] = context(original) == context(prepared) == context(converted) == context(roundtrip)
    record = {'status': 'complete' if all(checks.values()) else 'unsupported', 'version': pcbnew.Version(), 'checks': checks,
              'native_fingerprints': fingerprints, 'effective_rules': rules_after,
              'effective_rules_original': rules_before, 'effective_rules_prepared': rules_stable,
              'prepared': str(prepared), 'converted': str(converted),
              'prepared_hashes': source_files(prepared), 'converted_hashes': source_files(converted),
              'scope': 'Native serialization preserves all supported object state except explicitly mapped identities; DRC/connectivity audit still required'}
    write_json(out/'conversion.json', record)
    if not all(checks.values()):
        raise ValueError('Native conversion equivalence failed: ' + repr(checks))


def compare_drc(before, after, raw):
    from pcb_local.validation import drc_delta
    def compare(left, right):
        return drc_delta(left, right, raw['stored_uuids'], raw['stored_uuids'],
            before.get('known_generated_field_uuids', []), after.get('known_generated_field_uuids', []))
    delta = compare(before['drc'], after['drc'])
    checks = {'no_new_or_changed_drc': delta['no_new_or_changed_findings'],
              'no_disappeared_drc': not any(v['resolved'] for v in delta['sections'].values())}
    left, right = before.get('supplemental_drc'), after.get('supplemental_drc')
    extra = None
    checks['supplemental_drc_equal'] = not left and not right
    if left and right:
        extra = compare(left['drc'], right['drc'])
        checks['supplemental_drc_equal'] = (left['checks_enabled'] == right['checks_enabled']
            and left['coverage']['complete'] and right['coverage']['complete']
            and extra['no_new_or_changed_findings']
            and not any(v['resolved'] for v in extra['sections'].values()))
    return checks, delta, extra


def verify(args):
    from pcb_local.validation import measure, read_board
    out = args.out_dir
    conversion = json.loads((out/'conversion.json').read_text())
    conversion_sha256 = sha256(out/'conversion.json')
    old, new = Path(conversion['prepared']), Path(conversion['converted'])
    if source_files(old) != conversion['prepared_hashes'] or source_files(new) != conversion['converted_hashes']:
        raise ValueError('Conversion inputs changed before audit')
    directory = out/'native-audit'
    directory.mkdir(exist_ok=False)
    options = SimpleNamespace(out_dir=directory, before=old, kicad_cli=args.kicad_cli,
                              kicad_python=args.kicad_python, deadline=time.monotonic()+115)
    raw = read_board(new)
    # Original old grammar has no footprint token support in the normal parser.
    # Identity-only enrichment and full native serialization equality independently
    # prove this expected identity set before measuring both physical boards.
    before = measure(old, 'before', options, raw)
    after = measure(new, 'after', options, raw)
    checks = {'before_coverage_complete': before['coverage']['complete'],
              'after_coverage_complete': after['coverage']['complete']}
    drc_checks, delta, extra = compare_drc(before, after, raw)
    checks.update(drc_checks)
    # Migration is not routing: neither new nor resolved findings may disappear
    # through conversion and be credited as repair progress.
    def components(snapshot):
        return sorted((c['net'], tuple(sorted(c['items']))) for c in snapshot['components'])
    checks['conductive_objects_equal'] = before['snapshot']['items'] == after['snapshot']['items']
    checks['connectivity_partitions_equal'] = components(before['snapshot']) == components(after['snapshot'])
    checks['copper_layers_equal'] = before['snapshot']['copper_layers'] == after['snapshot']['copper_layers']
    checks['refilled_native_geometry_equal'] = semantic_digest(Path(before['refilled_board'])) == semantic_digest(Path(after['refilled_board']))
    # Authored rule and local library bytes must remain identical across import.
    def context(board):
        return {str(Path(p).relative_to(board.parent)): h for p,h in source_files(board).items() if Path(p) != board}
    checks['context_bytes_equal'] = context(old) == context(new)
    for key, path in (('prepared', old), ('converted', new)):
        checks[key+'_untouched'] = source_files(path) == conversion[key+'_hashes']
    record = {'status': 'complete' if all(checks.values()) else 'verification_incomplete',
              'checks': checks, 'drc_delta': delta, 'supplemental_drc_delta': extra,
              'before_coverage': before['coverage'], 'after_coverage': after['coverage'],
              'reason': 'Import preserves geometry, rules, identities and original electrical partitions' if all(checks.values()) else 'Import equivalence check failed',
              'evidence': str(directory), 'not_a_routing_success': True}
    if sha256(out/'conversion.json') != conversion_sha256:
        raise ValueError('Conversion proof changed during native audit')
    record['conversion_sha256'] = conversion_sha256
    record['evidence_hashes'] = {str(path.relative_to(out)):sha256(path)
        for path in out.rglob('*') if path.is_file() and path.name != 'verification.json'}
    write_json(out/'verification.json', record)
    return 0 if all(checks.values()) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('convert', 'verify'))
    parser.add_argument('--board', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--kicad-cli')
    parser.add_argument('--kicad-python')
    args = parser.parse_args()
    try:
        if args.phase == 'convert':
            convert(args)
            return 0
        return verify(args)
    except Exception as exc:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        path = args.out_dir/('conversion.json' if args.phase == 'convert' else 'verification.json')
        record = json.loads(path.read_text()) if path.is_file() else {}
        record.update(status='unsupported' if args.phase == 'convert' else 'verification_incomplete',
                      reason=type(exc).__name__ + ': ' + str(exc))
        write_json(path, record)
        print(type(exc).__name__ + ': ' + str(exc), flush=True)
        return 1


if __name__ == '__main__':
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # KiCad SWIG object destruction can assert during interpreter shutdown.
    # All evidence and return status are persisted before this worker-only exit.
    os._exit(code)
