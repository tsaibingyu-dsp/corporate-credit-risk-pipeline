import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from .screening import canonical_url, publication_date, news_window

SOURCES = ['google_news', 'cna']


class NewsBlocked(RuntimeError):
    pass


def check_page(page):
    text = page.locator('body').inner_text()
    if ('/sorry/' in page.url or re.search(r'Web Page Blocked|Access Denied|異常流量|流量有異常|驗證您是真人', text, re.I)):
        raise NewsBlocked('網站要求驗證或拒絕目前連線；未視為無新聞，不繞過驗證')


def raw_file(root, html, suffix='.html'):
    data = html.encode('utf-8') if isinstance(html, str) else html
    digest = hashlib.sha256(data).hexdigest()
    path = root / 'raw' / (digest + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path.resolve())


def article_record(source, url, title, text, published_at, publisher, scope, raw, search_url):
    identity = [canonical_url(url), title, published_at, hashlib.sha256(text.encode()).hexdigest()]
    return dict(article_id=hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest(),
                source=source, url=canonical_url(url), title=title, text=text,
                published_at=published_at, publisher=publisher, content_scope=scope,
                raw_path=raw, search_url=search_url)


def parse_cna(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.select_one('h1')
    nodes = [node for node in soup.select('article .paragraph')
             if not {'appDownload', 'articleADbox', 'bottomArticleBanner'} & set(node.get('class', []))]
    for node in nodes:
        for junk in node.select('script,style,.advertisement,.ad,.subscribe'):
            junk.decompose()
    text = '\n'.join(node.get_text('\n', strip=True) for node in nodes if node.get_text(strip=True))
    if heading is None or len(text.strip()) < 40:
        raise ValueError('中央社未取得新聞正文，不能以導覽或相關新聞代替')
    published = soup.select_one('meta[property="article:published_time"]')
    canonical = soup.select_one('link[rel="canonical"]')
    target = urljoin(url, canonical.get('href', '')) if canonical else url
    if urlparse(target).hostname not in ('www.cna.com.tw', 'cna.com.tw') or not urlparse(target).path.startswith('/news/'):
        raise ValueError('中央社 canonical 網址不符預期')
    return dict(url=target, title=heading.get_text(' ', strip=True), text=text,
                published_at=published.get('content') if published else None)


def collect(source, query, limit, delay, root, report, start, end, browser='msedge', headed=False):
    start, end, _ = news_window(start.isoformat(), end.isoformat())
    if source == 'google_news':
        from .rss import collect_rss
        yield from collect_rss(query, limit, delay, root, report, start, end)
        return
    from playwright.sync_api import sync_playwright
    url = 'https://www.cna.com.tw/search/hysearchws.aspx?' + urlencode(dict(q=query))
    report['search_url'] = url
    report['coverage'] = 'loaded_search_results_only'
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel=None if browser == 'chromium' else browser, headless=not headed)
        context = b.new_context()
        page = context.new_page()
        page.set_default_timeout(20000)
        try:
            response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
            page.wait_for_timeout(2000)
            check_page(page)
            if response and response.status >= 400:
                raise ValueError(f'查詢頁 HTTP {response.status}')
            seen, previous_count, idle = set(), -1, 0
            page_number=0
            while True:
                page_number+=1
                if limit is not None and page_number>20:
                    report['state']='limited'
                    return
                report['pages'] = page_number
                html = page.content()
                snapshot = raw_file(root, html)
                entries = page.locator('#jsMainList li a:has(.listInfo time)').evaluate_all('''(els)=>els.map(e=>({
                    url:e.href,title:e.querySelector('h2')?.innerText||'',
                    published_at:e.querySelector('time')?.getAttribute('datetime')}))''')
                report['loaded_results'] = len(entries)
                if not entries:
                    body = page.locator('body').inner_text()
                    if re.search(r'查無|找不到|沒有.*結果|沒有.*新聞|0\s*筆|No results', body, re.I):
                        report['state'] = 'complete'
                        return
                    raise ValueError('沒有辨識到結果清單，未視為零筆；可能載入失敗或版型變更')
                for entry in entries:
                    if entry['url'] in seen:
                        continue
                    seen.add(entry['url'])
                    listed_day = publication_date(entry.get('published_at'))
                    if listed_day and not start <= listed_day <= end:
                        report.setdefault('date_exclusions', []).append(dict(
                            url=entry['url'], published_at=entry['published_at'],
                            reason='listing_date_out_of_range', title=entry['title']))
                        if limit is not None and len(seen) >= limit:
                            report['state'] = 'limited'
                            return
                        continue
                    try:
                        if urlparse(entry['url']).hostname != 'www.cna.com.tw':
                            raise ValueError('新聞不在中央社來源內')
                        time.sleep(delay)
                        detail = context.new_page()
                        try:
                            response = detail.goto(entry['url'], wait_until='domcontentloaded', timeout=45000)
                            check_page(detail)
                            if response and response.status >= 400:
                                raise ValueError(f'新聞頁 HTTP {response.status}')
                            body_html = detail.content()
                            raw = raw_file(root, body_html)
                            parsed = parse_cna(body_html, entry['url'])
                            # Never substitute modified time or today's date for publication time.
                            if not parsed['published_at']:
                                parsed['published_at'] = entry.get('published_at')
                            yield article_record(source, scope='full_article', raw=raw,
                                                 search_url=url, publisher='中央社', **parsed)
                        finally:
                            detail.close()
                        report['collected'] += 1
                    except Exception as error:
                        report['errors'].append(dict(url=entry['url'], error=str(error)))
                    if limit is not None and len(seen) >= limit:
                        report['state'] = 'limited'
                        return
                idle = idle + 1 if len(entries) == previous_count else 0
                if idle >= 2:
                    report['state'] = 'complete'
                    return
                previous_count = len(entries)
                page.evaluate('window.scrollTo(0,document.body.scrollHeight)')
                page.wait_for_timeout(int(delay * 1000))
                check_page(page)
            report['state'] = 'limited'
        except Exception as error:
            report['state'] = 'blocked' if isinstance(error, NewsBlocked) else 'failed'
            report['errors'].append(dict(error=str(error)))
            diagnostics = root / 'diagnostics'
            diagnostics.mkdir(parents=True, exist_ok=True)
            stem = source + '-' + hashlib.sha256(query.encode()).hexdigest()[:8]
            try:
                (diagnostics / (stem + '.html')).write_text(page.content(), encoding='utf-8')
                page.screenshot(path=str(diagnostics / (stem + '.png')), full_page=True)
            except Exception:
                pass
            raise
        finally:
            b.close()
