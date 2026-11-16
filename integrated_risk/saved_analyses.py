"""Load successful native analysis outputs without invoking any model or collector."""
from pathlib import Path
from verification.adapters import load_items
from verification.engine import Verifier
from .modules import add_analysis,run_modules


def identity(module,record):
    if module=='environment':
        return (record.get('provenance',{}).get('file_sha256'),record.get('record_locator',{}).get('csv_row'),record.get('full_text_sha256'))
    if module=='judicial':return (record.get('source_url'),record.get('text_sha256'))
    return record.get('article_id') if module=='news' else record.get('announcement_id')


def load_saved_analyses(sources,company,root,data_root,as_of,*,progress=print):
    analyses,statuses=[],{}
    verifier=Verifier(workspace=data_root,as_of=as_of)
    import hashlib
    names={'judicial':'judgment_analysis_results.json','sfb':'sfb_analysis_results.json',
           'environment':'environment_analysis_results.json','news':'news.json','mops':'analysis_results.json'}
    for module,source in sources.items():
        if not source.records:
            _,empty=run_modules({module:source},company,root,data_root,as_of,progress=progress)
            statuses.update(empty)
            continue
        status=dict(status='partial',input_records=len(source.records),success=0,failed=0,filtered=0,validation_failed=0,
            coverage_confirmed=source.coverage,coverage_scope=source.scope,input_files=source.paths,
            date_scope=source.date_scope,errors=list(source.errors),query_assessment=source.query_assessment,
            mode='saved_successful_analyses_only',excluded_records=[])
        statuses[module]=status
        originals={identity(module,r['record']):r for r in source.records}
        folders={Path(p['path']).parent for p in source.paths}
        folders|={p.parent for p in list(folders) if p.name=='documents'}
        if module in ('sfb','environment'):
            folders|={Path(data_root)/'regulatory'/hashlib.sha256(n.encode()).hexdigest()[:16] for n in company['trusted_names']}
        paths=sorted({p/names[module] for p in folders if (p/names[module]).is_file()},key=lambda p:p.stat().st_mtime,reverse=True)
        seen=set()
        rejected=[]
        for path in paths:
            try:
                items,_=load_items(path)
            except (ValueError,OSError,KeyError,TypeError) as exc:
                status['errors'].append(dict(path=str(path),error=str(exc)))
                continue
            for item in items:
                if item.module!=module or not isinstance(item.source_record,dict):continue
                key=identity(module,item.source_record)
                if key not in originals or key in seen:continue
                seen.add(key)
                round=item.source_record.get('analysis_round') if module=='news' else None
                eligible=not (module=='news' and round==2 and item.analysis.get('risk_level') not in ('中','高'))
                add_analysis(item,originals[key],status,analyses,verifier,eligible=eligible,analysis_round=round)
                if analyses[-1]['verification']['verdict']=='failed':
                    rejected.append(dict(source_record_id=key,source_file=str(path),
                        reason='已保存分析未通過目前驗證，排除評分',verification=analyses[-1]['verification']))
                    analyses.pop()
                    status['success']-=1
        missing=set(originals)-seen
        status['excluded_records']=[dict(source_record_id=r['record'].get('document_id') or r['record'].get('article_id') or r['record'].get('announcement_id'),
            source_file=r['path'],reason='沒有成功保存的分析；不重試、不直接扣分，列為資料覆核參考') for key,r in originals.items() if key in missing]
        status['skipped_without_successful_analysis']=len(missing)
        status['excluded_records'].extend(rejected)
        status['skipped_without_successful_analysis']+=len(rejected)
        if missing:
            status['errors'].append(dict(error='部分來源沒有成功的分析結果，本次跳過並保留覆核需求',count=len(missing)))
        status['status']='success' if status['success'] and not missing and not status['validation_failed'] and not status['errors'] and source.coverage else 'partial' if status['success'] else 'failed'
        progress(f'SAVED MODULE {module} success={status["success"]} excluded={len(missing)} validation_failed={status["validation_failed"]}')
    return analyses,statuses
