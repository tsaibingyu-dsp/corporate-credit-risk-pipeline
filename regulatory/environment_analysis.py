import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from judicial.company import normalize_name
from judicial.extractor import GeminiExtractor
from .common import record_text
from .csv_source import COLUMNS, DOWNLOAD_COLUMNS, parse_record_date
from .environment_schema import EnvironmentAnalysis, EnvironmentDecision, ENVIRONMENT_SYSTEM_PROMPT, PROMPT_VERSION
from credit_risk.assessment import load_taxonomy, indicator_candidates, validate_assessment, severity_negative
from company_names import analysis_profile, identity_names, model_profile


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def optional(value):
    return value if value.strip() else None


def fine_value(raw):
    text, currency = raw.strip(), None
    for code, labels in {'TWD': ['新臺幣', '新台幣', 'NT$', 'TWD'], 'USD': ['美元', '美金', 'US$', 'USD'],
                         'JPY': ['日圓', 'JPY'], 'CNY': ['人民幣', 'CNY'], 'HKD': ['港幣', 'HKD']}.items():
        for label in labels:
            if text.startswith(label):
                currency, text = code, text[len(label):].strip()
                break
    text = re.sub(r'(元整|元)$', '', text).strip()
    if not re.fullmatch(r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?', text):
        return None, currency
    number = Decimal(text.replace(',', ''))
    return (int(number) if number == number.to_integral_value() else float(number)), currency


def make_input(record, company, aliases):
    row = record.get('raw_record')
    if (record.get('source') != 'prtr' or record.get('full_text_kind') != 'provided_penalty_csv_record'
            or not isinstance(row, dict) or not set(COLUMNS).issubset(row)
            or not all(isinstance(value, str) for value in row.values())):
        raise ValueError('需要環境部 CSV 原始結構化紀錄；不接受 PRTR 網頁或證期局公告')
    text = record_text(row)
    if (record.get('full_text') != text
            or record.get('full_text_sha256') != hashlib.sha256(text.encode()).hexdigest()):
        raise ValueError('CSV 原始欄位與保存正文／雜湊不一致')
    locator = record.get('record_locator', {})
    csv_row = locator.get('csv_row')
    if type(csv_row) is not int or csv_row < 2:
        raise ValueError('缺少可追溯的 CSV 資料列位置')
    try:
        day = parse_record_date(row['date'])
    except ValueError:
        day = None
    amount, currency = fine_value(row['fine'])
    native=set(DOWNLOAD_COLUMNS).issubset(row)
    payment={'無分期已繳清':'paid','已繳清':'paid','未繳清':'unpaid','尚未繳納':'unpaid','分期繳納中':'installments'}.get(row.get('罰鍰是否繳清','').strip(),'unknown') if native else 'unknown'
    exact = normalize_name(row['name']) == normalize_name(company)
    profile = analysis_profile(company, aliases, supplied=record.get('company_name_profile'))
    verified_match = normalize_name(row['name']) in {normalize_name(n) for n in identity_names(profile)}
    derived = dict(source_type='environment_penalty', source_id=f'environment_{csv_row - 1}', company_name=company,
                   event_date=day, source_url=record.get('provenance', {}).get('original_download_url'),
                   financial_impact=dict(amount=amount, currency=currency, type='administrative_fine', payment_status=payment),
                   source_details=dict(case=optional(row['case']), appeal=optional(row['appeal']),
                       appeal_result=optional(row['result']), restricted_date_raw=optional(row['restricted_date']),
                       improvement_status_raw=optional(row['improve']),
                       download_details={k:row[k] for k in DOWNLOAD_COLUMNS} if native else None))
    taxonomy=load_taxonomy()
    return dict(company_name=company, aliases=aliases, raw_record=row, derived=derived,
                company_name_profile=model_profile(profile), candidate_aliases=profile['candidate_aliases'],
                credit_risk_taxonomy=taxonomy, risk_indicator_candidates=indicator_candidates(list(row.values()),taxonomy),
                name_exact_match=exact, name_verified_match=verified_match, name_only_not_uniform_number_verification=True)


def validate_analysis(result, payload):
    derived, row = payload['derived'], payload['raw_record']
    for key in ('source_type', 'source_id', 'company_name', 'event_date', 'source_url'):
        if getattr(result, key) != derived[key]:
            raise ValueError(f'AI 變更來源欄位 {key}')
    if result.source_details.model_dump() != derived['source_details']:
        raise ValueError('AI 變更原始案件、訴願、結果或改善／日期欄位')
    if result.financial_impact.model_dump() != derived['financial_impact']:
        raise ValueError('AI 變更原始 fine、幣別或付款狀態，不能補造金額或已付款事實')
    if result.company_match == 'confirmed' and not payload['name_verified_match']:
        raise ValueError('名稱相似、別名或關係企業不能直接確認為目標企業')
    if result.company_match == 'unrelated' and payload['name_verified_match']:
        raise ValueError('名稱完全吻合卻判定無關；如身分不明應標 possible')
    if result.company_match == 'possible' and result.severity != '待確認':
        raise ValueError('企業身分待確認時不得直接評估目標企業負面風險')
    if result.company_match == 'unrelated' and (result.severity != '無明確負面風險' or result.risk_category):
        raise ValueError('不得將第三方裁罰歸責目標企業')
    for quote in result.evidence_text:
        if not quote.strip() or not any(quote in value for value in row.values()):
            raise ValueError('證據不是單一 CSV 原始欄位中的連續原文')
    narrative_evidence = [q for q in result.evidence_text if any(q in row[k] for k in ('case', 'fact', 'low'))]
    if result.severity in ('高', '中', '低') and (not narrative_evidence or not result.risk_category):
        raise ValueError('風險程度缺少違規事實／法規證據或風險分類，不能只依罰鍰金額判定')
    evidence = '\n'.join(result.evidence_text)
    if 'credit' in result.risk_category and not re.search(r'償債|清償|現金流|無力償還|無法償還|違約|履約能力', evidence):
        raise ValueError('credit 分類缺少償債、現金流或履約能力受影響的具體證據')
    if 'operational' in result.risk_category and not re.search(r'停工|停業|停用|停產|營運|產能|停止操作|停止作業|停止運作|停止營業|限制生產', evidence):
        raise ValueError('營運風險缺少具體停工、營運或產能影響證據，不能由 improve=否推定停工')
    if row['appeal'].strip() == '是' and not row['result'].strip() and result.event_status == 'resolved':
        raise ValueError('有訴願紀錄但結果缺漏，不可推定事件已全部解決')
    if result.event_status == 'resolved' and row['improve'].strip() != '是' and not row['result'].strip():
        raise ValueError('缺少改善或救濟結果，不能推定事件已解決')
    notes = []
    if derived['financial_impact']['currency'] is None:
        notes.append('CSV 未明定罰鍰幣別／單位；保留 fine 原始數值，不預設新臺幣或換算單位')
    if derived['financial_impact']['amount'] is None:
        notes.append('fine 缺漏或不是可確定轉換的單一數值；未估計罰鍰、改善成本或其他損失')
    if derived['event_date'] is None:
        notes.append('CSV date 缺漏或格式／日期無法確定，未以其他日期欄位代填')
    if row['restricted_date'].strip():
        notes.append('restricted_date 僅保留原值，未核實欄位定義，不解釋為改善期限、停工日期或確定日期')
    if row['improve'].strip() == '是':
        notes.append('已改善僅依 CSV 記載，無法確認所有法律責任均已終結或企業目前狀況')
    if derived['financial_impact']['payment_status']=='unknown':
        notes.append('CSV 未提供可確認的罰鍰付款狀態，也不等於企業總損失')
    if row['appeal'].strip() == '是' and not row['result'].strip():
        notes.append('CSV 有訴願紀錄但缺少結果，不推定已撤銷或維持原裁罰')
    if not payload['name_verified_match']:
        notes.append('CSV 名稱與目標公司全名未完全吻合，不能僅憑相似或別名確認法人身分')
    for note in notes:
        if note not in result.limitations:
            result.limitations.append(note)
    critical_missing = (derived['financial_impact']['amount'] is None or derived['financial_impact']['currency'] is None
                        or derived['event_date'] is None or row['improve'].strip() not in ('是', '否')
                        or row['appeal'].strip() not in ('是', '否')
                        or row['appeal'].strip() == '是' and not row['result'].strip())
    if (result.company_match != 'confirmed' or result.severity in ('高', '待確認')
            or result.event_status == 'unknown' or {'operational', 'credit'} & set(result.risk_category)
            or critical_missing or not result.event_summary or not result.risk_reasoning or not narrative_evidence):
        result.review_required = True
    validate_assessment(result.risk_assessment, list(row.values()), payload['credit_risk_taxonomy'],
                        is_negative=severity_negative(result.severity), severity=result.severity)
    review = result.review_required or result.risk_assessment.review_required
    result.review_required = result.risk_assessment.review_required = review


def analyze_records(records, company, aliases, root, *, extractor=None, coverage=None, progress=print):
    root = Path(root)
    output, manifest_path = root / 'environment_analysis_results.json', root / 'environment_analysis_manifest.json'
    manifest = dict(schema_version=PROMPT_VERSION, prompt_version=PROMPT_VERSION, score_item=2,
                    company_name=company, aliases=aliases, status='running', started_at=datetime.now(timezone.utc).isoformat(),
                    source_search_completed=(coverage or {}).get('search_completed', False),
                    summary=dict(success=0, failed=0), records=[])
    results, seen, initialization_error = [], set(), None
    def checkpoint():
        save(output, results)
        save(manifest_path, manifest)
    checkpoint()
    for record in records:
        key = (record.get('provenance', {}).get('file_sha256'), record.get('record_locator', {}).get('csv_row'),
               record.get('full_text_sha256'))
        if key in seen:
            continue
        seen.add(key)
        entry = dict(document_id=record.get('document_id'), record_locator=record.get('record_locator'),
                     raw_record=record.get('raw_record'), provenance=record.get('provenance'), status='pending', error=None)
        manifest['records'].append(entry)
        try:
            payload = make_input(record, company, aliases)
            if initialization_error:
                raise ValueError(initialization_error)
            if extractor is None:
                try:
                    extractor = GeminiExtractor(schema=EnvironmentDecision, system=ENVIRONMENT_SYSTEM_PROMPT)
                except Exception as error:
                    initialization_error = str(error)
                    raise
            manifest.update(provider=extractor.provider, model=extractor.model)
            identity = [payload, record.get('provenance', {}).get('file_sha256'), PROMPT_VERSION,
                        ENVIRONMENT_SYSTEM_PROMPT, EnvironmentDecision.model_json_schema(), EnvironmentAnalysis.model_json_schema(), extractor.provider, extractor.model]
            digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            cache = root / 'environment_analysis_cache' / (digest + '.json')
            cached = False
            if cache.exists():
                try:
                    analysis = EnvironmentAnalysis.model_validate_json(cache.read_text(encoding='utf-8'))
                    validate_analysis(analysis, payload)
                    cached = True
                except ValueError:
                    cache.unlink()
            if not cached:
                decision = EnvironmentDecision.model_validate(extractor.extract(json.dumps(payload, ensure_ascii=False)))
                analysis = EnvironmentAnalysis.model_validate({**payload['derived'], **decision.model_dump()})
                validate_analysis(analysis, payload)
                save(cache, analysis.model_dump())
            entry.update(status='success', source_id=analysis.source_id, cached=cached, result_index=len(results), cache_key=digest)
            results.append(analysis.model_dump())
            manifest['summary']['success'] += 1
        except Exception as error:
            entry.update(status='failed', error=str(error), requires_human_review=True)
            manifest['summary']['failed'] += 1
        progress(f"ENVIRONMENT AI {entry['document_id']} {entry['status']}")
        checkpoint()
    manifest['status'] = 'partial' if manifest['summary']['failed'] or not manifest['source_search_completed'] else 'complete'
    if not manifest['records']:
        manifest.update(status='not_started', reason='沒有可分析的環境部 CSV 紀錄，未呼叫 AI')
    manifest['finished_at'] = datetime.now(timezone.utc).isoformat()
    checkpoint()
    return output, manifest


def analyze_file(path, company, aliases, root, **kwargs):
    source = Path(path)
    raw = source.read_bytes()
    document = json.loads(raw.decode('utf-8-sig'))
    if document.get('module_id') != 'regulatory_announcements' or not isinstance(document.get('documents'), list):
        raise ValueError('請提供 03_regulatory.py 產生的 regulatory.json')
    records = [r for r in document['documents'] if r.get('source') == 'prtr']
    output, manifest = analyze_records(records, company, aliases, root, coverage=document.get('summary'), **kwargs)
    manifest.update(source_file=str(source.resolve()), source_sha256=hashlib.sha256(raw).hexdigest())
    save(Path(root) / 'environment_analysis_manifest.json', manifest)
    return output, manifest
