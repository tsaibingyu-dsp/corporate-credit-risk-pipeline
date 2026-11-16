import csv
import hashlib
import io
import re
import unicodedata
from datetime import date, datetime, timezone, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from company_names import analysis_profile, identity_names

TAIPEI = timezone(timedelta(hours=8))
SCREEN_VERSION = 'news-screen-v4-two-years'


def news_window(start=None, end=None, as_of=None):
    from credit_risk.date_scope import window,metadata
    begin,finish=window(start,end,as_of=as_of)
    return begin,finish,metadata(begin,finish,as_of)


def review_route(screening):
    if screening['passed']:
        return dict(priority='high', route='high_priority_ai', eligible=True)
    if screening['reason'] == 'no_keyword':
        return dict(priority='low', route='audit_pool', eligible=True)
    return dict(priority='none', route='excluded', eligible=False)


def audit_order(company, article_id, seed):
    return hashlib.sha256((company + '\0' + article_id + '\0' + seed).encode()).hexdigest()


def second_round_sample_size(pool_size):
    # Fixed 20%, rounded upward; an empty pool never triggers an AI call.
    return (pool_size + 4) // 5


def normalize_with_positions(text):
    chars, positions = [], []
    for index, character in enumerate(text):
        for value in unicodedata.normalize('NFKC', character).casefold().replace('臺', '台'):
            if not value.isspace():
                chars.append(value)
                positions.append(index)
    return ''.join(chars), positions


def normalize(text):
    return normalize_with_positions(text)[0]


def occurrences(text, term):
    normalized, positions = normalize_with_positions(text)
    needle = normalize(term)
    if not needle:
        return []
    matches = []
    for match in re.finditer(re.escape(needle), normalized):
        # Avoid e.g. ABC matching XABCY; Chinese names remain substring mentions.
        left, right = match.span()
        if needle[0].isascii() and needle[0].isalnum() and left and re.match(r'[a-z0-9_]', normalized[left-1]):
            continue
        if needle[-1].isascii() and needle[-1].isalnum() and right < len(normalized) and re.match(r'[a-z0-9_]', normalized[right]):
            continue
        start, end = positions[left], positions[right-1] + 1
        matches.append(dict(start=start, end=end, quote=text[start:end],
                            context=text[max(0, start-60):min(len(text), end+60)]))
    return matches


def load_keywords(path):
    raw = path.read_bytes()
    for encoding in ('utf-8-sig', 'cp950'):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('關鍵字 CSV 編碼須為 UTF-8 或 CP950')
    reader = csv.DictReader(io.StringIO(text, newline=''))
    if not reader.fieldnames or '負面關鍵字' not in reader.fieldnames:
        raise ValueError('關鍵字 CSV 缺少「負面關鍵字」欄位')
    entries, seen = [], set()
    for row_number, row in enumerate(reader, 2):
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f'關鍵字 CSV 第 {row_number} 列欄位數異常')
        for word in re.split(r'[、,，;；\r\n]+', row['負面關鍵字']):
            word = word.strip()
            key = (row.get('風險類別', ''), normalize(word))
            if not word or key in seen:
                continue
            seen.add(key)
            entries.append(dict(keyword=word, category=row.get('風險類別', ''),
                                risk_meaning=row.get('授信風險意義', ''), csv_row=row_number))
    if not entries:
        raise ValueError('負面關鍵字清單為空')
    return entries, dict(path=str(path.resolve()), sha256=hashlib.sha256(raw).hexdigest(),
                         encoding=encoding, keyword_count=len(entries))


def publication_date(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TAIPEI)
        return parsed.astimezone(TAIPEI).date()
    except ValueError:
        return None


def canonical_url(url):
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid', 'hl', 'gl', 'ceid')]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(sorted(query)), ''))


def screen(article, company, aliases, keywords, start, end, *, name_profile=None):
    profile = analysis_profile(company, aliases, supplied=name_profile)
    text = article['title'] + '\n' + article['text']
    names = []
    for name in profile['search_names']:
        matches = occurrences(text, name)
        if matches:
            entry = next(e for e in profile['entries'] if e['name']==name)
            names.append(dict(name=name, matches=matches, origins=entry['origins']))
    hits = []
    for entry in keywords:
        matches = occurrences(text, entry['keyword'])
        if matches:
            hits.append(dict(**entry, matches=matches))
    day = publication_date(article.get('published_at'))
    date_status = 'unknown' if day is None else 'in_range' if start <= day <= end else 'out_of_range'
    reason = ('date_unknown' if day is None else 'date_out_of_range' if date_status != 'in_range'
              else 'company_not_found' if not names else 'no_keyword' if not hits else 'passed')
    return dict(passed=reason == 'passed', reason=reason, company_mentions=names,
                company_name_profile=profile,
                strong_name_mention=any(occurrences(text, n) for n in identity_names(profile)),
                keyword_hits=hits, publication_date=day.isoformat() if day else None,
                date_status=date_status, evidence_text=text, identity_verified=False,
                content_scope=article['content_scope'],
                note='字面命中只代表待分析候選；否認、澄清、歷史事件或他人事件仍交 AI 判斷')


def deduplicate(articles, start=None, end=None):
    # Prefer full articles over aggregator cards; preserve every discarded record.
    def preference(article):
        day = publication_date(article.get('published_at'))
        eligible = bool(day and start <= day <= end) if start and end else True
        return not eligible, article['content_scope'] != 'full_article'
    ordered = sorted(articles, key=preference)
    keys, kept, duplicates = {}, [], []
    for article in ordered:
        day = publication_date(article.get('published_at'))
        signatures = [('url', canonical_url(article['url']))]
        text = normalize(article['text'])
        if len(text) >= 100 and article['content_scope'] == 'full_article':
            signatures.append(('body', hashlib.sha256(text.encode()).hexdigest()))
        title, publisher = normalize(article['title']), normalize(article.get('publisher', ''))
        if publisher in ('中央社cna', 'cna'):
            publisher = '中央社'
        if len(title) >= 16 and day and publisher:
            signatures.append(('title_date_publisher', title, day.isoformat(), publisher))
        previous = next((keys[key] for key in signatures if key in keys), None)
        if previous is not None:
            duplicates.append(dict(article_id=article['article_id'], duplicate_of=previous,
                                   source=article['source'], url=article['url'],
                                   raw_path=article.get('raw_path'), title=article['title'],
                                   published_at=article.get('published_at'),
                                   matched_key=next(key[0] for key in signatures if key in keys)))
            for key in signatures:
                keys[key] = previous
            continue
        for key in signatures:
            keys[key] = article['article_id']
        kept.append(article)
    return kept, duplicates
