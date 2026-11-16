import copy
import csv
import hashlib
import io
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from judicial.company import normalize_name
from judicial.extractor import GeminiExtractor
from judicial.quote_repair import resolve_quote
from judicial.risk_money import source_number, CURRENCIES
from .common import record_text
from .sfb_schema import (SfbAnalysis, SfbQuoteRepairBatch, SFB_SYSTEM_PROMPT,
                         SFB_QUOTE_REPAIR_PROMPT, PROMPT_VERSION)
from credit_risk.assessment import load_taxonomy, indicator_candidates, validate_assessment, severity_negative
from company_names import analysis_profile, identity_names, model_profile


class SfbGeminiExtractor(GeminiExtractor):
    def __init__(self, client=None):
        super().__init__(client=client, schema=SfbAnalysis, system=SFB_SYSTEM_PROMPT)

    def repair_sfb_evidence(self, payload):
        return self._generate(json.dumps(payload, ensure_ascii=False), SfbQuoteRepairBatch,
                              SFB_QUOTE_REPAIR_PROMPT, max_attempts=1)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def validate_sfb_evidence(result, raw_record):
    source_texts = [value for value in raw_record.values() if isinstance(value, str) and value.strip()]
    evidence_list = result.get('evidence_text', [])
    if not isinstance(evidence_list, list):
        raise ValueError('evidence_text 必須是陣列')
    if result.get('risk_category') and not evidence_list:
        raise ValueError('風險判斷缺少證據')
    issues = []
    for index, quote in enumerate(evidence_list):
        if not isinstance(quote, str) or not quote.strip():
            issues.append((index, quote, '空白或無效引文'))
        elif not any(quote in source for source in source_texts):
            issues.append((index, quote, '引文不在原始資料中'))
    return issues


def original_fields(record):
    raw = record.get('raw_record')
    if isinstance(raw, dict):
        return copy.deepcopy(raw)
    text = record.get('full_text')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('缺少原始公告全文或結構化 raw_record')
    # Older snapshots combined HTML and PDF. Restore separate literal source fields.
    chunks = text.split('\n\n[附件全文]\n')
    title = record.get('title') or ''
    body = chunks[0][len(title)+1:] if title and chunks[0].startswith(title + '\n') else chunks[0]
    return dict(title=title, announcement_text=body,
                **{f'attachment_text_{i}': value for i, value in enumerate(chunks[1:], 1)})


def make_input(record, company, aliases):
    if record.get('source') != 'sfb':
        raise ValueError('不是證期局原始資料')
    text = record.get('full_text')
    if record.get('full_text_sha256') and (not isinstance(text, str)
            or hashlib.sha256(text.encode()).hexdigest() != record['full_text_sha256']):
        raise ValueError('證期局保存全文的雜湊不符')
    raw = original_fields(record)
    if record.get('full_text_kind') == 'announcement_html' and isinstance(record.get('raw_record'), dict):
        if any(isinstance(v, str) and v.strip() and v not in text for v in raw.values()):
            raise ValueError('原始公告欄位與保存全文不一致，不接受新增的證據文字')
    if not any(isinstance(value, str) and value.strip() for value in raw.values()):
        raise ValueError('沒有可讀原始文字，不能以清單或缺漏資料分析')
    identity = record.get('document_id') or hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    profile = analysis_profile(company, aliases, supplied=record.get('company_name_profile'))
    taxonomy=load_taxonomy()
    return dict(raw_record=raw, aliases=aliases, metadata=dict(source_id='sfb_' + identity,
                company_name=company, source_url=record.get('source_url')),
                company_name_profile=model_profile(profile), candidate_aliases=profile['candidate_aliases'],
                recognized_company_names=identity_names(profile),
                credit_risk_taxonomy=taxonomy, risk_indicator_candidates=indicator_candidates(list(raw.values()),taxonomy),
                source_incomplete=record.get('full_text_status') == 'partial',
                source_limitations=record.get('collection_limitations', []))


def known_dates(raw):
    found = set()
    for value in raw.values():
        if not isinstance(value, str):
            continue
        for match in re.finditer(r'(?<!\d)(\d{2,4})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})(?:\s*日)?(?!\d)', value):
            year, month, day = map(int, match.groups())
            try:
                found.add(date(year if year >= 1912 else year+1911, month, day).isoformat())
            except ValueError:
                pass
        if re.fullmatch(r'\d{7}', value.strip()):
            compact = value.strip()
            try:
                found.add(date(int(compact[:3])+1911, int(compact[3:5]), int(compact[5:])).isoformat())
            except ValueError:
                pass
    return found


def validate_analysis(result, payload, *, check_quotes=True):
    metadata, raw = payload['metadata'], payload['raw_record']
    for key in ('source_id', 'company_name', 'source_url'):
        if getattr(result, key) != metadata[key]:
            raise ValueError(f'AI 變更來源資料 {key}')
    source_texts = [v for v in raw.values() if isinstance(v, str)]
    text = '\n'.join(source_texts)
    details = result.source_details
    for value in (result.event_date, details.publication_date):
        if value is not None and value not in known_dates(raw):
            raise ValueError('分析日期未有原始日期資料支持')
    if details.document_number is not None and not any(details.document_number in s for s in source_texts):
        raise ValueError('文號未出現在原始資料中')
    if details.penalized_party is not None and not any(normalize_name(details.penalized_party) in normalize_name(s) for s in source_texts):
        raise ValueError('受處分對象名稱未有原文支持')
    if result.company_match == 'confirmed':
        if (details.party_type != 'target_company' or details.relationship != 'direct'
                or not details.penalized_party
                or normalize_name(details.penalized_party) not in {normalize_name(n) for n in payload['recognized_company_names']}):
            raise ValueError('目標公司本身受罰的確認與實際受處分對象不一致')
    elif details.party_type == 'target_company' and details.relationship == 'direct' and result.company_match == 'not_matched':
        raise ValueError('公司確認狀態與直接受罰對象矛盾')
    if result.company_match == 'possible' and result.severity != '待確認':
        raise ValueError('受處分身分不明時須標待確認')
    if result.severity in ('高', '中', '低') and not result.risk_category:
        raise ValueError('風險嚴重程度缺少風險分類與證據')
    if details.relationship == 'unrelated' and (result.severity != '無明確負面風險' or result.risk_category):
        raise ValueError('不能把明確無關第三方事件歸責目標公司')
    if details.party_type in ('related_company', 'natural_person', 'accountant', 'third_party') and result.company_match == 'confirmed':
        raise ValueError('第三方受罰不能視為目標公司本身受罰')
    financial = result.financial_impact
    if financial.amount is not None:
        if financial.type == 'administrative_fine' and details.party_type != 'target_company':
            raise ValueError('不能把董事、會計師、子公司或第三方罰鍰列為目標公司直接罰鍰')
        if financial.amount_original:
            if not any(financial.amount_original in value for value in source_texts):
                raise ValueError('財務金額原文不在輸入資料中')
            number = source_number(financial.amount_original)
            if number is None or number != Decimal(str(financial.amount)):
                raise ValueError('財務金額與原文明示金額不符，不得推估或加總')
            supported_currencies = {code for code, labels in CURRENCIES.items() if any(label in financial.amount_original for label in labels)}
            if financial.currency is not None and financial.currency not in supported_currencies:
                raise ValueError('金額幣別缺乏明示原文')
        else:
            supported = [v for key, v in raw.items() if key.lower() in ('fine', 'amount', 'penalty_amount')
                         and type(v) in (int, float) and Decimal(str(v)) == Decimal(str(financial.amount))]
            if not supported or financial.currency != raw.get('currency'):
                raise ValueError('財務數值及幣別缺少原始結構化欄位支持')
    elif financial.currency is not None or financial.amount_original is not None:
        raise ValueError('沒有可確認金額時不得補入幣別或金額原文')
    if not check_quotes:
        return
    issues = validate_sfb_evidence(result.model_dump(), raw)
    if issues:
        raise ValueError('引文未通過逐字驗證：' + str(issues))
    evidence = '\n'.join(result.evidence_text)
    if financial.amount_original and not any(financial.amount_original in quote for quote in result.evidence_text):
        raise ValueError('財務影響缺少對應金額原文引句')
    if financial.amount is not None:
        if financial.type == 'financial_loss' and not re.search(r'損失|虧損', evidence):
            raise ValueError('已確認財務損失缺少原文支持，不能把罰鍰或財報違規當作損失')
        if financial.type == 'compensation' and not re.search(r'賠償|補償|求償', evidence):
            raise ValueError('賠償金額缺少對應原文支持')
        if financial.type == 'administrative_fine' and not re.search(r'罰鍰|罰款|裁罰|處罰', evidence) and 'fine' not in raw:
            raise ValueError('罰鍰金額缺少裁罰性質的原文支持')
    if 'credit' in result.risk_category and not re.search(r'償債|清償|債務支付|履約|違約|無法償還', evidence):
        raise ValueError('credit 風險缺少償債或履約影響證據')
    if 'liquidity' in result.risk_category and not re.search(r'現金流|流動性|資金調度|到期支付|資金短缺|支付義務', evidence):
        raise ValueError('liquidity 風險缺少資金或支付影響證據')
    if result.event_status == 'resolved':
        completed = [m for m in re.finditer(r'已改善|改善完成|已完成|已處理|處理完成|已撤銷|撤銷原處分', evidence)
                     if not re.search(r'(?:未|不|無法|尚無).{0,4}$', evidence[max(0, m.start()-6):m.start()])]
        if not completed:
            raise ValueError('沒有明示問題已完成處理，不能把改善要求視為已解決')
    validate_assessment(result.risk_assessment, source_texts, payload['credit_risk_taxonomy'],
                        is_negative=severity_negative(result.severity), severity=result.severity)


def apply_review_policy(result, payload):
    notes = []
    if payload['source_incomplete']:
        notes.append('來源有附件下載或解析缺漏，僅分析已取得內容，不代表完整裁罰資料')
    if result.financial_impact.amount is not None:
        notes.append('罰鍰或明載金額不代表已繳納、企業總損失或信用違約')
    if result.event_status == 'confirmed':
        notes.append('confirmed 僅表示已作成裁罰，不表示法律程序已確定或後續問題已解決')
    for note in [*payload['source_limitations'], *notes]:
        if note not in result.limitations:
            result.limitations.append(note)
    if (result.company_match != 'confirmed' or result.source_details.relationship != 'direct'
            or result.source_details.party_type != 'target_company' or payload['source_incomplete']
            or result.severity in ('高', '待確認') or result.event_status in ('disputed', 'unknown')
            or {'financial', 'governance', 'credit', 'liquidity'} & set(result.risk_category)
            or result.event_date is None or result.source_details.document_number is None
            or not result.event_summary or not result.risk_reasoning):
        result.review_required = True
    review = result.review_required or result.risk_assessment.review_required
    result.review_required = result.risk_assessment.review_required = review


def locate_quote(quote, raw):
    for key, value in raw.items():
        if isinstance(value, str) and quote and quote.strip() and quote in value:
            start = value.index(quote)
            return value[start:start+len(quote)], dict(field=key, start=start, end=start+len(quote))
    matches = []
    for key, value in raw.items():
        if not isinstance(value, str):
            continue
        try:
            found, start, end = resolve_quote(quote, value)
            matches.append((found, dict(field=key, start=start, end=end)))
        except ValueError:
            pass
    if len(matches) != 1:
        raise ValueError('找不到可唯一對應的原始欄位連續引文；不能刪括號、改寫或拼接')
    return matches[0]


class SfbEvidenceFailure(ValueError):
    def __init__(self, error, audit):
        super().__init__('證期局證據驗證／修復失敗，需人工覆核：' + str(error))
        self.audit = audit


def repair_evidence(result, payload, extractor, *, max_rounds=2):
    if max_rounds not in (0, 1, 2):
        raise ValueError('引文修復上限只能為 0、1、2')
    raw = payload['raw_record']
    frozen, current = result.model_dump(), result.model_copy(deep=True)
    audit = dict(version='sfb-quote-repair-v1', max_rounds=max_rounds, original_analysis=frozen,
                 format_repairs=[], rounds=[], status='validating', review_required=True)
    last_error = None
    for number in range(max_rounds + 1):
        # Only deterministic whitespace restoration is permitted without AI.
        for index, quote in enumerate(current.evidence_text):
            if any(isinstance(value, str) and quote and quote in value for value in raw.values()):
                continue
            try:
                restored, location = locate_quote(quote, raw)
                current.evidence_text[index] = restored
                audit['format_repairs'].append(dict(index=index, original_quote=quote, source_quote=restored, **location))
            except ValueError:
                pass
        try:
            validate_analysis(current, payload)
            if {k: v for k, v in current.model_dump().items() if k != 'evidence_text'} != {k: v for k, v in frozen.items() if k != 'evidence_text'}:
                raise ValueError('引文修復改動了其他分析欄位')
            audit['status'] = 'success'
            return current, audit
        except ValueError as error:
            last_error = last_error or str(error)
        if number == max_rounds:
            break
        if current.risk_category and not current.evidence_text:
            issues = [(-1, None, '風險判斷缺少證據')]
        else:
            issues = validate_sfb_evidence(current.model_dump(), raw)
        if not issues or not callable(getattr(extractor, 'repair_sfb_evidence', None)):
            break
        attempt = dict(round=number+1, issues=issues, response=None, status='pending', error=None)
        audit['rounds'].append(attempt)
        print(f'SFB QUOTE REPAIR {number+1}/{max_rounds} issues={len(issues)}', flush=True)
        try:
            response = SfbQuoteRepairBatch.model_validate(extractor.repair_sfb_evidence(dict(
                raw_record=raw, analysis=current.model_dump(), issues=issues, feedback=last_error)))
            attempt['response'] = response.model_dump()
            indices = [p.index for p in response.replacements]
            if len(indices) != len(set(indices)) or set(indices) != {i[0] for i in issues}:
                raise ValueError('修復索引重複、缺漏或包含未授權修改')
            candidate = current.model_copy(deep=True)
            accepted = []
            for patch in response.replacements:
                if patch.support != 'supported':
                    raise ValueError('原文不支持既有分析，不能用無關文字替換')
                quote, location = locate_quote(patch.quote, raw)
                if patch.index == -1:
                    candidate.evidence_text.append(quote)
                else:
                    candidate.evidence_text[patch.index] = quote
                accepted.append(dict(index=patch.index, source_quote=quote, **location))
            if {k: v for k, v in candidate.model_dump().items() if k != 'evidence_text'} != {k: v for k, v in frozen.items() if k != 'evidence_text'}:
                raise ValueError('只允許修改 evidence_text')
            validate_analysis(candidate, payload)
            current = candidate
            attempt.update(status='validated', accepted=accepted)
        except Exception as error:
            last_error = str(error)
            attempt.update(status='failed', error=last_error)
            if attempt['response'] is None or any(p['support'] != 'supported' for p in attempt['response']['replacements']):
                break
    audit['status'] = 'manual_review'
    raise SfbEvidenceFailure(last_error, audit)


def analyze_records(records, company, aliases, root, *, extractor=None, coverage=None, quote_repair_attempts=2, progress=print):
    if quote_repair_attempts not in (0, 1, 2):
        raise ValueError('引文修復上限只能為 0、1、2')
    root = Path(root)
    output, manifest_path = root / 'sfb_analysis_results.json', root / 'sfb_analysis_manifest.json'
    manifest = dict(schema_version=PROMPT_VERSION, prompt_version=PROMPT_VERSION, score_item=2,
                    company_name=company, aliases=aliases, status='running', started_at=datetime.now(timezone.utc).isoformat(),
                    source_search_completed=(coverage or {}).get('search_completed', False), summary=dict(success=0, failed=0), records=[])
    results, seen, initialization_error = [], set(), None
    def checkpoint():
        save(output, results)
        save(manifest_path, manifest)
    checkpoint()
    for record in records:
        identity = hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if identity in seen:
            continue
        seen.add(identity)
        entry = dict(document_id=record.get('document_id'), source_url=record.get('source_url'),
                     source_record_sha256=identity, raw_record=None, source_incomplete=record.get('full_text_status') == 'partial',
                     status='pending', error=None)
        manifest['records'].append(entry)
        try:
            payload = make_input(record, company, aliases)
            entry['raw_record'] = payload['raw_record']
            if initialization_error:
                raise ValueError(initialization_error)
            if extractor is None:
                try:
                    extractor = SfbGeminiExtractor()
                except Exception as error:
                    initialization_error = str(error)
                    raise
            manifest.update(provider=extractor.provider, model=extractor.model)
            key = hashlib.sha256(json.dumps([payload, PROMPT_VERSION, SFB_SYSTEM_PROMPT,
                SFB_QUOTE_REPAIR_PROMPT, SfbAnalysis.model_json_schema(), extractor.provider, extractor.model],
                ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            cache = root / 'sfb_analysis_cache' / (key+'.json')
            audit_path = root / 'sfb_quote_repair_logs' / (key+'.json')
            cached = False
            if cache.exists():
                try:
                    result = SfbAnalysis.model_validate_json(cache.read_text(encoding='utf-8'))
                    validate_analysis(result, payload)
                    cached = True
                except ValueError:
                    cache.unlink()
            if not cached:
                result = SfbAnalysis.model_validate(extractor.extract(json.dumps(payload, ensure_ascii=False)))
                entry['unvalidated_model_analysis'] = result.model_dump()
                validate_analysis(result, payload, check_quotes=False)
                apply_review_policy(result, payload)
                try:
                    result, audit = repair_evidence(result, payload, extractor, max_rounds=quote_repair_attempts)
                except SfbEvidenceFailure as error:
                    save(audit_path, dict(source_id=payload['metadata']['source_id'], raw_record=payload['raw_record'], **error.audit))
                    entry['quote_repair_log'] = str(audit_path.resolve())
                    raise
                save(audit_path, dict(source_id=payload['metadata']['source_id'], raw_record=payload['raw_record'], **audit))
                save(cache, result.model_dump())
                entry.pop('unvalidated_model_analysis', None)
            entry.update(status='success', cached=cached, result_index=len(results), cache_key=key,
                         quote_repair_log=str(audit_path.resolve()))
            results.append(result.model_dump())
            manifest['summary']['success'] += 1
        except Exception as error:
            entry.update(status='failed', error=str(error), review_required=True)
            if 'unvalidated_model_analysis' in entry:
                rejected = root / 'sfb_rejected_results' / (identity+'.json')
                save(rejected, dict(validated=False, review_required=True, error=str(error),
                                    raw_record=entry['raw_record'], analysis=entry.pop('unvalidated_model_analysis')))
                entry['rejected_result'] = str(rejected.resolve())
            manifest['summary']['failed'] += 1
        progress(f"SFB AI {entry['document_id']} {entry['status']}")
        checkpoint()
    manifest['status'] = ('partial' if manifest['summary']['failed'] or not manifest['source_search_completed']
                          or any(r['source_incomplete'] for r in manifest['records']) else 'complete')
    if not manifest['records']:
        manifest.update(status='not_started', reason='沒有可分析的證期局資料，未呼叫 AI')
    manifest['finished_at'] = datetime.now(timezone.utc).isoformat()
    checkpoint()
    return output, manifest


def load_input(path):
    path = Path(path)
    raw = path.read_bytes()
    if path.suffix.lower() == '.txt':
        text = raw.decode('utf-8-sig')
        items = [dict(announcement_text=text)]
    elif path.suffix.lower() == '.csv':
        for encoding in ('utf-8-sig', 'cp950'):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError('CSV 請使用 UTF-8 或 CP950')
        reader = csv.DictReader(io.StringIO(text, newline=''))
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError('CSV 缺少欄名或欄名重複')
        items = list(reader)
        if any(None in item or None in item.values() for item in items):
            raise ValueError('CSV 欄位數不一致')
    else:
        data = json.loads(raw.decode('utf-8-sig'))
        if isinstance(data, dict) and data.get('module_id') == 'regulatory_announcements':
            if not isinstance(data.get('documents'), list) or any(not isinstance(r, dict) for r in data['documents']):
                raise ValueError('regulatory.json 缺少有效的原始 documents 陣列')
            return data['documents'], data.get('summary', {}), dict(path=str(path.resolve()), sha256=hashlib.sha256(raw).hexdigest())
        items = data if isinstance(data, list) else [data]
    records = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError('輸入必須為原始資料物件或物件陣列')
        if item.get('source_type') in ('sfb_penalty', 'environment_penalty') and 'risk_reasoning' in item:
            raise ValueError('不能將先前 AI 風險分析結果當成原始證據；請提供原始 CSV／JSON 或 regulatory.json')
        if item.get('source') == 'sfb' and ('full_text' in item or 'raw_record' in item):
            records.append(item)
        else:
            text = record_text(item)
            records.append(dict(source='sfb', document_id=hashlib.sha256(raw).hexdigest()+f'_row_{index}',
                raw_record=item, full_text=text, full_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                source_url=item.get('source_url') if isinstance(item.get('source_url'), str) else None,
                full_text_status='provided_record'))
    return records, {}, dict(path=str(path.resolve()), sha256=hashlib.sha256(raw).hexdigest())
