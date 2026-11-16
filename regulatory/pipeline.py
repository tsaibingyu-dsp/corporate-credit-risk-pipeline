import argparse
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from judicial.company import normalize_name
from judicial.extractor import GeminiExtractor, validate_evidence
from .common import Progress, SITES
from .schema import RegulatoryNotice, SYSTEM, PROMPT_VERSION
from .sources import collect
from .csv_source import DEFAULT_CSV, MAPPING_VERSION, bounds, collect_csv,collect_inputs,snapshot_paths
from company_names import analysis_profile, resolve_profile, identity_names


def company_match(company, aliases, text, extraction, name_profile=None):
    profile = analysis_profile(company, aliases, supplied=name_profile)
    names = {normalize_name(name) for name in [*identity_names(profile), *aliases]}
    matches = []
    for entity in (extraction or {}).get('entities', []):
        fact = entity['name']
        if fact['status'] == 'stated' and fact['value'] and normalize_name(fact['value']) in names:
            matches.append(entity)
    mentioned = any(normalize_name(name) in normalize_name(text) for name in profile['search_names'])
    return dict(type='entity_name_match' if matches else 'text_mention' if mentioned else 'unmatched',
                entities=matches, identity_verified=False)


def process_notice(notice, root, extractor, company, aliases, name_profile=None):
    # SFB now goes directly from raw data to risk analysis; no AI fact-extraction pass.
    if notice.source == 'sfb':
        extractor = None
    mapped = notice.structured is not None
    provider = 'rules' if mapped else extractor.provider if extractor else None
    model = None if mapped else extractor.model if extractor else None
    version = MAPPING_VERSION if mapped else PROMPT_VERSION
    body_hash = hashlib.sha256(notice.full_text.encode()).hexdigest()
    identity = [notice.source, notice.source_url, notice.record_locator, body_hash,
                version, provider, model]
    doc_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = root / 'documents' / (doc_id + '.json')
    record = None
    if path.exists():
        cached = json.loads(path.read_text(encoding='utf-8'))
        if cached['extraction_status'] in ('success', 'not_requested'):
            record = cached
            print(f'CACHE {notice.source} {doc_id}', flush=True)
    if record is None:
        record = dict(document_id=doc_id, score_item=2, module_id='regulatory_announcements',
                      schema_version='1.0', prompt_version=version,
                      source=notice.source, source_url=notice.source_url, search_url=notice.search_url,
                      fetched_at=datetime.now(timezone.utc).isoformat(), title=notice.title,
                      full_text=notice.full_text, full_text_kind=notice.full_text_kind,
                      full_text_sha256=body_hash, raw_paths=notice.raw_paths,
                      attachments=notice.attachments, record_locator=notice.record_locator,
                      provenance=notice.provenance, raw_record=notice.raw_record,
                      collection_limitations=notice.limitations,
                      full_text_status='partial' if any(a['status'] == 'failed' for a in notice.attachments)
                      else 'provided_record' if notice.full_text_kind == 'provided_penalty_csv_record'
                      else 'published_record' if notice.full_text_kind == 'official_penalty_record' else 'complete',
                      provider=provider, model=model,
                      extraction_method='csv_mapping' if mapped else 'gemini' if extractor else 'not_requested',
                      extraction_status='not_requested', extraction=None, error=None)
        if mapped:
            result = RegulatoryNotice.model_validate(notice.structured)
            validate_evidence(result, notice.full_text)
            record['extraction'], record['extraction_status'] = result.model_dump(), 'success'
        elif extractor:
            try:
                result = extractor.extract(notice.full_text)
                validate_evidence(result, notice.full_text)
                record['extraction'] = result.model_dump()
                record['extraction_status'] = 'success'
            except Exception as error:
                record['extraction_status'], record['error'] = 'failed', str(error)
        path.parent.mkdir(parents=True, exist_ok=True)
    record['company'] = company
    record['date_metadata'] = notice.date_metadata
    record['company_name_profile'] = analysis_profile(company, aliases, supplied=name_profile)
    if mapped:
        record['full_text_status'] = 'provided_record'
        record['provenance'] = notice.provenance
    record['company_match'] = company_match(company, aliases, record['full_text'], record['extraction'], record['company_name_profile'])
    record['requires_human_review'], record['score_ready'] = True, False
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{record["extraction_status"].upper()} {notice.source} {doc_id}', flush=True)
    return record


def run(company, aliases, selected, root, limit, delay, browser, headed, start, end,
        verification_wait, extractor, collector=collect, prtr_csv=None, name_profile=None):
    from credit_risk.date_scope import window,record_day
    begin,finish=window(start,end)
    start,end=begin.isoformat(),finish.isoformat()
    profile = analysis_profile(company, aliases, supplied=name_profile)
    root.mkdir(parents=True, exist_ok=True)
    reports, documents, failures, seen = [], [], [], set()
    for source in selected:
        for query in profile['search_names']:
            report = Progress(source, query, SITES[source], date_scope={
                'start': start,'end': end,
                'field': 'csv_date' if source == 'prtr' else 'official_page_date'})
            print(f'SEARCH {source} {query}', flush=True)
            try:
                notices = (collect_inputs(prtr_csv, query, limit, root, report, start, end)
                           if source == 'prtr' and prtr_csv else
                           collector(source, query, limit, delay, root, report, browser=browser,
                                     headed=headed, start=start, end=end, verification_wait=verification_wait))
                for notice in notices:
                    day,basis=record_day('environment' if source=='prtr' else 'sfb',
                        dict(raw_record=notice.raw_record,full_text=notice.full_text,raw_paths=notice.raw_paths,date_metadata=notice.date_metadata),
                        allowed_roots=[root])
                    if day is None:
                        report.errors.append(dict(source_url=notice.source_url,error='來源日期不足，已排除且需覆核'))
                        continue
                    if not begin<=day<=finish:
                        report.date_scope['excluded_count']=report.date_scope.get('excluded_count',0)+1
                        continue
                    key = (notice.source, notice.source_url,
                           json.dumps(notice.record_locator, sort_keys=True),
                           hashlib.sha256(notice.full_text.encode()).hexdigest())
                    if notice.source=='prtr' and notice.raw_record and '裁處書字號' in notice.raw_record:
                        key=('prtr',notice.raw_record['裁處書字號'],hashlib.sha256(notice.full_text.encode()).hexdigest())
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        documents.append(process_notice(notice, root, extractor, company, aliases, profile))
                    except Exception as error:
                        failures.append(dict(source=source, source_url=notice.source_url, error=str(error)))
            except Exception as error:
                if report.state == 'running':
                    report.state = 'failed'
                    report.errors.append(dict(error=str(error)))
                print(f'{report.state.upper()} {source}: {error}', flush=True)
            if report.state == 'running':
                report.state = 'failed'
                report.errors.append(dict(error='資料來源未回報完成狀態'))
            reports.append(report.to_dict())
            # Checkpoint after each source, preserving results if a later source fails.
            write_output(root, company, aliases, selected, reports, documents, failures, name_profile=profile)
    return write_output(root, company, aliases, selected, reports, documents, failures, name_profile=profile)


def write_output(root, company, aliases, selected, reports, documents, failures, name_profile=None):
    # A PRTR-only import must retain the previously saved SFB snapshot.
    existing=root/'regulatory.json'
    if existing.is_file():
        try:
            previous=json.loads(existing.read_text(encoding='utf-8'))
            retained=set(previous.get('requested_sources',[]))-set(selected) if previous.get('company')==company else set()
            if retained:
                reports=[*reports,*[r for r in previous.get('sources',[]) if r.get('source') in retained]]
                documents=[*documents,*[r for r in previous.get('documents',[]) if r.get('source') in retained]]
                failures=[*failures,*[r for r in previous.get('failures',[]) if r.get('source') in retained]]
                selected=list(dict.fromkeys([*selected,*retained]))
        except (OSError,ValueError):
            pass
    extraction_failed = sum(d['extraction_status'] == 'failed' for d in documents)
    result = dict(schema_version='1.0', module_id='regulatory_announcements', score_item=2,
                  company=company, aliases=aliases, generated_at=datetime.now(timezone.utc).isoformat(),
                  company_name_profile=analysis_profile(company, aliases, supplied=name_profile or (documents[0].get('company_name_profile') if documents else None)),
                  requested_sources=selected, sources=reports, documents=documents, failures=failures,
                  summary=dict(document_count=len(documents), extraction_failed=extraction_failed,
                               search_completed=bool(reports) and all(r['state'] == 'complete' and not r['errors'] for r in reports),
                               processing_completed=not failures and extraction_failed == 0
                               and all(d['full_text_status'] != 'partial' for d in documents),
                               scoring_ready=False),
                  limitations=['本模組是四項評分中的第二項資料來源，尚未設定評分權重或自動評分',
                               '同名、公司被提及、負責人受罰與公司本身受罰須區分',
                               '查詢受阻、達上限與查無資料是不同狀態；查無資料也不是無違規證明',
                               'PRTR 的本機快照或公開裁處紀錄都不是原始裁處書；未提供的內容不補造，也不代表完整歷史'])
    from .query_assessment import empty_assessments
    result['query_assessments'] = empty_assessments(result)
    result['summary']['zero_deduction_sources'] = list(result['query_assessments'])
    if 'prtr' in selected:
        from integrated_risk.config import load_scoring_table
        standards=dict(source_type='environment_penalty',scoring_table=load_scoring_table(),
            method='Gemini 建議扣分；main 驗證公司歸責、原文證據、CSV 區間及事件去重後計算',
            missing_or_uncertain='待覆核，不直接扣分',risk_levels=['高','中','低','無明確負面風險','待確認'])
        (root/'environment_scoring_standards.json').write_text(json.dumps(standards,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary = root / 'regulatory.json.tmp'
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(root / 'regulatory.json')
    return result


def date_window(start, end, *, as_of=None):
    from credit_risk.date_scope import window
    begin,finish=window(start,end,as_of=as_of)
    return begin.isoformat(), finish.isoformat()


def analyze_selected(records, company, aliases, selected, root, coverage=None, quote_repair_attempts=2, input_info=None, name_profile=None):
    if name_profile:
        records = [dict(record, company_name_profile=name_profile) for record in records]
    failed = False
    for source in dict.fromkeys(selected):
        if source == 'sfb':
            from .sfb_analysis import analyze_records
            path, analysis = analyze_records([r for r in records if r.get('source') == 'sfb'],
                company, aliases, root, coverage=coverage, quote_repair_attempts=quote_repair_attempts)
            label = 'SFB'
            manifest_path = root / 'sfb_analysis_manifest.json'
        else:
            from .environment_analysis import analyze_records
            path, analysis = analyze_records([r for r in records if r.get('source') == 'prtr'],
                company, aliases, root, coverage=coverage)
            label = 'ENVIRONMENT'
            manifest_path = root / 'environment_analysis_manifest.json'
        if input_info:
            analysis.update(source_file=input_info['path'], source_sha256=input_info['sha256'])
            temporary = manifest_path.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(manifest_path)
        print(f"{label} AI {analysis['status'].upper()} {path.resolve()}", flush=True)
        failed = failed or bool(analysis['summary']['failed'])
        for entry in analysis['records']:
            if entry['error']:
                print(f"{entry['document_id']}: {entry['error']}", flush=True)
    return failed


def main():
    parser = argparse.ArgumentParser(description='評分項目二：證期局裁罰公告＋環境部 CSV，統一輸出 JSON')
    parser.add_argument('--company', required=True)
    parser.add_argument('--alias', action='append', default=[])
    parser.add_argument('--no-auto-alias', action='store_true', help='停用自動簡稱及官方名稱對照')
    parser.add_argument('--sources', nargs='+', choices=SITES, default=list(SITES))
    parser.add_argument('--limit', type=int, default=5, help='每個來源、每個名稱最多幾份；1–100')
    parser.add_argument('--delay', type=float, default=3)
    parser.add_argument('--browser', choices=['msedge', 'chrome', 'chromium'], default='msedge')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--verification-wait', type=int, default=0, help='有介面模式下等待本人完成 PRTR 驗證的秒數，最多 300')
    parser.add_argument('--prtr-csv', type=Path, default=DEFAULT_CSV, help='人工下載 CSV 檔或資料夾；預設 data/environment_imports，檔名不限')
    parser.add_argument('--prtr-web', action='store_true', help='明確改用 PRTR 網站，否則不連 PRTR 網站')
    parser.add_argument('--start', help='公告／裁罰日起 YYYY-MM-DD，預設兩年前；只允許近兩年')
    parser.add_argument('--end', help='公告／裁罰日迄 YYYY-MM-DD，預設台灣今日')
    parser.add_argument('--collect-only', action='store_true')
    parser.add_argument('--analyze', action='store_true', help='以 Gemini 直接分析證期局原始資料及環境部 CSV，不另做證期局 AI 欄位萃取')
    parser.add_argument('--analysis-input', type=Path, help='搭配 --analyze，讀取 regulatory.json，或指定 --sources sfb 分析原始 CSV／JSON')
    parser.add_argument('--quote-repair-attempts', type=int, choices=[0, 1, 2], default=2, help='證期局引文修復上限，預設 2 輪')
    parser.add_argument('--out', type=Path, default=Path('data/regulatory'))
    args = parser.parse_args()
    if not args.company.strip() or any(not name.strip() for name in args.alias):
        parser.error('公司名稱及別名不得空白')
    if not 1 <= args.limit <= 100 or args.delay < 3:
        parser.error('limit 必須為 1–100，delay 至少 3 秒')
    if not 0 <= args.verification_wait <= 300 or args.verification_wait and not args.headed:
        parser.error('verification-wait 必須為 0–300 且搭配 --headed')
    if args.analyze and (args.collect_only or args.prtr_web and 'prtr' in args.sources):
        parser.error('--analyze 不可搭配 --collect-only，環境部分析只接受 CSV、不接受 --prtr-web')
    if args.analysis_input and not args.analyze:
        parser.error('--analysis-input 請搭配 --analyze')
    root = args.out / hashlib.sha256(args.company.encode()).hexdigest()[:16]
    profile = resolve_profile(args.company, args.alias, enabled=not args.no_auto_alias)
    if args.analysis_input:
        from .sfb_analysis import load_input
        try:
            records, coverage, input_info = load_input(args.analysis_input)
            from credit_risk.date_scope import window,filter_records
            begin,finish=window(args.start,args.end)
            eligible=[]
            scopes={}
            for source in args.sources:
                picked,scope=filter_records([r for r in records if r.get('source')==source],
                    'sfb' if source=='sfb' else 'environment',begin,finish,
                    allowed_roots=[Path.cwd(),args.analysis_input.resolve().parent])
                eligible.extend(picked)
                scopes[source]=scope
                if scope['unknown_date_count']:
                    coverage=dict(coverage or {},search_completed=False)
            records=eligible
            root.mkdir(parents=True,exist_ok=True)
            (root/'analysis_date_scope.json').write_text(json.dumps(scopes,ensure_ascii=False,indent=2),encoding='utf-8')
            if not any(r.get('source') in args.sources for r in records) and records:
                parser.error('輸入資料與 --sources 不符；原始證期局 CSV／JSON 請用 --sources sfb')
            failed = analyze_selected(records, args.company, args.alias, args.sources, root,
                                      coverage, args.quote_repair_attempts, input_info, profile)
        except (ValueError, OSError) as error:
            parser.error(str(error))
        if failed:
            raise SystemExit(1)
        return
    if 'prtr' in args.sources and not args.prtr_web:
        try: snapshot_paths(args.prtr_csv)
        except ValueError as error: parser.error(str(error))
    try:
        start, end = date_window(args.start, args.end)
        needs_ai = not args.collect_only and args.prtr_web and 'prtr' in args.sources
        extractor = GeminiExtractor(schema=RegulatoryNotice, system=SYSTEM) if needs_ai else None
    except ValueError as error:
        parser.error(str(error))
    result = run(args.company, args.alias, list(dict.fromkeys(args.sources)), root,
                 args.limit, args.delay, args.browser, args.headed, start, end,
                 args.verification_wait, extractor, prtr_csv=None if args.prtr_web else args.prtr_csv, name_profile=profile)
    print(f'OUTPUT {(root / "regulatory.json").resolve()}', flush=True)
    print(json.dumps(result['summary'], ensure_ascii=False), flush=True)
    analysis_failed = False
    if args.analyze:
        analysis_failed = analyze_selected(result['documents'], args.company, args.alias,
                                          args.sources, root, result['summary'], args.quote_repair_attempts, name_profile=profile)
    if analysis_failed or result['failures'] or result['summary']['extraction_failed'] or any(r['state'] in ('failed', 'blocked') or r['errors'] for r in result['sources']):
        raise SystemExit(1)
