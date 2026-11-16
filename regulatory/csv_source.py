"""User-supplied environmental penalty snapshot; no browser or AI required."""
import csv
import hashlib
import io
from datetime import date
from pathlib import Path

from judicial.company import normalize_name
from judicial.extractor import validate_evidence
from .common import Notice, record_text, save_raw
from .schema import RegulatoryNotice

DEFAULT_CSV = Path('data/environment_imports')
DOWNLOAD_COLUMNS = ['裁處書字號','主旨','裁處理由及法令','裁處金額','裁處時間','裁處機關',
    '改善完妥與否','限期改善日期','複查稽查單稽查日期','複查稽查單稽查結果','污染類別',
    '違反法令','違反事實','違反地址','管制編號','事業名稱','統一編號','公司（工廠）地址',
    '違規人名稱','是否訴願訴訟','訴願提出日期','訴願訴訟結果','陳情意見結果','審議機關',
    '不法利得','水污追繳不法利得','情節重大','其他處罰方式','是否減免罰鍰','罰鍰是否繳清',
    '是否進行催繳','是否行政移送','核准復工日期']
COLUMNS = ['no', 'name', 'date', 'case', 'fact', 'low', 'fine',
           'appeal', 'result', 'restricted_date', 'improve']
MAPPING_VERSION = 'prtr-csv-v3-download'


def parse_record_date(value):
    value = value.strip()
    if '/' in value:
        parts=value.split('/')
        if len(parts)==3 and all(p.isdigit() for p in parts):
            year,month,day=map(int,parts)
            if year<1911: year+=1911
            return date(year,month,day).isoformat()
    if len(value) == 7 and value.isdigit():
        return date(int(value[:3]) + 1911, int(value[3:5]), int(value[5:])).isoformat()
    return date.fromisoformat(value).isoformat()


def bounds(start, end):
    begin = date.fromisoformat(start).isoformat() if start else None
    finish = date.fromisoformat(end).isoformat() if end else None
    if begin and finish and begin > finish:
        raise ValueError('CSV 日期區間不得倒置')
    return begin, finish


def empty_fact():
    return dict(value=None, status='not_stated', quotes=[])


def fact(value, *, normalized=None):
    if not value.strip():
        return empty_fact()
    return dict(value=normalized if normalized is not None else value, status='stated', quotes=[value])


def date_fact(value):
    if not value.strip():
        return empty_fact()
    try:
        return fact(value, normalized=parse_record_date(value))
    except ValueError:
        return dict(value=value, status='uncertain', quotes=[value])


def map_record(row):
    data = {key: empty_fact() for key in [
        'authority', 'title', 'document_number', 'published_date', 'disposition_date',
        'event_date', 'notice_type', 'disposition', 'remediation',
        'appeal_or_revocation', 'finality']}
    data.update(entities=[], facts=[], legal_provisions=[], amounts=[], limitations=[
        '來源為使用者提供的 CSV 快照；尚未核對原始下載網址及資料集完整性',
        'no 是檔內編號，不當作裁處書文號；date 保留來源日期，restricted_date 意義未經欄位定義確認，不當作改善期限或停工日期',
        'CSV 未附統編、主管機關、原始裁處書網址及金額幣別／單位，不自行補造',
        'appeal 否不代表處分已確定，improve 是保留為檔案記載，未查核目前狀態'])
    data['title'] = fact(row['case'])
    data['disposition_date'] = date_fact(row['date'])
    if row['name'].strip():
        data['entities'].append(dict(name=fact(row['name']), uniform_number=empty_fact(),
                                     role=empty_fact(), entity_type='unknown'))
    if row['fact'].strip():
        data['facts'].append(fact(row['fact']))
    if row['low'].strip():
        data['legal_provisions'].append(fact(row['low']))
    if row['fine'].strip():
        data['amounts'].append(dict(amount_original=row['fine'], currency=None,
                                   subject=row['name'] or None, purpose='fine', quotes=[row['fine']]))
    remediation = [(label, row[key]) for label, key in [('改善欄位', 'improve'), ('restricted_date 原欄位', 'restricted_date')]]
    appeal = [(label, row[key]) for label, key in [('訴願欄位', 'appeal'), ('結果欄位', 'result')]]
    for key, values in [('remediation', remediation), ('appeal_or_revocation', appeal)]:
        present = [(label, value) for label, value in values if value.strip()]
        if present:
            data[key] = dict(value='；'.join(f'{label}：{value}' for label, value in present),
                             status='stated', quotes=[value for _, value in present])
    result = RegulatoryNotice.model_validate(data)
    validate_evidence(result, record_text(row))
    return result.model_dump()


def normalize_download_row(row):
    row=dict(row)
    if not set(DOWNLOAD_COLUMNS).issubset(row): return row
    def value(key):
        text=row[key]
        return '' if text.strip() in ('-','--','') else text
    row.update(no=value('裁處書字號'),name=value('違規人名稱') or value('事業名稱'),
        date=value('裁處時間'),case=value('主旨'),fact=value('違反事實'),low=value('違反法令'),
        fine=value('裁處金額'),appeal=value('是否訴願訴訟'),result=value('訴願訴訟結果'),
        restricted_date=value('限期改善日期'),improve=value('改善完妥與否'))
    return row


def load_snapshot(path):
    path=Path(path)
    raw = path.read_bytes()
    for encoding in ('utf-8-sig', 'cp950'):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('CSV 編碼不支援，請使用 UTF-8 或 CP950')
    reader = csv.DictReader(io.StringIO(text, newline=''))
    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError('CSV 缺少標題或含重複欄名')
    native=set(DOWNLOAD_COLUMNS).issubset(reader.fieldnames)
    missing = (set() if native else set(COLUMNS) - set(reader.fieldnames))
    if missing:
        raise ValueError('CSV 缺少欄位：' + ', '.join(sorted(missing)))
    rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError('CSV 欄位數不一致，請檢查引號與分隔符號')
    if native:
        rows=[normalize_download_row(row) for row in rows]
    return raw, rows, encoding


def snapshot_paths(path=DEFAULT_CSV):
    path=Path(path)
    if path.is_file(): return [path]
    files=sorted(path.glob('*.csv')) if path.is_dir() else []
    if not files:
        raise ValueError(f'環境部匯入資料夾沒有 CSV：{path}；請放入人工下載的 CSV，檔名不限')
    return files


def collect_inputs(path,query,limit,root,report,start=None,end=None):
    from .common import Progress
    reports=[]
    for file in snapshot_paths(path):
        child=Progress('prtr',query,file.resolve().as_uri())
        yield from collect_csv(file,query,limit,root,child,start,end)
        reports.append(child)
        report.errors.extend(child.errors)
    report.entry_url=Path(path).resolve().as_uri()
    report.collected=sum(r.collected for r in reports)
    report.total_reported=sum(r.total_reported or 0 for r in reports)
    report.pages=len(reports)
    report.provenance=dict(mode='local_csv_folder',files=[r.provenance for r in reports])
    report.date_scope=dict(start=start,end=end,field='date',scope='supplied_csv_only')
    report.state='limited' if any(r.state!='complete' for r in reports) else 'complete'


def collect_csv(path, query, limit, root, report, start=None, end=None):
    path = Path(path).resolve()
    raw, rows, encoding = load_snapshot(path)
    original = save_raw(root, raw, '.csv')
    digest = hashlib.sha256(raw).hexdigest()
    report.entry_url = path.as_uri()
    report.date_scope = dict(start=start, end=end, field='date', scope='supplied_csv_only')
    dates, selected = [], []
    for index, row in enumerate(rows, start=2):
        try:
            row_date = parse_record_date(row['date'])
            dates.append(row_date)
        except ValueError:
            row_date = None
        if normalize_name(query) not in normalize_name(row['name']):
            continue
        if row_date is None and (start or end):
            report.errors.append(dict(csv_row=index, error='處分日無法解析，無法判斷是否符合日期範圍'))
            continue
        if start and row_date < start or end and row_date > end:
            continue
        selected.append((index, row))
    metadata = dict(mode='local_csv', provenance_status='user_supplied_not_independently_verified',
                    file_path=str(path), file_sha256=digest, encoding=encoding,
                    file_row_count=len(rows), matched_row_count=len(selected),
                    observed_date_min=min(dates) if dates else None,
                    observed_date_max=max(dates) if dates else None,
                    official_reference_url='https://prtr.moenv.gov.tw/index.html',
                    original_download_url=None, mapping_version=MAPPING_VERSION,
                    limitations=['查無匹配僅代表這份快照內沒有，不能推論目前或其他環保機關無裁罰',
                                 '檔名日期不當作資料更新日；日期範圍依檔案實際紀錄'])
    report.provenance, report.total_reported, report.pages = metadata, len(selected), 1
    for index, row in selected[:limit]:
        report.collected += 1
        yield Notice('prtr', path.as_uri() + f'#row={index}', path.as_uri(), row['case'],
                     record_text(row), 'provided_penalty_csv_record', [original],
                     limitations=metadata['limitations'],
                     record_locator=dict(csv_row=index, no=row['no'], file_sha256=digest),
                     structured=map_record(row), provenance=metadata, raw_record=row)
    report.state = 'limited' if len(selected) > limit else 'complete'
