import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from judicial.extractor import GeminiExtractor
from .schema import MopsAnalysis, PROMPT_VERSION, SYSTEM_PROMPT
from credit_risk.assessment import load_taxonomy, indicator_candidates, validate_assessment


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def make_input(document, item):
    text = item.get('full_text')
    if not isinstance(text, str) or not text.strip() or not item.get('content') or item['content'] not in text:
        raise ValueError('缺少公告全文，不接受只有主旨或清單的資料')
    if hashlib.sha256(text.encode('utf-8')).hexdigest() != item.get('full_text_sha256'):
        raise ValueError('公告全文雜湊不符，請確認來源檔案完整性')
    company = document['company']
    if item['company_code'] != company['company_code']:
        raise ValueError('公告公司代號與檔案公司不符')
    taxonomy=load_taxonomy()
    return dict(company_id=company['company_code'], company_name=company['company_name'],
                credit_risk_taxonomy=taxonomy, risk_indicator_candidates=indicator_candidates([text],taxonomy),
                announcement_date=item['announcement_date'], announcement_title=item['title'],
                source_url=item.get('source', {}).get('page_url'), announcement_full_text=text)


def validate_analysis(result, payload):
    for field in ('company_id', 'company_name', 'announcement_date', 'announcement_title', 'source_url'):
        if getattr(result, field) != payload[field]:
            raise ValueError(f'AI 回傳的 {field} 與來源不符')
    text = payload['announcement_full_text']
    levels = {'無明確負面風險': 0, '低': 1, '中': 2, '高': 3}
    for event in result.risk_events:
        if not event.evidence_text.strip() or event.evidence_text not in text:
            raise ValueError('事件引文不在公告全文中')
        impact = event.financial_impact
        if impact:
            if impact.amount is not None:
                if not impact.amount_original or impact.amount_original not in event.evidence_text:
                    raise ValueError('財務金額必須附含幣別與單位的原文證據')
                # Check the original numeric token without multiplying units.
                tokens = re.findall(r'[-+]?\d+(?:,\d{3})*(?:\.\d+)?', impact.amount_original)
                if not any(float(t.replace(',', '')) == impact.amount for t in tokens):
                    raise ValueError('金額數值與原文不符，禁止自行換算或推估')
            if impact.amount_original and impact.amount_original not in event.evidence_text:
                raise ValueError('財務金額原文不在事件引文中')
            if impact.unit is not None and (not impact.unit.strip() or not impact.amount_original or impact.unit not in impact.amount_original):
                raise ValueError('財務單位必須保留金額原文的單位')
            if impact.impact_type == 'claim_amount' and impact.confirmed_loss is True:
                raise ValueError('求償金額不可標為已確認損失')
            if any(getattr(impact, f) is None for f in ('amount', 'currency', 'unit', 'impact_type', 'confirmed_loss')):
                event.review_required = True
        if (event.event_status == 'unknown' or event.severity == '待確認'
                or any(getattr(event, f) is None for f in ('event_description', 'financial_impact', 'operational_impact', 'company_response'))):
            event.review_required = True
        if event.severity in levels and result.overall_severity in levels:
            if levels[event.severity] > levels[result.overall_severity]:
                raise ValueError('整體嚴重程度低於事件嚴重程度')
    if result.overall_severity in levels and result.risk_events:
        known = [levels[e.severity] for e in result.risk_events if e.severity in levels]
        if known and levels[result.overall_severity] != max(known):
            raise ValueError('整體嚴重程度與事件最高嚴重程度不一致')
    if result.is_negative is True and (not result.risk_events or result.overall_severity == '無明確負面風險'):
        raise ValueError('負面判斷缺少事件或風險程度')
    if result.is_negative is False and result.overall_severity != '無明確負面風險':
        raise ValueError('無負面事件與整體嚴重程度矛盾')
    if result.is_negative is None and result.overall_severity != '待確認':
        raise ValueError('負面性待確認時，嚴重程度應為待確認')
    if result.overall_severity in ('高', '中', '低') and result.is_negative is not True:
        raise ValueError('負面嚴重程度必須有負面判斷')
    if result.overall_severity == '待確認' or result.analysis_confidence in (None, 'low'):
        if not result.risk_events:
            raise ValueError('資訊不足時須有事件說明缺口與人工覆核旗標')
        for event in result.risk_events:
            event.review_required = True
    validate_assessment(result.risk_assessment, [payload['announcement_full_text']], payload['credit_risk_taxonomy'],
                        is_negative=result.is_negative, severity=result.overall_severity)
    if any(event.review_required for event in result.risk_events):
        result.risk_assessment.review_required = True


def analyze_file(source, *, extractor=None, progress=print):
    source = Path(source)
    raw = source.read_bytes()
    document = json.loads(raw.decode('utf-8-sig'))
    if document.get('module_id') != 'mops_material_announcements' or not isinstance(document.get('announcements'), list):
        raise ValueError('請提供 05_mops.py 產生的 mops.json')
    root = source.parent
    output = root / 'analysis_results.json'
    manifest_path = root / 'analysis_manifest.json'
    manifest = dict(schema_version=PROMPT_VERSION, prompt_version=PROMPT_VERSION,
                    source_file=str(source.resolve()), source_sha256=hashlib.sha256(raw).hexdigest(),
                    collection_status=document.get('status'), started_at=datetime.now(timezone.utc).isoformat(),
                    annual_collection_complete=document.get('summary', {}).get('annual_collection_complete', False),
                    source_scope_complete=document.get('summary',{}).get('date_window_collection_complete',document.get('summary',{}).get('annual_collection_complete',False)),
                    date_window=document.get('date_window'),
                    status='running', records=[], summary=dict(success=0, failed=0, skipped=0))
    results = []
    initialization_error = None
    def checkpoint():
        write_json(output, results)
        write_json(manifest_path, manifest)
    checkpoint()
    for item in document['announcements']:
        record = dict(announcement_id=item.get('announcement_id'), status='pending', error=None)
        manifest['records'].append(record)
        if item.get('status') != 'success':
            record['status'] = 'skipped_no_full_text'
            manifest['summary']['skipped'] += 1
            checkpoint()
            continue
        try:
            payload = make_input(document, item)
            if initialization_error:
                raise ValueError(initialization_error)
            if extractor is None:
                try:
                    extractor = GeminiExtractor(schema=MopsAnalysis, system=SYSTEM_PROMPT)
                except Exception as exc:
                    initialization_error = str(exc)
                    raise
            manifest.update(provider=extractor.provider, model=extractor.model)
            key = digest([payload, PROMPT_VERSION, SYSTEM_PROMPT, MopsAnalysis.model_json_schema(), extractor.provider, extractor.model])
            cache = root / 'analysis_cache' / (key + '.json')
            cached = False
            if cache.exists():
                try:
                    analysis = MopsAnalysis.model_validate_json(cache.read_text(encoding='utf-8'))
                    validate_analysis(analysis, payload)
                    cached = True
                except ValueError:
                    cache.unlink()
            if not cached:
                analysis = MopsAnalysis.model_validate(extractor.extract(json.dumps(payload, ensure_ascii=False)))
                validate_analysis(analysis, payload)
                write_json(cache, analysis.model_dump())
            record.update(status='success', cached=cached, cache_key=key, result_index=len(results))
            results.append(analysis.model_dump())
            manifest['summary']['success'] += 1
        except Exception as exc:
            record.update(status='failed', error=str(exc))
            manifest['summary']['failed'] += 1
        progress(f"AI {item.get('announcement_date')} #{item.get('serial_number')} {record['status']}")
        checkpoint()
    summary = manifest['summary']
    manifest['status'] = ('partial' if summary['failed'] or summary['skipped']
                          or not manifest['source_scope_complete'] else 'complete')
    manifest['finished_at'] = datetime.now(timezone.utc).isoformat()
    checkpoint()
    return output, manifest


def scoped_file(source,start,end):
    """Filter a saved annual snapshot without modifying its original bytes."""
    from credit_risk.date_scope import filter_records
    source=Path(source).resolve()
    raw=source.read_bytes()
    document=json.loads(raw.decode('utf-8-sig'))
    if not isinstance(document,dict) or document.get('module_id')!='mops_material_announcements' or not isinstance(document.get('announcements'),list):
        raise ValueError('請提供既有的 MOPS 全文 mops.json，不接受摘要或其他 JSON')
    selected,scope=filter_records(document['announcements'],'mops',start,end)
    document['announcements']=selected
    document['date_window']=scope
    document['source_snapshot']=dict(path=str(source),sha256=hashlib.sha256(raw).hexdigest())
    summary=document.setdefault('summary',{})
    summary['date_window_collection_complete']=bool(summary.get('listing_complete',summary.get('annual_collection_complete',False))) and all(r.get('status')=='success' for r in selected) and not scope['unknown_date_count']
    target=source.parent/('window_'+start.isoformat()+'_'+end.isoformat())/'mops.json'
    write_json(target,document)
    return target
