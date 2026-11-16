"""Shared two-calendar-year source scope. Unknown dates are never guessed."""
import re
from datetime import date,datetime,timedelta,timezone
from pathlib import Path

from judicial.dates import two_year_window

TAIPEI=timezone(timedelta(hours=8))


def window(start=None,end=None,*,as_of=None):
    try:
        return two_year_window(as_of=as_of,start=start,end=end)
    except ValueError as exc:
        raise ValueError(str(exc).replace('裁判日期','來源日期')) from exc


def metadata(start,end,as_of=None):
    return dict(policy='last_2_calendar_years',start=start.isoformat(),end=end.isoformat(),
                as_of=(as_of or datetime.now(TAIPEI).date()).isoformat(),inclusive=True)


def parse_day(value):
    if not isinstance(value,str):
        return None
    text=re.sub(r'\s+','',value)
    try:
        if re.fullmatch(r'\d{7}',text):
            return date(int(text[:3])+1911,int(text[3:5]),int(text[5:]))
        match=re.fullmatch(r'(?:民國\s*)?(\d{2,4})[年/.-]\s*(\d{1,2})[月/.-]\s*(\d{1,2})日?',text)
        if match:
            y,m,d=map(int,match.groups())
            return date(y+1911 if y<1912 else y,m,d)
    except ValueError:
        return None
    return None


def sfb_publication_date(html):
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(html,'html.parser')
    nodes=soup.select('#maincontent .contentdate')
    values=[n.get_text(' ',strip=True) for n in nodes]
    if not values:
        # DC.Date is the official page's dated record, not footer update dates or
        # DC.coverage metadata. Never infer from dataserno or dates of cited cases.
        values=[n.get('content','').split(' ',1)[0] for n in soup.select('meta[name="DC.Date"]')]
    parsed=[parse_day(v) for v in values]
    if not parsed or any(v is None for v in parsed) or len(set(parsed))!=1:
        return None
    return parsed[0]


def record_day(module,record,*,allowed_roots=()):
    if module=='news':
        try:
            value=record.get('published_at','')
            if isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
                return parse_day(value),'explicit_publication_day'
            stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
            return (stamp.astimezone(TAIPEI).date(),'publication_date') if stamp.tzinfo else (None,'publication_date')
        except (ValueError,TypeError,AttributeError):
            return None,'publication_date'
    if module=='mops':
        return parse_day(record.get('announcement_date')),'announcement_date'
    if module=='environment':
        raw=record.get('raw_record') or {}
        if 'date' in raw:
            return parse_day(raw['date']),'csv_date'
        match=re.search(r'(?:處分日期|裁處日期)\s*[:：]\s*((?:民國\s*)?\d{2,4}[年/.-]\s*\d{1,2}[月/.-]\s*\d{1,2}日?)',record.get('full_text',''))
        return (parse_day(match.group(1)) if match else None),'explicit_disposition_date'
    if module=='judicial':
        from judicial.dates import judgment_date
        day=judgment_date(record.get('text',''),is_html=False)
        if not day:
            day=parse_day(record.get('judgment_date'))
        if not day:
            fact=(record.get('extraction') or {}).get('judgment_date',{})
            if fact.get('status')=='stated':
                day=parse_day(fact.get('value'))
        return day,'judgment_date'
    if module=='sfb':
        roots=[Path(p).resolve() for p in allowed_roots]
        for value in record.get('raw_paths',[]):
            path=Path(value).resolve()
            if path.suffix.lower()=='.html' and any(path.is_relative_to(r) for r in roots) and path.is_file() and path.stat().st_size<=30*1024*1024:
                day=sfb_publication_date(path.read_text(encoding='utf-8-sig'))
                if day:
                    return day,'official_page_date'
        raw=record.get('raw_record') or {}
        for key in ('publication_date','published_date','發布日期','發佈日期','disposition_date','處分日期','裁處日期'):
            if key in raw:
                return parse_day(raw[key]),key
        text=record.get('full_text','')
        match=re.search(r'(?:發布日期|發佈日期|處分日期|裁處日期)\s*[:：]\s*((?:民國\s*)?\d{2,4}[年/.-]\s*\d{1,2}[月/.-]\s*\d{1,2}日?)',text)
        return (parse_day(match.group(1)) if match else None),'explicit_labeled_source_date'
    return None,'unknown'


def filter_records(records,module,start,end,*,allowed_roots=(),as_of=None):
    selected=[]
    excluded=[]
    unknown=[]
    for record in records:
        if not isinstance(record,dict):
            unknown.append(dict(source_id=None,date=None,basis='invalid_record',reason='date_unknown'))
            continue
        try:
            day,basis=record_day(module,record,allowed_roots=allowed_roots)
        except (OSError,ValueError,TypeError,AttributeError):
            day,basis=None,'unreadable_date'
        if day and start<=day<=end:
            selected.append(record)
        else:
            entry=dict(source_id=record.get('document_id') or record.get('announcement_id') or record.get('article_id'),
                       date=day.isoformat() if day else None,basis=basis,
                       reason='outside_date_window' if day else 'date_unknown')
            (excluded if day else unknown).append(entry)
    return selected,dict(**metadata(start,end,as_of),eligible_count=len(selected),excluded_count=len(excluded),
                         unknown_date_count=len(unknown),review_required=bool(unknown),excluded=excluded,unknown=unknown)
