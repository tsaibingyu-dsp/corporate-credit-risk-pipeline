import argparse
import json
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from .adapters import discover, load_items
from .engine import Verifier


def write_json(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def main(argv=None):
    parser=argparse.ArgumentParser(description='四項授信 AI 分析 L1–L5 驗證；預設離線、不修改原始資料、不呼叫 AI')
    inputs=parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--root',type=Path,help='遞迴搜尋各模組正式分析結果')
    inputs.add_argument('--input',type=Path,nargs='+',help='指定一個或多個分析 JSON')
    parser.add_argument('--output',type=Path,default=Path('data/verification'))
    parser.add_argument('--as-of',type=date.fromisoformat,help='驗證基準日期 YYYY-MM-DD；預設台灣今天')
    parser.add_argument('--historical',action='store_true',help='歷史資料稽核，不套用新聞五年／判決兩年範圍')
    parser.add_argument('--check-http',action='store_true',help='額外檢查白名單來源連線；不代表已驗證全文真實性')
    parser.add_argument('--semantic-review',action='store_true',help='額外使用 Gemini 逐項語意覆核；每筆最多一次 API 呼叫')
    parser.add_argument('--review-model',help='語意覆核模型 ID；預設 GEMINI_MODEL')
    parser.add_argument('--strict',action='store_true',help='待人工覆核或空結果亦以非零代碼結束')
    args=parser.parse_args(argv)
    if args.review_model and not args.semantic_review:
        parser.error('--review-model 需搭配 --semantic-review')
    paths=discover(args.root) if args.root else args.input
    output=args.output.resolve()
    # Output must never overwrite an input snapshot or its sidecar/manifest.
    for path in paths:
        if output==path.resolve().parent:
            parser.error('--output 必須使用獨立資料夾，不能與分析輸入放同一層')
    reviewer=None
    if args.semantic_review:
        from .semantic import SemanticReviewer
        try:
            reviewer=SemanticReviewer(model=args.review_model)
        except Exception as exc:
            parser.error(f'Gemini 覆核初始化失敗：{exc}')
    verifier=Verifier(as_of=args.as_of,check_http=args.check_http,reviewer=reviewer,historical=args.historical)
    files,results,analyses={},[],{}
    input_errors=[]
    for path in paths:
        try:
            items,info=load_items(path)
            files[str(path.resolve())]=info
        except Exception as exc:
            input_errors.append(dict(path=str(path),error=f'{type(exc).__name__}: {exc}'))
            print(f'INPUT FAILED {path}: {exc}')
            continue
        for item in items:
            print(f'VERIFY {item.module} {path} [{item.index}]')
            try:
                result=verifier.validate(item)
            except Exception as exc:
                input_errors.append(dict(path=str(path),index=item.index,error=f'{type(exc).__name__}: {exc}'))
                continue
            results.append(result)
            analyses[result['record_id']]=item.analysis
            print(f"  {result['verdict']}")
    counts=Counter(r['verdict'] for r in results)
    empty=[v for v in files.values() if not v['analysis_count']]
    report=dict(schema_version='credit-risk-verification-v2',created_at=datetime.now(timezone.utc).isoformat(),
        as_of=verifier.as_of.isoformat(),mode=dict(http=args.check_http,semantic_review=args.semantic_review,
        review_model=getattr(reviewer,'model',None),historical=args.historical),
        limitations=['檔案及引文一致不代表事件或 AI 解讀已獲客觀證實。',
                    '本工具不作授信評等、核貸或扣分。',
                    '本機雜湊證明保存資料的一致性；若原文與雜湊同時遭竄改，仍須官方來源與人工查核。',
                    '同供應商或同模型語意覆核仍可能有共同錯誤；不會取代人工覆核。'],
        summary=dict(files=len(files),records=len(results),passed=counts['passed'],failed=counts['failed'],
                     needs_review=counts['needs_review'],input_errors=len(input_errors),empty_files=len(empty)),
        input_files=list(files.values()),input_errors=input_errors,results=results)
    output.mkdir(parents=True,exist_ok=True)
    write_json(output/'verification_report.json',report)
    write_json(output/'manual_review_queue.json',[dict(**r,analysis=analyses[r['record_id']],
        human_review=dict(status='pending',reviewer=None,reviewed_at=None,decision=None,reason=None))
        for r in results if r['review_required']])
    write_json(output/'quarantined_results.json',[dict(verification=r,analysis=analyses[r['record_id']]) for r in results if r['verdict']=='failed'])
    write_json(output/'evidence_validated_results.json',[dict(verification=r,analysis=analyses[r['record_id']])
        for r in results if r['verdict']!='failed' and any(c['name']=='literal_evidence' and c['status']=='pass' for c in r['checks'])])
    write_json(output/'approved_results.json',[dict(verification=r,analysis=analyses[r['record_id']]) for r in results if r['verdict']=='passed'])
    print('SUMMARY '+json.dumps(report['summary'],ensure_ascii=False))
    print(f'REPORT {output / "verification_report.json"}')
    if input_errors or counts['failed']:
        return 1
    return 2 if args.strict and (counts['needs_review'] or empty or not results) else 0
