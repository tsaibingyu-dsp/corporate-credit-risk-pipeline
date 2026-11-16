"""Company search orchestration and reviewable feature exports."""
import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .collector import collect, CollectionReport
from .extractor import create_extractor, PROMPT_VERSION
from .parser import parse_html
from .capture import save_collected, load_collected
from .dates import two_year_window
from company_names import resolve_profile, analysis_profile, identity_names


def normalize_name(name):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', name)).replace('臺', '台')


def canonical_url(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    # Search session identifiers and result position are not document identity.
    if parsed.hostname == 'judgment.judicial.gov.tw' and query.get('jid'):
        return urlunparse(parsed._replace(query=urlencode({'id': query.get('id', [''])[0],
                                                          'jid': query['jid'][0]}), fragment=''))
    if parsed.hostname == 'judgment.judicial.gov.tw' and query.get('id') and parsed.path.endswith('/data.aspx'):
        return urlunparse(parsed._replace(query=urlencode({'ty': query.get('ty', ['JD'])[0],
                                                          'id': query['id'][0]}), fragment=''))
    return url


def company_relation(company, aliases, text, extraction, name_profile=None):
    profile = analysis_profile(company, aliases, supplied=name_profile)
    names = {normalize_name(name) for name in [*identity_names(profile), *aliases]}
    matched = []
    for party in extraction.get('parties', []):
        fact = party['name']
        if (fact['status'] == 'stated' and fact['value'] and not party['anonymized']
                and normalize_name(fact['value']) in names):
            matched.append(party)
    mention = any(normalize_name(name) in normalize_name(text) for name in profile['search_names'])
    return dict(match_type='party_name_match' if matched else 'text_mention' if mention else 'unmatched',
                matched_parties=matched, identity_verified=False,
                requires_human_review=True,
                note='名稱吻合不等於法人身分已確認；僅被提及不等於涉案當事人')


def make_features(record, company, aliases):
    extraction = record.get('extraction', {})
    relation = company_relation(company, aliases, record['text'], extraction, record.get('company_name_profile'))
    matched_names = {normalize_name(p['name']['value']) for p in relation['matched_parties']}
    signals = [s for s in extraction.get('risk_signals', [])
               if s.get('subject') and normalize_name(s['subject']) in matched_names]
    def value(key):
        return extraction.get(key, {}).get('value')
    return dict(document_id=record['document_id'], source_url=record['source_url'],
                company=company, match_type=relation['match_type'],
                matched_party_names=[p['name']['value'] for p in relation['matched_parties']],
                party_roles=sorted({r for p in relation['matched_parties'] for r in p['roles']}),
                court=value('court'), case_number=value('case_number'),
                case_category=value('case_category'), judgment_date=value('judgment_date') or record.get('judgment_date'),
                document_type=value('document_type'), cause=value('cause'),
                disposition=value('disposition'), finality=value('finality'),
                company_signals=signals,
                company_court_signal_count=sum(s['basis'] in ('court_finding', 'disposition') for s in signals),
                company_allegation_signal_count=sum(s['basis'] == 'allegation' for s in signals),
                amounts=extraction.get('amounts', []),
                extraction_status='success' if extraction else 'not_requested',
                identity_verified=False, requires_human_review=True,
                score_ready=False)


class DocumentProcessor:
    def __init__(self, root, extractor, quote_repair_attempts=2):
        self.root, self.extractor = root, extractor
        if quote_repair_attempts not in (0, 1, 2):
            raise ValueError('引文修復上限只能為 0、1 或 2')
        self.quote_repair_attempts = quote_repair_attempts
        (root / 'raw').mkdir(parents=True, exist_ok=True)
        (root / 'documents').mkdir(exist_ok=True)
        self.db = sqlite3.connect(root / 'pipeline.sqlite3')
        self.db.execute('CREATE TABLE IF NOT EXISTS company_documents '
                        '(id TEXT PRIMARY KEY, url TEXT, status TEXT, error TEXT, path TEXT)')

    def process(self, url, content, is_html=True):
        canonical = canonical_url(url)
        raw_hash = hashlib.sha256(content.encode()).hexdigest()
        raw_path = self.root / 'raw' / (raw_hash + ('.html' if is_html else '.txt'))
        # Preserve exactly the UTF-8 bytes used to calculate raw_hash on Windows too.
        raw_path.write_bytes(content.encode('utf-8'))
        # Parse failure remains auditable and is reported by the caller.
        text = (parse_html(content) if is_html else content).strip()
        if not text.strip():
            raise ValueError('空白正文')
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        identity = [canonical, text_hash, PROMPT_VERSION,
                    self.extractor.provider if self.extractor else 'collect',
                    self.extractor.model if self.extractor else None]
        doc_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        path = self.root / 'documents' / (doc_id + '.json')
        row = self.db.execute('SELECT status FROM company_documents WHERE id=?', (doc_id,)).fetchone()
        if row and row[0] == 'success' and path.exists():
            print(f'CACHE {doc_id}', flush=True)
            return json.loads(path.read_text(encoding='utf-8'))
        try:
            record = dict(document_id=doc_id, schema_version='1.0', prompt_version=PROMPT_VERSION,
                          source_url=canonical, fetched_url=url,
                          fetched_at=datetime.now(timezone.utc).isoformat(),
                          raw_sha256=raw_hash, text_sha256=text_hash,
                          raw_path=str(raw_path.resolve()), text=text,
                          provider=self.extractor.provider if self.extractor else None,
                          model=self.extractor.model if self.extractor else None,
                          identity_match='unverified', requires_human_review=True)
            if self.extractor:
                result = self.extractor.extract(text)
                from .quote_repair import validate_or_repair, QuoteRepairFailed
                audit_path = self.root / 'quote_repair_logs' / (doc_id + '.json')
                try:
                    result, audit = validate_or_repair(result, text, self.extractor, max_rounds=self.quote_repair_attempts)
                except ValueError as error:
                    if isinstance(error, QuoteRepairFailed):
                        audit_path.parent.mkdir(parents=True, exist_ok=True)
                        audit_path.write_text(json.dumps(dict(document_id=doc_id, source_url=canonical,
                            text_sha256=text_hash, model=self.extractor.model, **error.audit),
                            ensure_ascii=False, indent=2), encoding='utf-8')
                    rejected = self.root / 'rejected_extractions' / (doc_id + '.json')
                    rejected.parent.mkdir(parents=True, exist_ok=True)
                    rejected.write_text(json.dumps(dict(validated=False, document_id=doc_id,
                        source_url=canonical, text_sha256=text_hash, raw_path=str(raw_path.resolve()),
                        provider=self.extractor.provider, model=self.extractor.model,
                        requires_human_review=True, quote_repair_log=str(audit_path.resolve()),
                        validation_error=str(error), validation_path=getattr(error, 'path', None),
                        extraction=result.model_dump()), ensure_ascii=False, indent=2), encoding='utf-8')
                    raise ValueError(f'{error}；未通過的模型結果已保存：{rejected.resolve()}') from error
                audit_path.parent.mkdir(parents=True, exist_ok=True)
                audit_path.write_text(json.dumps(dict(document_id=doc_id, source_url=canonical,
                    text_sha256=text_hash, model=self.extractor.model, **audit), ensure_ascii=False, indent=2), encoding='utf-8')
                record['evidence_repairs'] = audit['format_repairs']
                record['quote_repair_log'] = str(audit_path.resolve())
                record['quote_repair_rounds'] = len(audit['rounds'])
                record['extraction'] = result.model_dump()
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            self.db.execute('INSERT OR REPLACE INTO company_documents VALUES (?,?,?,?,?)',
                            (doc_id, canonical, 'success', None, str(path)))
            self.db.commit()
            print(f'OK {doc_id}', flush=True)
            return record
        except Exception as error:
            self.db.execute('INSERT OR REPLACE INTO company_documents VALUES (?,?,?,?,?)',
                            (doc_id, canonical, 'failed', str(error), str(path)))
            self.db.commit()
            raise


def export(root, company, aliases, records, searches, failures, *, date_window=None, collection_count=None):
    features = [make_features(record, company, aliases) for record in records]
    complete = bool(searches) and not failures and all(s['completed'] and not s['limit_reached'] and not s['errors'] for s in searches)
    summary = dict(company=company, aliases=aliases, generated_at=datetime.now(timezone.utc).isoformat(),
                   search_completed=complete, searches=searches,
                   successful_document_count=len(features), failed_document_count=len(failures),
                   search_error_count=sum(len(s['errors']) for s in searches),
                   party_name_match_document_count=sum(f['match_type'] == 'party_name_match' for f in features),
                   text_mention_document_count=sum(f['match_type'] == 'text_mention' for f in features),
                   failures=failures, identity_verified=False, score_ready=False,
                   date_window=date_window, collected_document_count=collection_count,
                   limitations=['搜尋結果不等於完整訴訟歷史；不同裁判文書可能屬同一案件或歷審',
                                '無結果不等於無訴訟；名稱匹配不等於授信對象身分確認',
                                '金額保留各筆性質與對象，不自動相加；風險訊號不等於違約或信用不良'])
    for name, data in [('company_summary.json', summary), ('case_features.json', features)]:
        (root / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    fields = ['document_id', 'company', 'match_type', 'matched_party_names', 'party_roles',
              'court', 'case_number', 'case_category', 'judgment_date', 'document_type',
              'cause', 'disposition', 'finality', 'company_court_signal_count',
              'company_allegation_signal_count', 'company_signals', 'amounts',
              'extraction_status', 'identity_verified', 'requires_human_review', 'score_ready', 'source_url']
    with (root / 'case_features.csv').open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in features:
            cells = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in row.items()}
            # Prevent spreadsheet formula interpretation of untrusted document text.
            writer.writerow({k: "'" + v if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@')) else v
                             for k, v in cells.items()})
    return summary


def main():
    parser = argparse.ArgumentParser(description='輸入公司名稱，自動搜尋、擷取、Gemini 萃取及匯出')
    parser.add_argument('--company', required=True, help='公司完整名稱')
    parser.add_argument('--alias', action='append', default=[], help='別名／舊名，可重複指定並分別搜尋')
    parser.add_argument('--no-auto-alias', action='store_true', help='停用自動簡稱及官方名稱對照')
    parser.add_argument('--limit', type=int, default=None, help='選用測試上限；預設遍歷近兩年全部結果，不設筆數上限')
    parser.add_argument('--start', help='裁判起日 YYYY-MM-DD，預設今天往前兩年；只能縮小範圍')
    parser.add_argument('--end', help='裁判迄日 YYYY-MM-DD，預設台灣今天')
    parser.add_argument('--delay', type=float, default=3)
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--browser', choices=['chromium', 'msedge', 'chrome'], default='chromium',
                        help='可使用已安裝的 Edge／Chrome，免下載 Chromium')
    parser.add_argument('--collect-only', action='store_true', help='只蒐集，不呼叫 AI')
    parser.add_argument('--analyze', action='store_true', help='欄位萃取後，以 Gemini 分析目標企業的裁判風險')
    parser.add_argument('--quote-repair-attempts', type=int, choices=[0, 1, 2], default=2,
                        help='引文驗證失敗後最多額外修復幾輪，預設 2；0 僅允許本機空白恢復')
    parser.add_argument('--analysis-input', type=Path, help='直接分析既有單筆裁判 JSON 或 documents 資料夾，不重新爬取或萃取')
    parser.add_argument('--process-collected', type=Path, help='對既有 collected_documents 資料夾進行 Gemini 萃取；搭配 --analyze 再分析風險')
    parser.add_argument('--file', type=Path, help='離線測試：改用單一本機 .txt／HTML，不查網站')
    parser.add_argument('--out', type=Path, default=Path('data/companies'))
    args = parser.parse_args()
    if not args.company.strip() or any(not alias.strip() for alias in args.alias):
        parser.error('公司名稱及別名不得空白')
    if (args.limit is not None and args.limit < 1) or args.delay < 3:
        parser.error('limit 必須為正整數，delay 至少 3 秒')
    if args.collect_only and (args.analyze or args.analysis_input):
        parser.error('--collect-only 不可搭配 --analyze 或 --analysis-input')
    if args.analysis_input and args.file:
        parser.error('--analysis-input 與 --file 請擇一使用')
    if args.process_collected and (args.file or args.analysis_input or args.collect_only):
        parser.error('--process-collected 不可搭配 --file、--analysis-input 或 --collect-only')
    try:
        start, end = two_year_window(start=args.start, end=args.end)
    except ValueError as error:
        parser.error(str(error))
    window = dict(start=start.isoformat(), end=end.isoformat(), basis='judgment_date', inclusive=True)
    company_id = hashlib.sha256(args.company.encode()).hexdigest()[:16]
    root = args.out / company_id
    profile = resolve_profile(args.company, args.alias, enabled=not args.no_auto_alias)
    if args.analysis_input:
        from .risk_analysis import analyze_records, load_records
        try:
            records = load_records(args.analysis_input)
            from credit_risk.date_scope import filter_records
            records,date_scope=filter_records(records,'judicial',start,end,
                allowed_roots=[Path.cwd(),args.analysis_input.resolve().parent])
            root.mkdir(parents=True,exist_ok=True)
            (root/'analysis_date_scope.json').write_text(json.dumps(date_scope,ensure_ascii=False,indent=2),encoding='utf-8')
            for record in records:
                record['company_name_profile'] = profile
            output, analysis = analyze_records(records, args.company, args.alias, root)
        except (ValueError, OSError) as error:
            parser.error(str(error))
        print(f"AI {analysis['status'].upper()} {output.resolve()}", flush=True)
        for entry in analysis['records']:
            if entry['error']:
                print(f"{entry['document_id']}: {entry['error']}", file=sys.stderr)
        if analysis['summary']['failed']:
            raise SystemExit(1)
        return
    root.mkdir(parents=True, exist_ok=True)
    collected, records, searches, failures, seen = [], [], [], [], set()
    print(f'WINDOW judgment_date {start} .. {end}', flush=True)
    # Phase one completes and persists JSON before constructing any AI client.
    try:
        if args.process_collected:
            collected = load_collected(args.process_collected, start, end)
            searches.append(dict(query=args.company, mode='collected_json', completed=False, limit_reached=False, errors=[]))
            prior_path=root/'collection_manifest.json'
            if prior_path.is_file():
                prior=json.loads(prior_path.read_text(encoding='utf-8'))
                same_window=prior.get('date_window',{}).get('start')==str(start) and prior.get('date_window',{}).get('end')==str(end)
                same_documents={r['document_id'] for r in collected}=={r.get('document_id') for r in prior.get('documents',[])}
                if prior.get('company')==args.company and same_window and same_documents and not prior.get('failures'):
                    searches=prior.get('searches') or searches
        for query in ([] if args.process_collected else profile['search_names']):
            report = CollectionReport(query=query)
            try:
                if args.file:
                    inputs = [(args.file.resolve().as_uri(), args.file.read_text(encoding='utf-8'))]
                else:
                    print(f'SEARCH {query} (limit={args.limit if args.limit is not None else "all"})', flush=True)
                    query_dir = root / 'diagnostics' / hashlib.sha256(query.encode()).hexdigest()[:12]
                    inputs = collect(query, args.limit, args.delay, query_dir, args.headed,
                                     report=report, browser_channel=None if args.browser == 'chromium' else args.browser,
                                     start_date=start, end_date=end)
                for url, content in inputs:
                    canonical = canonical_url(url)
                    if canonical in seen:
                        continue
                    seen.add(canonical)
                    try:
                        record = save_collected(root, canonical, content,
                                                not args.file or args.file.suffix.lower() != '.txt', start, end, name_profile=profile)
                        if record['date_status'] == 'eligible':
                            collected.append(record)
                            print(f'SAVED {record["judgment_date"]} {record["collection_record_path"]}', flush=True)
                        elif record['date_status'] == 'date_unknown':
                            raise ValueError('無法核實裁判日期；JSON 已保留，不送 AI')
                        else:
                            report.errors.append(f'{canonical}: 官網篩選後仍出現範圍外裁判；JSON 已保留，不送 AI')
                    except Exception as error:
                        failures.append(dict(source_url=canonical, error=str(error)))
                        print(f'FAILED {canonical}: {error}', flush=True)
            except Exception as error:
                report.errors.append(str(error))
                print(f'SEARCH FAILED {query}: {error}', flush=True)
            searches.append(report.to_dict())
            if args.file:
                searches[-1]['mode'] = 'local_file'
                break
    except (ValueError, OSError) as error:
        failures.append(dict(source_url=str(args.process_collected), error=str(error)))
    # Save the collection snapshot before any API invocation.
    collection_manifest = dict(schema_version='judgment-collection-v1', company=args.company, aliases=args.alias,
                               company_name_profile=profile,
                               date_window=window, collected_document_count=len(collected), searches=searches,
                               failures=failures, documents=[dict(document_id=r['document_id'],
                                   path=r['collection_record_path'], judgment_date=r['judgment_date']) for r in collected])
    (root / 'collection_manifest.json').write_text(json.dumps(collection_manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'COLLECTION SAVED {len(collected)} documents; starting next phase', flush=True)
    if args.collect_only:
        records = collected
    elif collected:
        try:
            extractor = create_extractor('gemini')
        except (ValueError, KeyError) as error:
            export(root, args.company, args.alias, collected, searches, failures,
                   date_window=window, collection_count=len(collected))
            parser.error(f'{error}；全文 JSON 已存於 {root / "collected_documents"}')
        processor = DocumentProcessor(root, extractor, quote_repair_attempts=args.quote_repair_attempts)
        try:
            for item in collected:
                try:
                    record = processor.process(item['source_url'], item['text'], False)
                    record['company_name_profile'] = profile
                    (root / 'documents' / (record['document_id']+'.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
                    records.append(record)
                except Exception as error:
                    failures.append(dict(source_url=item['source_url'], error=str(error)))
                    print(f'FAILED {item["source_url"]}: {error}', flush=True)
        finally:
            processor.db.close()
    summary = export(root, args.company, args.alias, records, searches, failures,
                     date_window=window, collection_count=len(collected))
    print(f'OUTPUT {root.resolve()}', flush=True)
    extracted_count = sum(bool(r.get('extraction')) for r in records)
    print(f'DONE collected={len(collected)} extracted={extracted_count} failed={len(failures)} search_completed={summary["search_completed"]}')
    analysis_failed = False
    if args.analyze:
        from .risk_analysis import analyze_records
        output, analysis = analyze_records(records, args.company, args.alias, root, coverage=summary)
        print(f"AI {analysis['status'].upper()} {output.resolve()}", flush=True)
        analysis_failed = bool(analysis['summary']['failed'])
        for entry in analysis['records']:
            if entry['error']:
                print(f"{entry['document_id']}: {entry['error']}", file=sys.stderr)
    if failures or analysis_failed or any(s['errors'] for s in searches):
        raise SystemExit(1)
