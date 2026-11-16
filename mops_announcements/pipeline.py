import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .client import MopsClient
from .parsing import PAGE_URL, parse_year, resolve_company, parse_listing, parse_detail


def save(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def run(company, year, *, out='data/mops', limit=None, channel='msedge', delay=3.0,
        refresh=False, client_factory=MopsClient, progress=print,start=None,end=None,as_of=None):
    from credit_risk.date_scope import window,metadata,filter_records
    begin,finish=window(start,end,as_of=as_of)
    company = company.strip()
    if not company:
        raise ValueError('請輸入公司名稱或股票代號')
    roc, gregorian = parse_year(year)
    if not begin.year<=gregorian<=finish.year:
        raise ValueError('查詢年度不在近兩年日期範圍內')
    if limit is not None and limit < 1:
        raise ValueError('limit 必須大於零；不指定即擷取全年度')
    root = Path(out)
    identity = hashlib.sha256(company.encode()).hexdigest()[:12]
    path = root / ('query_' + identity) / str(roc) / 'mops.json'
    result = dict(schema_version='mops-v1', module_id='mops_material_announcements', score_item=4,
                  input_company=company, year_roc=roc, year_ad=gregorian,
                  date_window=metadata(begin,finish,as_of),
                  source_url=PAGE_URL, queried_at=datetime.now(timezone.utc).isoformat(),
                  status='running', company=None, announcements=[], errors=[],
                  summary=dict(list_count=0, selected_count=0, success_count=0, failed_count=0,
                               skipped_count=0, listing_complete=False, all_details_complete=False,
                               annual_collection_complete=False, limited=limit is not None))
    try:
        with client_factory(root / 'raw', channel=channel, delay=delay, refresh=refresh) as client:
            payload, provenance = client.fetch('KeywordsQuery', dict(queryFunction=False, keyword=company))
            result['company_lookup_source'] = provenance
            resolved = resolve_company(payload, company)
            result['company'] = resolved
            code = resolved['company_code']
            path = root / code / str(roc) / 'mops.json'
            params = dict(companyId=code, year=str(roc), month='all', firstDay='', lastDay='')
            payload, provenance = client.fetch('t05st01', params)
            result['listing_source'] = provenance
            items = parse_listing(payload, code, roc)
            eligible,scope=filter_records(items,'mops',begin,finish,as_of=as_of)
            result['date_window'].update(scope)
            result['excluded_announcements']=scope['excluded']+scope['unknown']
            result['announcements'] = eligible
            summary = result['summary']
            summary.update(list_count=len(items), listing_complete=True,excluded_date_count=len(items)-len(eligible),date_eligible_count=len(eligible))
            items=eligible
            selected = items if limit is None else items[:limit]
            summary.update(selected_count=len(selected), skipped_count=len(items)-len(selected))
            for item in items[len(selected):]:
                item['status'] = 'skipped_limit'
            save(path, result)
            progress(f'MOPS {code} 民國{roc}年：年度清單 {summary["list_count"]} 筆，近兩年範圍 {len(items)} 筆，擷取全文 {len(selected)} 筆')
            for index, item in enumerate(selected, 1):
                try:
                    payload, provenance = client.fetch('t05st01_detail', item['detail_parameters'], cache=True)
                    item['source'] = dict(page_url=PAGE_URL, **provenance)
                    item.update(parse_detail(payload, item), status='success')
                    summary['success_count'] += 1
                except Exception as exc:
                    if hasattr(client, 'invalidate_detail'):
                        client.invalidate_detail(item['detail_parameters'])
                    item.update(status='failed', error=str(exc))
                    summary['failed_count'] += 1
                save(path, result)
                progress(f"  [{index}/{len(selected)}] {item['announcement_date']} #{item['serial_number']} {item['status']}")
            summary['all_details_complete'] = summary['success_count'] == len(items)
            summary['date_window_collection_complete'] = summary['listing_complete'] and summary['all_details_complete'] and not scope['unknown_date_count']
            summary['annual_collection_complete'] = summary['date_window_collection_complete'] and summary['excluded_date_count']==0
            result['status'] = 'complete' if summary['date_window_collection_complete'] else 'partial'
    except Exception as exc:
        result['errors'].append(str(exc))
        result['status'] = 'failed'
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    save(path, result)
    return path, result


def main(argv=None):
    parser = argparse.ArgumentParser(description='第四項：MOPS 近兩年重大訊息全文；--extract 選用 Gemini 風險分析')
    parser.add_argument('--company', help='上市櫃公司簡稱、全名或股票代號')
    parser.add_argument('--year', help='選用，僅查近兩年區間內的指定年度；未指定時自動跨年度')
    parser.add_argument('--start',help='公告起日 YYYY-MM-DD，預設兩年前')
    parser.add_argument('--end',help='公告迄日 YYYY-MM-DD，預設今天')
    parser.add_argument('--file', type=Path, help='直接分析既有 mops.json，不重新查詢網站；搭配 --extract')
    parser.add_argument('--extract', action='store_true', help='下載後以 Gemini 分析完整公告，或分析 --file 指定的檔案')
    parser.add_argument('--out', default='data/mops', help='輸出根目錄')
    parser.add_argument('--limit', type=int, help='僅供小量測試；不指定即擷取全年度')
    parser.add_argument('--channel', choices=['msedge', 'chrome', 'chromium'], default='msedge')
    parser.add_argument('--delay', type=float, default=3.0, help='請求間隔秒數，最少 3 秒')
    parser.add_argument('--refresh', action='store_true', help='重新下載已快取公告全文')
    args = parser.parse_args(argv)
    if args.file and (args.company or args.year):
        parser.error('--file 與 --company／--year 請擇一使用')
    if args.file and not args.extract:
        parser.error('--file 請搭配 --extract')
    if not args.file and not args.company:
        parser.error('請指定 --company，或 --file 搭配 --extract')
    try:
        from credit_risk.date_scope import window
        begin,finish=window(args.start,args.end)
        batches=[]
        if args.file:
            from .analysis import scoped_file
            path=scoped_file(args.file,begin,finish)
            batches.append((path,None))
        else:
            years=[args.year] if args.year else range(begin.year,finish.year+1)
            for year in years:
                path, result = run(args.company, year, out=args.out, limit=args.limit,
                                   channel=args.channel, delay=args.delay, refresh=args.refresh,
                                   start=begin,end=finish)
                batches.append((path,result))
                print(f"{result['status'].upper()} {path.resolve()}")
                for error in result['errors']:
                    print(error, file=sys.stderr)
        ai_failed = False
        for path,result in batches:
            if args.extract and (result is None or result['status'] != 'failed'):
                from .analysis import analyze_file
                analysis_path, manifest = analyze_file(path)
                print(f"AI {manifest['status'].upper()} {analysis_path.resolve()}")
                ai_failed = ai_failed or manifest['summary']['failed'] > 0
                for record in manifest['records']:
                    if record['error']:
                        print(f"{record['announcement_id']}: {record['error']}", file=sys.stderr)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    collection_failed = any(result is not None and (result['status']=='failed' or result['summary']['failed_count']) for _,result in batches)
    return 1 if collection_failed or ai_failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
