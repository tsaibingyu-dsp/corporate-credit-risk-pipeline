import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from .parsing import API_BASE, PAGE_URL


class MopsClient:
    """Read-only calls observed from the public MOPS form, in a normal browser session."""

    def __init__(self, raw_dir, *, channel='msedge', delay=3.0, refresh=False):
        self.raw_dir = Path(raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.channel, self.delay, self.refresh = channel, max(3.0, delay), refresh
        self.last_request = 0.0

    def __enter__(self):
        self.playwright = sync_playwright().start()
        try:
            options = dict(headless=True)
            if self.channel != 'chromium':
                options['channel'] = self.channel
            self.browser = self.playwright.chromium.launch(**options)
            self.context = self.browser.new_context(locale='zh-TW')
            self.page = self.context.new_page()
            self.page.goto(PAGE_URL, wait_until='domcontentloaded', timeout=60000)
            self.page.locator('#companyId').wait_for(state='visible', timeout=30000)
            self.last_request = time.monotonic()
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if hasattr(self, 'browser'):
            self.browser.close()
        self.playwright.stop()

    def invalidate_detail(self, parameters):
        key = hashlib.sha256(json.dumps(['t05st01_detail', parameters], sort_keys=True).encode()).hexdigest()
        # Keep original response bytes for audit, but do not reuse invalid content.
        (self.raw_dir / 't05st01_detail' / (key + '.json')).unlink(missing_ok=True)

    def fetch(self, endpoint, parameters, *, cache=False):
        if endpoint not in ('KeywordsQuery', 't05st01', 't05st01_detail'):
            raise ValueError('不支援的 MOPS 端點')
        key = hashlib.sha256(json.dumps([endpoint, parameters], sort_keys=True).encode()).hexdigest()
        folder = self.raw_dir / endpoint
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (key + '.json')
        raw_path = folder / (key + '.body')
        if cache and not self.refresh and path.exists() and raw_path.exists():
            record = json.loads(path.read_text(encoding='utf-8'))
            raw = raw_path.read_bytes()
            if record.get('body_sha256') == hashlib.sha256(raw).hexdigest():
                try:
                    payload = json.loads(raw.decode('utf-8-sig'))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = None
                if record.get('http_status') == 200 and isinstance(payload, dict) and payload.get('code') == 200:
                    return payload, dict(record, cached=True)
        # No rapid retries on throttling, verification pages or server errors.
        time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        response = self.context.request.post(API_BASE + endpoint, data=parameters,
                                             headers={'Referer': PAGE_URL}, timeout=60000)
        self.last_request = time.monotonic()
        raw = response.body()
        digest = hashlib.sha256(raw).hexdigest()
        raw_path.write_bytes(raw)
        record = dict(url=API_BASE + endpoint, method='POST', parameters=parameters,
                      http_status=response.status, fetched_at=datetime.now(timezone.utc).isoformat(),
                      body_sha256=digest, raw_file=str(raw_path.resolve()), cached=False)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        if response.status != 200:
            raise RuntimeError(f'MOPS HTTP {response.status}；已保留原始回應，請稍後重試')
        try:
            payload = json.loads(raw.decode('utf-8-sig'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError('MOPS 未回傳有效 UTF-8 JSON，可能為驗證或維護頁；已保留原文') from exc
        # Only success payloads are eligible for reuse on later detail requests.
        if not isinstance(payload, dict) or payload.get('code') != 200:
            path.unlink(missing_ok=True)
        return payload, record
