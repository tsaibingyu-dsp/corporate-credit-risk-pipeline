import hashlib
import json
import re
import unicodedata
from datetime import date
from urllib.parse import parse_qs

PAGE_URL = 'https://mops.twse.com.tw/mops/#/web/t05st01'
API_BASE = 'https://mops.twse.com.tw/mops/api/'


def normalize(value):
    return ''.join(unicodedata.normalize('NFKC', value).replace('臺', '台').split()).casefold()


def parse_year(value):
    text = str(value).strip()
    if not re.fullmatch(r'\d{1,4}', text):
        raise ValueError('年度請輸入民國年或西元年，例如 115 或 2026')
    year = int(text)
    roc = year - 1911 if year >= 1912 else year
    if not 1 <= roc <= date.today().year - 1911:
        raise ValueError('年度必須為有效的過去年度或本年度')
    return roc, roc + 1911


def iso_date(value):
    match = re.fullmatch(r'(\d{2,3})/(\d{1,2})/(\d{1,2})', str(value).strip())
    if not match:
        raise ValueError(f'MOPS 日期格式異常：{value}')
    year, month, day = map(int, match.groups())
    return date(year + 1911, month, day).isoformat()


def result_of(payload):
    if not isinstance(payload, dict) or payload.get('code') != 200:
        message = payload.get('message', '回應不是成功 JSON') if isinstance(payload, dict) else '回應格式異常'
        raise ValueError(f'MOPS 查詢未成功：{message}')
    result = payload.get('result')
    if not isinstance(result, dict):
        raise ValueError('MOPS 回應缺少 result 物件；不可視為零筆')
    return result


def resolve_company(payload, query):
    result = result_of(payload)
    if not isinstance(result.get('companyList'), list):
        raise ValueError('找不到公司清單；請使用上市櫃公司名稱或股票代號')
    candidates = {}
    for group in result['companyList']:
        market = group.get('title', '')
        if not (market.startswith('上市') or market.startswith('上櫃')):
            continue
        for item in group.get('data', []):
            # MOPS puts routing parameters after '#' in its SPA links.
            code = parse_qs(item.get('url', '').partition('?')[2]).get('companyId', [''])[0]
            label = item.get('result', '')
            if not re.fullmatch(r'\d{4,6}', code):
                continue
            name = re.sub(r'^' + re.escape(code) + r'\s*', '', label).strip()
            candidates[code] = dict(company_code=code, company_name=name, market=market)
    exact = [c for c in candidates.values()
             if normalize(query) in (normalize(c['company_code']), normalize(c['company_name']))]
    choices = exact or list(candidates.values())
    if len(choices) != 1:
        labels = '、'.join(f"{c['company_code']} {c['company_name']}" for c in choices)
        raise ValueError('公司名稱無法唯一識別，請改用股票代號。候選：' + (labels or '無上市櫃公司'))
    company = choices[0]
    if query.isdigit() and company['company_code'] != query:
        raise ValueError('股票代號與回傳公司不一致')
    return company


def parse_listing(payload, company_code, roc_year):
    result = result_of(payload)
    if str(result.get('companyId')) != company_code:
        raise ValueError('年度清單公司代號與目標不一致')
    if not isinstance(result.get('data'), list):
        raise ValueError('年度清單缺少 data，不可視為零筆')
    rows, seen = [], {}
    for row in result['data']:
        if not isinstance(row, list) or len(row) != 6 or not isinstance(row[5], dict):
            raise ValueError('年度清單欄位已變更，停止避免漏抓')
        code, name, announced, clock, title, detail = row
        params = detail.get('parameters', {})
        if (str(code) != company_code or detail.get('apiName') != 't05st01_detail'
                or set(params) != {'serialNumber', 'enterDate', 'marketKind', 'companyId'}
                or str(params['companyId']) != company_code or params['marketKind'] not in ('sii', 'otc')):
            raise ValueError('公告明細參數與上市櫃目標不一致')
        day = iso_date(announced)
        entry = str(params['enterDate'])
        if (date.fromisoformat(day).year != roc_year + 1911
                or not re.fullmatch(r'\d{7}', entry)
                or not str(params['serialNumber']).isdigit()):
            raise ValueError('公告年度、日期或序號與查詢不一致')
        # enterDate is the official lookup key, and can differ from the displayed
        # announcement date (observed on OTC announcements). Do not reconstruct it.
        iso_date(f'{entry[:3]}/{entry[3:5]}/{entry[5:]}')
        identity = json.dumps(params, sort_keys=True)
        item = dict(announcement_id=hashlib.sha256(identity.encode()).hexdigest(),
                    company_code=company_code, company_name=str(name).strip(),
                    announcement_date=day, announcement_time=str(clock).strip(),
                    title=clean_text(title), serial_number=str(params['serialNumber']),
                    detail_parameters=params, status='pending', error=None)
        if identity in seen:
            if seen[identity] != item:
                raise ValueError('同公告識別碼對應不同內容，需人工確認')
            continue
        seen[identity] = item
        rows.append(item)
    for key in ('total', 'totalCount'):
        if key in result and int(result[key]) > len(result['data']):
            raise ValueError('網站回傳分頁清單，尚未取得所有頁；停止避免漏抓')
    return rows


def clean_text(value):
    if not isinstance(value, str):
        raise ValueError('公告文字欄位格式異常')
    return value.replace('\\r\\n', '\n').replace('\r\n', '\n').replace('\r', '\n').strip()


def parse_detail(payload, item):
    result = result_of(payload)
    if str(result.get('companyId')) != item['company_code']:
        raise ValueError('全文回傳其他公司')
    rows, titles = result.get('data'), result.get('titles')
    expected = ['序號', '發言日期', '發言時間', '發言人', '發言人職稱', '發言人電話', '主旨', '符合條款', '事實發生日', '說明']
    if (not isinstance(titles, list) or [t.get('main') for t in titles] != expected
            or not isinstance(rows, list) or len(rows) != 1 or len(rows[0]) != len(expected)):
        raise ValueError('全文欄位格式已變更或不是單筆明細')
    fields = dict(zip(expected, map(clean_text, rows[0])))
    if (fields['序號'] != item['serial_number'] or iso_date(fields['發言日期']) != item['announcement_date']
            or fields['發言時間'] != item['announcement_time'] or fields['主旨'] != item['title']):
        raise ValueError('全文公告序號、日期、時間或主旨與清單不符')
    if not fields['說明']:
        raise ValueError('明細未包含說明全文，不可標為成功')
    # Keep the source occurrence date verbatim too; irregular/unknown dates are not invented.
    try:
        event_date = iso_date(fields['事實發生日'])
    except ValueError:
        event_date = None
    full_text = '\n'.join(f'{label}：{text}' for label, text in fields.items())
    return dict(content=fields['說明'], full_text=full_text, source_fields=fields,
                event_date=event_date, event_date_original=fields['事實發生日'],
                applicable_clause=fields['符合條款'], spokesperson=fields['發言人'],
                spokesperson_title=fields['發言人職稱'], spokesperson_phone=fields['發言人電話'],
                full_text_sha256=hashlib.sha256(full_text.encode('utf-8')).hexdigest())
