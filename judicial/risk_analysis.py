import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

from .company import normalize_name
from .extractor import GeminiExtractor, validate_evidence
from .risk_money import supported_amounts
from .risk_schema import JudgmentRisk, JUDGMENT_SYSTEM_PROMPT, PROMPT_VERSION
from .schema import Judgment
from credit_risk.assessment import load_taxonomy, indicator_candidates, validate_assessment, severity_negative
from company_names import analysis_profile, identity_names, model_profile


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def explicit_finality(extraction):
    fact = extraction['finality']
    if fact['status'] != 'stated':
        return 'unknown'
    quotes = '\n'.join(fact['quotes'])
    negative = bool(re.search(r'尚未確定|未確定|尚未定讞|未定讞', quotes))
    positive = bool(re.search(r'已確定|業已確定|已告確定|確定在案|已定讞|業已定讞', quotes))
    return 'unknown' if negative == positive else 'not_final' if negative else 'final'


def make_input(record, company, aliases):
    text = record.get('text')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('缺少裁判正文，不能分析清單或特徵匯出檔')
    if record.get('text_sha256') and hashlib.sha256(text.encode()).hexdigest() != record['text_sha256']:
        raise ValueError('裁判正文雜湊與來源紀錄不符')
    if not record.get('extraction'):
        raise ValueError('缺少第一階段欄位萃取，請先執行 02_company.py，勿加 --collect-only')
    extracted = Judgment.model_validate(record['extraction'])
    validate_evidence(extracted, text)
    extraction = extracted.model_dump()
    for amount in extraction['amounts']:
        if not amount['amount_original'].strip() or amount['amount_original'] not in '\n'.join(amount['quotes']):
            raise ValueError('前階段金額原文與引句不符，請檢查萃取結果')
    profile = analysis_profile(company, aliases, supplied=record.get('company_name_profile'))
    known_names = [*identity_names(profile), *aliases]
    names = {normalize_name(n) for n in known_names}
    matched = [p for p in extraction['parties']
               if p['name']['status'] == 'stated' and p['name']['value']
               and not p['anonymized'] and p['entity_type'] == 'organization'
               and normalize_name(p['name']['value']) in names]
    taxonomy=load_taxonomy()
    return dict(company_name=company, aliases=aliases, judgment_full_text=text, extraction=extraction,
                company_name_profile=model_profile(profile), candidate_aliases=profile['candidate_aliases'],
                credit_risk_taxonomy=taxonomy, risk_indicator_candidates=indicator_candidates([text],taxonomy),
                matched_parties=matched, supported_finality=explicit_finality(extraction),
                supported_amounts=supported_amounts(extraction, company, [n for n in known_names if n!=company]),
                identity_verified=False)


def allowed_roles(parties):
    allowed = set()
    for party in parties:
        for role in party['roles']:
            found = False
            if '被上訴人' in role:
                allowed.add('appellee')
                role = role.replace('被上訴人', '')
                found = True
            for label, code in [('上訴人', 'appellant'), ('原告', 'plaintiff'), ('被告', 'defendant'), ('第三人', 'third_party')]:
                if label in role:
                    allowed.add(code)
                    found = True
            if not found:
                allowed.add('other')
    return allowed


def validate_analysis(result, payload):
    if result.company_name != payload['company_name']:
        raise ValueError('AI 回傳的目標公司與輸入不符')
    if not result.evidence_text.strip() or result.evidence_text not in payload['judgment_full_text']:
        raise ValueError('分析引句不是裁判正文的連續原文')
    extraction, matched = payload['extraction'], payload['matched_parties']
    for field in ('court', 'case_number', 'judgment_date'):
        fact = extraction[field]
        expected = fact['value'] if fact['status'] == 'stated' else None
        if getattr(result, field) != expected:
            raise ValueError(f'{field} 與前階段已核對的裁判資訊不符')
    if result.judgment_date is not None:
        date.fromisoformat(result.judgment_date)
    category = extraction['case_category']
    if category['status'] == 'stated' and category['value'] in ('民事', '刑事', '行政'):
        choices = {'民事': {'civil', 'enforcement'}, '刑事': {'criminal'}, '行政': {'administrative'}}
        if result.case_type not in choices[category['value']] | {None}:
            raise ValueError('案件類型與原始裁判分類不符')
    if result.is_target_company is True:
        if not matched:
            raise ValueError('未有目標企業當事人名稱證據，不能認定為當事人')
        if result.company_role not in allowed_roles(matched) | {'unknown'}:
            raise ValueError('企業訴訟角色與當事人萃取不符')
        if not any(q in result.evidence_text for p in matched for q in p['name']['quotes'] if q.strip()):
            raise ValueError('分析引文缺少目標當事人的原文支持')
    elif result.is_target_company is False:
        if matched:
            raise ValueError('已匹配目標當事人，不可直接標為非當事人；有疑義請用 null')
        if (result.company_role not in ('mentioned_only', 'unknown') or result.severity != '無明確負面風險'
                or result.risk_category or result.judgment_result != 'unknown'
                or result.claim_amount is not None or result.awarded_amount is not None):
            raise ValueError('非目標當事人，不得將他人責任歸屬目標公司')
    elif result.company_role != 'unknown' or result.severity != '待確認':
        raise ValueError('目標身分待確認時，角色及風險須標示未知')
    if result.is_final != payload['supported_finality']:
        raise ValueError('案件確定狀態未有明示原文支持')
    for field in ('claim_amount', 'awarded_amount'):
        amount = getattr(result, field)
        if amount is not None:
            supported = payload['supported_amounts']
            if (result.is_target_company is not True or amount != supported[field]
                    or result.currency != supported['currency']):
                raise ValueError(f'{field} 缺乏目標公司、金額性質或幣別的原文支持')
            if not any(q in result.evidence_text for q in supported['quotes'][field] if q.strip()):
                raise ValueError(f'{field} 的原文引句未包含於分析證據')
    if result.currency is not None and result.currency != payload['supported_amounts']['currency']:
        raise ValueError('分析幣別與原始金額證據不符')
    if result.court_findings is not None and not any(
            f['basis'] == 'court_finding' and any(q in result.evidence_text for q in f['quotes'] if q.strip())
            for f in extraction['findings']):
        raise ValueError('缺乏法院認定事實的原文引句，不可將當事人主張視為法院認定')
    if result.judgment_result in ('adverse', 'favorable', 'mixed', 'procedural'):
        disposition = extraction['disposition']
        if disposition['status'] != 'stated' or not any(q in result.evidence_text for q in disposition['quotes'] if q.strip()):
            raise ValueError('裁判結果必須有主文原文支持')
    if result.severity in ('高', '中', '低') and (result.is_target_company is not True or not result.risk_category):
        raise ValueError('負面風險程度缺乏目標當事人或風險類別')
    if result.severity == '高' and result.judgment_result not in ('adverse', 'mixed'):
        raise ValueError('高風險須有對目標企業不利的裁判結果支持')
    if (not payload['identity_verified'] or payload['supported_amounts']['ambiguous']
            or result.is_final == 'unknown' or result.severity == '待確認'
            or result.company_role == 'unknown' or result.judgment_result == 'unknown'
            or any(v is None for v in result.model_dump().values())):
        result.review_required = True
    validate_assessment(result.risk_assessment, [payload['judgment_full_text']], payload['credit_risk_taxonomy'],
                        is_negative=severity_negative(result.severity), severity=result.severity)
    review = result.review_required or result.risk_assessment.review_required
    result.review_required = result.risk_assessment.review_required = review


def analyze_records(records, company, aliases, root, *, extractor=None, coverage=None, progress=print):
    root = Path(root)
    output, manifest_path = root / 'judgment_analysis_results.json', root / 'judgment_analysis_manifest.json'
    manifest = dict(schema_version=PROMPT_VERSION, prompt_version=PROMPT_VERSION,
                    company_name=company, aliases=aliases, started_at=datetime.now(timezone.utc).isoformat(),
                    status='running', search_completed=(coverage or {}).get('search_completed', False),
                    identity_verified=False, score_ready=False,
                    summary=dict(success=0, failed=0), records=[])
    results, initialization_error = [], None
    def checkpoint():
        save(output, results)
        save(manifest_path, manifest)
    checkpoint()
    for record in records:
        source_hash = hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        entry = dict(document_id=record.get('document_id'), source_url=record.get('source_url'),
                     source_file=record.get('_source_file'), source_record_sha256=source_hash,
                     status='pending', error=None)
        manifest['records'].append(entry)
        try:
            payload = make_input(record, company, aliases)
            if initialization_error:
                raise ValueError(initialization_error)
            if extractor is None:
                try:
                    extractor = GeminiExtractor(schema=JudgmentRisk, system=JUDGMENT_SYSTEM_PROMPT)
                except Exception as exc:
                    initialization_error = str(exc)
                    raise
            manifest.update(provider=extractor.provider, model=extractor.model)
            identity = [payload, PROMPT_VERSION, JUDGMENT_SYSTEM_PROMPT, JudgmentRisk.model_json_schema(), extractor.provider, extractor.model]
            key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            cache = root / 'judgment_analysis_cache' / (key + '.json')
            cached = False
            if cache.exists():
                try:
                    result = JudgmentRisk.model_validate_json(cache.read_text(encoding='utf-8'))
                    validate_analysis(result, payload)
                    cached = True
                except ValueError:
                    cache.unlink()
            if not cached:
                result = JudgmentRisk.model_validate(extractor.extract(json.dumps(payload, ensure_ascii=False)))
                validate_analysis(result, payload)
                save(cache, result.model_dump())
            entry.update(status='success', cached=cached, cache_key=key, result_index=len(results))
            results.append(result.model_dump())
            manifest['summary']['success'] += 1
        except Exception as exc:
            entry.update(status='failed', error=str(exc))
            manifest['summary']['failed'] += 1
        progress(f"JUDGMENT AI {entry['document_id'] or source_hash[:12]} {entry['status']}")
        checkpoint()
    manifest['status'] = 'partial' if manifest['summary']['failed'] or not manifest['search_completed'] else 'complete'
    if not manifest['records']:
        manifest.update(status='not_started', reason='沒有成功萃取且可供風險分析的裁判；未呼叫風險分析模型',
                        upstream_failed_document_count=(coverage or {}).get('failed_document_count', 0))
    manifest['finished_at'] = datetime.now(timezone.utc).isoformat()
    checkpoint()
    return output, manifest


def load_records(path):
    path = Path(path)
    files = sorted(path.glob('*.json')) if path.is_dir() else [path]
    if not files:
        raise ValueError('找不到裁判 JSON；請指向 documents 資料夾或單筆裁判 JSON')
    selected = {}
    for file in files:
        record = json.loads(file.read_text(encoding='utf-8-sig'))
        if not isinstance(record, dict) or not isinstance(record.get('text'), str):
            raise ValueError(f'{file.name} 不是含 text 正文的單筆裁判 JSON；請勿指定摘要或特徵檔')
        identity = (record.get('source_url'), hashlib.sha256(record['text'].encode()).hexdigest())
        record['_source_file'] = str(file.resolve())
        rank = (bool(record.get('extraction')), file.stat().st_mtime)
        if identity not in selected or rank > selected[identity][0]:
            selected[identity] = (rank, record)
    return [value[1] for value in selected.values()]
