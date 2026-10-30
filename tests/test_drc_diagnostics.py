"""Bad native logs must veto otherwise successful JSON/exit-code evidence."""
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pcb_local import validation


def good_report():
    return {"kicad_version": "10.0.4", "included_severities": ["error", "warning", "exclusion"],
            "ignored_checks": [], "violations": [], "unconnected_items": [], "schematic_parity": []}


def complete_trace(providers=None, complete=True):
    providers = sorted(validation.DRC_EXPECTED_PROVIDERS) if providers is None else providers
    rows = ["00:00:00: Trace: (KICAD_DRC_PROFILE) DRC provider '%s' took 0.001 ms" % name
            for name in providers]
    if complete:
        rows.append("00:00:00: Trace: (KICAD_DRC_PROFILE) DRC took 1.000 ms")
    return "\n".join(rows)


class DrcDiagnostics(unittest.TestCase):
    def test_reported_failure_vetoes_valid_json_on_either_stream(self):
        messages = ["Error: unable to build DRC cache", "DRC provider failed", "Aborted",
                    "Operation cancelled", "Operation canceled", "Assertion failed at drc_engine.cpp",
                    "Traceback (most recent call last):", "Segmentation fault: 11",
                    "DRC tests not completed", "DRC stopped early", "Timeout in worker",
                    "缓存生成失败", "DRC 已取消", "檢查未完成", "发现错误"]
        for message in messages:
            for stream in ("stdout", "stderr"):
                with self.subTest(message=message, stream=stream):
                    logs = {"stdout": "Saved board\n", "stderr": ""}
                    logs[stream] += message + "\n"
                    result = validation.drc_coverage(good_report(), **logs)
                    self.assertFalse(result["complete"], result)
                    self.assertEqual(result["diagnostics"]["findings"][0]["stream"], stream)

    def test_unknown_stderr_requires_investigation(self):
        result = validation.drc_coverage(good_report(), stdout="Saved board", stderr="Unexpected native diagnostic")
        self.assertFalse(result["complete"])
        self.assertIn("unrecognized", result["diagnostics"]["findings"][0]["reason"])

    def test_normal_font_configuration_and_substitution_warnings_are_benign(self):
        log = '''Fontconfig warning: "/opt/homebrew/etc/fonts/conf.d/48-guessfamily.conf", line 19: invalid attribute 'xsi:nil'
Fontconfig warning: "/opt/homebrew/etc/fonts/conf.d/48-guessfamily.conf", line 20: invalid constant used :
Fontconfig warning: "/opt/homebrew/etc/fonts/conf.d/48-spacing.conf", line 14: invalid constant used : monospace
06:23:02 PM: 找不到字体 'Ubuntu Sans'; 用 'Verdana Bold' 代替。
'''
        result = validation.drc_coverage(good_report(), stdout="发现 271 条违规项\n已保存电路板\n", stderr=log)
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["diagnostics"]["benign_font_diagnostic_count"], 4)
        self.assertTrue(result["diagnostics"]["checked"])

    def test_fontconfig_fatal_is_not_hidden_by_warning_prefix(self):
        result = validation.drc_coverage(good_report(), stdout="", stderr="Fontconfig warning: failed to initialize")
        self.assertFalse(result["complete"])

    def test_report_destination_words_are_not_error_diagnostics(self):
        for output in ("Saved DRC Report to /tmp/timeout-error/drc.json\nSaved board",
                       "保存 DRC 报告到 /tmp/失败/error/drc.json\n已保存电路板"):
            result = validation.drc_coverage(good_report(), stdout=output, stderr="")
            self.assertTrue(result["complete"], result)

    def test_colored_error_and_error_after_save_are_not_hidden(self):
        result = validation.drc_coverage(good_report(),
            stdout="Saved DRC Report to /tmp/result.json\n\x1b[31mError: provider stopped\x1b[0m", stderr="")
        self.assertFalse(result["complete"])

    def test_json_only_api_explicitly_does_not_claim_logs_checked(self):
        result = validation.drc_coverage(good_report())
        self.assertTrue(result["complete"])
        self.assertFalse(result["diagnostics"]["checked"])
        self.assertFalse(validation.drc_coverage(good_report(), stdout="")["complete"])

    def test_inner_outer_keywords_cannot_be_quoted_layer_names(self):
        with tempfile.TemporaryDirectory() as temp:
            board = Path(temp) / "board.kicad_pcb"
            board.write_text("(kicad_pcb)")
            board.with_suffix(".kicad_pro").write_text("{}")
            dru = board.with_suffix(".kicad_dru")
            for layer in ("inner", "outer"):
                for quoted in (True, False):
                    token = '"' + layer + '"' if quoted else layer
                    dru.write_text('(version 1)(rule "scope" (layer ' + token +
                                   ')(constraint clearance (min 0.3mm)))')
                    result = validation.rule_support(board)
                    self.assertEqual(result["supported"], not quoted, result)
                    if quoted:
                        self.assertIn("unquoted", result["reasons"][0])

    def test_trace_requires_exact_provider_set_and_one_final_marker(self):
        providers = sorted(validation.DRC_EXPECTED_PROVIDERS)
        good = validation.drc_coverage(good_report(), stdout="Saved board", stderr=complete_trace(),
                                       require_provider_trace=True)
        self.assertTrue(good["complete"], good)
        self.assertTrue(good["provider_trace"]["verified"])
        bad_traces = ["", complete_trace(providers[:-1]), complete_trace(providers + providers[:1]),
                      complete_trace(providers + ["unknown"]), complete_trace(complete=False),
                      complete_trace() + "\n00:00:00: Trace: (KICAD_DRC_PROFILE) DRC took 1.000 ms",
                      "00:00:00: Trace: (KICAD_DRC_PROFILE) DRC took 1.000 ms\n" + complete_trace(complete=False)]
        for trace in bad_traces:
            with self.subTest(trace=trace[-150:]):
                result = validation.drc_coverage(good_report(), stdout="Saved board", stderr=trace,
                                                 require_provider_trace=True)
                self.assertFalse(result["complete"], result)

    def test_unmasked_native_trace_requires_bound_capacity_and_all_providers(self):
        from pcb_local.drc_complete import REPORT_CAPACITY,SOURCE_COMMIT,PATCH_SHA256
        bound=dict(per_type=REPORT_CAPACITY,extended=REPORT_CAPACITY,source_commit=SOURCE_COMMIT,
                   patch_sha256=PATCH_SHA256,manifest_sha256='a'*64)
        plain=complete_trace().replace('(KICAD_DRC_PROFILE) ','')
        report=good_report();report['violations']=[dict(type='silk_overlap') for _ in range(205)]
        good=validation.drc_coverage(report,stdout='',stderr=plain,require_provider_trace=True,capacity_evidence=bound)
        self.assertTrue(good['complete']);self.assertTrue(good['provider_trace']['verified'])
        untrusted=validation.drc_coverage(report,stdout='',stderr=plain,require_provider_trace=True)
        self.assertFalse(untrusted['complete'])
        for broken in ('\n'.join(plain.splitlines()[1:]),plain+'\n'+plain.splitlines()[0],plain+'\nUnknown native warning'):
            result=validation.drc_coverage(report,stdout='',stderr=broken,require_provider_trace=True,capacity_evidence=bound)
            self.assertFalse(result['complete'])
        with self.assertRaises(validation.VerificationIncomplete):
            validation.drc_coverage(report,stdout='',stderr=plain,require_provider_trace=True,
                                    capacity_evidence=dict(bound,patch_sha256='0'*64))

    def test_ignored_critical_needs_successful_independent_coverage(self):
        report = good_report()
        report["ignored_checks"] = [{"key": "track_not_centered_on_via"}]
        self.assertFalse(validation.drc_coverage(report)["complete"])
        result = validation.drc_coverage(report, covered_ignored_checks=["track_not_centered_on_via"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["critical_ignored_checks"], ["track_not_centered_on_via"])
        self.assertEqual(result["supplemented_ignored_checks"], ["track_not_centered_on_via"])

    def test_measure_keeps_bad_log_evidence_despite_exit_zero_and_native_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            board = root / "input.kicad_pcb"
            board.write_text('''(kicad_pcb (version 20241229) (net 1 "N")
              (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
              (footprint "Fixture" (uuid "fp") (layer "F.Cu")
                (pad "1" smd circle (uuid "p") (layers "F.Cu") (net 1 "N"))))''')
            board.with_suffix(".kicad_pro").write_text("{}")
            raw = validation.read_board(board)
            out = root / "out"
            out.mkdir()
            args = SimpleNamespace(out_dir=out, before=board, kicad_cli="fake-cli",
                                   kicad_python="fake-python", deadline=time.monotonic() + 10)

            def fake_run(command, stage, name, deadline, **kwargs):
                (stage / (name + ".stdout.log")).write_text("Saved board\n")
                (stage / (name + ".stderr.log")).write_text("Error: DRC cache failed\n" if name == "drc" else "")
                if name == "drc":
                    (stage / "drc.json").write_text(json.dumps(good_report()))
                else:
                    (stage / "connectivity.json").write_text(json.dumps({
                        "version": "10.0.4", "items": raw["identities"]}))
                return {"returncode": 0, "timed_out": False}

            with patch.object(validation, "run_command", fake_run):
                result = validation.measure(board, "before", args, raw)
            self.assertFalse(result["coverage"]["complete"])
            saved = json.loads((out / "before/drc-coverage.json").read_text())
            self.assertEqual(saved, result["coverage"])
            self.assertEqual(saved["diagnostics"]["findings"][0]["text"], "Error: DRC cache failed")


if __name__ == "__main__":
    unittest.main()
