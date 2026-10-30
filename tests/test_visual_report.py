"""Read-only overview identity, stale evidence and board-text injection checks."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from pcb_local.visual_report import build_model,render_html,write_visual_report


HEADER='''(kicad_pcb (version 20241229) (generator "visual-test")
(layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal))
(net 1 "SIGNAL")
(footprint "test" (layer "F.Cu") (uuid "fp") (at 10 10 90)
(property "Reference" "J1")
(pad "1" thru_hole circle (at 1 0) (size 1 1) (drill .4) (layers "*.Cu" "*.Mask") (net 1 "SIGNAL") (uuid "pad")))
'''
SEGMENT='(segment (start {x} 10) (end 20 10) (width .25) (layer "In1.Cu") (net 1) (uuid "{uid}"))'
VIA='(via (at 20 10) (size .8) (drill .4) (layers "F.Cu" "B.Cu") (net 1) (uuid "via"))'


class VisualReportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.before=self.root/'before.kicad_pcb';self.after=self.root/'after.kicad_pcb'
        self.before.write_text(HEADER+SEGMENT.format(x=10,uid='changed')+SEGMENT.format(x=11,uid='removed')+VIA+')')
        self.after.write_text(HEADER+SEGMENT.format(x=12,uid='changed')+SEGMENT.format(x=13,uid='added')+VIA+')')
        self.report=dict(status='accepted',accepted=True,source_board=str(self.before),candidate=str(self.after),
            source_hashes={str(self.before):hashlib.sha256(self.before.read_bytes()).hexdigest()},
            candidate_hashes={str(self.after):hashlib.sha256(self.after.read_bytes()).hexdigest()},
            task={'region_mm':[5,5,25,15]},diagnosis={'summary':'完整验收通过','objects':[], 'drc_findings':[]})
    def tearDown(self):self.temp.cleanup()
    def test_routing_delta_and_all_stack_via(self):
        m=build_model(self.report)
        self.assertEqual(m['change_counts'],dict(unchanged=2,added=1,removed=1,changed_before=1,changed_after=1))
        via=next(o for o in m['objects'] if o['uuid']=='via')
        self.assertEqual(via['layers'],['F.Cu','In1.Cu','In2.Cu','B.Cu'])
        pad=next(o for o in m['objects'] if o['uuid']=='pad')
        self.assertAlmostEqual(pad['center'][0],10);self.assertAlmostEqual(pad['center'][1],9)
        self.assertFalse(m['adoption_available'])
    def test_native_pad_position_and_board_hashes(self):
        self.report['preflight']={'measurement':{'snapshot':{'items':{'key':{'kind':'pad','uuid':'pad','footprint_uuid':'fp','position_mm':[10,9]}}}}}
        hashes=[hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.before,self.after)]
        output=self.root/'report.html';receipt=write_visual_report(self.report,output)
        self.assertEqual(receipt['sha256'],hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(hashes,[hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.before,self.after)])
        with self.assertRaises(FileExistsError):write_visual_report(self.report,output)
    def test_failed_report_without_candidate_is_never_accepted(self):
        self.report.update(status='no_progress',accepted=False,candidate=None)
        self.report['diagnosis']['objects']=[dict(uuid='via',layer=['F.Cu','In1.Cu'],position_mm=[20,10],evidence_kind='direct_corridor_intersection')]
        m=build_model(self.report);page=render_html(m)
        self.assertFalse(m['accepted_record']);self.assertFalse(m['adoption_available'])
        self.assertIsNone(m['candidate']);self.assertEqual(m['issues'][0]['layers'],['F.Cu','In1.Cu'])
        self.assertIn('未通过验收／仅供诊断，不可采纳',page)
        self.assertIn('不是 KiCad 精确渲染',page)
    def test_drc_position_preserved(self):
        self.report['diagnosis']['drc_findings']=[dict(type='clearance',description='too close',items=[dict(uuid='via',pos={'x':20,'y':10})])]
        m=build_model(self.report);i=m['issues'][0]
        self.assertEqual(i['kind'],'drc');self.assertEqual(i['bounds'],[19.6,9.6,20.4,10.4])
    def test_malicious_board_text_is_data_not_markup(self):
        attack='</script><script>alert("board")</script><img src=x onerror=alert(1)>'
        self.report['reason']=attack;self.report['diagnosis']['summary']=attack
        self.report['diagnosis']['objects']=[dict(uuid=attack,layer='In1.Cu',bounds_mm=[1,1,2,2],reason=attack)]
        page=render_html(build_model(self.report))
        self.assertNotIn(attack,page)
        self.assertEqual(page.count('<script'),2)
        self.assertNotIn('innerHTML',page)
        self.assertIn("default-src &#x27;none&#x27;",page)
        payload=page.split('<script id="model" type="application/json">',1)[1].split('</script>',1)[0]
        self.assertEqual(json.loads(payload)['issues'][0]['uuid'],attack)
    def test_changed_board_is_marked_stale(self):
        self.after.write_text(self.after.read_text()+'\n')
        m=build_model(self.report)
        self.assertTrue(any('候选板已不同' in w for w in m['warnings']))
        self.assertFalse(m['adoption_available'])
    def test_moved_footprint_is_not_silently_unchanged(self):
        self.after.write_text(self.after.read_text().replace('(at 10 10 90)','(at 11 10 90)'))
        pads=[o for o in build_model(self.report)['objects'] if o['kind']=='pad']
        self.assertEqual({p['change'] for p in pads},{'changed_before','changed_after'})
    def test_nonfinite_geometry_rejected(self):
        self.before.write_text(self.before.read_text().replace('(at 20 10)','(at nan 10)'))
        with self.assertRaises(ValueError):build_model(self.report)


if __name__=='__main__':unittest.main()
