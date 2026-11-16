import time
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin, urlparse
from pathlib import Path

BASE = 'https://judgment.judicial.gov.tw/FJUD/'


class SourceUnavailable(RuntimeError):
    pass


@dataclass
class CollectionReport:
    query: str
    completed: bool = False
    limit_reached: bool = False
    pages: int = 0
    fetched: int = 0
    start_date: str | None = None
    end_date: str | None = None
    reported_total: int | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == 'https' and parsed.hostname == 'judgment.judicial.gov.tw' and parsed.path.startswith('/FJUD/')


def collect(query: str, limit: int | None, delay: float, raw_dir: Path, headed=False, report=None, browser_channel=None,
            start_date=None, end_date=None):
    """Yield (URL, HTML). Fail closed on challenges or changed layouts."""
    from playwright.sync_api import sync_playwright
    fail_fast = report is None
    report = report if report is not None else CollectionReport(query=query)
    if (limit is not None and limit < 1) or delay < 3:
        raise ValueError('limit 必須為正整數或不設上限，delay 至少 3 秒')
    if (start_date is None) != (end_date is None):
        raise ValueError('裁判日期範圍必須同時提供起訖日')
    if start_date is not None and start_date > end_date:
        raise ValueError('起日不得晚於迄日')
    report.start_date = start_date.isoformat() if start_date else None
    report.end_date = end_date.isoformat() if end_date else None
    raw_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed, channel=browser_channel)
        context = browser.new_context()
        page = context.new_page()
        try:
            if start_date is not None:
                page.goto(BASE + 'Default_AD.aspx', wait_until='domcontentloaded')
                page.locator('#jud_kw').fill(query)
                for suffix, day in [('1', start_date), ('2', end_date)]:
                    for prefix, value in [('dy', day.year - 1911), ('dm', day.month), ('dd', day.day)]:
                        page.locator('#' + prefix + suffix).fill(str(value))
                page.locator('#btnQry').click()
            else:
                page.goto(BASE + 'default.aspx', wait_until='domcontentloaded')
                page.locator('#txtKW').fill(query)
                page.locator('#btnSimpleQry').click()
            seen, visited = set(), set()
            while limit is None or len(seen) < limit:
                # Search results may be embedded in an iframe.
                deadline = time.monotonic() + 30
                frame = None
                while time.monotonic() < deadline:
                    for candidate in page.frames:
                        if candidate.locator('a[href*="data.aspx"]').count():
                            frame = candidate
                            break
                        if re.search(r'查無資料|查無符合|查無裁判|查無相關資料|沒有符合.*資料', candidate.locator('body').inner_text()):
                            report.completed = True
                            return
                    if frame:
                        break
                    page.wait_for_timeout(500)
                if frame is None:
                    raise RuntimeError('未取得結果連結：可能無結果、需驗證或版型變更；請人工檢查')
                links = frame.locator('a[href*="data.aspx"]').evaluate_all('(els) => els.map(e => e.href)')
                signature = tuple(links)
                if signature in visited:
                    raise RuntimeError('分頁未前進，已停止避免循環')
                visited.add(signature)
                report.pages += 1
                body_text = frame.locator('body').inner_text()
                total_match = re.search(r'共\s*([\d,]+)\s*筆', body_text)
                if total_match:
                    report.reported_total = int(total_match[1].replace(',', ''))
                (raw_dir / f'results-page-{report.pages}.html').write_text(frame.content(), encoding='utf-8')
                for url in links:
                    url = urljoin(frame.url, url)
                    if url in seen or not allowed(url):
                        continue
                    time.sleep(delay)
                    seen.add(url)
                    print(f'FETCH {len(seen)}/{limit if limit is not None else "all"} {url}', flush=True)
                    detail = context.new_page()
                    try:
                        response = detail.goto(url, wait_until='domcontentloaded')
                        if response is not None and response.status in (403, 429, 500, 502, 503, 504):
                            raise SourceUnavailable(f'司法院 HTTP {response.status}，停止本次查詢並保留已取得文件')
                        if response is None or response.status >= 400:
                            raise RuntimeError('裁判頁 HTTP 錯誤，停止擷取')
                        detail.locator('.jud_content, #jud').first.wait_for(timeout=15000)
                        html = detail.content()
                    except Exception as error:
                        if fail_fast:
                            raise
                        report.errors.append(f'{url}: {type(error).__name__}: {error}')
                        detail.screenshot(path=str(raw_dir / f'detail-error-{len(seen)}.png'), full_page=True)
                        (raw_dir / f'detail-error-{len(seen)}.html').write_text(detail.content(), encoding='utf-8')
                        if isinstance(error, SourceUnavailable):
                            raise
                        if re.search(r'驗證碼|Web Page Blocked|存取遭拒', detail.locator('body').inner_text()):
                            raise SourceUnavailable('司法院要求驗證或拒絕存取，已停止並保存診斷資料') from error
                        if limit is not None and len(seen) >= limit:
                            report.limit_reached = True
                            return
                        continue
                    finally:
                        detail.close()
                    report.fetched += 1
                    yield url, html
                    if limit is not None and len(seen) >= limit:
                        report.limit_reached = True
                        return
                next_link = frame.locator('a').filter(has_text=re.compile(r'^\s*下[一]?頁\s*[>＞]?\s*$'))
                if next_link.count() == 0:
                    if report.reported_total is not None and len(seen) < report.reported_total:
                        raise RuntimeError(f'網站顯示 {report.reported_total} 筆，但僅取得 {len(seen)} 筆；不可視為完整')
                    report.completed = True
                    return
                time.sleep(delay)
                next_link.first.click()
                # Pagination updates asynchronously; do not read the old page again.
                frame.wait_for_function(
                    '(oldLinks) => { const links = Array.from(document.querySelectorAll(\'a[href*="data.aspx"]\')).map(a => a.href); return links.length > 0 && JSON.stringify(links) !== JSON.stringify(oldLinks); }',
                    arg=links, timeout=30000)
        except Exception:
            page.screenshot(path=str(raw_dir / 'collector-error.png'), full_page=True)
            (raw_dir / 'collector-error.html').write_text(page.content(), encoding='utf-8')
            raise
        finally:
            browser.close()
