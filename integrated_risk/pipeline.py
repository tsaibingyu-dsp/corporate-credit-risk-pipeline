from datetime import datetime,timezone,timedelta
from decimal import Decimal,InvalidOperation
import hashlib
from pathlib import Path

from .config import load_policy,load_scoring_table,fingerprint
from .inputs import read,resolve_company,load_sources,restrict_to_window
from .modules import run_modules
from .scoring import score_analyses,apply_deductions
from .decisions import decide
from .simulation import simulate,evidence_basis,NOTICE
from .reporting import save_reports


def run(code,amount,*,data_root='data',output=None,policy_path=None,inputs_path=None,
        financials_path=None,as_of=None,extractors=None,scorer=None,progress=print,saved_analyses_only=False):
    as_of=as_of or datetime.now(timezone(timedelta(hours=8))).date()
    try:
        amount=Decimal(str(amount))
    except InvalidOperation as exc:
        raise ValueError('申請額度不是有效金額') from exc
    if not amount.is_finite() or not 0<amount<=Decimal('1000000000000000000') or amount.quantize(Decimal('0.01'))!=amount:
        raise ValueError('申請額度須為正數金額，最多兩位小數且不超過 10^18 元')
    output=Path(output or Path(data_root)/'integrated'/code).resolve()
    table=load_scoring_table()
    policy=load_policy(policy_path)
    manifest=read(inputs_path) if inputs_path else None
    if inputs_path and not isinstance(manifest,dict):
        raise ValueError('來源路徑清單必須為 JSON 物件')
    company=resolve_company(code,data_root,manifest,as_of)
    sources=load_sources(company,data_root,manifest=manifest,
        manifest_parent=Path(inputs_path).resolve().parent if inputs_path else None,exclude=output)
    restrict_to_window(sources,data_root,as_of)
    if saved_analyses_only:
        from .saved_analyses import load_saved_analyses
        analyses,statuses=load_saved_analyses(sources,company,output,data_root,as_of,progress=progress)
    else:
        analyses,statuses=run_modules(sources,company,output,data_root,as_of,extractors=extractors,progress=progress)
    events=score_analyses(analyses,company,table,output,scorer=scorer,progress=progress)
    score,total,groups=apply_deductions(events,policy,as_of=as_of)
    financials=None
    financial_error=None
    if financials_path:
        try:
            financials=read(financials_path)
        except (ValueError,OSError) as exc:
            financial_error=str(exc)
    underwriting=decide(company,amount,'TWD',score,statuses,events,policy,financial_data=financials,as_of=as_of)
    basis=evidence_basis(analyses,events,company,statuses)
    if not basis['score_valid']:
        underwriting.update(credit_decision=None,recommended_approved_amount=None,recommended_interest_rate_percent=None)
    recommendation=simulate(company,amount,score,statuses,events,policy,basis=basis,underwriting=underwriting)
    categories=[]
    for code_key,rule in table['rules'].items():
        applied=[e for e in events if e['status']=='deducted' and e['risk_category']==rule['category']]
        related=[e for e in events if e['risk_category']==rule['category'] or any(
            i['code']==code_key and i['status'] in ('有證據支持','資料不足') for i in e['assessment']['risk_indicators'])]
        pending=[e for e in related if e['status'] in ('pending_review','rejected_proposal','scoring_failed','duplicate_pending_review')]
        reasons=[e['score_reason'] for e in applied]
        reasons.extend(reason for e in pending for reason in e['review_reasons'])
        categories.append(dict(code=code_key,category=rule['category'],applied_deduction=sum(e['applied_deduction'] for e in applied),
            event_ids=[e['event_id'] for e in applied],pending_event_ids=[e['event_id'] for e in pending],
            reasons=list(dict.fromkeys(reasons)) or ['本次沒有已驗證可採用的扣分；不代表企業完全沒有此風險'],
            minimum_deduction=rule['minimum_deduction'],maximum_deduction=rule['maximum_deduction'],severity_criteria=rule['severity_criteria']))
    report=dict(schema_version='integrated-credit-risk-v2-simulation',created_at=datetime.now(timezone.utc).isoformat(),as_of=as_of.isoformat(),
        mode='credit_decision_simulation',simulation_notice=NOTICE,provisional=True,
        analysis_input_mode='saved_successful_only' if saved_analyses_only else 'existing_ai_modules',
        company={k:v for k,v in company.items() if k!='profile'},requested_credit_amount=str(amount),currency='TWD',
        risk_score=recommendation['risk_score'],risk_level=recommendation['risk_level'],credit_recommendation=recommendation,
        underwriting_assessment=underwriting,
        score_calculation=dict(base_score=100,observed_score=score,score_valid=basis['score_valid'],
            evidence_basis=basis,uncapped_event_deductions=total,effective_deduction=100-score,minimum_score=0),
        scoring_table=table,policy=policy.model_dump(),policy_sha256=fingerprint(policy.model_dump()),
        module_statuses=statuses,score_items=dict(judicial=['judicial'],regulatory=['sfb','environment'],news=['news'],mops=['mops']),
        category_deductions=categories,events=events,event_groups=groups,
        source_assessments=[s['query_assessment'] for s in statuses.values() if s.get('query_assessment')],
        risk_factors=[dict(event_id=e['event_id'],**f) for e in events for f in e['risk_factors']],
        module_analyses=[{k:r[k] for k in ('id','module','analysis','source_path','analysis_path','verification','scoring_eligible','analysis_round')} for r in analyses],
        financial_input=dict(path=str(Path(financials_path).resolve()) if financials_path else None,error=financial_error,
            sha256=hashlib.sha256(Path(financials_path).read_bytes()).hexdigest() if financials_path and Path(financials_path).is_file() else None),
        limitations=['只使用已保存的來源，不執行爬蟲；缺資料或來源不完整不表示無風險。',
            NOTICE,
            'AI 只提出事件分析與扣分；Python 驗證、去重及依政策計算模擬，不由 AI 決定准駁、額度或利率。',
            '本機來源與逐字引文不能保證解讀、事件現況或企業整體信用已被核實。',
            '新聞依既有規則先分析關鍵字命中，再抽查未命中者 20%；未抽樣者不是已確認無風險。',
            '預設政策為可調整的研究範例；模擬利率不使用實際財務與定價資料假裝成市場報價。'])
    save_reports(output,report)
    return report
