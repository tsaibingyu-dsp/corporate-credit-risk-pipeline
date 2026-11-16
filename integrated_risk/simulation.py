"""Deterministic, explicitly provisional decision simulation; no model calls."""
import re
from decimal import Decimal,ROUND_DOWN,ROUND_HALF_UP,localcontext

NOTICE='授信決策模擬，非正式核准或實際利率報價'
CONFIRMED_STATES={'deducted','duplicate_not_deducted'}


def evidence_basis(analyses,events,company,statuses=None):
    """Accept verified events and completed scoped zero-result searches, never missing inputs."""
    valid=[]
    priced_ids={e['source_record_id'] for e in events if e['status'] in CONFIRMED_STATES}
    for record in analyses:
        verification=record['verification']
        checks=verification['checks']
        passed={c['name'] for c in checks if c['status']=='pass'}
        from verification.engine import digest
        if verification.get('analysis_sha256') and verification['analysis_sha256']!=digest(record['analysis']):
            continue
        if not company['identity_confirmed'] or any(c['status']=='fail' for c in checks):
            continue
        if not {'json_schema','literal_evidence','module_consistency','archive_sha256'}.issubset(passed):
            continue
        a=record['analysis']
        neutral=a['risk_assessment']['is_negative'] is False
        m=record['module']
        if neutral:
            if m=='judicial':
                neutral=a.get('is_target_company') is True and a.get('company_role') not in ('unknown','mentioned_only')
            elif m in ('sfb','environment'):
                neutral=a.get('company_match')=='confirmed'
            elif m=='news':
                neutral=(a.get('is_relevant') is True and a.get('company_match')=='confirmed'
                         and a.get('target_company_role')=='direct_party'
                         and record['source_record'].get('content_scope')=='full_article')
            elif m=='mops':
                neutral=a.get('company_id')==company['company_code']
        if neutral or record['id'] in priced_ids:
            valid.append(dict(analysis_id=record['id'],module=m,
                basis='verified_non_negative_record' if neutral else 'verified_and_scored_event',
                source_file=record['source_path'],source_fingerprint=verification.get('source_fingerprint')))
    if company['identity_confirmed']:
        for module,status in (statuses or {}).items():
            query=status.get('query_assessment',{})
            if (module in ('sfb','environment') and status.get('status')=='no_records'
                    and not status.get('errors') and query.get('status')=='no_records_confirmed'
                    and query.get('scoring_eligible') is True and query.get('source_sha256')):
                valid.append(dict(analysis_id='query:'+module,module=module,basis='completed_search_no_records',
                    source_file=query['source_file'],source_fingerprint=query['source_sha256']))
    return dict(valid_evidence_count=len(valid),completed_zero_deduction_count=sum(r['basis']=='completed_search_no_records' for r in valid),verified_non_negative_count=sum(r['basis']=='verified_non_negative_record' for r in valid),
        scored_evidence_count=sum(r['basis']=='verified_and_scored_event' for r in valid),valid_records=valid,
        score_valid=bool(valid),reason=None if valid else '沒有已驗證可評分事件或已驗證的目標企業非負面資料，不把缺資料當成 100 分低風險')


def has_ambiguous_context(text):
    # This guard only prevents hard adverse overrides. It never proves an event.
    return bool(re.search(r'否認.{0,15}(?:違約|破產|舞弊)|(?:駁回|撤銷).{0,15}(?:破產|重整)|'
                          r'(?:尚未|未曾|未經|並未).{0,10}(?:違約|破產|重整|舞弊)|'
                          r'(?:涉嫌|指控|傳聞).{0,12}(?:舞弊|詐欺|侵占|背信)',text))


def simulate(company,request_amount,score,statuses,events,policy,*,basis,underwriting=None):
    request_amount=Decimal(str(request_amount))
    if not request_amount.is_finite() or request_amount<=0:
        raise ValueError('模擬申請額度須為有限正數')
    config=policy.simulation
    reviews=[]
    for item in (underwriting or {}).get('review_items',[]):
        if item.get('code')=='simulation_only':
            continue
        reviews.append(dict(item,reason='保守審查資料限制（不阻擋模擬）：'+item['reason'],
                            blocks_underwriting=item.get('blocks_decision',False),blocks_decision=False))
    coverage={m:dict(status=s['status'],input_records=s.get('input_records',0),ai_success=s.get('success',0),
        ai_failed=s.get('failed',0),validation_failed=s.get('validation_failed',0),coverage_confirmed=s['coverage_confirmed'],
        date_scope=s.get('date_scope',{}),scope=s.get('coverage_scope'),
        query_assessment=s.get('query_assessment')) for m,s in statuses.items()}
    complete=set(statuses)=={'judicial','sfb','environment','news','mops'} and all(s['status'] in ('success','no_records') and s['coverage_confirmed'] for s in statuses.values())
    pending=[e['event_id'] for e in events if e['status'] in ('pending_review','rejected_proposal','scoring_failed','duplicate_pending_review')]
    complete=complete and not pending
    result=dict(mode='credit_decision_simulation',notice=NOTICE,provisional=True,is_final_credit_decision=False,
        assessment_status='無法評估',risk_score=None,observed_risk_level='待確認',risk_level='待確認',
        scoring_complete=complete,credit_decision=None,recommended_approved_amount=None,
        recommended_interest_rate_percent=None,recommended_amount_ratio=None,decision_rule=None,
        review_required=bool(reviews),review_items=reviews,conditions=[NOTICE],data_coverage=coverage,evidence_basis=basis,
        pending_event_ids=pending,calculation=dict(observed_score=score if type(score) is int else None),overrides=[])
    basis_count=basis.get('valid_evidence_count')
    consistent_basis=(type(basis_count) is int and basis_count>0 and isinstance(basis.get('valid_records'),list)
                      and len(basis['valid_records'])==basis_count)
    if basis.get('score_valid') is not True or not consistent_basis or type(score) is not int or not 0<=score<=100:
        reviews.append(dict(code='no_effective_evidence',reason=basis.get('reason') or '分數無法有效計算',
            blocks_decision=True,event_id=None))
        result['review_required']=True
        return result
    if not complete:
        reviews.append(dict(code='provisional_coverage',reason='資料涵蓋或評分尚未完整；以下僅依有效觀測分數模擬',blocks_decision=False,event_id=None))
    band=next(b for b in config.bands if b.minimum<=score<=b.maximum)
    with localcontext() as ctx:
        ctx.prec=40
        t=Decimal(score-band.minimum)/Decimal(band.maximum-band.minimum) if band.maximum!=band.minimum else Decimal(0)
        D=lambda n:Decimal(str(n))
        ratio=D(band.ratio_min)+t*(D(band.ratio_max)-D(band.ratio_min))
        rate=D(band.rate_max)-t*(D(band.rate_max)-D(band.rate_min)) if band.rate_max is not None else None
        base_ratio,base_rate=ratio,rate
        decision=band.decision
        level=band.risk_level
        priorities={'准核':0,'條件式准核':1,'婉拒':2}
        rank={'低':1,'中':2,'高':3}
        triggered=[]
        pending_critical=[]
        for event in events:
            for indicator in event['assessment']['risk_indicators']:
                if indicator['status']!='有證據支持':
                    if indicator['status']=='資料不足' and indicator['evidence_quotes'] and any(
                        re.search(p,q) for q in indicator['evidence_quotes'] for rule in config.overrides if rule.code==indicator['code'] for p in rule.evidence_patterns):
                        pending_critical.append(dict(code=indicator['code'],event_id=event['event_id'],
                            reason='重大事件線索資料不足，先採限制性模擬並人工覆核，不作已確認違約或破產認定'))
                    continue
                critical=(indicator['code'] in policy.mandatory_review_codes or
                          any(r.code==indicator['code'] and rank[indicator['risk_level']]>=rank[r.minimum_level] for r in config.overrides) or
                          indicator['code']=='F07' and indicator['risk_level']==policy.major_governance_level)
                if not critical:
                    continue
                quotes=indicator['evidence_quotes']
                verified=(event['status'] in CONFIRMED_STATES and bool(quotes)
                          and not any(c['status']=='fail' for c in event['verification']['checks']))
                matched=False
                for rule in config.overrides:
                    if rule.code!=indicator['code'] or rank[indicator['risk_level']]<rank[rule.minimum_level]:
                        continue
                    evidence='\n'.join(quotes)
                    if verified and not has_ambiguous_context(evidence) and any(re.search(p,evidence) for p in rule.evidence_patterns):
                        matched=True
                        entry=dict(rule=rule.name,code=rule.code,event_id=event['event_id'],
                            decision=rule.decision,max_amount_ratio=rule.max_amount_ratio,
                            minimum_rate_percent=rule.minimum_rate_percent,evidence_quotes=quotes)
                        triggered.append(entry)
                if not matched:
                    pending_critical.append(dict(code=indicator['code'],event_id=event['event_id'],
                        reason='重大風險線索尚未達可確認覆寫條件；不得僅依總分准核'))
        for override in triggered:
            if priorities[override['decision']]>priorities[decision]:
                decision=override['decision']
            ratio=min(ratio,D(override['max_amount_ratio']))
            if override['minimum_rate_percent'] is not None and rate is not None:
                rate=max(rate,D(override['minimum_rate_percent']))
            level='高'
            reviews.append(dict(code='simulation_major_override',reason=override['rule']+'：模擬決策已覆寫，強制人工覆核',
                blocks_decision=False,event_id=override['event_id']))
        if pending_critical:
            if decision=='准核':
                decision='條件式准核'
            ratio=min(ratio,D(config.pending_critical_max_ratio))
            if rate is not None:
                rate=max(rate,D(config.pending_critical_rate_floor))
            for signal in pending_critical:
                reviews.append(dict(code='simulation_critical_pending',reason=signal['reason'],blocks_decision=False,event_id=signal['event_id']))
            if not triggered:
                level='待確認'
        amount_before_cap=request_amount*ratio
        approved=min(request_amount,amount_before_cap,D(config.maximum_amount)).quantize(Decimal('0.01'),rounding=ROUND_DOWN)
        if decision=='婉拒' or approved==0:
            decision='婉拒'
            approved=Decimal('0.00')
            ratio=Decimal(0)
            rate=None
        else:
            rate=rate.quantize(Decimal(1).scaleb(-config.rate_decimal_places),rounding=ROUND_HALF_UP)
        result.update(assessment_status='可模擬',risk_score=score,observed_risk_level=band.risk_level,risk_level=level,
            credit_decision=decision,recommended_approved_amount=str(approved),
            recommended_interest_rate_percent=float(rate) if rate is not None else None,recommended_amount_ratio=str(ratio),
            overrides=triggered,pending_critical_signals=pending_critical,
            decision_rule=dict(policy_version=policy.version,score_band=band.model_dump()),
            calculation=dict(score=score,t=str(t),t_formula='(score-minimum)/(maximum-minimum)',
                ratio_formula='ratio_min+t*(ratio_max-ratio_min)',rate_formula='rate_max-t*(rate_max-rate_min)',
                base_ratio=str(base_ratio),base_rate_percent=str(base_rate) if base_rate is not None else None,
                ratio_after_overrides=str(ratio),amount_before_cap=str(amount_before_cap),maximum_amount=config.maximum_amount,
                amount_formula='floor_to_cents(min(requested_amount,requested_amount*ratio_after_overrides,maximum_amount))',
                actual_amount_ratio=str(approved/request_amount),final_amount=str(approved),
                rate_rounding='ROUND_HALF_UP',amount_rounding='ROUND_DOWN',
                final_rate_percent=str(rate) if rate is not None else None))
        if amount_before_cap>D(config.maximum_amount):
            result['conditions'].append('依設定最高一億元上限調降額度，實際核准比例可能低於原區間比例')
        if reviews or pending_critical or triggered:
            result['conditions'].append('所有覆核事項完成前，不得將此模擬視為正式核准或承諾')
    result['review_required']=bool(reviews)
    return result
