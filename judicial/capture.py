"""Persist full documents and verify dates before any model call."""
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

from .dates import judgment_date
from .parser import parse_html


def save_collected(root, url, content, is_html, start, end, *, name_profile=None):
    root = Path(root)
    raw_hash = hashlib.sha256(content.encode()).hexdigest()
    raw_path = root / 'raw' / (raw_hash + ('.html' if is_html else '.txt'))
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(content.encode('utf-8'))
    day = judgment_date(content, is_html=is_html)
    text = parse_html(content) if is_html else content.strip()
    if not text:
        raise ValueError('空白裁判正文；原始回應已保留')
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    identifier = hashlib.sha256(json.dumps([url, text_hash], ensure_ascii=False).encode()).hexdigest()
    path = root / 'collected_documents' / (identifier + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    status = 'eligible' if day and start <= day <= end else 'date_out_of_range' if day else 'date_unknown'
    record = dict(schema_version='judgment-collected-v1', document_id=identifier,
                  source_url=url, fetched_at=datetime.now(timezone.utc).isoformat(),
                  raw_path=str(raw_path.resolve()), raw_sha256=raw_hash, text=text, text_sha256=text_hash,
                  judgment_date=day.isoformat() if day else None, date_status=status,
                  date_window=dict(start=start.isoformat(), end=end.isoformat()),
                  collection_record_path=str(path.resolve()))
    if name_profile:
        record['company_name_profile'] = name_profile
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    return record


def load_collected(folder, start, end):
    files = sorted(Path(folder).glob('*.json'))
    if not files:
        raise ValueError('找不到已蒐集的裁判 JSON')
    records = {}
    for path in files:
        record = json.loads(path.read_text(encoding='utf-8-sig'))
        if record.get('schema_version') != 'judgment-collected-v1':
            raise ValueError(f'{path.name} 不是 collected_documents 裁判紀錄')
        if hashlib.sha256(record['text'].encode()).hexdigest() != record['text_sha256']:
            raise ValueError(f'{path.name} 正文雜湊不符')
        if record['date_status'] == 'eligible' and record.get('judgment_date'):
            day = date.fromisoformat(record['judgment_date'])
            if start <= day <= end:
                key = record['source_url']
                if key not in records or record['fetched_at'] > records[key]['fetched_at']:
                    records[key] = record
    return list(records.values())
