import hashlib
import json
from dataclasses import dataclass,field
from datetime import date,datetime,timedelta,timezone
from pathlib import Path

from company_names.resolver import read_cache,normalize,build_profile
from .config import fingerprint


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


@dataclass
class Source:
    module: str
    records: list = field(default_factory=list)
    paths: list = field(default_factory=list)
    coverage: bool = False
    errors: list = field(default_factory=list)
    scope: str = 'saved_local_sources_only'
    date_scope: dict = field(default_factory=dict)
    query_assessment: dict = field(default_factory=dict)


def resolve_company(code,data_root,manifest=None,as_of=None):
    """Local directory/snapshots only: never refresh the directory over the network."""
    as_of=as_of or date.today()
    cache=read_cache(Path(data_root)/'company_names/directory.json')
    found=[c for c in (cache or {}).get('companies',[]) if c['code']==code]
    if len(found)>1:
        raise ValueError('公司代號在本機官方對照中不唯一，需人工確認')
    name=found[0]['full_name'] if found else None
    confirmed=False
    reasons=[]
    if found:
        # Compare calendar dates in the same timezone as the assessment date.
        # E.g. 11/15 18:38 UTC is 11/16 02:38 in Taiwan, not 11/15.
        fetched=datetime.fromisoformat(cache['fetched_at'].replace('Z','+00:00'))
        if fetched.tzinfo is None:
            raise ValueError('官方公司對照檔更新時間缺少時區，無法確認有效日期')
        stamp=fetched.astimezone(timezone(timedelta(hours=8))).date()
        confirmed=timedelta(0)<=as_of-stamp<=timedelta(days=1)
        if not confirmed:
            reasons.append('本機官方公司名稱對照已過期或日期異常，須確認代號與法人身分')
    declared=(manifest or {}).get('company_name')
    if manifest and str(manifest.get('company_code',''))!=code:
        raise ValueError('輸入清單的公司代號與使用者輸入不同')
    if declared and found and normalize(declared) not in {normalize(found[0][k]) for k in ('full_name','short_name')}:
        raise ValueError('輸入清單的公司名稱與代號官方對照不符')
    if not name:
        names=set()
        if declared:
            names.add(declared)
        for path in Path(data_root).glob(f'mops/{code}/*/mops.json'):
            try:
                company=read(path).get('company',{})
                if company.get('company_code')==code and company.get('company_name'):
                    names.add(company['company_name'])
            except (ValueError,OSError,AttributeError):
                continue
        if len(names)==1:
            name=next(iter(names))
        reasons.append('缺少可用且未過期的本機官方代號／公司全名對照，無法確認法人身分')
    profile=build_profile(name,official=cache) if name else None
    return dict(company_code=code,company_name=name,identity_confirmed=confirmed,
                identity_source='local_official_directory' if found else 'local_manifest_or_mops',
                identity_directory_file=str(Path(data_root)/'company_names/directory.json') if cache else None,
                identity_directory_sha256=hashlib.sha256((Path(data_root)/'company_names/directory.json').read_bytes()).hexdigest() if cache else None,
                identity_directory_fetched_at=(cache or {}).get('fetched_at'),
                identity_reasons=reasons,profile=profile,
                trusted_names=list(dict.fromkeys([name,*([found[0]['short_name']] if found else [])])) if name else [])


def load_sources(company,data_root,*,manifest=None,manifest_parent=None,exclude=None):
    data_root=Path(data_root).resolve()
    manifest_parent=Path(manifest_parent or Path.cwd())
    sources={m:Source(m) for m in ('judicial','sfb','environment','news','mops')}
    names={normalize(n) for n in company['trusted_names']}
    def supplied(key):
        value=(manifest or {}).get(key,[])
        value=[value] if isinstance(value,str) else value
        if not isinstance(value,list) or not all(isinstance(v,str) for v in value):
            raise ValueError(f'輸入清單 {key} 必須為路徑字串或陣列')
        return [(Path(v) if Path(v).is_absolute() else manifest_parent/v).resolve() for v in value]
    def eligible(path):
        return 'module_ai' not in path.parts and (not exclude or not path.resolve().is_relative_to(Path(exclude).resolve()))
    def matching(bundle):
        value=bundle.get('company',bundle.get('company_name'))
        if isinstance(value,dict):
            return value.get('company_code')==company['company_code']
        return isinstance(value,str) and normalize(value) in names
    def path_info(path):
        return dict(path=str(path.resolve()),sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    def objects(bundle,key,module):
        value=bundle.get(key,[])
        if not isinstance(value,list):
            sources[module].errors.append(dict(error=f'來源 {key} 不是陣列'))
            return []
        if any(not isinstance(r,dict) for r in value):
            sources[module].errors.append(dict(error=f'來源 {key} 含無效紀錄'))
        return [r for r in value if isinstance(r,dict)]
    def summary(bundle):
        value=bundle.get('summary')
        return value if isinstance(value,dict) else {}
    def bundles(key,filename):
        paths=supplied(key) if manifest is not None else [p for p in data_root.rglob(filename) if eligible(p)]
        selected=[]
        for path in paths:
            try:
                bundle=read(path)
                if not isinstance(bundle,dict) or not matching(bundle):
                    if manifest is not None:
                        raise ValueError('來源查詢公司與指定公司代號／名稱不符')
                    continue
                selected.append((path,bundle))
            except (ValueError,OSError,TypeError) as exc:
                if manifest is not None:
                    targets=('sfb','environment') if key=='regulatory' else (key,)
                    for target in targets:
                        sources[target].errors.append(dict(path=str(path),error=str(exc)))
        return sorted(selected,key=lambda p:p[0].stat().st_mtime,reverse=True)

    # Latest company snapshot per source; annual MOPS snapshots are selected per year.
    regulatory_seen=set()
    for path,bundle in bundles('regulatory','regulatory.json'):
        for module,source_code in [('sfb','sfb'),('environment','prtr')]:
            if source_code not in bundle.get('requested_sources',['sfb','prtr']):
                continue
            if manifest is None and module in regulatory_seen:
                continue
            regulatory_seen.add(module)
            s=sources[module]
            s.paths.append(path_info(path))
            s.coverage=bool(summary(bundle).get('search_completed'))
            from regulatory.query_assessment import empty_assessments
            s.query_assessment=empty_assessments(bundle,snapshot_path=path).get(module,{})
            s.records.extend(dict(record=r,path=str(path),bundle=bundle) for r in objects(bundle,'documents',module) if r.get('source')==source_code)
    for path,bundle in bundles('news','news.json')[:1 if manifest is None else None]:
        s=sources['news']
        s.paths.append(path_info(path))
        s.coverage=bool(summary(bundle).get('search_completed'))
        s.records.extend(dict(record=r,path=str(path),bundle=bundle) for r in objects(bundle,'articles','news'))
    years=set()
    for path,bundle in bundles('mops','mops.json'):
        query=bundle.get('query')
        year=bundle.get('year_ad') or bundle.get('year_roc') or (query.get('roc_year') if isinstance(query,dict) else None) or bundle.get('roc_year') or path.parent.name
        if str(year).isdigit():
            year=int(year)
            year=year+1911 if year<1912 else year
        if manifest is None and year in years:
            continue
        years.add(year)
        s=sources['mops']
        complete=bool(summary(bundle).get('date_window_collection_complete',summary(bundle).get('annual_collection_complete')))
        s.coverage=complete if not s.paths else s.coverage and complete
        s.paths.append(path_info(path))
        announcements=objects(bundle,'announcements','mops')
        s.records.extend(dict(record=r,path=str(path),bundle=bundle) for r in announcements if r.get('status')=='success')
        if any(r.get('status')!='success' for r in announcements):
            s.coverage=False
    paths=supplied('judicial') if manifest is not None else []
    if manifest is None and names:
        hashes={hashlib.sha256(n.encode()).hexdigest()[:16] for n in company['trusted_names']}
        for folder in (data_root/'companies').glob('*'):
            summary=folder/'company_summary.json'
            try:
                accepted=folder.name in hashes or summary.is_file() and normalize(read(summary).get('company','')) in names
                if accepted and (folder/'documents').is_dir():
                    paths.append(folder/'documents')
            except (ValueError,OSError,AttributeError,TypeError):
                continue
    selected_judgments={}
    judicial_coverage=[]
    for path in paths:
        folder=path if path.is_dir() else path.parent
        summary_path=(folder.parent if folder.name=='documents' else folder)/'company_summary.json'
        try:
            data=read(summary_path)
            matched=normalize(data.get('company','')) in names
            judicial_coverage.append(matched and data.get('search_completed') is True)
            if matched:
                sources['judicial'].paths.append(path_info(summary_path))
        except (OSError,ValueError,TypeError,AttributeError):
            judicial_coverage.append(False)
        for file in sorted(path.glob('*.json')) if path.is_dir() else [path]:
            try:
                record=read(file)
                if not isinstance(record,dict) or not record.get('text') or not record.get('extraction'):
                    raise ValueError('需要已保存的裁判全文及第一階段 extraction；總入口不執行爬蟲或重新萃取')
                key=(record.get('source_url'),record.get('text_sha256'))
                rank=file.stat().st_mtime
                if key in selected_judgments and selected_judgments[key][0]>=rank:
                    continue
                record['_source_file']=str(file.resolve())
                sources['judicial'].paths.append(path_info(file))
                selected_judgments[key]=(rank,dict(record=record,path=str(file),bundle=None))
            except (ValueError,OSError,TypeError) as exc:
                sources['judicial'].errors.append(dict(path=str(file),error=str(exc)))
    sources['judicial'].records=[value[1] for value in selected_judgments.values()]
    sources['judicial'].coverage=bool(judicial_coverage) and all(judicial_coverage) and not sources['judicial'].errors
    # User-designated environmental snapshot takes precedence over a limited prior AI run.
    csv_paths=supplied('environment') if manifest is not None else sorted((data_root/'environment_imports').glob('*.csv'))
    csv_paths=[f for p in csv_paths for f in (sorted(p.glob('*.csv')) if p.is_dir() else [p])]
    env=sources['environment']
    env.records=[]
    env.paths=[]
    env.coverage=False
    env.query_assessment={}
    environment_seen=set()
    for path in csv_paths:
        if not path.is_file():
            if manifest is not None:
                sources['environment'].errors.append(dict(path=str(path),error='環境裁罰 CSV 不存在'))
            continue
        try:
            from regulatory.csv_source import load_snapshot
            from regulatory.common import record_text
            raw,rows,encoding=load_snapshot(path)
            digest=hashlib.sha256(raw).hexdigest()
            records=[]
            for i,row in enumerate(rows,2):
                if not any(name in normalize(row['name']) for name in names):
                    continue
                text=record_text(row)
                signature=hashlib.sha256(text.encode()).hexdigest()
                if signature in environment_seen: continue
                environment_seen.add(signature)
                record=dict(document_id=fingerprint([digest,i]),source='prtr',raw_record=row,full_text=text,
                    full_text_sha256=hashlib.sha256(text.encode()).hexdigest(),full_text_kind='provided_penalty_csv_record',
                    full_text_status='provided_record',provenance=dict(file_path=str(path),file_sha256=digest,
                    encoding=encoding,original_download_url=None,provenance_status='user_supplied_not_independently_verified'),
                    record_locator=dict(csv_row=i,no=row['no'],file_sha256=digest))
                records.append(dict(record=record,path=str(path),bundle=None))
            sources['environment'].records.extend(records)
            sources['environment'].paths.append(path_info(path))
            sources['environment'].coverage=True
            sources['environment'].scope='complete_user_supplied_csv_snapshot_only_not_current_official_coverage'
        except (ValueError,OSError,TypeError) as exc:
            sources['environment'].errors.append(dict(path=str(path),error=str(exc)))
    if env.errors: env.coverage=False
    if env.paths and not env.records and not env.errors:
        env.query_assessment=dict(module='environment',status='no_records_confirmed',scoring_eligible=True,
            applied_deduction=0,review_required=False,basis='import_folder_name_scan',scope='supplied_csv_only',
            reason='已掃描人工匯入 CSV，未找到目標公司名稱匹配，僅於提供的快照範圍內扣分 0。',
            queries=company['trusted_names'],source_file=env.paths[0]['path'],source_sha256=env.paths[0]['sha256'],
            files=env.paths,date_scopes=[])
    return sources


def restrict_to_window(sources,data_root,as_of):
    from credit_risk.date_scope import window,filter_records
    import copy
    begin,finish=window(as_of=as_of)
    for module,source in sources.items():
        raw=[r['record'] for r in source.records]
        selected,scope=filter_records(raw,module,begin,finish,
            allowed_roots=[Path(data_root),*[Path(r['path']).parent for r in source.records]],as_of=as_of)
        selected_ids={id(r) for r in selected}
        source.records=[r for r in source.records if id(r['record']) in selected_ids]
        source.date_scope=scope
        if source.query_assessment:
            if source.query_assessment.get('basis')=='import_folder_name_scan':
                source.query_assessment['date_scopes']=[dict(start=str(begin),end=str(finish),scope='supplied_csv_only')]
                continue
            from regulatory.query_assessment import empty_assessments
            try:
                path=Path(source.query_assessment['source_file'])
                source.query_assessment=empty_assessments(read(path),snapshot_path=path,start=begin,end=finish).get(module,{})
            except (OSError,ValueError,KeyError):
                source.query_assessment={}
        if scope['unknown_date_count']:
            source.coverage=False
            source.errors.append(dict(error='來源日期無法確認，已排除且需覆核',count=scope['unknown_date_count']))
        if module=='mops':
            years=set()
            narrower=[]
            for info in source.paths:
                try:
                    path=Path(info['path'])
                    saved=read(path)
                    year=saved.get('year_ad') or saved.get('year_roc') or path.parent.name
                    if str(year).isdigit():
                        year=int(year)
                        year=year+1911 if year<1912 else year
                        years.add(year)
                        declared=saved.get('date_window') or {}
                        if declared.get('start') and declared.get('end'):
                            left=max(begin,date(year,1,1))
                            right=min(finish,date(year,12,31))
                            if date.fromisoformat(declared['start'])>left or date.fromisoformat(declared['end'])<right:
                                narrower.append(dict(year=year,start=declared['start'],end=declared['end']))
                except (OSError,ValueError,TypeError,AttributeError):
                    continue
            required=set(range(begin.year,finish.year+1))
            scope.update(required_years=sorted(required),available_years=sorted(years),missing_years=sorted(required-years),narrower_saved_scopes=narrower)
            if required-years or narrower:
                source.coverage=False
            for path in dict.fromkeys(r['path'] for r in source.records):
                same=[r for r in source.records if r['path']==path]
                bundle=copy.deepcopy(same[0]['bundle'])
                bundle['announcements']=[r['record'] for r in same]
                bundle['date_window']=scope
                for r in same:
                    r['bundle']=bundle
    return sources
