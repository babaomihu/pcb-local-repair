import copy,json,tempfile,unittest
from pathlib import Path
from pcb_local.diagnostics import explain,markdown
from pcb_local.visual_report import build_model,render_html
from test_visual_report import HEADER

def drc(n=2):
    return dict(coordinate_units='mm',violations=[dict(type='existing_rule',severity='warning',description='existing finding',items=[dict(uuid='pad',pos=dict(x=10,y=10))]) for _ in range(n)],unconnected_items=[],schematic_parity=[])

def report(raw=None):
    raw=drc() if raw is None else raw
    return dict(status='complete',accepted=False,discovery=dict(supported_target_count=0),preflight=dict(complete=True,measurement=dict(drc=raw,coverage=dict(complete=True),supplemental_drc=dict(drc=copy.deepcopy(raw),coverage=dict(complete=True)))))

class BaselineReport(unittest.TestCase):
    def test_independent_reports_not_added_to_candidate_violations(self):
        d=explain(report());self.assertEqual(d['category'],'no_supported_targets')
        self.assertEqual([x['total_records'] for x in d['baseline_drc']],[2,2])
        self.assertEqual(d['drc_findings'],[])
        self.assertIn('数量不能相加',markdown(d));self.assertIn('原规则：2条',markdown(d))
    def test_no_supplemental_run_can_be_null_in_accepted_reports(self):
        r=report(drc(0));r['status']='accepted'
        for value in [None,False,[]]:
            r['preflight']['measurement']['supplemental_drc']=value
            d=explain(r);self.assertEqual(d['category'],'accepted')
            self.assertEqual(len(d['baseline_drc']),1)
            self.assertEqual(d['baseline_drc'][0]['total_records'],0)
    def test_missing_sections_are_unknown_not_zero(self):
        r=report(dict(violations=[]));d=explain(r)
        self.assertIsNone(d['baseline_drc'][0]['total_records'])
        self.assertFalse(d['baseline_drc'][0]['records_complete'])
        self.assertIn('未完整记录',markdown(d))
    def test_incomplete_coverage_not_claimed_pass(self):
        r=report(drc(0));r['preflight']['measurement']['coverage']['complete']=False
        d=explain(r);self.assertEqual(d['baseline_drc'][0]['total_records'],0)
        self.assertFalse(d['baseline_drc'][0]['coverage_complete']);self.assertIn('覆盖未确认完整',markdown(d))
    def test_total_counts_are_not_truncated_with_display(self):
        d=explain(report(drc(74)));row=d['baseline_drc'][0]
        self.assertEqual(row['total_records'],74);self.assertEqual(len(row['findings']),20)
        self.assertTrue(row['findings_truncated']);self.assertEqual(row['type_counts'],{'existing_rule':74})
    def test_no_baseline_does_not_invent_clean_board(self):
        d=explain(dict(status='no_progress'));self.assertEqual(d['baseline_drc'],[])
        self.assertNotIn('原规则：0',markdown(d))
    def test_existing_findings_never_override_acceptance_or_protection(self):
        for status,category in [('accepted','accepted'),('protection_violation','protected_state_violation'),('verification_incomplete','verification_incomplete')]:
            r=report();r.update(status=status,accepted=status=='accepted');saved=copy.deepcopy(r)
            self.assertEqual(explain(r)['category'],category);self.assertEqual(r,saved)
    def test_visual_locations_units_invalid_values_and_escaping(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'board.kicad_pcb';p.write_text(HEADER+')')
            for units,pos,located in [('mm',dict(x=10,y=10),True),('in',dict(x=10,y=10),False),('mm',dict(x=float('nan'),y=10),False),('mm',dict(x='bad',y=10),False)]:
                with self.subTest(units=units,pos=pos):
                    raw=drc(1);raw['coordinate_units']=units
                    raw['violations'][0].update(type='<img src=x onerror=alert(1)>',description='</script><script>bad()</script>')
                    raw['violations'][0]['items'][0]['pos']=pos
                    r=report(raw);r['source_board']=str(p);r['diagnosis']=explain(r)
                    m=build_model(r);items=[i for i in m['issues'] if i['kind']=='baseline_drc']
                    self.assertEqual(len(items),2);self.assertEqual(items[0]['bounds'] is not None,located)
                    self.assertFalse(m['adoption_available']);self.assertFalse(m['accepted_record'])
                    text=render_html(m);self.assertIn('原板检查记录',text)
                    self.assertNotIn('<img src=x',text);self.assertNotIn('</script><script>bad()',text)
                    self.assertEqual(p.read_text(),HEADER+')')
    def test_malformed_record_not_counted_as_complete(self):
        raw=drc();raw['violations'].append(None);d=explain(report(raw))
        self.assertIsNone(d['baseline_drc'][0]['total_records'])
        self.assertFalse(d['baseline_drc'][0]['records_complete'])
