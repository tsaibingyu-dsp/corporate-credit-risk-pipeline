"""Google News RSS search, using the public feed URL supplied by the user."""
import re
import time
import xml.etree.ElementTree as ET
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from .sources import NewsBlocked, article_record, raw_file
from .screening import news_window

MAX_FEED_BYTES = 2 * 1024 * 1024


def build_search_url(company, start=None, end=None):
    start, end, _ = news_window(start.isoformat() if start else None,
                               end.isoformat() if end else None)
    # Encode the entire query so company names containing &, + or spaces stay intact.
    query = '"' + company.replace('"', ' ').strip() + '"'
    if start:
        # Search a slightly wider window; local Taiwan-date filtering is authoritative.
        query += f' after:{(start - timedelta(days=1)).isoformat()}'
    if end:
        query += f' before:{(end + timedelta(days=1)).isoformat()}'
    return 'https://news.google.com/rss/search?' + urlencode(
        dict(q=query, hl='zh-TW', gl='TW', ceid='TW:zh-Hant'))


def parse_feed(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('RSS 不接受 DTD／實體宣告')
    root = ET.fromstring(data)
    channel = root.find('channel') if root.tag == 'rss' else None
    if channel is None:
        raise ValueError('回應不是有效的 RSS channel，不視為零筆')
    entries = []
    for item in channel.findall('item'):
        title = (item.findtext('title') or '').strip()
        link = (item.findtext('link') or '').strip()
        if not title or not link or urlparse(link).scheme not in ('https', 'http'):
            raise ValueError('RSS 項目缺少標題或有效連結')
        source = item.find('source')
        publisher = (source.text or '').strip() if source is not None else ''
        if publisher and title.endswith(' - ' + publisher):
            title = title[:-(len(publisher) + 3)].strip()
        description = BeautifulSoup(item.findtext('description') or '', 'html.parser')
        for tag in description.select('script,style'):
            tag.decompose()
        stamp = (item.findtext('pubDate') or '').strip()
        try:
            parsed = parsedate_to_datetime(stamp)
            # RFC 822 feeds normally supply GMT; don't guess if the timezone is absent.
            published_at = parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
        except (ValueError, TypeError, OverflowError):
            published_at = None
        entries.append(dict(url=link, title=title, text=description.get_text('\n', strip=True),
                            published_at=published_at, publisher=publisher,
                            rss_metadata=dict(guid=item.findtext('guid'), pub_date_original=stamp,
                                              publisher_url=source.get('url') if source is not None else None)))
    return entries


def collect_rss(query, limit, delay, root, report, start, end):
    if limit is None:
        yield from collect_all_rss(query,delay,root,report,start,end)
        return
    url = build_search_url(query, start, end)
    report.update(search_url=url, transport='rss', coverage='rss_response_only', pages=1)
    try:
        time.sleep(delay)
        request = Request(url, headers={'Accept': 'application/rss+xml, application/xml, text/xml'})
        with urlopen(request, timeout=45) as response:
            data = response.read(MAX_FEED_BYTES + 1)
            final_url = response.geturl()
        if len(data) > MAX_FEED_BYTES:
            raise ValueError('RSS 超過 2 MiB 安全上限，未接受部分內容')
        raw = raw_file(root, data, suffix='.xml')
        if (urlparse(final_url).hostname != 'news.google.com' or '/sorry/' in final_url
                or re.search(rb'<!doctype\s+html|<html', data[:1000], re.I)):
            raise NewsBlocked('Google RSS 回傳驗證／HTML 或非預期轉址，未視為無新聞')
        entries = parse_feed(data)
        report['loaded_results'] = len(entries)
        report['raw_path'] = raw
        for entry in entries[:limit]:
            metadata = entry.pop('rss_metadata')
            record = article_record('google_news', scope='rss_item', raw=raw, search_url=url, **entry)
            record['rss_metadata'] = metadata
            report['collected'] += 1
            yield record
        report['state'] = 'limited' if len(entries) > limit else 'complete'
    except Exception as error:
        if isinstance(error, HTTPError):
            raw_file(root, error.read(MAX_FEED_BYTES), suffix='.rss-error')
        report['state'] = 'blocked' if isinstance(error, NewsBlocked) or isinstance(error, HTTPError) and error.code in (403, 429) else 'failed'
        report['errors'].append(dict(error=str(error)))
        raise


def collect_all_rss(query, delay, root, report, start, end):
    """Scan monthly windows, splitting saturated feeds to a single day at most."""
    from .screening import publication_date
    report.update(transport='rss',coverage='date_partitioned_rss_responses_not_all_media_history',
                  search_url=build_search_url(query,start,end),pages=0,windows=[])
    seen=set()
    saturated=False
    def scan(left,right):
        nonlocal saturated
        print(f'RSS WINDOW {query} {left} .. {right}',flush=True)
        child=dict(state='running',errors=[],collected=0,pages=0)
        try:
            records=list(collect_rss(query,1000000,delay,root,child,left,right))
        except Exception:
            report['windows'].append(dict(start=left.isoformat(),end=right.isoformat(),**child))
            report['errors'].extend(child['errors'])
            report['state']=child['state']
            raise
        report['pages']+=1
        report['windows'].append(dict(start=left.isoformat(),end=right.isoformat(),**child))
        if child['loaded_results']>=100 and left<right:
            midpoint=left+(right-left)//2
            yield from scan(left,midpoint)
            yield from scan(midpoint+timedelta(days=1),right)
            return
        if child['loaded_results']>=100:
            saturated=True
            report.setdefault('saturated_days',[]).append(left.isoformat())
        for record in records:
            day=publication_date(record.get('published_at'))
            if day is not None and not left<=day<=right:
                continue
            key=record['article_id']
            if key in seen:
                continue
            seen.add(key)
            report['collected']+=1
            yield record
    cursor=start
    while cursor<=end:
        following=(cursor.replace(day=28)+timedelta(days=4)).replace(day=1)
        finish=min(end,following-timedelta(days=1))
        yield from scan(cursor,finish)
        cursor=finish+timedelta(days=1)
    report['state']='limited' if saturated else 'complete'
