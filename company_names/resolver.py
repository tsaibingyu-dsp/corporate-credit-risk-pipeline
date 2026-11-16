import hashlib
import copy
import json
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

VERSION = 'company-names-v1'
CACHE = Path(__file__).resolve().parents[1] / 'data/company_names/directory.json'
SOURCES = {
    'twse': 'https://openapi.twse.com.tw/v1/opendata/t187ap03_L',
    'tpex': 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O',
}
LEGAL_SUFFIXES = ('股份有限公司', '股份有限責任公司', '有限責任公司', '有限公司', '有限合夥', '股份公司', '公司')
INDUSTRIES = ('積體電路製造', '半導體製造', '證券投資信託', '綜合證券', '商業銀行', '金融控股', '環境科技', '環境工程', '生物科技',
              '電子工業', '光電科技', '精密科技', '半導體',
              '資訊服務', '精密工業', '建設開發', '投資控股', '建設', '營造', '開發', '證券', '銀行', '金控',
              '科技', '資訊', '電子', '電機', '工業', '實業', '企業', '海運', '航空', '運輸', '電信', '生技',
              '食品', '化學', '化工', '工程', '投資')
GENERIC = {'台灣', '中國', '中華', '國際', '全球', '亞洲', '第一', '中央', '統一', '聯合', '科技', '建設',
           '證券', '銀行', '企業', '工業', '資訊', '有限公司', '股份有限公司'}

NAME_MATCH_PROMPT = '''
【公司名稱擴充與身分限制】
company_name_profile 記錄原查詢名稱、人工別名、自動候選名稱、產生規則及官方公司簡稱對照。
自動規則名稱僅用於搜尋召回，不是同一法人的證據；不可因為簡稱、品牌或字面相似，就把子公司、同名公司或第三方事件歸責目標企業。
official_match 的唯一全名／簡稱對照可協助識別名稱，但不是事件歸屬、統編身分驗證或目標公司受罰的證據。
candidate_aliases 是自動產生的候選名稱，不是經使用者核實的法人別名。無法確認時保留待確認／possible／candidate／insufficient_evidence 並要求人工覆核。
recognized_company_names 或 name_verified_match 只允許正式全名及唯一、未過期的官方對照名稱，不能把一般刪字規則的簡稱當作已確認身分；official_mapping_stale=true 時官方舊對照也只作候選。
只根據原始事件資料判斷事件主體與直接／間接角色；名稱對照表不得作為事件原文證據。
'''


def normalize(value):
    return ''.join(unicodedata.normalize('NFKC', value).replace('臺', '台').split()).casefold()


def parse_directory(rows, market, source_url):
    if not isinstance(rows, list) or not rows:
        raise ValueError('官方公司清單不是非空陣列')
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('官方公司基本資料格式已變更')
        code = row.get('公司代號', row.get('SecuritiesCompanyCode'))
        full = row.get('公司名稱', row.get('CompanyName'))
        short = row.get('公司簡稱', row.get('CompanyAbbreviation'))
        if not all(isinstance(v, str) and v.strip() for v in (code, full, short)):
            raise ValueError('官方公司清單缺少代號、全名或簡稱')
        result.append(dict(code=code.strip(), full_name=full.strip(), short_name=short.strip(), market=market, source_url=source_url))
    return result


def read_cache(path=CACHE):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('version') != VERSION or not isinstance(data.get('companies'), list):
            return None
        if datetime.fromisoformat(data['fetched_at']).tzinfo is None:
            return None
        for item in data['companies']:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k] for k in ('code','full_name','short_name','source_url')):
                return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def fetch_json(url):
    request = Request(url, headers={'User-Agent':'CorporateRiskResearch/1.0', 'Accept':'application/json'})
    with urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise ValueError(f'HTTP {response.status}')
        raw = response.read(12_000_001)
    if len(raw) > 12_000_000:
        raise ValueError('官方名稱清單回應超過大小上限')
    return json.loads(raw.decode('utf-8-sig')), hashlib.sha256(raw).hexdigest()


def directory(path=CACHE, *, fetcher=fetch_json, now=None):
    path = Path(path)
    now = now or datetime.now(timezone.utc)
    old = read_cache(path)
    if old:
        try:
            if timedelta(0) <= now-datetime.fromisoformat(old['fetched_at']) < timedelta(days=1):
                return old
        except (ValueError, KeyError, TypeError):
            pass
    companies, sources, errors = [], [], []
    for market, url in SOURCES.items():
        try:
            rows, digest = fetcher(url)
            companies.extend(parse_directory(rows, market, url))
            sources.append(dict(url=url, body_sha256=digest, status='success'))
        except Exception as error:
            # Do not print arbitrary network exceptions or credential-bearing URLs.
            errors.append(dict(url=url, error=type(error).__name__))
            if old:
                companies.extend(c for c in old['companies'] if c['source_url'] == url)
    data = dict(version=VERSION, fetched_at=now.isoformat(), companies=companies, sources=sources, errors=errors,
                stale_cache_fetched_at=old.get('fetched_at') if errors and old else None)
    # Failed refreshes do not extend the age of old data or erase a good cache.
    if not errors:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.json.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)
    return data


def build_profile(company, aliases=(), *, official=None, enabled=True):
    company = company.strip()
    entries, indexed, skipped = [], {}, []
    def add(name, origin, automatic=True):
        name = name.strip()
        key = normalize(name)
        if not key:
            return
        if automatic and (key in GENERIC or len(key) < (2 if origin.startswith('official_') else 3)):
            skipped.append(dict(name=name, reason='too_short_or_generic'))
            return
        if key in indexed:
            if origin not in indexed[key]['origins']:
                indexed[key]['origins'].append(origin)
            return
        item = dict(name=name, origins=[origin], candidate_only=origin not in ('input_full_name',))
        entries.append(item)
        indexed[key] = item
    add(company, 'input_full_name', False)
    for alias in aliases:
        add(alias, 'user_alias', False)
    matches = []
    if enabled:
        matches = [row for row in (official or {}).get('companies', [])
                   if normalize(company) in {normalize(row['full_name']), normalize(row['short_name']), row['code']}]
        unique = {(r['market'], r['code']):r for r in matches}
        matches = list(unique.values())
        if len(matches) == 1:
            add(matches[0]['full_name'], 'official_full_name')
            add(matches[0]['short_name'], 'official_short_name')
        bases = [company, *([matches[0]['full_name']] if len(matches)==1 else [])]
        generated = []
        for full in bases:
            stem = full
            for suffix in LEGAL_SUFFIXES:
                if stem.endswith(suffix):
                    stem = stem[:-len(suffix)].strip()
                    break
            if stem != full:
                add(stem, 'legal_suffix_removed')
            for long, short in [('綜合證券','證券'),('商業銀行','銀行'),('金融控股','金控')]:
                if stem.endswith(long):
                    generated.append((stem[:-len(long)]+short, 'sector_abbreviation'))
            for suffix in INDUSTRIES:
                if stem.endswith(suffix):
                    generated.append((stem[:-len(suffix)], 'industry_suffix_removed'))
                    break
        for name, origin in generated:
            if sum(not any(o in ('input_full_name','user_alias') for o in e['origins']) for e in entries) >= 5:
                break
            add(name, origin)
    auto = [entry['name'] for entry in entries if not any(o in ('input_full_name','user_alias') for o in entry['origins'])]
    return dict(version=VERSION, input_company=company, user_aliases=list(aliases), automatic_enabled=enabled,
                search_names=[e['name'] for e in entries], candidate_aliases=auto, entries=entries, skipped=skipped,
                official_match=matches[0] if len(matches)==1 else None,
                official_ambiguous=matches if len(matches)>1 else [],
                directory_fetched_at=(official or {}).get('fetched_at'), directory_errors=(official or {}).get('errors', []),
                directory_stale_cache_at=(official or {}).get('stale_cache_fetched_at'),
                identity_verified=False)


def resolve_profile(company, aliases=(), *, enabled=True, cache_path=CACHE, fetcher=fetch_json):
    official = directory(cache_path, fetcher=fetcher) if enabled else None
    return build_profile(company, aliases, official=official, enabled=enabled)


def analysis_profile(company, aliases=(), *, supplied=None):
    if supplied and normalize(supplied.get('input_company','')) == normalize(company):
        profile = copy.deepcopy(supplied)
    else:
        profile = build_profile(company, aliases, official=read_cache())
    if profile.get('directory_fetched_at'):
        try:
            age = datetime.now(timezone.utc)-datetime.fromisoformat(profile['directory_fetched_at'])
            if not timedelta(0) <= age < timedelta(days=1):
                profile['directory_stale_cache_at'] = profile['directory_fetched_at']
        except (ValueError, TypeError):
            profile['directory_stale_cache_at'] = profile['directory_fetched_at']
    return profile


def identity_names(profile):
    if profile.get('official_ambiguous'):
        return []
    names = [profile['input_company']]
    if profile.get('official_match') and not (profile.get('directory_stale_cache_at') or profile.get('official_mapping_stale')):
        names += [profile['official_match']['full_name'], profile['official_match']['short_name']]
    return list(dict.fromkeys(names))


def model_profile(profile):
    # Refresh timestamps are audit metadata, not a reason to pay for new AI analysis.
    return {key:profile[key] for key in ('version','input_company','user_aliases','automatic_enabled',
        'search_names','candidate_aliases','entries','official_match','official_ambiguous','identity_verified')} | {
        'official_mapping_stale':bool(profile.get('directory_stale_cache_at'))}
