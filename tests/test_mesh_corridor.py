import copy,json,math,unittest
from pathlib import Path
from unittest.mock import patch
from shapely.geometry import LineString, box
from pcb_local.geometry_search import search,configuration_space,SearchLimit
from pcb_local.mesh_corridor import corridor_path,MeshLimit
from test_direct_visibility import complex_model
from test_boundary_corridor import transform


def dense():
    m=complex_model(True)
    for p in m['primitives']:
        if p['center'][1]==12.8:p['center'][1]=14
        if p['center'][1]==17.2:p['center'][1]=16
    return m

class MeshCorridorTests(unittest.TestCase):
    def test_dense_rotated_reflected_reverse(self):
        for a in (0,90,180,270):
            for mirror in (False,True):
                for reverse in (False,True):
                    with self.subTest(angle=a,mirror=mirror,reverse=reverse):
                        m=transform(dense(),a,mirror)
                        if reverse:m['endpoints'].reverse()
                        original=copy.deepcopy(m);r=search(m,timeout=4)
                        self.assertEqual(r['method'],'conservative-configuration-space-contracted-mesh')
                        self.assertLessEqual(r['vertices'],1200);self.assertLessEqual(r['expanded'],r['vertices'])
                        self.assertGreater(r['graph_scope']['local_vertices'],1200)
                        self.assertEqual(r['graph_scope']['radius_mm'],1.125)
                        free,*_=configuration_space(m)
                        self.assertTrue(free.covers(LineString(r['path_mm'])))
                        self.assertEqual(m,original)
    def test_mesh_caps_and_deadline(self):
        free,*_=configuration_space(dense());ends=[tuple(p['center']) for p in dense()['endpoints']]
        for kwargs in ({'max_cells':1,'max_vertices':1200},{'max_vertices':1}):
            with self.assertRaises(MeshLimit):corridor_path(free,*ends,check=lambda:None,**kwargs)
        with patch('shapely.constrained_delaunay_triangles',side_effect=AssertionError('must not allocate over cap')):
            with self.assertRaises(MeshLimit):corridor_path(free,*ends,check=lambda:None,max_vertices=1200,max_cells=1)
        def stop():raise TimeoutError('test')
        with self.assertRaises(TimeoutError):corridor_path(free,*ends,check=stop,max_vertices=1200)
    def test_hole_and_same_cell(self):
        free=box(0,0,10,10).difference(box(4,4,6,6))
        for ends in [((1,5),(9,5)),((1,1),(2,1))]:
            r=corridor_path(free,*ends,check=lambda:None,max_vertices=1200)
            self.assertTrue(free.covers(LineString(r['path_mm'])))
            self.assertEqual(r['path_mm'][0],ends[0]);self.assertEqual(r['path_mm'][-1],ends[1])
    def test_triangulation_cannot_drop_holes(self):
        from shapely.geometry import GeometryCollection,Polygon
        free=box(0,0,10,10).difference(box(4,4,6,6))
        with patch('shapely.constrained_delaunay_triangles',return_value=GeometryCollection([Polygon([(0,0),(10,0),(10,10)]),Polygon([(0,0),(0,10),(10,10)])])):
            with self.assertRaises(ValueError):corridor_path(free,(1,5),(9,5),check=lambda:None,max_vertices=1200)
    def test_closed_gap_still_fails(self):
        m=dense();m['primitives'].append(dict(kind='keepout',net='',blocks_same_net=True,clearance_mm=0,polygons=[dict(shell=[[14,10],[16,10],[16,20],[14,20]],holes=[])]))
        self.assertEqual(search(m)['status'],'no_path')
    def test_larger_clearance_not_ignored(self):
        m=dense();m['minimum_clearance_mm']=.9
        self.assertNotEqual(search(m)['status'],'found')
    def test_direct_route_remains_direct(self):
        r=search(complex_model());self.assertEqual(r['method'],'conservative-configuration-space-direct')

    def test_standalone_worker_and_limit_transport(self):
        import os,subprocess,sys,tempfile
        import pcb_local.geometry_search as module
        from geometry_fixture import create
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);board,task=create(folder/'input',gates=(),name='arbitrary')
            model=dense();model['task']=task
            document=folder/'model.json';document.write_text(json.dumps(dict(status='ready',model=model)))
            for cap in (1200,1):
                worker=folder/('worker'+str(cap)+'.py')
                worker.write_text(Path(module.__file__).read_text().replace('MAX_VERTICES = 1200','MAX_VERTICES = '+str(cap)))
                out=folder/('output'+str(cap));env=dict(os.environ,PYTHONPATH=str(Path(module.__file__).resolve().parents[1]))
                proc=subprocess.run([sys.executable,str(worker),'--board',str(board),'--model',str(document),'--out-dir',str(out),'--timeout','4'],capture_output=True,text=True,env=env,timeout=6)
                self.assertEqual(proc.returncode,0,proc.stderr)
                result=json.loads((out/'search.json').read_text())
                self.assertEqual(result['status'],'found' if cap==1200 else 'bounded_out',result)
                self.assertTrue(result['input_unchanged'])
                if cap==1:self.assertEqual(result['limit_kind'],'vertex_limit')
                else:self.assertTrue(result['requires_native_verification'])

if __name__=='__main__':unittest.main()
