import json
import re
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

from company_names.resolver import normalize
from judicial.extractor import GeminiExtractor
from judicial.risk_money import source_number,CURRENCIES
from verification.engine import MONEY,monetary_claims
from .config import fingerprint
from .schema import EventProposal,SCORING_PROMPT


class ScoreExtractor(GeminiExtractor):
    def __init__(self):
        super().__init__(schema=EventProposal,system=SCORING_PROMPT)

    def extract(self,text):
        return self._generate(text,EventProposal,SCORING_PROMPT,max_attempts=1)


def source_texts(record):
    module=record['module']
    raw=record['source_record']
    if module=='judicial':
        return [raw['text']]
    if module in ('sfb','environment'):
        from regulatory.sfb_analysis import original_fields
        return [s for s in original_fields(raw).values() if isinstance(s,str) and s.strip()]
    if module=='news':
        return [raw.get('title','')+'\n'+raw.get('text','')]
    return [raw['full_text']]


def direct_attribution(record,company):
    a=record['analysis']
    if not company['identity_confirmed']:
        return False,'公司代號與法人身分尚待確認'
    m=record['module']
    if m=='judicial':
        valid=a['is_target_company'] is True and a['company_role'] not in ('unknown','mentioned_only','third_party')
    elif m=='sfb':
        d=a['source_details']
        valid=a['company_match']=='confirmed' and d['party_type']=='target_company' and d['relationship']=='direct'
    elif m=='environment':
        valid=a['company_match']=='confirmed' and normalize(record['source_record']['raw_record']['name']) in {normalize(n) for n in company['trusted_names']}
    elif m=='news':
        valid=(a['company_match']=='confirmed' and a['target_company_role']=='direct_party'
               and a['attribution_status']=='confirmed_direct' and a['negative_event_verified'] is True
               and a['event_status']['verification']=='已確認事實' and record['source_record'].get('content_scope')=='full_article'
               and normalize(a.get('event_subject') or '') in {normalize(n) for n in company['trusted_names']})
    else:
        valid=a['company_id']==company['company_code']
    return valid,'已驗證目標公司直接角色' if valid else '公司歸責不明、僅間接受影響、第三方或事件尚未確認'


def event_units(analysis):
    a=analysis['analysis']
    events=a.get('risk_events') if analysis['module']=='mops' else None
    if not events:
        return [dict(index=0,event_description=a.get('case_summary') or a.get('event_summary') or a.get('announcement_title'),
                     risk_level=a['risk_assessment']['risk_level'],event_status=a.get('event_status'))]
    return [dict(index=i,event_description=e.get('event_description'),risk_level=e['severity'],
                 event_status=e['event_status'],unit_evidence=e['evidence_text']) for i,e in enumerate(events)]


def base_event(record,unit,company):
    a=record['analysis']
    raw=record['source_record']
    url=a.get('source_url') or raw.get('source_url') or raw.get('url')
    if record['module']=='mops':
        url=raw.get('source',{}).get('page_url')
    reference=a.get('case_number') if record['module']=='judicial' else a.get('source_details',{}).get('document_number') if record['module']=='sfb' else None
    if record['module']=='environment':
        reference=raw.get('raw_record',{}).get('裁處書字號') or None
    # Use the announcement ID, not MOPS's common landing-page URL, to distinguish announcements.
    identity=raw.get('announcement_id') or raw.get('article_id') or raw.get('document_id') or url
    assessment=a['risk_assessment']
    source_review=bool(assessment['review_required'] or record['verification'].get('review_required'))
    return dict(event_id=fingerprint([record['id'],unit['index']]),module=record['module'],
        source_record_id=record['id'],event_index=unit['index'],company_code=company['company_code'],
        company_name=company['company_name'],risk_category=assessment['risk_category'],event_content=unit['event_description'],
        risk_level=unit['risk_level'],proposed_deduction=None,applied_deduction=0,score_reason=assessment['risk_reason'],
        risk_factors=assessment['risk_factor_evidence'],evidence_quotes=assessment['evidence_quotes'],source=dict(url=url,file=record['source_path'],analysis_file=record['analysis_path']),
        status='pending',review_required=source_review,review_reasons=['原分析要求人工覆核'] if source_review else [],validated_reference=reference,
        court=a.get('court'),
        primary_reference=reference,source_identity=fingerprint([identity,unit['index']]),
        text_signature=fingerprint([normalize(s) for s in source_texts(record)]),
        source_event_status=unit.get('event_status'),event_date=None,verification=record['verification'],
        publication_date=raw.get('published_at') if record['module']=='news' else None,
        assessment=record['analysis']['risk_assessment'],proposal=None)


def validate_fact_claims(proposal,sources):
    text='\n'.join([proposal.event_description,proposal.score_reason,*[f.factor for f in proposal.risk_factors]])
    return validate_text_claims(text,sources)


def validate_text_claims(text,sources):
    bad=[]
    amount_sources=[m.group().strip() for s in sources for m in MONEY.finditer(s)]
    def currencies(text):
        return {code for code,labels in CURRENCIES.items() if any(label in text for label in labels)}
    for _,phrase in monetary_claims(dict(event_summary=text)):
        amount=source_number(phrase)
        if amount is None or not any(source_number(q)==amount and currencies(phrase).issubset(currencies(q)) for q in amount_sources):
            bad.append('AI 金額或幣別無法由原文驗證：'+phrase)
    legal=r'[\u4e00-\u9fff]{2,30}(?:法|條例|規則|辦法)(?:（[^）]*）)?第[0-9一二三四五六七八九十百零〇]+條(?:之[0-9一二三四五六七八九十]+)?(?:第[0-9一二三四五六七八九十]+項)?'
    for ref in re.findall(legal,re.sub(r'\s','',text)):
        ref=re.split(r'違反|依據|依照|適用|根據|涉及|認定|法條為',ref)[-1]
        normalized_sources=[re.sub(r'（(?:下稱|以下稱|以下簡稱)[^）]+）','',re.sub(r'\s','',s)) for s in sources]
        if not any(ref in s for s in normalized_sources):
            bad.append('AI 法條無法由原文逐字驗證：'+ref)
    return bad


def analysis_claim_issues(record):
    narratives=[]
    keys={'event_description','case_summary','court_findings','event_summary','reasoning','risk_reasoning',
          'negative_reason','risk_reason','risk_factors','inference_reason','reason','company_response','operational_impact'}
    def walk(value):
        if isinstance(value,dict):
            for key,child in value.items():
                if key in keys and isinstance(child,str):
                    narratives.append(child)
                elif key=='risk_factors' and isinstance(child,list):
                    narratives.extend(v for v in child if isinstance(v,str))
                else:
                    walk(child)
        elif isinstance(value,list):
            for child in value:
                walk(child)
    walk(record['analysis'])
    extraction=record['source_record'].get('extraction')
    for law in (extraction if isinstance(extraction,dict) else {}).get('legal_provisions',[]):
        if isinstance(law,dict) and law.get('status')=='stated' and isinstance(law.get('value'),str):
            narratives.append(law['value'])
    return validate_text_claims('\n'.join(narratives),source_texts(record))


def validate_proposal(proposal,record,unit,company,table):
    issues=[]
    sources=source_texts(record)
    def literal(quote):
        return isinstance(quote,str) and quote.strip() and any(quote in s for s in sources)
    for q in [*proposal.evidence_quotes,*proposal.identity_evidence_quotes,*[q for f in proposal.risk_factors for q in f.evidence_quotes]]:
        if not literal(q):
            issues.append('原文證據不存在或跨欄位拼接')
    if not proposal.event_description.strip() or not proposal.score_reason.strip():
        issues.append('缺少事件內容或評分原因')
    issues.extend(validate_fact_claims(proposal,sources))
    if proposal.source_reference and not literal(proposal.source_reference):
        issues.append('案號／處分文號不是原文')
    expected_reference=(record['analysis'].get('case_number') if record['module']=='judicial' else
                        record['analysis'].get('source_details',{}).get('document_number') if record['module']=='sfb' else None)
    if proposal.source_reference and expected_reference and normalize(expected_reference) not in normalize(proposal.source_reference):
        issues.append('案號／處分文號與指定事件不符，不能拿引用的其他案件作為本案')
    if proposal.event_date:
        try:
            date.fromisoformat(proposal.event_date)
            from regulatory.sfb_analysis import known_dates
            if proposal.event_date not in known_dates({str(i):s for i,s in enumerate(sources)}):
                issues.append('事件日期無法由原文驗證')
        except ValueError:
            issues.append('事件日期格式無效')
    code=proposal.category_code
    supported={i['code']:i for i in record['analysis']['risk_assessment']['risk_indicators'] if i['status']=='有證據支持'}
    if proposal.proposed_deduction is not None:
        valid,reason=direct_attribution(record,company)
        if not valid or proposal.attribution!='direct':
            issues.append(reason)
        if code not in supported:
            issues.append('建議扣分類別沒有經驗證的十類指標支持')
        else:
            if proposal.risk_level!=supported[code]['risk_level'] or proposal.risk_level not in ('高','中','低'):
                issues.append('建議扣分程度與已驗證指標不一致')
        if code in table['rules']:
            bounds=table['rules'][code]
            if not bounds['minimum_deduction']<=proposal.proposed_deduction<=bounds['maximum_deduction']:
                issues.append('AI 建議扣分超出 CSV 區間；不截斷、不直接扣分')
        else:
            issues.append('風險類別不在評分 CSV')
        if not proposal.evidence_quotes or not proposal.identity_evidence_quotes or not proposal.risk_factors:
            issues.append('扣分缺少事件證據、公司角色證據或風險判斷因子')
        for factor in proposal.risk_factors:
            if not factor.factor.strip() or not factor.evidence_quotes or any(q not in proposal.evidence_quotes for q in factor.evidence_quotes):
                issues.append('風險判斷因子缺少對應原文')
        names=company['trusted_names']+(['本公司'] if record['module']=='mops' else [])
        if not any(len(q)>min(len(n) for n in names)+3 and any(normalize(n) in normalize(q) for n in names) for q in proposal.identity_evidence_quotes):
            issues.append('公司角色引文不足，不能僅以名稱出現確認歸責')
        if unit.get('unit_evidence') and not any(q in unit['unit_evidence'] or unit['unit_evidence'] in q for q in proposal.evidence_quotes):
            issues.append('扣分引文未支持指定的 MOPS 事件')
        if record['module']=='mops':
            quotes='\n'.join(proposal.identity_evidence_quotes)
            context=unit.get('unit_evidence') or '\n'.join(sources)
            related=re.search(r'子公司|孫公司|關係企業|董事|經理人',context)
            explicit_parent=False
            for name in company['trusted_names']:
                # In an announcement on behalf of a subsidiary, 本公司 may denote
                # that subsidiary. Require the parent's explicit name and own duty.
                for m in re.finditer(re.escape(normalize(name))+r'([^。；\n]{0,20}?)(?:負連帶責任|須承擔|應給付|提供背書保證|保證責任|遭裁罰|違反)',normalize(quotes)):
                    if not re.search(r'董事|經理人|代子公司|代孫公司',m.group(1)):
                        explicit_parent=True
            if related and not explicit_parent:
                issues.append('子公司／自然人事件沒有目標公司直接責任證據')
    return list(dict.fromkeys(issues))


def score_analyses(analyses,company,table,root,*,scorer=None,progress=print):
    cache=Path(root)/'scoring_cache'
    cache.mkdir(parents=True,exist_ok=True)
    output=[]
    initialization_error=None
    for record in analyses:
        # Legacy or malformed results must never reach event construction.
        # Their verifier report/module status retains the exclusion reason.
        if (not isinstance(record.get('analysis'),dict)
                or not isinstance(record['analysis'].get('risk_assessment'),dict)
                or any(c.get('name')=='json_schema' and c.get('status')=='fail'
                       for c in record.get('verification',{}).get('checks',[]))):
            progress(f'SCORE SKIPPED {record.get("module")} invalid_or_legacy_schema')
            continue
        for unit in event_units(record):
            event=base_event(record,unit,company)
            output.append(event)
            assessment=record['analysis']['risk_assessment']
            bad_checks=[c for c in record['verification']['checks'] if c['status']=='fail']
            integrity=any(c['name']=='archive_sha256' and c['status']=='pass' for c in record['verification']['checks'])
            direct,why=direct_attribution(record,company)
            fact_issues=analysis_claim_issues(record)
            if fact_issues:
                event.update(status='pending_review',review_required=True,review_reasons=fact_issues)
                continue
            if bad_checks or not integrity:
                event.update(status='pending_review',review_required=True,
                    review_reasons=['原文、金額、法條或 JSON 驗證未完成／失敗，不直接扣分'])
                continue
            if assessment['is_negative'] is False or unit['risk_level']=='無明確負面風險':
                event.update(status='not_negative',score_reason=assessment['negative_reason'])
                continue
            if not record['scoring_eligible']:
                event.update(status='filtered_news_second_round',score_reason='新聞第二輪僅保留中／高風險，不另扣分')
                continue
            if not direct or assessment['is_negative'] is None or unit['risk_level']=='待確認':
                event.update(status='pending_review',review_required=True,review_reasons=[why if not direct else '資料不足或事件尚未確認，不直接扣分'])
                continue
            payload=dict(company_code=company['company_code'],company_name=company['company_name'],event_unit=unit,
                raw_sources=source_texts(record),validated_analysis=record['analysis'],
                supported_categories=[i['code'] for i in assessment['risk_indicators'] if i['status']=='有證據支持'],
                scoring_rules=table['rules'],scoring_csv_sha256=table['sha256'])
            try:
                if initialization_error:
                    raise ValueError(initialization_error)
                if scorer is None:
                    try:
                        scorer=ScoreExtractor()
                    except Exception as exc:
                        initialization_error=str(exc)
                        raise
                key=fingerprint([payload,SCORING_PROMPT,EventProposal.model_json_schema(),scorer.provider,scorer.model])
                file=cache/(key+'.json')
                cached=False
                proposal=None
                if file.exists():
                    try:
                        proposal=EventProposal.model_validate_json(file.read_text(encoding='utf-8'))
                        if validate_proposal(proposal,record,unit,company,table):
                            proposal=None
                        else:
                            cached=True
                    except ValueError:
                        pass
                if proposal is None:
                    proposal=EventProposal.model_validate(scorer.extract(json.dumps(payload,ensure_ascii=False)))
                event.update(proposal=proposal.model_dump(),proposed_deduction=proposal.proposed_deduction,
                    risk_category=table['rules'].get(proposal.category_code,{}).get('category'),event_content=proposal.event_description,
                    risk_level=proposal.risk_level,score_reason=proposal.score_reason,risk_factors=[f.model_dump() for f in proposal.risk_factors],
                    evidence_quotes=proposal.evidence_quotes,event_date=proposal.event_date,scoring_cached=cached)
                issues=validate_proposal(proposal,record,unit,company,table)
                if issues:
                    event.update(status='rejected_proposal',review_required=True,review_reasons=issues)
                    continue
                file.write_text(proposal.model_dump_json(indent=2),encoding='utf-8')
                event.update(status='validated_proposal' if proposal.proposed_deduction is not None else 'pending_review',
                    review_required=event['review_required'] or proposal.needs_manual_review or proposal.proposed_deduction is None)
                if proposal.needs_manual_review or proposal.proposed_deduction is None:
                    event['review_reasons'].append('AI 要求人工覆核或未提出可驗證扣分')
                if proposal.source_reference:
                    # Cross-source reference is usable only with a long original quote
                    # linking that reference and the target, not a bare cited case ID.
                    linked=any(proposal.source_reference in q and any(normalize(n) in normalize(q) for n in company['trusted_names']) for q in proposal.identity_evidence_quotes)
                    if linked and (len(proposal.source_reference)>=8) and (event['primary_reference'] or re.search(r'法院|裁罰字|裁處字|金管證',proposal.source_reference)):
                        event['validated_reference']=proposal.source_reference
            except Exception as exc:
                event.update(status='scoring_failed',review_required=True,review_reasons=[f'評分 AI 或驗證失敗：{exc}'])
            progress(f'SCORE {record["module"]} {event["status"]}')
    return output


def normalized_reference(event):
    ref=event.get('validated_reference')
    if not ref:
        return None
    # A court name prevents equal case-number strings from different courts merging.
    court=event.get('court') or ''
    if event['module']=='judicial' and not court and '法院' not in ref:
        return None
    return normalize(ref if normalize(court) in normalize(ref) else court+ref)


def apply_deductions(events,policy,*,as_of=None):
    """Exact identities merge; unresolved near-duplicates are held, never double charged."""
    from datetime import datetime,timezone,timedelta
    from decimal import Decimal,ROUND_HALF_UP
    from credit_risk.date_scope import record_day
    as_of=as_of or datetime.now(timezone(timedelta(hours=8))).date()
    effective={}
    for e in events:
        if e['status'] not in ('validated_proposal','deducted','duplicate_not_deducted'):
            continue
        e.update(status='validated_proposal',applied_deduction=0)
        amount=e['proposed_deduction']
        if e['module']=='news':
            day,_=record_day('news',{'published_at':e.get('publication_date')})
            age=(as_of-day).days if day else None
            band=next((b for b in policy.news_recency.bands if age is not None and 0<=age<=b.maximum_age_days),None)
            trace=dict(as_of=as_of.isoformat(),publication_date=day.isoformat() if day else None,
                age_days=age,original_deduction=amount,weight=band.weight if band else None,
                weighted_deduction=None,rounding='ROUND_HALF_UP; positive deduction minimum 1',
                date_basis='verified_source_publication_date',formula='max(1, round_half_up(original_deduction * weight))')
            e['recency_adjustment']=trace
            if band is None:
                e.update(status='pending_review',review_required=True)
                e['review_reasons'].append('新聞發布日期不明、在未來或超出時間權重範圍，暫不扣分')
                continue
            amount=max(1,int((Decimal(str(amount))*Decimal(str(band.weight))).quantize(Decimal('1'),rounding=ROUND_HALF_UP))) if amount else 0
            trace['weighted_deduction']=amount
        effective[e['event_id']]=amount
    parent=list(range(len(events)))
    def find(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]]
            i=parent[i]
        return i
    def union(i,j):
        parent[find(j)]=find(i)
    possible=[]
    normalized=[(normalize(e.get('event_content') or ''),[normalize(q) for q in e['evidence_quotes'] if len(normalize(q))>=30],normalized_reference(e)) for e in events]
    for i,a in enumerate(events):
        for j in range(i):
            b=events[j]
            refs=(normalized[i][2],normalized[j][2])
            same_reference=all(refs) and refs[0]==refs[1]
            same_text=a['text_signature']==b['text_signature'] and a['event_index']==b['event_index']
            if a['source_identity']==b['source_identity'] or same_reference or same_text:
                union(i,j)
            else:
                # Short generic summaries alone do not establish event identity.
                left,right=normalized[i][0],normalized[j][0]
                shared_evidence=any(q in r or r in q for q in normalized[i][1] for r in normalized[j][1])
                if shared_evidence or min(len(left),len(right))>=18 and SequenceMatcher(None,left,right).ratio()>=policy.possible_duplicate_similarity:
                    possible.append((i,j))
    unresolved=set()
    for i,j in possible:
        if find(i)!=find(j):
            unresolved|={find(i),find(j)}
    groups={}
    for i,event in enumerate(events):
        groups.setdefault(find(i),[]).append(event)
    grouped=[]
    for key,members in groups.items():
        group_id=fingerprint(sorted(e['event_id'] for e in members))
        candidates=[e for e in members if e['status']=='validated_proposal']
        resolution_flags=[any(word in str(e['source_event_status']) for word in ('resolved','已撤銷','已解決')) for e in members]
        conflicting_resolution=any(resolution_flags) and not all(resolution_flags)
        blocked=key in unresolved or conflicting_resolution and bool(candidates)
        if blocked:
            for e in members:
                e.update(review_required=True)
                e['review_reasons'].append('疑似同一事件或後續處分／解決狀態存在差異，確認前不重複或直接扣分')
                if e['status']=='validated_proposal':
                    e['status']='duplicate_pending_review'
        selected=max(candidates,key=lambda e:effective[e['event_id']]) if candidates and not blocked else None
        for e in members:
            e['event_group_id']=group_id
            if e is selected:
                e['applied_deduction']=effective[e['event_id']]
                e['status']='deducted'
            elif e['status']=='validated_proposal':
                e['status']='duplicate_not_deducted'
        grouped.append(dict(event_group_id=group_id,members=[e['event_id'] for e in members],
            selected_event_id=selected['event_id'] if selected else None,
            deduction=selected['applied_deduction'] if selected else 0,
            strategy='maximum_single_validated_deduction_no_sum',review_required=blocked))
    total=sum(g['deduction'] for g in grouped)
    return max(0,policy.base_score-total),total,grouped
