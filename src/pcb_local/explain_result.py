"""Re-explain a hash-bound saved report without routing, validating or adopting."""
import argparse
import hashlib
import json
from pathlib import Path

from . import __version__
from .diagnostics import explain, markdown
from .visual_report import build_model, render_html
from .workspace import source_files


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reexplain(result_path, out_dir):
    path=Path(result_path).resolve();output=Path(out_dir).resolve()
    if output.is_relative_to(path.parent):
        raise ValueError('Explanation must be outside the frozen result directory')
    if output.exists():
        raise ValueError('Explanation output directory already exists')
    raw=path.read_bytes();report=json.loads(raw)
    bound={}
    for key in ('source_hashes','input_hashes','candidate_hashes'):
        values=report.get(key,{})
        if not isinstance(values,dict):
            raise ValueError('Invalid recorded context hashes: '+key)
        for filename,sha in values.items():
            if filename in bound and bound[filename]!=sha:
                raise ValueError('Conflicting recorded context hashes')
            bound[filename]=sha
    inputs=[Path(p) for p in report.get('input_hashes',{}) if Path(p).suffix=='.kicad_pcb']
    if len(inputs)!=1:
        raise ValueError('One hash-bound input board is required')
    candidate=report.get('candidate')
    if candidate and candidate not in report.get('candidate_hashes',{}):
        raise ValueError('Candidate has no recorded hash')
    def unchanged():
        if path.read_bytes()!=raw:
            raise ValueError('Saved report changed during explanation')
        for filename,sha in bound.items():
            if digest(filename)!=sha:
                raise ValueError('Saved board/context changed: '+filename)
        # Detect added rule/library files too, not only modifications to listed files.
        for key,board in [('source_hashes',report.get('source_board')),
                          ('input_hashes',inputs[0]),('candidate_hashes',candidate)]:
            if report.get(key) and board and source_files(Path(board))!=report[key]:
                raise ValueError('Saved context file set changed: '+key)
    unchanged()
    diagnosis=explain(report)
    view=dict(report,diagnosis=diagnosis)
    model=build_model(view,before=inputs[0])
    model['warnings'].insert(0,'历史报告解释；本次只核对已记录文件哈希，没有重新运行原生验收。')
    html=render_html(model)
    receipt=dict(schema_version=1,version=__version__,status='explanation_complete',
                 accepted=False,adoption_available=False,display_only=True,revalidation_performed=False,
                 source_result=str(path),source_result_sha256=hashlib.sha256(raw).hexdigest(),
                 recorded_status=report['status'],recorded_accepted=report.get('accepted',False),
                 context_hashes=bound,diagnosis=diagnosis,visualization='comparison.html')
    text='# 已保存结果的离线解释\n\n仅核对文件哈希，没有重新验收或授权采纳。原状态：'+str(report['status'])+'。\n'+markdown(diagnosis)
    unchanged()
    outputs=[('explanation.json',json.dumps(receipt,ensure_ascii=False,indent=2,allow_nan=False)+'\n'),
             ('report.md',text),('comparison.html',html)]
    output.mkdir(parents=True,exist_ok=False)
    for name,value in outputs:
        (output/name).write_text(value,encoding='utf-8')
    return receipt


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result',type=Path,required=True)
    parser.add_argument('--out-dir',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        result=reexplain(args.result,args.out_dir)
        print(json.dumps(dict(status=result['status'],category=result['diagnosis']['category'],
                             explanation=str(args.out_dir/'explanation.json'),accepted=False),ensure_ascii=False))
        return 0
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(json.dumps(dict(status='error',reason=str(exc),accepted=False),ensure_ascii=False))
        return 50


if __name__=='__main__':
    raise SystemExit(main())
