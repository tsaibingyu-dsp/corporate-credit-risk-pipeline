import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from .collector import collect
from .extractor import create_extractor, PROMPT_VERSION, validate_evidence
from .parser import parse_html


def main():
    parser = argparse.ArgumentParser(description='裁判書蒐集與證據式結構化萃取')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--query')
    source.add_argument('--file', type=Path, help='本機 UTF-8 HTML 或純文字')
    parser.add_argument('--limit', type=int, default=5)
    parser.add_argument('--delay', type=float, default=3)
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--extract', action='store_true', help='將正文傳送至選定的 AI API（預設 Gemini）')
    parser.add_argument('--provider', choices=['gemini', 'openai'], default='gemini')
    parser.add_argument('--out', type=Path, default=Path('data'))
    args = parser.parse_args()
    root = args.out
    raw = root / 'raw'
    raw.mkdir(parents=True, exist_ok=True)
    try:
        extractor = create_extractor(args.provider) if args.extract else None
    except (ValueError, KeyError) as error:
        parser.error(str(error))
    db = sqlite3.connect(root / 'pipeline.sqlite3')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, url TEXT, sha256 TEXT, status TEXT, error TEXT)')
    if args.file:
        inputs = [(args.file.resolve().as_uri(), args.file.read_text(encoding='utf-8'), args.file.suffix.lower() != '.txt')]
    else:
        inputs = ((url, html, True) for url, html in collect(args.query, args.limit, args.delay, raw, args.headed))
    failed = 0
    try:
        for url, content, is_html in inputs:
            digest = hashlib.sha256(content.encode()).hexdigest()
            identity = [url, digest, PROMPT_VERSION,
                        extractor.provider if extractor else 'collect',
                        extractor.model if extractor else None]
            run_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
            old = db.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()
            if old and old[0] == 'success':
                print(f'SKIP {run_id}')
                continue
            (raw / (run_id + ('.html' if is_html else '.txt'))).write_text(content, encoding='utf-8')
            try:
                text = parse_html(content) if is_html else content
                if not text.strip():
                    raise ValueError('空白文件')
                record = dict(schema_version='1.0', prompt_version=PROMPT_VERSION,
                              source_url=url, fetched_at=datetime.now(timezone.utc).isoformat(),
                              raw_sha256=digest, text=text, model=extractor.model if extractor else None,
                              provider=extractor.provider if extractor else None,
                              identity_match='unverified', requires_human_review=True)
                if extractor:
                    result = extractor.extract(text)
                    validate_evidence(result, text)
                    record['extraction'] = result.model_dump()
                (root / (run_id + '.json')).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
                db.execute('INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)', (run_id, url, digest, 'success', None))
                print(f'OK {run_id}')
            except Exception as error:
                failed += 1
                db.execute('INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)', (run_id, url, digest, 'failed', str(error)))
                print(f'FAILED {run_id}: {error}')
            db.commit()
    finally:
        db.close()
    if failed:
        raise SystemExit(1)
