"""Read-only, layered verification of source-linked AI risk analyses."""
import csv
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from .adapters import resolve_local, sha256
from .http_check import allowed_url, probe
from .semantic import claims_for, verify_review


def digest(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())


def strings(value):
    return [v for v in value.values() if isinstance(v,str) and v.strip()] if isinstance(value,dict) else []


def quote_fields(value, path='$'):
    keys={'quotes','evidence','evidence_text','evidence_quote','evidence_quotes','amount_original'}
    if isinstance(value,dict):
        for key,child in value.items():
            target=f'{path}.{key}'
            if key in keys and child is not None:
                if isinstance(child,list):
                    for i,q in enumerate(child):
                        yield f'{target}[{i}]',q
                else:
                    yield target,child
            else:
                yield from quote_fields(child,target)
    elif isinstance(value,list):
        for i,child in enumerate(value):
            yield from quote_fields(child,f'{path}[{i}]')


def review_flags(value,path='$'):
    found=[]
    if isinstance(value,dict):
        for key,child in value.items():
            target=f'{path}.{key}'
            if key in {'review_required','requires_human_review','needs_manual_review'} and child is True:
                found.append(target)
            found.extend(review_flags(child,target))
    elif isinstance(value,list):
        for i,child in enumerate(value):
            found.extend(review_flags(child,f'{path}[{i}]'))
    return found


MONEY = re.compile(r'(?:新臺幣|新台幣|美元|人民幣|港幣|日圓|TWD|USD|HKD|CNY|JPY)?\s*'
    r'[0-9０-９零〇一壹二貳兩三參四肆五伍六陸七柒八捌九玖十拾百佰千仟萬億,，.．]+\s*(?:億元|萬元|元整|元|圓)')


def monetary_claims(value,path='$'):
    narrative={'event_summary','case_summary','court_findings','event_description','operational_impact',
               'company_response','reasoning','risk_reasoning','negative_reason','risk_reason','risk_factors','inference_reason','reason'}
    if isinstance(value,dict):
        for key,child in value.items():
            target=f'{path}.{key}'
            if key in narrative:
                for text in (child if isinstance(child,list) else [child]):
                    if isinstance(text,str):
                        for match in MONEY.finditer(text):
                            yield target,match.group().strip()
            else:
                yield from monetary_claims(child,target)
    elif isinstance(value,list):
        for i,child in enumerate(value):
            yield from monetary_claims(child,f'{path}[{i}]')


def native_validation(item, sources):
    """Re-run module rules on a copy: validation must never rewrite the saved analysis."""
    module=item.module
    analysis=item.analysis
    company=item.manifest.get('company_name') or (item.source_document or {}).get('company_name') or analysis.get('company_name')
    aliases=item.manifest.get('aliases',[])
    record=item.source_record
    if module=='judicial':
        from judicial.risk_schema import JudgmentRisk as Schema
        from judicial.risk_analysis import make_input, validate_analysis
        model=Schema.model_validate(analysis,strict=True)
        validate_analysis(model,make_input(record,company,aliases))
    elif module=='sfb':
        from regulatory.sfb_schema import SfbAnalysis as Schema
        from regulatory.sfb_analysis import make_input, validate_analysis
        model=Schema.model_validate(analysis,strict=True)
        validate_analysis(model,make_input(record,company,aliases))
    elif module=='environment':
        from regulatory.environment_schema import EnvironmentAnalysis as Schema
        from regulatory.environment_analysis import make_input, validate_analysis
        model=Schema.model_validate(analysis,strict=True)
        validate_analysis(model,make_input(record,company,aliases))
    elif module=='news':
        from adverse_news.schema import NewsAnalysis as Schema
        from adverse_news.pipeline import validate_analysis
        from credit_risk.assessment import load_taxonomy
        model=Schema.model_validate(analysis,strict=True)
        validate_analysis(model,'\n'.join(sources),[c['category'] for c in load_taxonomy()],
                          company=company,content_scope=record.get('content_scope'),
                          name_profile=(item.source_document or {}).get('company_name_profile'))
    elif module=='mops':
        from mops_announcements.schema import MopsAnalysis as Schema
        from mops_announcements.analysis import make_input, validate_analysis
        model=Schema.model_validate(analysis,strict=True)
        validate_analysis(model,make_input(item.source_document,record))
    else:
        raise ValueError('Unknown analysis schema')
    return model.model_dump()


def schema_for(module):
    from judicial.risk_schema import JudgmentRisk
    from regulatory.sfb_schema import SfbAnalysis
    from regulatory.environment_schema import EnvironmentAnalysis
    from adverse_news.schema import NewsAnalysis
    from mops_announcements.schema import MopsAnalysis
    return dict(judicial=JudgmentRisk,sfb=SfbAnalysis,environment=EnvironmentAnalysis,
                news=NewsAnalysis,mops=MopsAnalysis).get(module)


class Verifier:
    def __init__(self, *, workspace=None, as_of=None, check_http=False, reviewer=None, historical=False):
        self.workspace=Path(workspace or Path.cwd()).resolve()
        self.as_of=as_of or datetime.now(timezone(timedelta(hours=8))).date()
        self.check_http=check_http
        self.reviewer=reviewer
        self.historical=historical
        self.seen={}
        self.http_cache={}

    def validate(self,item):
        checks=[]
        archives=[]
        def add(level,name,status,detail,**extra):
            checks.append(dict(level=level,name=name,status=status,detail=detail,**extra))
        def check(level,name,condition,detail):
            add(level,name,'pass' if condition else 'fail',detail)
        record=item.source_record or {}
        analysis=item.analysis
        result=dict(record_id=digest([str(item.analysis_path),item.index]),module=item.module,
                    analysis_file=str(item.analysis_path),result_index=item.index,analysis_sha256=digest(analysis),
                    source_file=str(item.source_path) if item.source_path else None,checks=checks)
        for issue in item.context_issues:
            add('L1','source_link','unverifiable',issue)
        Schema=schema_for(item.module)
        valid_schema=False
        try:
            if Schema is None:
                raise ValueError('Unrecognized risk analysis schema')
            Schema.model_validate(analysis,strict=True)
            valid_schema=True
            add('L3','json_schema','pass','Current schema, required fields, types and enumerations validated')
        except ValidationError as exc:
            add('L3','json_schema','fail','Schema validation failed; older schemas require migration or reanalysis',
                issues=[dict(path=list(e['loc']),type=e['type'],message=e['msg']) for e in exc.errors()])
        except (ValueError,TypeError) as exc:
            add('L3','json_schema','fail',str(exc))

        if not record:
            add('L1','original_record','unverifiable','Original record unavailable; analysis cannot be its own source')
            return self.finish(result,analysis,archives)
        roots=[self.workspace,item.analysis_path.parent]
        def archive(raw_path, expected=None):
            path=resolve_local(raw_path,item.analysis_path.parent,roots)
            if path is None or not path.is_file():
                add('L1','raw_archive','unverifiable','Raw archive absent or outside permitted roots',path=str(raw_path))
                return None
            if path.stat().st_size>30*1024*1024:
                add('L1','raw_archive','unverifiable','Archive exceeds 30 MiB verification limit',path=str(path))
                return None
            raw=path.read_bytes()
            actual=sha256(raw)
            # Collectors name raw files by SHA-256; do not accept an arbitrary filename as a hash.
            expected=expected or (path.stem if re.fullmatch('[a-f0-9]{64}',path.stem) else None)
            archives.append(dict(path=str(path),sha256=actual))
            if expected:
                if actual!=expected:
                    # Older Windows writers hashed LF text but wrote CRLF. Accept only this
                    # precisely reproducible legacy transformation, recording both hashes.
                    legacy=item.module=='judicial' and path.suffix.lower() in ('.txt','.html') and sha256(raw.replace(b'\r\n',b'\n'))==expected
                    if legacy:
                        add('L1','archive_sha256','unverifiable','Legacy Windows LF/CRLF difference reproduced; bytes are not hash-identical',
                            expected_sha256=expected,actual_sha256=actual)
                        raw=raw.replace(b'\r\n',b'\n')
                    else:
                        check('L1','archive_sha256',False,'Archived bytes do not match recorded SHA-256')
                        return None
                else:
                    check('L1','archive_sha256',True,'Archived bytes compared with recorded SHA-256')
            else:
                add('L1','archive_sha256','unverifiable','Archive has no previously recorded SHA-256',path=str(path))
            return raw

        url=(record.get('source') or {}).get('page_url') if item.module=='mops' and isinstance(record.get('source'),dict) else record.get('source_url') or record.get('url')
        if item.module=='environment':
            url=record.get('provenance',{}).get('original_download_url')
        result['source_url']=url
        if url:
            check('L1','source_whitelist',allowed_url(url,item.module),'Exact HTTPS host whitelist; credentials and custom ports disallowed')
            if self.check_http and allowed_url(url,item.module):
                key=(item.module,url)
                if key not in self.http_cache:
                    self.http_cache[key]=probe(url,item.module)
                status=self.http_cache[key]
                add('L1','http_probe',status['status'],status['detail'],response={k:v for k,v in status.items() if k not in ('status','detail')})
            else:
                add('L1','http_probe','unverifiable','Live HTTP verification not performed; archived content is checked separately')
        else:
            add('L1','source_whitelist','unverifiable','No original publication/download URL; local CSV is not an authenticated official download')

        sources=[]
        text=''
        try:
            if item.module=='judicial':
                text=record.get('text','')
                sources=[text]
                check('L1','text_sha256',sha256(text.encode())==record.get('text_sha256'),'Saved judgment text hash')
                expected_record=item.manifest_entry.get('source_record_sha256')
                if expected_record:
                    source_copy=dict(record)
                    if item.manifest_entry.get('source_file'):
                        source_copy['_source_file']=item.manifest_entry['source_file']
                    actual_record=sha256(json.dumps(source_copy,ensure_ascii=False,sort_keys=True).encode())
                    check('L1','source_record_sha256',actual_record==expected_record,'Source record compared with analysis-time manifest snapshot hash')
                raw=archive(record.get('raw_path'),record.get('raw_sha256'))
                if raw is not None:
                    decoded=raw.decode('utf-8-sig')
                    if '<html' in decoded.lower() or '<div' in decoded.lower():
                        from judicial.parser import parse_html
                        parsed=parse_html(decoded)
                        from bs4 import BeautifulSoup
                        legacy=BeautifulSoup(decoded,'html.parser').select_one('.jud_content')
                        check('L2','archive_content',text in [parsed,legacy.get_text('\n',strip=True) if legacy else None],
                              'Reparse judgment HTML, including supported legacy body normalization')
                    else:
                        check('L2','archive_content',decoded==text,'Archived normalized text equals saved text; original HTML origin still requires review')
                        add('L2','html_completeness','unverifiable','Only normalized text archive available, not original HTML')
                    from judicial.dates import judgment_date
                    day=judgment_date(decoded,is_html=('<html' in decoded.lower() or '<div' in decoded.lower()))
                    if day and isinstance(analysis,dict):
                        check('L2','judgment_date_metadata',analysis.get('judgment_date')==day.isoformat(),
                              'Analysis date compared with explicitly labeled judgment date, not dates of cited cases')
                    else:
                        add('L2','judgment_date_metadata','unverifiable','No independently readable official judgment-date metadata in archived text')
            elif item.module in ('sfb','environment'):
                from regulatory.common import record_text
                from regulatory.sfb_analysis import original_fields
                fields=original_fields(record)
                sources=strings(fields)
                text=record.get('full_text','')
                check('L1','text_sha256',sha256(text.encode())==record.get('full_text_sha256'),'Saved regulatory text hash')
                if item.module=='environment':
                    check('L2','csv_fields',text==record_text(record.get('raw_record',{})),'CSV cells match saved normalized record')
                    provenance=record.get('provenance',{})
                    raw=archive(provenance.get('file_path'),provenance.get('file_sha256'))
                    if raw is not None:
                        for encoding in ('utf-8-sig','cp950'):
                            try:
                                decoded=raw.decode(encoding)
                                break
                            except UnicodeDecodeError:
                                continue
                        rows=list(csv.DictReader(io.StringIO(decoded,newline='')))
                        from regulatory.csv_source import normalize_download_row
                        rows=[normalize_download_row(row) for row in rows]
                        row_no=record.get('record_locator',{}).get('csv_row')
                        check('L2','csv_row',type(row_no) is int and 2<=row_no<=len(rows)+1 and rows[row_no-2]==record.get('raw_record'),
                              'Original CSV row position and every field value compared')
                else:
                    paths=record.get('raw_paths',[])
                    if not paths:
                        add('L2','archive_content','unverifiable','SFB original HTML/PDF archive absent')
                    for raw_path in paths:
                        raw=archive(raw_path)
                        if raw is None:
                            continue
                        if Path(raw_path).suffix.lower()=='.pdf':
                            from regulatory.common import pdf_text
                            parsed=pdf_text(raw)
                            check('L2','attachment_content',parsed in sources,'PDF independently parsed and compared with an individual source field')
                        else:
                            from regulatory.common import parse_sfb
                            title,body=parse_sfb(raw.decode('utf-8-sig'))
                            check('L2','archive_content',title in sources and body in sources,'SFB HTML title and full announcement independently parsed')
            elif item.module=='news':
                text=record.get('text','')
                sources=[record.get('title','')+'\n'+text]
                from adverse_news.sources import article_record
                rebuilt=article_record(record.get('source'),record.get('url'),record.get('title'),text,
                    record.get('published_at'),record.get('publisher'),record.get('content_scope'),
                    record.get('raw_path'),record.get('search_url'))
                check('L1','article_fingerprint',rebuilt['article_id']==record.get('article_id'),'Article ID binds URL, title, publication timestamp and text')
                raw=archive(record.get('raw_path'))
                if record.get('content_scope')=='rss_item':
                    add('L2','article_full_text','unverifiable','Google News RSS contains title/summary, not publisher full text')
                    if raw is not None:
                        from adverse_news.rss import parse_feed
                        entries=parse_feed(raw)
                        matches=[e for e in entries if all(e.get(k)==record.get(k) for k in ('url','title','text','published_at'))]
                        check('L2','archive_content',len(matches)==1,'Unique RSS item matches URL, title, date and summary')
                elif raw is not None:
                    from adverse_news.sources import parse_cna
                    parsed=parse_cna(raw.decode('utf-8-sig'),record.get('url'))
                    check('L2','archive_content',all(parsed.get(k)==record.get(k) for k in ('title','text','published_at')),
                          'CNA HTML full text, title and publication timestamp independently parsed')
            elif item.module=='mops':
                text=record.get('full_text','')
                sources=[text]
                check('L1','text_sha256',sha256(text.encode())==record.get('full_text_sha256'),'MOPS announcement full-text hash')
                src=record.get('source',{})
                raw=archive(src.get('raw_file'),src.get('body_sha256'))
                if raw is not None:
                    from mops_announcements.parsing import parse_detail
                    parsed=parse_detail(json.loads(raw),record)
                    check('L2','archive_content',parsed.get('full_text')==text,'MOPS raw API response independently parsed and matched')
            else:
                raise ValueError('Unsupported source module')
        except Exception as exc:
            add('L2','source_parse','fail',f'{type(exc).__name__}: {exc}')

        if item.manifest.get('source_sha256') and item.source_path and item.source_path.is_file():
            check('L1','input_file_sha256',sha256(item.source_path.read_bytes())==item.manifest['source_sha256'],
                  'Analysis manifest input snapshot hash')
        result['source_fingerprint']=digest(dict(texts=sources,archives=archives,url=url))
        if not sources or not any(s.strip() for s in sources):
            add('L2','source_content','fail','Missing or empty original text')
        elif item.module!='environment':
            check('L2','content_length',len(text.strip())>=30,'Minimum 30 characters is only a truncation heuristic, not proof of completeness')
        if record.get('full_text_status') in ('partial','partial_attachment') or record.get('limitations'):
            add('L2','source_limitations','unverifiable','Original collector recorded limitations',limitations=record.get('limitations',[]))
        coverage=item.manifest.get('source_search_completed',item.manifest.get('annual_collection_complete',
                  item.manifest.get('search_completed',(item.source_document or {}).get('summary',{}).get('search_completed'))))
        if coverage is not True:
            add('L2','collection_coverage','unverifiable','Full collection coverage not confirmed; this report cannot establish absence of other adverse events')

        self.dates(item,record,analysis,add)
        if isinstance(analysis,dict):
            self.identity(item,record,analysis,sources,add)
        key=(item.module,url or record.get('document_id'),digest(sources))
        if key in self.seen:
            add('L2','duplicate','fail','Same source and content repeated; do not count twice',first_record_id=self.seen[key])
        else:
            self.seen[key]=result['record_id']
            add('L2','duplicate','pass','No identical source/content previously encountered in this run')

        if isinstance(analysis,dict) and sources:
            quotes=list(quote_fields(analysis))
            failures=[]
            matches=[]
            for path,quote in quotes:
                found=[]
                if isinstance(quote,str) and quote.strip():
                    found=[dict(source_field=i,start=s.index(quote),end=s.index(quote)+len(quote))
                           for i,s in enumerate(sources) if quote in s]
                if not found:
                    failures.append(dict(path=path,quote=quote))
                else:
                    matches.append(dict(path=path,matches=found))
            check('L4','literal_evidence',not failures,'Every quote must occur contiguously in one source field; no cross-field stitching')
            checks[-1].update(issues=failures,matches=matches,quote_count=len(quotes))
            if not quotes:
                add('L4','evidence_presence','unverifiable','Analysis provides no evidence quotes')
            # Individual CSV cells and source field boundaries are preserved in reports.
            result['evidence_sources']=[dict(index=i,sha256=sha256(s.encode()),length=len(s)) for i,s in enumerate(sources)]
            from judicial.risk_money import source_number, CURRENCIES
            source_amounts=[q.group().strip() for s in sources for q in MONEY.finditer(s)]
            # Bare numeric CSV fine cells do not declare a monetary unit. They may be
            # preserved in typed fields, but cannot justify a newly invented 元/萬元 phrase.
            fine=(record.get('raw_record') or {}).get('fine')
            if item.module in ('environment','sfb') and isinstance(fine,str) and MONEY.search(fine):
                source_amounts.append(fine)
            def currencies(phrase):
                return {code for code,labels in CURRENCIES.items() if any(label in phrase for label in labels)}
            unsupported=[]
            for path,phrase in monetary_claims(analysis):
                amount=source_number(phrase)
                if amount is None or not any(source_number(q)==amount and
                      (not currencies(phrase) or currencies(phrase).issubset(currencies(q))) for q in source_amounts):
                    unsupported.append(dict(path=path,claim=phrase))
            check('L4','narrative_amounts',not unsupported,'Monetary claims in summaries/reasons/factors checked against explicit source amounts and currencies; no aggregation or exchange conversion')
            checks[-1]['issues']=unsupported
        if valid_schema and sources:
            try:
                effective=native_validation(item,sources)
                add('L4','module_consistency','pass','Rechecked identity, role, date, amount, currency, status, severity and risk-factor rules')
                result['effective_review_flags']=review_flags(effective)
            except Exception as exc:
                add('L4','module_consistency','fail',f'{type(exc).__name__}: {exc}')
        else:
            add('L4','module_consistency','unverifiable','Source or valid schema missing; module-specific factual checks unavailable')

        deterministic_fail=any(c['status']=='fail' for c in checks)
        if self.reviewer and valid_schema and sources and not deterministic_fail:
            try:
                claims=claims_for(analysis)
                reviewed=self.reviewer.review(dict(company_name=analysis.get('company_name'),claims=claims,
                     content_scope=record.get('content_scope'),raw_sources=sources))
                supported=verify_review(reviewed,claims,sources)
                result['semantic_review']=reviewed.model_dump()
                add('L4','semantic_support','pass' if supported else 'unverifiable',
                    'Second-model review is advisory; disagreement/insufficient evidence requires human review')
            except Exception as exc:
                add('L4','semantic_support','unverifiable',f'Second review unavailable or invalid: {exc}')
        else:
            add('L4','semantic_support','unverifiable','Semantic review not performed; literal matching does not prove interpretation')
        return self.finish(result,analysis,archives)

    def identity(self,item,record,analysis,sources,add):
        from company_names import analysis_profile, identity_names
        from judicial.company import normalize_name
        profile=record.get('company_name_profile') or (item.source_document or {}).get('company_name_profile')
        target=item.manifest.get('company_name') or (item.source_document or {}).get('company_name')
        if item.module=='mops':
            company=(item.source_document or {}).get('company',{})
            target=company.get('company_name')
            add('L2','company_identity','pass' if analysis.get('company_id')==company.get('company_code')==record.get('company_code') else 'fail',
                'MOPS target code, listed announcement code and analysis code compared; API archive also checked')
            return
        if not target and isinstance(profile,dict):
            target=profile.get('input_company')
        if not target:
            add('L2','company_identity','unverifiable','No independent requested company metadata; AI company name cannot establish target identity')
            return
        add('L2','target_company','pass' if analysis.get('company_name')==target else 'fail','Analysis company name compared with original requested company')
        profile=analysis_profile(target,item.manifest.get('aliases',[]),supplied=profile)
        trusted=identity_names(profile)
        candidates=profile.get('search_names',[])
        hits=[n for n in trusted if any(normalize_name(n) in normalize_name(s) for s in sources)]
        aliases=[n for n in candidates if any(normalize_name(n) in normalize_name(s) for s in sources)]
        add('L2','company_name_match','pass' if hits else 'unverifiable',
            'Name matching is a candidate check, not proof of party role or liability',trusted_name_hits=hits,candidate_name_hits=aliases)
        official=profile.get('official_match') or {}
        target_number=official.get('uniform_number')
        raw_number=(record.get('raw_record') or {}).get('uniform_number')
        if target_number and raw_number:
            add('L2','uniform_number','pass' if target_number==raw_number else 'fail','Source uniform number compared with company directory')
        else:
            add('L2','uniform_number','unverifiable','Both target and source uniform numbers unavailable; name match alone cannot prove legal identity')

    def dates(self,item,record,analysis,add):
        value=None
        years=None
        if item.module=='news':
            value=record.get('published_at')
            years=2
        elif item.module=='judicial':
            value=analysis.get('judgment_date') if isinstance(analysis,dict) else None
            years=2
        elif item.module=='mops':
            value=record.get('announcement_date')
            years=2
        elif item.module=='environment':
            years=2
            from regulatory.csv_source import parse_record_date
            try:
                value=parse_record_date(record.get('raw_record',{}).get('date',''))
            except ValueError:
                pass
        else:
            from credit_risk.date_scope import record_day
            day,basis=record_day('sfb',record,allowed_roots=[self.workspace,item.analysis_path.parent])
            value=day.isoformat() if day else None
            years=2
            stated=(analysis.get('source_details') or {}).get('publication_date') if isinstance(analysis,dict) else None
            if day and basis=='official_page_date' and stated is not None:
                add('L2','sfb_publication_date','pass' if stated==day.isoformat() else 'fail',
                    'Publication date compared with official page metadata, not disposition or cited-case dates')
        try:
            if not value:
                raise ValueError('Publication/judgment date absent; not replaced by crawl or event date')
            if item.module=='news':
                stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
                if stamp.tzinfo is None:
                    raise ValueError('News timestamp has no timezone')
                day=stamp.astimezone(timezone(timedelta(hours=8))).date()
            else:
                day=date.fromisoformat(value)
            oldest=None
            if years and not self.historical:
                try:
                    oldest=self.as_of.replace(year=self.as_of.year-years)
                except ValueError:
                    oldest=self.as_of.replace(year=self.as_of.year-years,day=28)
            okay=day<=self.as_of and (oldest is None or day>=oldest)
            add('L2','source_date','pass' if okay else 'fail','Source date and requested age window',
                source_date=day.isoformat(),as_of=self.as_of.isoformat(),oldest=oldest.isoformat() if oldest else None)
        except (ValueError,TypeError,AttributeError) as exc:
            add('L2','source_date','unverifiable',str(exc))

    def finish(self,result,analysis,archives):
        checks=result['checks']
        flags=review_flags(analysis)+result.get('effective_review_flags',[])
        risks=[]
        def scan(value):
            if isinstance(value,dict):
                for k,v in value.items():
                    if k in {'severity','overall_severity','risk_level'} and v in ('high','高','uncertain','待確認'):
                        risks.append(v)
                    if isinstance(v,(dict,list)):
                        scan(v)
            elif isinstance(value,list):
                for v in value:
                    scan(v)
        scan(analysis)
        reasons=[]
        if flags:
            reasons.append('AI or module validation requests manual review')
        if risks:
            reasons.append('High risk or uncertainty requires manual review')
        if any(c['status']=='unverifiable' for c in checks):
            reasons.append('One or more checks could not be completed')
        if any(c['status']=='fail' for c in checks):
            reasons.append('Validation failure: quarantine analysis pending correction/review')
        checks.append(dict(level='L5',name='manual_review',status='unverifiable' if reasons else 'pass',
                           detail='Human review is required before acceptance' if reasons else 'No additional review triggers',reasons=reasons))
        result.update(verdict='failed' if any(c['status']=='fail' for c in checks) else 'needs_review' if reasons else 'passed',
                      review_required=bool(reasons),manual_review_reasons=reasons,archives=archives)
        result['levels']={level:('fail' if any(c['status']=='fail' for c in checks if c['level']==level)
                            else 'unverifiable' if any(c['status']=='unverifiable' for c in checks if c['level']==level)
                            else 'pass') for level in ('L1','L2','L3','L4','L5')}
        return result
