"""Call existing AI analysis functions against saved inputs; no collectors or CLI calls."""
import copy
import hashlib
import json
import shutil
from pathlib import Path

from verification.adapters import Item
from verification.engine import Verifier
from .config import fingerprint


def seed_cache(source,destination,folder,company,data_root):
    roots={Path(p['path']).parent for p in source.paths}
    roots|={r.parent for r in list(roots) if r.name=='documents'}
    hashes={hashlib.sha256(n.encode()).hexdigest()[:16] for n in company['trusted_names']}
    roots|={Path(data_root)/'regulatory'/h for h in hashes}
    target=Path(destination)/folder
    for root in roots:
        cache=root/folder
        if cache.is_dir() and cache.resolve()!=target.resolve():
            target.mkdir(parents=True,exist_ok=True)
            for file in cache.glob('*.json'):
                dest=target/file.name
                if not dest.exists():
                    shutil.copyfile(file,dest)


def run_modules(sources,company,root,data_root,as_of,*,extractors=None,progress=print):
    extractors=extractors or {}
    root=Path(root)
    verifier=Verifier(workspace=data_root,as_of=as_of)
    analyses=[]
    statuses={}
    for module,source in sources.items():
        status=dict(status='pending',input_records=len(source.records),success=0,failed=0,filtered=0,validation_failed=0,
                    coverage_confirmed=source.coverage,coverage_scope=source.scope,input_files=source.paths,
                    date_scope=source.date_scope,
                    errors=copy.deepcopy(source.errors))
        statuses[module]=status
        status['query_assessment']=copy.deepcopy(source.query_assessment)
        folder=root/'module_ai'/module
        folder.mkdir(parents=True,exist_ok=True)
        try:
            if not company['company_name']:
                raise ValueError('公司代號無法對應公司名稱；不猜測搜尋或分析對象')
            if not source.records:
                status['status']='failed' if status['errors'] else 'no_records' if source.paths and source.coverage else 'missing_source'
                if source.query_assessment and not source.errors:
                    status.update(status='no_records',scoring_eligible=True,applied_deduction=0,
                                  scoring_reason=source.query_assessment['reason'])
                status['failed']=len(status['errors'])
                continue
            progress(f'AI MODULE {module} saved_records={len(source.records)}')
            if module in ('judicial','sfb','environment'):
                if module=='judicial':
                    from judicial.risk_analysis import analyze_records
                    cache='judgment_analysis_cache'
                elif module=='sfb':
                    from regulatory.sfb_analysis import analyze_records
                    cache='sfb_analysis_cache'
                else:
                    from regulatory.environment_analysis import analyze_records
                    cache='environment_analysis_cache'
                seed_cache(source,folder,cache,company,data_root)
                output,manifest=analyze_records([r['record'] for r in source.records],company['company_name'],[],folder,
                    extractor=extractors.get(module),coverage=dict(search_completed=source.coverage),progress=progress)
                values=json.loads(output.read_text(encoding='utf-8'))
                for entry in manifest['records']:
                    if entry['status']!='success':
                        status['failed']+=1
                        status['errors'].append(entry)
                        continue
                    matches=[r for r in source.records if r['record'].get('document_id')==entry.get('document_id')]
                    if len(matches)!=1:
                        status['failed']+=1
                        status['errors'].append(dict(error='原始紀錄無法唯一對應，不依陣列順序猜測'))
                        continue
                    original=matches[0]
                    index=entry['result_index']
                    item=Item(output,index,module,values[index],source_record=original['record'],
                        source_path=Path(original['path']),source_document=original['bundle'],manifest=manifest,manifest_entry=entry)
                    add_analysis(item,original,status,analyses,verifier)
            elif module=='mops':
                from mops_announcements.analysis import analyze_file
                for original_path in dict.fromkeys(r['path'] for r in source.records):
                    matches=[r for r in source.records if r['path']==original_path]
                    work=folder/fingerprint(original_path)[:12]
                    work.mkdir(parents=True,exist_ok=True)
                    copied=work/'mops.json'
                    copied.write_text(json.dumps(matches[0]['bundle'],ensure_ascii=False,indent=2),encoding='utf-8')
                    seed_cache(type(source)(module,paths=[dict(path=original_path)]),work,'analysis_cache',company,data_root)
                    output,manifest=analyze_file(copied,extractor=extractors.get(module),progress=progress)
                    values=json.loads(output.read_text(encoding='utf-8'))
                    for entry in manifest['records']:
                        if entry['status']!='success':
                            status['failed']+=1
                            status['errors'].append(entry)
                            continue
                        found=[r for r in matches if r['record'].get('announcement_id')==entry.get('announcement_id')]
                        if len(found)!=1:
                            raise ValueError('MOPS 公告無法唯一對應')
                        original=found[0]
                        index=entry['result_index']
                        item=Item(output,index,module,values[index],source_record=original['record'],source_path=copied,
                            source_document=original['bundle'],manifest=manifest,manifest_entry=entry)
                        add_analysis(item,original,status,analyses,verifier)
            else:
                analyze_news(source,company,folder,as_of,extractors.get(module),status,analyses,verifier,progress,data_root)
            status['status']='partial' if status['failed'] or status['validation_failed'] or status['errors'] or not source.coverage else 'success'
            if not status['success'] and status['failed']:
                status['status']='failed'
        except Exception as exc:
            status['status']='partial' if status['success'] else 'failed'
            status['failed']+=1
            status['errors'].append(dict(error=f'{type(exc).__name__}: {exc}'))
            progress(f'MODULE FAILED {module}: {exc}')
    return analyses,statuses


def add_analysis(item,original,status,analyses,verifier,*,eligible=True,analysis_round=None):
    # Re-run the same offline validator used by 06, without invoking its CLI.
    verification=verifier.validate(item)
    if verification['verdict']=='failed':
        status['validation_failed']+=1
    status['success']+=1
    analyses.append(dict(id=fingerprint([item.module,str(item.analysis_path),item.index]),module=item.module,
        analysis=item.analysis,source_record=item.source_record,source_document=item.source_document,
        source_path=original['path'],analysis_path=str(item.analysis_path),verification=verification,
        scoring_eligible=eligible,analysis_round=analysis_round))


def analyze_news(source,company,folder,as_of,extractor,status,analyses,verifier,progress,data_root):
    from adverse_news.pipeline import analyze
    from adverse_news.screening import (load_keywords,screen,news_window,audit_order,second_round_sample_size,deduplicate)
    from adverse_news.schema import NewsAnalysis,SYSTEM
    from judicial.extractor import GeminiExtractor
    from .config import PROJECT
    keywords,metadata=load_keywords(PROJECT/'new_key_words.csv')
    start,end,_=news_window(as_of=as_of)
    # Deduplicate before AI. Never call run(), collect() or any search transport.
    records=deduplicate([r['record'] for r in source.records])[0]
    screened=[]
    for record in records:
        result=screen(record,company['company_name'],[],keywords,start,end,name_profile=company['profile'])
        if result['passed'] or result['reason']=='no_keyword':
            screened.append((record,result))
        else:
            status['filtered']+=1
    pool=sorted([r for r,s in screened if s['reason']=='no_keyword'],key=lambda r:audit_order(company['company_name'],r['article_id'],'news-audit-v1'))
    selected={r['article_id'] for r in pool[:second_round_sample_size(len(pool))]}
    status.update(coverage_scope='saved_sources_keyword_first_then_20_percent_no_keyword_sample',
                  audit_pool_count=len(pool),audit_sample_count=len(selected),unreviewed_audit_pool_records=len(pool)-len(selected))
    seed_cache(source,folder,'analysis_cache',company,data_root)
    for record,screening in screened:
        round_no=1 if screening['passed'] else 2
        if round_no==2 and record['article_id'] not in selected:
            status['filtered']+=1
            continue
        try:
            if extractor is None:
                extractor=GeminiExtractor(schema=NewsAnalysis,system=SYSTEM)
            result,cached=analyze(record,screening,company['company_name'],[],keywords,metadata,folder,extractor)
            original=next(r for r in source.records if r['record']['article_id']==record['article_id'])
            output=folder/(record['article_id']+'.analysis.json')
            output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            item=Item(output,0,'news',result,source_record=record,source_path=Path(original['path']),
                source_document=original['bundle'],manifest=dict(company_name=company['company_name']))
            eligible=round_no==1 or result['risk_level'] in ('中','高')
            add_analysis(item,original,status,analyses,verifier,eligible=eligible,analysis_round=round_no)
        except Exception as exc:
            status['failed']+=1
            status['errors'].append(dict(article_id=record.get('article_id'),error=str(exc)))
            progress(f'NEWS AI FAILED {record.get("article_id")}: {exc}')
