import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from judicial.extractor import GeminiExtractor
from .schema import NewsAnalysis, SYSTEM, PROMPT_VERSION
from .screening import (TAIPEI, SCREEN_VERSION, load_keywords, screen, deduplicate,
                        news_window, review_route, audit_order, second_round_sample_size)
from .sources import SOURCES, collect
from credit_risk.assessment import load_taxonomy, indicator_candidates, validate_assessment
from company_names import analysis_profile, resolve_profile, identity_names, model_profile
from .screening import occurrences
from .storage import write_json


def validate_analysis(result, evidence_text, categories, company=None, content_scope=None, name_profile=None):
    taxonomy=load_taxonomy()
    if company is not None and result.company_name != company:
        raise ValueError('AI 回填的企業名稱不是目標企業')
    if not result.company_name.strip() or not result.reasoning.strip() or not result.event_summary.strip():
        raise ValueError('AI 企業名稱、事件摘要與判斷理由不得空白')
    if not set(result.risk_category).issubset({row['category'] for row in taxonomy}):
        raise ValueError('AI 使用不在 CSV 的風險類別')
    for quote in result.evidence:
        if not quote.strip() or quote not in evidence_text:
            raise ValueError('AI 原文證據不存在於新聞文字')
    if not result.evidence and (result.is_relevant is not None or result.is_negative is not None
                                or result.relationship != '待確認' or result.risk_level != '待確認'):
        raise ValueError('AI 肯定判斷缺少原文依據')
    if result.is_relevant is False and result.relationship != '無關':
        raise ValueError('AI 相關性與企業關係矛盾')
    if result.is_relevant is True and result.relationship not in ('直接', '間接'):
        raise ValueError('AI 相關性與企業關係矛盾')
    if result.is_negative is True and (result.is_relevant is not True or result.relationship not in ('直接', '間接')):
        raise ValueError('AI 負面判斷缺少目標相關事件')
    if result.risk_level in ('高', '中') and result.is_negative is not True:
        raise ValueError('AI 風險程度與負面判斷矛盾')
    uncertain = (result.is_relevant is None or result.is_negative is None
                 or result.relationship == '待確認' or result.risk_level == '待確認'
                 or result.event_status.verification != '已確認事實'
                 or result.event_status.resolution == '待確認' or content_scope == 'rss_item')
    if uncertain:
        result.needs_manual_review = True
    if result.evidence_quote is not None and (not result.evidence_quote.strip() or result.evidence_quote not in evidence_text):
        raise ValueError('事件歸屬引文不存在於新聞原文')
    if result.event_subject is not None and (not result.event_subject.strip() or result.event_subject not in evidence_text):
        raise ValueError('事件主體不是新聞原文記載的名稱')
    if result.event_detected is True and (not result.event_type or not result.event_subject or not result.evidence_quote
                                         or result.event_subject not in result.evidence_quote):
        raise ValueError('偵測事件缺少類型、直接主體或對應原文引句')
    if result.negative_event_verified and (result.event_detected is not True or result.event_status.verification != '已確認事實'):
        raise ValueError('指控、傳聞或未確認事件不能標為已證實負面事件')
    if result.negative_event_verified and content_scope == 'rss_item':
        raise ValueError('RSS 標題摘要缺少全文，不能標為已證實負面事件')
    mapping = {'confirmed_direct': ('direct_party', '直接'), 'confirmed_indirect': ('indirectly_affected', '間接')}
    if name_profile and not any(occurrences(evidence_text, n) for n in identity_names(name_profile)):
        if result.company_match == 'confirmed' or result.attribution_status in mapping:
            raise ValueError('只有自動推導簡稱命中，不能直接確認法人身分或事件歸屬')
    if result.negative_event_verified and (result.is_negative is not True or result.attribution_status not in mapping):
        raise ValueError('未確認目標企業事件歸屬時，不能標為已證實的目標企業負面事件')
    if result.company_match in ('candidate', 'insufficient_evidence') and result.attribution_status in mapping:
        raise ValueError('企業身分僅候選時不能確認事件歸屬')
    if result.company_match == 'not_matched' and (result.is_negative is True or result.risk_level in ('高', '中')):
        raise ValueError('未匹配目標企業時不能將其他公司事件歸責目標')
    if result.company_match == 'not_matched' and (result.attribution_status != 'not_attributable'
            or result.target_company_role not in ('mentioned_only', 'unrelated')):
        raise ValueError('未匹配企業與事件歸屬／角色矛盾')
    if result.attribution_status in mapping:
        role, relationship = mapping[result.attribution_status]
        if result.target_company_role != role or result.relationship != relationship or result.is_relevant is not True:
            raise ValueError('責任歸屬與目標企業角色／關係矛盾')
    if result.attribution_status == 'insufficient_evidence':
        if (result.target_company_role != 'unverified' or result.relationship != '待確認'
                or result.is_negative is not None or result.risk_level != '待確認'):
            raise ValueError('歸屬證據不足時不得將第三方事件列為目標企業負面風險')
    if result.target_company_role in ('mentioned_only', 'unrelated') or result.attribution_status == 'not_attributable':
        if result.is_negative is True or result.risk_level in ('高', '中'):
            raise ValueError('僅提及或不可歸責的事件不得列為目標企業中高負面風險')
    if result.is_negative is True and (result.event_detected is not True or result.attribution_status not in mapping):
        raise ValueError('目標企業負面判斷缺少事件及可確認的直接／間接歸屬')
    if (result.attribution_status == 'insufficient_evidence' or result.company_match in ('candidate', 'insufficient_evidence')
            or result.target_company_role == 'unverified' or result.event_detected is None
            or result.event_detected and not result.negative_event_verified):
        result.needs_manual_review = True
    if result.risk_level != result.risk_assessment.risk_level:
        raise ValueError('新聞與共通風險程度必須使用同一個五類值')
    validate_assessment(result.risk_assessment, [evidence_text], taxonomy,
                        is_negative=result.is_negative, severity=result.risk_level)
    if result.is_negative is True and result.risk_assessment.risk_category not in result.risk_category:
        raise ValueError('共通主要風險分類必須出現在新聞風險類別中')
    review = result.needs_manual_review or result.requires_human_review or result.risk_assessment.review_required
    result.needs_manual_review = result.requires_human_review = review
    result.risk_assessment.review_required = review


def analyze(article, screening, company, aliases, keywords, keyword_file, root, extractor):
    profile = analysis_profile(company, aliases, supplied=screening.get('company_name_profile'))
    taxonomy=load_taxonomy()
    payload = dict(company_name=company, aliases=aliases, content_scope=article['content_scope'],
                   news_title=article['title'], news_content=article['text'],
                   news_text=screening['evidence_text'], keyword_hits=screening['keyword_hits'],
                   company_name_profile=model_profile(profile), candidate_aliases=profile['candidate_aliases'],
                   taxonomy=taxonomy)
    payload['credit_risk_taxonomy'] = taxonomy
    payload['risk_indicator_candidates'] = indicator_candidates([payload['news_text']],payload['credit_risk_taxonomy'])
    identity = [payload, keyword_file['sha256'], PROMPT_VERSION, extractor.provider, extractor.model]
    key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = root / 'analysis_cache' / (key + '.json')
    if path.exists():
        result = NewsAnalysis.model_validate_json(path.read_text(encoding='utf-8'))
        validate_analysis(result, payload['news_text'], {k['category'] for k in keywords}, company, article['content_scope'], profile)
        return result.model_dump(), True
    result = extractor.extract(json.dumps(payload, ensure_ascii=False))
    validate_analysis(result, payload['news_text'], {k['category'] for k in keywords}, company, article['content_scope'], profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2), encoding='utf-8')
    return result.model_dump(), False


def save_output(root, result):
    root=Path(root)
    low_priority = [a for a in result['articles'] if a['review']['priority'] == 'low']
    reported = [dict(**a['analysis'], article_id=a['article_id'], source=a['source'],
                     news_url=a['url'], published_at=a['published_at'], analysis_round=a['analysis_round'])
                for a in result['articles'] if a['analysis_status'] == 'success' and a['analysis'] is not None]
    second_round = [dict(article_id=a['article_id'], news_url=a['url'], status=a['analysis_status'],
                         retained=a['analysis_status'] == 'success', error=a['analysis_error'])
                    for a in low_priority if a['review']['sampled_for_review']]
    warnings=[]
    for name, data in [('audit_pool.json', low_priority),
                       ('audit_samples.json', [a for a in low_priority if a['review']['sampled_for_review']]),
                       ('analysis_results.json', reported), ('second_round_audit.json', second_round)]:
        try:
            write_json(root/name,data)
        except PermissionError as error:
            warnings.append(dict(file=name,error=str(error),recovery_path=getattr(error,'recovery_path',None)))
            print(f'EXPORT WARNING {error}',flush=True)
    result['export_warnings']=warnings
    # news.json contains every article and AI result, and remains the authoritative checkpoint.
    # Failure here stops safely after bounded retries with the new snapshot retained.
    write_json(root/'news.json',result)


def run(company, aliases, selected, root, keywords, keyword_file, start, end, limit, delay,
        browser='msedge', headed=False, extractor=None, collector=collect,
        audit_seed='news-audit-v1', as_of=None, name_profile=None, previous_failures=None):
    profile = analysis_profile(company, aliases, supplied=name_profile)
    start, end, policy = news_window(start.isoformat(), end.isoformat(), as_of=as_of)
    root.mkdir(parents=True, exist_ok=True)
    collected, reports = [], []
    for source in selected:
        for query in profile['search_names']:
            report = dict(source=source, query=query, state='running', pages=0, collected=0,
                          errors=[], start=start.isoformat(), end=end.isoformat())
            print(f'SEARCH {source} {query}', flush=True)
            try:
                collected.extend(collector(source, query, limit, delay, root, report, start, end,
                                           browser=browser, headed=headed))
            except Exception as error:
                if report['state'] == 'running':
                    report['state'] = 'failed'
                    report['errors'].append(dict(error=str(error)))
                print(f'{report["state"].upper()} {source}: {error}', flush=True)
            if report['state'] == 'running':
                report['state'] = 'failed'
                report['errors'].append(dict(error='來源沒有明確回報結束狀態'))
            reports.append(report)
            if not getattr(collector,'saved_snapshot',False):
                (root / 'search_manifest.json').write_text(json.dumps(dict(sources=reports, articles=collected, company_name_profile=profile),
                                                                         ensure_ascii=False, indent=2), encoding='utf-8')
    unique, duplicates = deduplicate(collected, start, end)
    screened = [(article, screen(article, company, aliases, keywords, start, end, name_profile=profile)) for article in unique]
    # Complete round one before selecting and processing the round-two sample.
    screened.sort(key=lambda pair: {'high': 0, 'low': 1, 'none': 2}[review_route(pair[1])['priority']])
    result = dict(schema_version='2.4', score_item=3, module_id='adverse_news', company=company,
                  aliases=aliases, generated_at=datetime.now(TAIPEI).isoformat(),
                  company_name_profile=profile,
                  requested_sources=selected, sources=reports, keyword_file=keyword_file,
                  keyword_taxonomy=keywords,
                  screen_version=SCREEN_VERSION, prompt_version=PROMPT_VERSION,
                  provider=extractor.provider if extractor else None, model=extractor.model if extractor else None,
                  date_scope=dict(policy, start=start.isoformat(), end=end.isoformat(), timezone='Asia/Taipei',
                                  field='published_at', inclusive=True),
                  phase='first_round',
                  review_policy=dict(second_round_rate=0.2, rounding='ceiling',
                                     denominator='deduplicated_company_matched_in_range_no_keyword',
                                     second_round_pool_size=0, second_round_sample_size=0, seed=audit_seed,
                                     second_round_keep_levels=['中', '高'],
                                     selection='deterministic_sha256_rank',
                                     low_priority_retained=True),
                  articles=[], duplicates=duplicates,
                  limitations=['新聞初篩及 AI 分析並非評分或已查證的信用風險；目前不進行評分',
                               '公司名稱／別名匹配只是字面提及，不能確認法人身分或事件歸責',
                               '日期未知不送 AI，來源受阻不當作零筆；搜尋結果不是完整新聞歷史',
                               'Google 新聞僅分析 RSS 標題與摘要，不自動抓取其他媒體全文；RSS 結果不等於完整新聞歷史',
                               '僅作 URL、相同正文與同發布者同日相同標題去重，不把不同報導視為同一事件'],
                  summary={})
    for position,(article, screening) in enumerate(screened,1):
        route = review_route(screening)
        requested = screening['passed']
        record = dict(**article, screening=screening, analysis=None,
                      review=dict(**route, sampled_for_review=False,
                                  selected_for_ai=requested),
                      analysis_round=1 if requested else None,
                      analysis_status='not_requested' if requested else 'pending_review' if route['priority'] == 'low' else 'not_eligible',
                      analysis_error=None, analysis_cached=False,
                      identity_verified=False, requires_human_review=True, score_ready=False)
        if requested and (previous_failures or {}).get(record['article_id']):
            record['analysis_status']='failed'
            record['analysis_error']=previous_failures[record['article_id']]
        elif requested and extractor:
            try:
                record['analysis'], record['analysis_cached'] = analyze(article, screening, company, aliases,
                                                                       keywords, keyword_file, root, extractor)
                record['analysis_status'] = 'success'
            except Exception as error:
                record['analysis_status'], record['analysis_error'] = 'failed', str(error)
        result['articles'].append(record)
        print(f'{screening["reason"].upper()} {article["source"]} analysis={record["analysis_status"]}', flush=True)
        result['summary'] = summarize(result, len(collected))
        if requested or position%25==0 or position==len(screened):
            save_output(root, result)
    result['phase'] = 'second_round'
    pool = sorted([a for a in result['articles'] if a['review']['priority'] == 'low'],
                  key=lambda a: audit_order(company, a['article_id'], audit_seed))
    sample_size = second_round_sample_size(len(pool))
    result['review_policy'].update(second_round_pool_size=len(pool), second_round_sample_size=sample_size)
    for record in pool[:sample_size]:
        record['review'].update(sampled_for_review=True, selected_for_ai=True)
        record['analysis_round'], record['analysis_status'] = 2, 'not_requested'
    result['summary'] = summarize(result, len(collected))
    save_output(root, result)
    print(f'ROUND 2 sample={sample_size}/{len(pool)} (20%, ceiling)', flush=True)
    for record in pool[:sample_size]:
        if extractor:
            try:
                analysis, record['analysis_cached'] = analyze(record, record['screening'], company, aliases,
                                                               keywords, keyword_file, root, extractor)
                if analysis['risk_level'] in ('中', '高'):
                    record['analysis'], record['analysis_status'] = analysis, 'success'
                else:
                    # Full model result remains in the internal cache, not the reported analysis fields.
                    record['analysis'], record['analysis_status'] = None, 'filtered_out'
            except Exception as error:
                record['analysis_status'], record['analysis_error'] = 'failed', str(error)
        print(f'ROUND 2 {record["source"]} analysis={record["analysis_status"]}', flush=True)
        result['summary'] = summarize(result, len(collected))
        save_output(root, result)
    result['phase'] = 'complete'
    result['summary'] = summarize(result, len(collected))
    save_output(root, result)
    return result


def summarize(result, collected_count):
    articles = result['articles']
    return dict(collected_count=collected_count, unique_count=len(articles),
                duplicate_count=len(result['duplicates']), screened_in=sum(a['screening']['passed'] for a in articles),
                analysis_success=sum(a['analysis_status'] == 'success' for a in articles),
                analysis_failed=sum(a['analysis_status'] == 'failed' for a in articles),
                analysis_not_requested=sum(a['analysis_status'] == 'not_requested' for a in articles),
                high_priority_count=sum(a['review']['priority'] == 'high' for a in articles),
                audit_pool_count=sum(a['review']['priority'] == 'low' for a in articles),
                audit_sample_count=sum(a['review']['sampled_for_review'] for a in articles),
                pending_review_count=sum(a['analysis_status'] == 'pending_review' for a in articles),
                second_round_sample_count=sum(a['analysis_round'] == 2 for a in articles),
                second_round_reported_count=sum(a['analysis_round'] == 2 and a['analysis_status'] == 'success' for a in articles),
                second_round_filtered_count=sum(a['analysis_round'] == 2 and a['analysis_status'] == 'filtered_out' for a in articles),
                second_round_failed_count=sum(a['analysis_round'] == 2 and a['analysis_status'] == 'failed' for a in articles),
                ai_completed_count=sum(a['analysis_status'] in ('success', 'filtered_out') for a in articles),
                search_completed=bool(result['sources']) and all(r['state'] == 'complete' and not r['errors'] for r in result['sources']),
                scoring_ready=False)


def resume_inputs(root,company,keywords,metadata,start,end,*,retry_failed=False):
    """Restore the complete saved search list, including interrupted first-round work."""
    import copy
    root=Path(root)
    saved=json.loads((root/'search_manifest.json').read_text(encoding='utf-8'))
    previous=json.loads((root/'news.json').read_text(encoding='utf-8'))
    if previous.get('company')!=company or saved.get('company_name_profile',{}).get('input_company')!=company:
        raise ValueError('續跑資料與目標公司不符')
    if not isinstance(saved.get('articles'),list) or not isinstance(saved.get('sources'),list):
        raise ValueError('搜尋快照格式錯誤')
    articles=copy.deepcopy(saved['articles'])
    reports=copy.deepcopy(saved['sources'])
    previous_failures={}
    if (not retry_failed and previous.get('keyword_file',{}).get('sha256')==metadata['sha256']
            and previous.get('prompt_version')==PROMPT_VERSION and previous.get('screen_version')==SCREEN_VERSION):
        previous_failures={a['article_id']:a['analysis_error'] for a in previous['articles']
                           if a.get('analysis_round')==1 and a.get('analysis_status')=='failed' and a.get('analysis_error')}
    # Assign each saved article once. Multiple aliases often returned the same item.
    # Existing deduplication in run() remains authoritative.
    emitted=set()
    def collector(source,query,limit,delay,root,report,begin,finish,**kwargs):
        matching=[r for r in reports if r.get('source')==source and r.get('query')==query]
        if len(matching)!=1:
            raise ValueError('缺少唯一的已存來源／名稱查詢，無法聲稱續跑完成')
        original=matching[0]
        if original.get('start','9999')>start.isoformat() or original.get('end','0000')<end.isoformat():
            raise ValueError('既有搜尋日期不足，請重新蒐集缺少期間')
        report.update(copy.deepcopy(original))
        report['resume_from_saved_manifest']=True
        if source in emitted:
            return
        emitted.add(source)
        for article in articles:
            if article.get('source')==source:
                yield copy.deepcopy(article)
    collector.saved_snapshot=True
    return collector,previous_failures


def retry_second_round(root,company,keywords,metadata,extractor,*,as_of=None):
    """Retry only previously selected, failed round-two articles; never resample."""
    root=Path(root)
    result=json.loads((root/'news.json').read_text(encoding='utf-8'))
    if result.get('company')!=company:
        raise ValueError('新聞資料與目標公司不符')
    if (result.get('keyword_file',{}).get('sha256')!=metadata['sha256']
            or result.get('prompt_version')!=PROMPT_VERSION or result.get('screen_version')!=SCREEN_VERSION):
        raise ValueError('關鍵字或分析規則已變更，請使用一般 --resume 重新篩選')
    start,end,_=news_window(as_of=as_of)
    selected=[a for a in result['articles'] if a.get('analysis_round')==2 and a.get('analysis_status')=='failed']
    job=dict(requested=len(selected),attempted=0,reported=0,filtered=0,failed=0,excluded=0,
             remaining=len(selected),stop_reason=None,started_at=datetime.now(TAIPEI).isoformat())
    if selected:
        write_json(root/('news.before_second_round_retry.'+datetime.now(TAIPEI).strftime('%Y%m%dT%H%M%S%f')+'.json'),result)
    result['phase']='second_round_retry'
    result['second_round_retry']=job
    def checkpoint():
        result['summary']=summarize(result,result['summary']['collected_count'])
        save_output(root,result)
        write_json(root/'second_round_retry_manifest.json',job)
    checkpoint()
    for position,record in enumerate(selected,1):
        screening=screen(record,company,result.get('aliases',[]),keywords,start,end,
                         name_profile=result.get('company_name_profile'))
        if review_route(screening)['priority']!='low':
            record.update(analysis_status='not_eligible',analysis_error='重新核對日期或公司後已不符合原抽查條件',analysis=None)
            job['excluded']+=1
        else:
            try:
                analysis,cached=analyze(record,screening,company,result.get('aliases',[]),keywords,metadata,root,extractor)
                retained=analysis['risk_level'] in ('中','高')
                record.update(analysis=analysis if retained else None,analysis_cached=cached,
                              analysis_status='success' if retained else 'filtered_out',analysis_error=None)
                job['reported' if retained else 'filtered']+=1
            except Exception as error:
                record.update(analysis_status='failed',analysis_error=str(error),analysis=None)
                job['failed']+=1
                if '額度' in str(error) or '頻率限制' in str(error):
                    job['stop_reason']=str(error)
        job.update(attempted=position,remaining=len(selected)-position)
        print(f'ROUND 2 RETRY {position}/{len(selected)} analysis={record["analysis_status"]}'+
              (f' error={record["analysis_error"]}' if record.get('analysis_error') else ''),flush=True)
        checkpoint()
        if job['stop_reason']:break
    result['phase']='second_round_retry_paused' if job['remaining'] else 'complete'
    job['finished_at']=datetime.now(TAIPEI).isoformat()
    checkpoint()
    return result


def main():
    parser = argparse.ArgumentParser(description='評分項目三：公司新聞查詢、CSV 關鍵字初篩、Gemini 分析（不評分）')
    parser.add_argument('--company', required=True)
    parser.add_argument('--alias', action='append', default=[])
    parser.add_argument('--no-auto-alias', action='store_true', help='停用自動簡稱與官方對照，只用原名及明示 --alias')
    parser.add_argument('--keywords', type=Path, default=Path('new_key_words.csv'))
    parser.add_argument('--sources', nargs='+', choices=SOURCES, default=SOURCES)
    parser.add_argument('--start', help='發布日起 YYYY-MM-DD，預設兩年前；不得超過近兩年範圍')
    parser.add_argument('--end', help='發布日迄 YYYY-MM-DD，預設台灣今日')
    parser.add_argument('--limit', type=int, default=20, help='每來源、每名稱最多讀取 1–100 篇（含未通過初篩者）')
    parser.add_argument('--all', action='store_true', help='取消本機篇數上限；Google RSS 分時段查詢，中央社持續載入至無新增結果，仍受來源提供範圍限制')
    parser.add_argument('--delay', type=float, default=3)
    parser.add_argument('--browser', choices=['msedge', 'chrome', 'chromium'], default='msedge')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--screen-only', action='store_true', help='只蒐集與初篩，不呼叫 AI')
    parser.add_argument('--audit-seed', default='news-audit-v1', help='固定抽查種子，相同資料可重現樣本')
    parser.add_argument('--out', type=Path, default=Path('data/adverse_news'))
    parser.add_argument('--resume', action='store_true', help='從既有 search_manifest.json 續跑，不重新爬取；成功結果使用有效快取，第一輪已失敗者保留待覆核')
    parser.add_argument('--retry-failed', action='store_true', help='搭配 --resume，重新嘗試第一輪失敗新聞')
    parser.add_argument('--retry-second-round', action='store_true', help='只重試已抽中的第二輪失敗新聞；不重爬、不重抽、不執行第一輪')
    args = parser.parse_args()
    if not args.company.strip() or any(not alias.strip() for alias in args.alias):
        parser.error('公司名稱／別名不得空白')
    if not 1 <= args.limit <= 100 or args.delay < 3:
        parser.error('limit 必須為 1–100，delay 至少 3 秒')
    if args.all:
        args.limit=None
    if args.retry_failed and not args.resume:
        parser.error('--retry-failed 需要 --resume')
    if args.retry_second_round and (args.resume or args.retry_failed or args.screen_only):
        parser.error('--retry-second-round 不可搭配 --resume、--retry-failed 或 --screen-only')
    if args.retry_second_round:
        try:
            keywords,metadata=load_keywords(args.keywords)
            extractor=GeminiExtractor(schema=NewsAnalysis,system=SYSTEM)
            root=args.out/hashlib.sha256(args.company.encode()).hexdigest()[:16]
            result=retry_second_round(root,args.company,keywords,metadata,extractor)
        except (ValueError,OSError,KeyError) as error:
            parser.error(str(error))
        print(json.dumps(result['second_round_retry'],ensure_ascii=False),flush=True)
        if result['second_round_retry']['failed'] or result['second_round_retry']['remaining']:
            raise SystemExit(1)
        return
    try:
        start, end, policy = news_window(args.start, args.end)
        keywords, metadata = load_keywords(args.keywords)
        profile = resolve_profile(args.company, args.alias, enabled=not args.no_auto_alias)
        extractor = None if args.screen_only else GeminiExtractor(schema=NewsAnalysis, system=SYSTEM)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    root = args.out / hashlib.sha256(args.company.encode()).hexdigest()[:16]
    collector=collect
    previous_failures={}
    if args.resume:
        try:
            collector,previous_failures=resume_inputs(root,args.company,keywords,metadata,
                start,end,retry_failed=args.retry_failed)
        except (OSError,ValueError,KeyError,TypeError) as error:
            parser.error(str(error))
    result = run(args.company, args.alias, list(dict.fromkeys(args.sources)), root,
                 keywords, metadata, start, end, args.limit, args.delay, args.browser, args.headed, extractor,
                 audit_seed=args.audit_seed, name_profile=profile,collector=collector,previous_failures=previous_failures)
    print(f'OUTPUT {(root / "news.json").resolve()}', flush=True)
    print(json.dumps(result['summary'], ensure_ascii=False), flush=True)
    if result['summary']['analysis_failed'] or any(r['state'] in ('blocked', 'failed') or r['errors'] for r in result['sources']):
        raise SystemExit(1)
