"""Official-site adapters. Use ordinary forms and page-generated responses only."""
import hashlib
import json
import re
import time
from urllib.parse import urljoin

from .common import (Notice, SITES, SourceBlocked, allowed, check_page, parse_sfb,
                     pdf_text, prtr_rows, record_text, save_raw)


def fetch_pdf(context, source, url, root):
    if not allowed(source, url):
        raise ValueError('附件不在該官方來源，未自動下載')
    response = context.request.get(url, timeout=45000)
    data = response.body()
    raw_path = save_raw(root, data, '.pdf' if data.startswith(b'%PDF-') else '.response')
    if response.status != 200 or not data.startswith(b'%PDF-'):
        raise ValueError(f'PDF 下載失敗 HTTP {response.status}；原始回應已保存')
    return pdf_text(data), raw_path


def sfb(page, context, query, limit, delay, root, report, start=None, end=None):
    from credit_risk.date_scope import window,sfb_publication_date
    begin,finish=window(start,end)
    report.date_scope.update(start=begin.isoformat(),end=finish.isoformat(),field='official_page_date',
                             excluded_count=0,unknown_date_count=0)
    page.goto(SITES['sfb'], wait_until='domcontentloaded')
    check_page(page)
    page.locator('input[onclick="advqry();"]').click()
    page.locator('#keyword').fill(query)
    page.locator('input[onclick="qry();"]').click()
    seen = set()
    for number in range(1, 1001):
        check_page(page)
        report.pages += 1
        links = page.locator('a[href*="multimessages_view.jsp"][href*="dataserno="]')
        links.first.wait_for(state='attached', timeout=5000) if links.count() else None
        urls = list(dict.fromkeys(links.evaluate_all('(els)=>els.map(e=>e.href)')))       
        if not urls:
            text = page.locator("#maincontent").inner_text()

            no_data_patterns = [
                r"目前暫無資料提供",
                r"查無資料",
                r"查無相關資料",
                r"無相關資料",
                r"共\s*0\s*筆",
            ]

            if any(re.search(p, text) for p in no_data_patterns):
                report.state = "complete"
                report.total_reported = 0
                print(
                    f"SFB NO RESULTS: {query} "
                    "(官方搜尋頁面顯示無資料)",
                    flush=True
                )
                return

            raise ValueError(
                "證期局未取得清單，且沒有明確的查無資料訊息"
            )
        if set(urls).issubset(seen):
            raise ValueError('證期局分頁未前進')
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            detail = context.new_page()
            try:
                time.sleep(delay)
                response=detail.goto(url, wait_until='domcontentloaded')
                html = detail.content()
                raw = save_raw(root, html.encode(), '.html')
                if not allowed('sfb',detail.url):
                    raise SourceBlocked('證期局明細轉址至非白名單來源；已保存回應，不送 AI')
                if response and response.status>=400:
                    raise ValueError(f'證期局明細 HTTP {response.status}；已保存回應，不送 AI')
                check_page(detail)
                published=sfb_publication_date(html)
                if published is None:
                    report.date_scope['unknown_date_count']+=1
                    report.errors.append(dict(source_url=url,error='無法確認證期局官方公告日期，不送 AI',raw_path=raw))
                    continue
                if not begin<=published<=finish:
                    report.date_scope['excluded_count']+=1
                    continue
                title, text = parse_sfb(html)
                original_fields = dict(title=title, announcement_text=text)
                attachments = []
                for href in detail.locator('#maincontent a[href]').evaluate_all('(els)=>els.map(e=>e.href)'):
                    if re.search(r'\.pdf(?:\?|$)', href, re.I):
                        attachments.append(dict(source_url=href, status='linked'))
                paths, notes = [raw], []
                for attachment in attachments:
                    try:
                        time.sleep(delay)
                        pdf, path = fetch_pdf(context, 'sfb', attachment['source_url'], root)
                        text += '\n\n[附件全文]\n' + pdf
                        original_fields[f'attachment_text_{len(paths)}'] = pdf
                        paths.append(path)
                        attachment['status'] = 'collected'
                    except Exception as error:
                        attachment['status'] = 'failed'
                        notes.append(str(error))
                report.collected += 1
                yield Notice('sfb', url, page.url, title, (title + '\n' + text).strip(),
                             'announcement_html', paths, attachments, notes, raw_record=original_fields,
                             date_metadata=dict(date=published.isoformat(),basis='official_page_date',raw_path=raw,
                                                http_status=response.status if response else None,final_url=detail.url))
            except SourceBlocked:
                raise
            except Exception as error:
                report.errors.append(dict(source_url=url, error=str(error)))
            finally:
                detail.close()
            if limit is not None and report.collected >= limit:
                report.state = 'limited'
                return
        next_page = page.locator(f'a[href="javascript:list({number + 1})"]')
        if not next_page.count():
            report.state = 'complete'
            return
        time.sleep(delay)
        next_page.first.click()
    raise ValueError('證期局頁數超過保護上限')


def prtr(page, context, query, limit, delay, root, report, start, end, verification_wait=0):
    page.goto('https://prtr.moenv.gov.tw/deal.html', wait_until='domcontentloaded')
    check_page(page)
    page.locator('input[name="FacilityName"]').wait_for(state='visible')
    page.wait_for_timeout(1500)
    # A visible verification modal must be completed by a person in headed mode.
    # Do not fabricate tokens or call protected endpoints directly.
    if verification_wait:
        print(f'PRTR 若顯示驗證，請在瀏覽器完成；最多等待 {verification_wait} 秒', flush=True)
        deadline = time.monotonic() + verification_wait
        while time.monotonic() < deadline:
            tokens = page.locator('input[name="cf-turnstile-response"]')
            if not tokens.count() or tokens.first.input_value():
                break
            page.wait_for_timeout(500)
    token = page.locator('input[name="cf-turnstile-response"]')
    if token.count() and not token.first.input_value():
        raise SourceBlocked('PRTR 人機驗證尚未完成；可使用 --headed --verification-wait 120，由本人完成驗證')
    page.locator('input[name="FacilityName"]').fill(query)
    for field, value in [('StartDate', start), ('EndDate', end)]:
        node = page.locator(f'input[data-type="{field}"]')
        node.fill(value)
        node.dispatch_event('change')
    page.locator('input[name="FacilityName"]').dispatch_event('change')
    with page.expect_response(lambda response: '/api/v1/Penalty?' in response.url, timeout=45000) as received:
        page.locator('.search_submit').click()
    response = received.value
    raw = save_raw(root, response.body(), '.json')
    if response.status in (401, 403):
        raise SourceBlocked('PRTR 官方查詢回應要求驗證或拒絕存取')
    if response.status != 200:
        raise ValueError(f'PRTR 查詢失敗 HTTP {response.status}')
    rows, total = prtr_rows(response.json())
    report.total_reported, report.pages = total, 1
    for row in rows[:limit]:
        report.collected += 1
        record_path = save_raw(root, json.dumps(row, ensure_ascii=False).encode(), '.json')
        yield Notice('prtr', page.url, page.url, str(row.get('SUBJECT') or row['DOCUMENTNO']),
                     record_text(row), 'official_penalty_record', [raw, record_path],
                     limitations=['全文為官方公開裁處紀錄全部欄位，並非原始裁處書 PDF；未取得原始裁處書'],
                     record_locator={key: row.get(key) for key in ['DOCUMENTNO', 'FACILITYNAME', 'REGISTRATIONNO']})
    report.state = 'limited' if len(rows) > limit or (isinstance(total, int) and total > len(rows)) else 'complete'


def collect(source, query, limit, delay, root, report, browser='msedge', headed=False,
            start=None, end=None, verification_wait=0):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser_instance = pw.chromium.launch(headless=not headed, channel=None if browser == 'chromium' else browser)
        context = browser_instance.new_context()
        page = context.new_page()
        page.set_default_timeout(20000)
        try:
            adapter = {'sfb': sfb, 'prtr': prtr}[source]
            extra = dict(start=start, end=end)
            if source == 'prtr':
                extra['verification_wait']=verification_wait
            yield from adapter(page, context, query, limit, delay, root, report, **extra)
        except Exception as error:
            report.state = 'blocked' if isinstance(error, SourceBlocked) else 'failed'
            report.errors.append(dict(error=str(error)))
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
            browser_instance.close()
