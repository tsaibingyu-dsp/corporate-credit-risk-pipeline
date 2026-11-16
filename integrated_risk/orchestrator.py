"""Sequential collection/analysis entry point with isolated stages and resume records."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime,timedelta,timezone
from pathlib import Path

from .config import PROJECT,fingerprint
from .cli import parse_amount
from .reporting import write_json
from credit_risk.date_scope import window


def read(path):
    try:return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):return {}


def company_name(code,data_root):
    from company_names.resolver import directory
    data=directory(Path(data_root)/'company_names/directory.json')
    matches=[c for c in data.get('companies',[]) if c['code']==code]
    if len(matches)!=1:raise ValueError('無法唯一對應公司代號，請確認上市櫃代號及官方公司對照連線')
    return matches[0]['full_name']


def snapshot_hash(paths):
    return fingerprint([(str(p),hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None) for p in paths])


def source_files(stage,data_root,code,name,begin,end):
    h=hashlib.sha256(name.encode()).hexdigest()[:16]
    r=Path(data_root)/'regulatory'/h
    if stage=='judicial':
        r=Path(data_root)/'companies'/h
        return [r/'company_summary.json',r/'judgment_analysis_manifest.json',r/'judgment_analysis_results.json']
    if stage in ('sfb','environment'):
        prefix='sfb' if stage=='sfb' else 'environment'
        return [r/'regulatory.json',r/(prefix+'_analysis_manifest.json'),r/(prefix+'_analysis_results.json')]
    if stage=='news':return [Path(data_root)/'adverse_news'/h/'news.json']
    return [Path(data_root)/'mops'/code/str(y-1911)/n for y in range(begin.year,end.year+1)
            for n in ('mops.json','analysis_manifest.json','analysis_results.json')]


def source_complete(stage,files):
    if stage=='judicial':
        return read(files[0]).get('search_completed') is True and read(files[1]).get('status')=='complete'
    if stage in ('sfb','environment'):
        bundle=read(files[0]);code='sfb' if stage=='sfb' else 'prtr'
        reports=[r for r in bundle.get('sources',[]) if r.get('source')==code]
        collection=bool(reports) and all(r.get('state')=='complete' and not r.get('errors') for r in reports)
        if not collection:return False
        records=[r for r in bundle.get('documents',[]) if r.get('source')==code]
        if not records:return bool(bundle.get('query_assessments',{}).get(stage,{}).get('scoring_eligible'))
        return read(files[1]).get('status')=='complete'
    if stage=='news':
        d=read(files[0]);return d.get('phase')=='complete' and d.get('summary',{}).get('search_completed') is True and d['summary'].get('analysis_failed',1)==0
    return all(read(files[i]).get('summary',{}).get('date_window_collection_complete') is True
               and read(files[i+1]).get('status')=='complete' for i in range(0,len(files),3))


def commands(stage,code,name,data_root,begin,end,*,restart=False):
    data_root=Path(data_root);h=hashlib.sha256(name.encode()).hexdigest()[:16]
    dates=['--start',str(begin),'--end',str(end)]
    if stage=='judicial':
        args=[str(PROJECT/'02_company.py'),'--company',name,'--out',str(data_root/'companies'),'--analyze',*dates]
        folder=data_root/'companies'/h
        saved=read(folder/'collection_manifest.json');scope=saved.get('date_window',{})
        searches=saved.get('searches',[])
        complete=bool(searches) and all(s.get('completed') and not s.get('limit_reached') and not s.get('errors') for s in searches)
        if not restart and scope.get('start')==str(begin) and scope.get('end')==str(end) and complete and not saved.get('failures'):
            args+=['--process-collected',str(folder/'collected_documents')]
        return args
    if stage in ('sfb','environment'):
        args=[str(PROJECT/'03_regulatory.py'),'--company',name,'--sources','sfb' if stage=='sfb' else 'prtr',
              '--out',str(data_root/'regulatory'),'--limit','100','--analyze',*dates]
        if stage=='environment':args+=['--prtr-csv',str(data_root/'environment_imports')]
        bundle=read(data_root/'regulatory'/h/'regulatory.json');source='sfb' if stage=='sfb' else 'prtr'
        reports=[r for r in bundle.get('sources',[]) if r.get('source')==source]
        if stage=='sfb' and not restart and reports and all(r.get('state')=='complete' and not r.get('errors')
            and r.get('date_scope',{}).get('start','9999')<=str(begin) and r.get('date_scope',{}).get('end','0000')>=str(end) for r in reports):
            args+=['--analysis-input',str(data_root/'regulatory'/h/'regulatory.json')]
        return args
    if stage=='news':
        args=[str(PROJECT/'04_news.py'),'--company',name,'--out',str(data_root/'adverse_news')]
        previous=read(data_root/'adverse_news'/h/'news.json')
        reports=previous.get('sources',[])
        resumable=bool(reports) and all(r.get('start','9999')<=str(begin) and r.get('end','0000')>=str(end) for r in reports)
        return args+(['--resume','--retry-failed'] if resumable and not restart else ['--all'])+dates
    paths=[data_root/'mops'/code/str(y-1911)/'mops.json' for y in range(begin.year,end.year+1)]
    if not restart and all(read(p).get('summary',{}).get('date_window_collection_complete') is True
        and read(p).get('date_window',{}).get('start')==str(begin) and read(p).get('date_window',{}).get('end')==str(end) for p in paths):
        script='import sys; from mops_announcements.analysis import analyze_file; batches=[analyze_file(p) for p in sys.argv[1:]]; sys.exit(1 if any(m["summary"]["failed"] or m["summary"]["skipped"] for _,m in batches) else 0)'
        return ['-c',script,*map(str,paths)]
    return [str(PROJECT/'05_mops.py'),'--company',code,'--out',str(data_root/'mops'),'--extract',*dates]


def run_all(code,amount,*,data_root=None,restart=False,runner=None,resolver=None,as_of=None):
    data_root=Path(data_root or PROJECT/'data').resolve()
    runner=runner or subprocess.run
    resolver=resolver or company_name
    as_of=as_of or datetime.now(timezone(timedelta(hours=8))).date()
    begin,end=window(as_of=as_of)
    name=resolver(code,data_root)
    root=data_root/'pipeline_runs'/code;path=root/'pipeline_run.json'
    old=read(path)
    code_files=[PROJECT/'run_pipeline.py',*[PROJECT/n for n in ('02_company.py','03_regulatory.py','04_news.py','05_mops.py','main.py')]]
    for folder in ('integrated_risk','judicial','regulatory','adverse_news','mops_announcements','credit_risk','verification','company_names'):
        code_files.extend(sorted((PROJECT/folder).glob('*.py')))
    signature=fingerprint([code,name,str(amount),str(begin),str(end),os.getenv('GEMINI_MODEL'),
        snapshot_hash(code_files),snapshot_hash([PROJECT/'new_key_words.csv',PROJECT/'config/credit_policy.json']),
        snapshot_hash(sorted((data_root/'environment_imports').glob('*.csv')))])
    result=dict(company_code=code,company_name=name,requested_amount=str(amount),date_window=dict(start=str(begin),end=str(end)),
        signature=signature,status='running',started_at=datetime.now(timezone.utc).isoformat(),stages=[])
    write_json(path,result)
    stages=['judicial','sfb','environment','news','mops']
    for index,stage in enumerate(stages,1):
        files=source_files(stage,data_root,code,name,begin,end)
        prior=next((s for s in old.get('stages',[]) if s.get('name')==stage),{})
        entry=dict(name=stage,status='running',started_at=datetime.now(timezone.utc).isoformat(),exit_code=None)
        result['stages'].append(entry)
        if not restart and old.get('signature')==signature and prior.get('status')=='complete' and source_complete(stage,files):
            entry.update(status='complete',reused=True,reason='本次公司、期間、程式、規則及匯入資料未變，來源與分析均已完成')
            write_json(path,result);print(f'PIPELINE [{index}/5] {stage} REUSED',flush=True);continue
        print(f'PIPELINE [{index}/5] {stage}',flush=True)
        write_json(path,result)
        if stage=='environment' and not list((data_root/'environment_imports').glob('*.csv')):
            entry.update(status='missing_input',error='缺少人工下載的環境部 CSV；請放入 data/environment_imports，繼續其他來源')
        else:
            try:
                command=[sys.executable,*commands(stage,code,name,data_root,begin,end,restart=restart)]
                completed=runner(command,cwd=str(PROJECT),check=False)
                entry.update(exit_code=completed.returncode,
                    status='complete' if completed.returncode==0 and source_complete(stage,files) else 'partial')
            except KeyboardInterrupt:
                entry['status']='interrupted';result['status']='interrupted';write_json(path,result);raise
            except Exception as exc:
                entry.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        entry['finished_at']=datetime.now(timezone.utc).isoformat();write_json(path,result)
    entry=dict(name='integration',status='running',started_at=datetime.now(timezone.utc).isoformat())
    result['stages'].append(entry);write_json(path,result)
    print('PIPELINE [6/6] integration (successful saved analyses only)',flush=True)
    try:
        report_path=data_root/'integrated'/code/'integrated_risk_report.json'
        before=report_path.read_bytes() if report_path.exists() else None
        completed=runner([sys.executable,str(PROJECT/'main.py'),'--company-code',code,'--amount',str(amount),
                          '--data-root',str(data_root),'--saved-analyses-only','--as-of',str(as_of)],cwd=str(PROJECT),check=False)
        report=read(report_path)
        fresh=report_path.exists() and report_path.read_bytes()!=before
        valid=fresh and str(report.get('company',{}).get('company_code'))==code and report.get('as_of')==str(as_of)
        entry.update(exit_code=completed.returncode,status='complete_with_review' if valid and completed.returncode==2 else
                     'complete' if valid and completed.returncode==0 else 'failed',report=str(report_path) if valid else None)
    except KeyboardInterrupt:
        entry['status']='interrupted';result['status']='interrupted';write_json(path,result);raise
    except Exception as exc:
        entry.update(status='failed',error=f'{type(exc).__name__}: {exc}')
    entry['finished_at']=datetime.now(timezone.utc).isoformat()
    result.update(status='failed' if entry['status']=='failed' else 'partial' if any(s['status']!='complete' for s in result['stages']) else 'complete',
                  finished_at=datetime.now(timezone.utc).isoformat())
    write_json(path,result)
    print(f'PIPELINE {result["status"].upper()} {path}',flush=True)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description='依序蒐集四個來源、AI 分析，再產生授信模擬報告')
    parser.add_argument('--company-code')
    parser.add_argument('--amount')
    parser.add_argument('--data-root',type=Path,default=PROJECT/'data')
    parser.add_argument('--restart',action='store_true',help='重新啟動全部階段；仍使用來源既有有效快取')
    args=parser.parse_args(argv)
    try:
        code=(args.company_code or input('公司代號：')).strip()
        if not re.fullmatch(r'\d{4,6}',code):raise ValueError('公司代號須為 4～6 位數字')
        amount=parse_amount(args.amount if args.amount is not None else input('申請授信額度（元）：'))
        if not os.getenv('GEMINI_API_KEY') or not os.getenv('GEMINI_MODEL'):
            raise ValueError('請先在此終端設定 GEMINI_API_KEY 與 GEMINI_MODEL')
        result=run_all(code,amount,data_root=args.data_root,restart=args.restart)
    except KeyboardInterrupt:return 130
    except (ValueError,OSError) as exc:parser.error(str(exc))
    return 1 if result['status']=='failed' else 2 if result['status']=='partial' else 0
