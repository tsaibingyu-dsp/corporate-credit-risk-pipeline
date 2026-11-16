from datetime import date
from decimal import Decimal,ROUND_DOWN
from typing import Literal

from pydantic import Field,StrictBool
from judicial.schema import StrictModel


class Pricing(StrictModel):
    verified: StrictBool
    source: str
    effective_date: str
    reference_rate_percent: Decimal = Field(ge=0,allow_inf_nan=False)
    margin_percent: Decimal = Field(ge=0,allow_inf_nan=False)


class Financials(StrictModel):
    company_code: str
    company_name: str
    currency: str
    as_of_date: str
    verified: StrictBool
    verification_source: str
    annual_operating_cash_flow: Decimal = Field(allow_inf_nan=False)
    debt_service_coverage_ratio: Decimal = Field(ge=0,allow_inf_nan=False)
    equity: Decimal = Field(allow_inf_nan=False)
    repayment_capacity_limit: Decimal = Field(ge=0,allow_inf_nan=False)
    pricing: Pricing | None = None


def review_item(code,reason,*,blocks=False,event_id=None):
    return dict(code=code,reason=reason,blocks_decision=blocks,event_id=event_id)


def collect_review(company,statuses,events,score,policy):
    items=[]
    for reason in company['identity_reasons']:
        items.append(review_item('company_identity',reason,blocks=True))
    for module,status in statuses.items():
        if status['status'] not in ('success','no_records'):
            items.append(review_item('module_incomplete',f'{module} 模組狀態為 {status["status"]}，不將未取得資料視為無風險',blocks=True))
        if not status['coverage_confirmed']:
            items.append(review_item('source_coverage',f'{module} 來源涵蓋未確認完整',blocks=policy.require_complete_sources_for_decision))
    for event in events:
        blocked=event['status'] in ('pending_review','rejected_proposal','scoring_failed','duplicate_pending_review')
        for reason in event['review_reasons']:
            items.append(review_item('event_review',reason,blocks=blocked,event_id=event['event_id']))
        supported={i['code']:i for i in event['assessment']['risk_indicators'] if i['status']=='有證據支持'}
        major=set(policy.mandatory_review_codes)&set(supported)
        if 'F07' in supported and supported['F07']['risk_level']==policy.major_governance_level:
            major.add('F07')
        if major:
            items.append(review_item('major_event','重大違約、破產或治理事件需人工審查：'+','.join(sorted(major)),blocks=True,event_id=event['event_id']))
    if score<policy.manual_review_below_score:
        items.append(review_item('low_score',f'分數 {score} 低於覆核門檻 {policy.manual_review_below_score}'))
    # Preserve different event provenance while avoiding repeated messages for one event.
    unique={ (i['code'],i['reason'],i['event_id']):i for i in items }
    return list(unique.values())


def decide(company,request_amount,currency,score,statuses,events,policy,*,financial_data=None,as_of=None):
    as_of=as_of or date.today()
    reviews=collect_review(company,statuses,events,score,policy)
    observed_level=next(b.risk_level for b in policy.score_bands if score>=b.minimum)
    scoring_complete=not any(i['blocks_decision'] for i in reviews)
    result=dict(risk_score=score,observed_risk_level=observed_level,
        risk_level=observed_level if scoring_complete else '待確認',scoring_complete=scoring_complete,
        credit_decision=None,recommended_approved_amount=None,recommended_interest_rate_percent=None,
        decision_rule=None,conditions=[],review_required=True,review_items=reviews,
        is_final_credit_decision=False)
    finance=None
    pricing_data=None
    try:
        if financial_data is None:
            raise ValueError('未提供已查核財務及償債能力資料，准駁及核准額度保留 null')
        finance_values=dict(financial_data)
        pricing_data=finance_values.pop('pricing',None)
        finance=Financials.model_validate(finance_values)
        if finance.company_code!=company['company_code'] or finance.company_name!=company['company_name']:
            raise ValueError('財務資料公司代號或法人名稱不符')
        age=(as_of-date.fromisoformat(finance.as_of_date)).days
        if not finance.verified or not finance.verification_source.strip() or not 0<=age<=policy.financial_max_age_days:
            raise ValueError('財務資料未查核、缺少來源、過期或日期異常')
        if finance.currency!=currency:
            raise ValueError('財務資料幣別與申請額度不符，不自行換匯')
    except (ValueError,TypeError) as exc:
        reviews.append(review_item('financial_data',str(exc),blocks=True))
        finance=None
    if any(i['blocks_decision'] for i in reviews) or finance is None:
        result['review_required']=True
        return result
    band=next(b for b in policy.decision_bands if score>=b.minimum)
    decision=band.decision
    ratio=Decimal(str(band.amount_ratio))
    financial_gate=(finance.annual_operating_cash_flow<=Decimal(str(policy.minimum_annual_operating_cash_flow)) or
                    finance.equity<=Decimal(str(policy.minimum_equity)) or
                    finance.debt_service_coverage_ratio<Decimal(str(policy.minimum_debt_service_coverage_ratio)))
    if financial_gate:
        if policy.financial_gate_failure_action=='manual_review':
            reviews.append(review_item('financial_gate','未通過設定的現金流、淨值或償債覆蓋率門檻，需人工確認財務口徑',blocks=True))
            return result
        decision='婉拒'
        ratio=Decimal(0)
        result['conditions'].append('未通過設定的正現金流、正淨值或償債覆蓋率門檻，仍須人工確認財務口徑')
        reviews.append(review_item('financial_gate','財務門檻未通過，僅為設定規則產生的初步建議'))
    approved=min(request_amount,finance.repayment_capacity_limit,request_amount*ratio).quantize(Decimal('0.01'),rounding=ROUND_DOWN)
    if approved<=0:
        decision='婉拒'
    elif approved<request_amount and decision=='准核':
        decision='條件式准核'
    result.update(credit_decision=decision,recommended_approved_amount=str(approved),
        decision_rule=dict(policy_version=policy.version,score_band_minimum=band.minimum,
                           amount_ratio=str(ratio),financial_gate_passed=not financial_gate))
    if decision=='條件式准核':
        result['conditions'].append('依償債能力與設定額度比例限制核准額度，並完成所有人工覆核')
    if decision!='婉拒':
        try:
            p=Pricing.model_validate(pricing_data) if pricing_data is not None else None
            if p is None or not p.verified or not p.source.strip():
                raise ValueError('缺少已查核定價資料，建議利率保留 null')
            if not 0<=(as_of-date.fromisoformat(p.effective_date)).days<=policy.pricing_max_age_days:
                raise ValueError('定價資料過期或日期異常，利率保留 null')
            rate=p.reference_rate_percent+p.margin_percent+Decimal(str(policy.rate_addon_percentage_points[observed_level]))
            if rate>Decimal(str(policy.maximum_interest_rate_percent)):
                raise ValueError('計算利率超過設定的檢核上限，不截斷或任意改價')
            result['recommended_interest_rate_percent']=float(rate.quantize(Decimal('0.0001'),rounding=ROUND_DOWN))
        except (ValueError,TypeError) as exc:
            reviews.append(review_item('pricing',str(exc)))
    result['review_required']=bool(reviews)
    return result
