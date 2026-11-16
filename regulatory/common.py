import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from bs4 import BeautifulSoup

SITES = {
    'sfb': 'https://www.sfb.gov.tw/ch/home.jsp?id=104&parentpath=0,2,102',
    'prtr': 'https://prtr.moenv.gov.tw/index.html',
}
HOSTS = {'sfb': {'www.sfb.gov.tw'}, 'prtr': {'prtr.moenv.gov.tw'}}


class SourceBlocked(RuntimeError):
    pass


@dataclass
class Progress:
    source: str
    query: str
    entry_url: str
    state: str = 'running'
    pages: int = 0
    collected: int = 0
    total_reported: int | None = None
    date_scope: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class Notice:
    source: str
    source_url: str
    search_url: str
    title: str
    full_text: str
    full_text_kind: str
    raw_paths: list
    attachments: list = field(default_factory=list)
    limitations: list = field(default_factory=list)
    record_locator: dict = field(default_factory=dict)
    structured: dict | None = None
    provenance: dict = field(default_factory=dict)
    raw_record: dict | None = None
    date_metadata: dict = field(default_factory=dict)


def allowed(source, url):
    parts = urlparse(url)
    return parts.scheme == 'https' and parts.hostname in HOSTS[source]


def save_raw(root: Path, data: bytes, suffix: str):
    path = root / 'raw' / (hashlib.sha256(data).hexdigest() + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path.resolve())


def blocked_text(text):
    return bool(re.search(r'Web Page Blocked|URL you requested has been blocked|Access Denied|驗證失敗|拒絕存取', text, re.I))


def check_page(page):
    if blocked_text(page.locator('body').inner_text()):
        raise SourceBlocked('官方網站拒絕目前連線；不等同查無資料，不繞過封鎖')


def parse_sfb(html):
    soup = BeautifulSoup(html, 'html.parser')
    node = soup.select_one('#maincontent .page_content')
    title = soup.select_one('#maincontent h3')
    if node is None or len(node.get_text(strip=True)) < 40:
        raise ValueError('未取得證期局公告正文；不接受清單或錯誤頁')
    text = node.get_text('\n', strip=True)
    return (title.get_text(' ', strip=True) if title else ''), text


def pdf_text(data):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ValueError('PDF 已加密，無法確認完整正文')
    pages = [(page.extract_text() or '').strip() for page in reader.pages]
    if not pages or any(len(re.sub(r'\s+', '', text)) < 10 for text in pages):
        raise ValueError('PDF 包含無法擷取文字的頁面，需 OCR；不以標題代替全文')
    return '\n\n'.join(pages)


def prtr_rows(payload):
    result = payload.get('Result')
    if not isinstance(result, dict) or not isinstance(result.get('Data'), list):
        raise ValueError('PRTR 回應格式異常，不視為零筆')
    rows = result['Data']
    if not rows and result.get('RowCount') not in (None, 0):
        raise ValueError('PRTR 筆數與資料不一致')
    if any(not isinstance(row, dict) or not row.get('DOCUMENTNO') for row in rows):
        raise ValueError('PRTR 裁處紀錄缺少裁處書字號')
    return rows, result.get('RowCount', len(rows))


def record_text(row):
    # Preserve every officially returned field, including the modal detail fields.
    return '\n'.join(f'{key}: {value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}'
                     for key, value in row.items())
