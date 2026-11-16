import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

RESULT_NAMES = {'judgment_analysis_results.json':'judicial', 'sfb_analysis_results.json':'sfb',
                'environment_analysis_results.json':'environment'}
MANIFESTS = {'judicial':'judgment_analysis_manifest.json', 'sfb':'sfb_analysis_manifest.json',
             'environment':'environment_analysis_manifest.json', 'mops':'analysis_manifest.json'}


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def infer_module(value):
    if not isinstance(value, dict):
        return 'unknown'
    if value.get('source_type') == 'sfb_penalty':
        return 'sfb'
    if value.get('source_type') == 'environment_penalty':
        return 'environment'
    if 'company_role' in value or 'is_final' in value:
        return 'judicial'
    if 'announcement_title' in value or 'risk_events' in value:
        return 'mops'
    if 'is_relevant' in value or 'attribution_status' in value:
        return 'news'
    return 'unknown'


@dataclass
class Item:
    analysis_path: Path
    index: int
    module: str
    analysis: object
    source_record: dict | None = None
    source_path: Path | None = None
    source_document: dict | None = None
    manifest: dict = field(default_factory=dict)
    manifest_entry: dict = field(default_factory=dict)
    context_issues: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def resolve_local(value, parent, allowed_roots):
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    path = (path if path.is_absolute() else parent/path).resolve()
    if not any(path.is_relative_to(root.resolve()) for root in allowed_roots):
        return None
    return path


def find_by_id(records, key, identifier):
    matches = [r for r in records if isinstance(r, dict) and r.get(key)==identifier]
    return matches[0] if len(matches)==1 else None


def load_items(path, *, workspace=None):
    path = Path(path).resolve()
    workspace = Path(workspace or Path.cwd()).resolve()
    roots = [workspace, path.parent]
    data = read_json(path)
    embedded_news = isinstance(data,dict) and data.get('module_id')=='adverse_news'
    if embedded_news:
        records = [(a.get('analysis'), a) for a in data.get('articles',[]) if isinstance(a,dict)
                   and a.get('analysis_status')=='success' and a.get('analysis') is not None]
    else:
        records = [(a,{}) for a in data] if isinstance(data,list) else [(data,{})]
    items = []
    for index,(analysis, embedded_source) in enumerate(records):
        module = 'news' if embedded_news else RESULT_NAMES.get(path.name) or infer_module(analysis)
        item = Item(path,index,module,analysis)
        if module=='news':
            if isinstance(analysis,dict):
                extra = {'article_id','source','news_url','published_at','analysis_round'}
                item.metadata = {k:v for k,v in analysis.items() if k in extra}
                item.analysis = {k:v for k,v in analysis.items() if k not in extra}
            bundle = data if embedded_news else read_json(path.parent/'news.json') if (path.parent/'news.json').exists() else None
            item.source_path = path if embedded_news else path.parent/'news.json'
            if isinstance(bundle,dict):
                item.source_document = bundle
                item.source_record = embedded_source or find_by_id(bundle.get('articles',[]),'article_id',item.metadata.get('article_id'))
            if item.source_record is None:
                item.context_issues.append('找不到唯一 article_id 對應的 news.json 原始新聞；不可用陣列順序猜測')
        elif module in MANIFESTS:
            manifest_path = path.parent/MANIFESTS[module]
            if manifest_path.exists():
                manifest = read_json(manifest_path)
                if isinstance(manifest,dict):
                    item.manifest = manifest
                    matches = [r for r in manifest.get('records',[]) if isinstance(r,dict)
                               and r.get('status')=='success' and type(r.get('result_index')) is int and r['result_index']==index]
                    if len(matches)==1:
                        item.manifest_entry = matches[0]
                    else:
                        item.context_issues.append('manifest 缺少唯一成功 result_index 對應，無法確認原始紀錄')
            else:
                item.context_issues.append('缺少分析 manifest，無法直接確認結果與來源關聯')
            entry = item.manifest_entry
            if module=='judicial':
                candidate = resolve_local(entry.get('source_file'),path.parent,roots)
                if candidate is None or not candidate.exists():
                    document_id = entry.get('document_id')
                    candidate = resolve_local('documents/'+str(document_id)+'.json',path.parent,roots) if document_id else None
                if candidate and candidate.exists():
                    item.source_path, item.source_record = candidate, read_json(candidate)
            elif module=='mops':
                candidate = resolve_local(item.manifest.get('source_file'),path.parent,roots)
                if candidate is None or not candidate.exists():
                    candidate = path.parent/'mops.json'
                if candidate.exists():
                    item.source_path, item.source_document = candidate, read_json(candidate)
                    item.source_record = find_by_id(item.source_document.get('announcements',[]),'announcement_id',entry.get('announcement_id'))
            else:
                bundle_path = path.parent/'regulatory.json'
                if bundle_path.exists():
                    item.source_path, item.source_document = bundle_path, read_json(bundle_path)
                    item.source_record = find_by_id(item.source_document.get('documents',[]),'document_id',entry.get('document_id'))
                if item.source_record is None and isinstance(entry.get('raw_record'),dict):
                    raw = entry['raw_record']
                    from regulatory.common import record_text
                    text = record_text(raw)
                    item.source_record = dict(source='prtr' if module=='environment' else 'sfb',
                        document_id=entry.get('document_id'), raw_record=raw, full_text=text,
                        full_text_sha256=sha256(text.encode()), full_text_kind='provided_penalty_csv_record' if module=='environment' else 'provided_record',
                        full_text_status='provided_record', source_url=entry.get('source_url'),
                        provenance=entry.get('provenance',{}), record_locator=entry.get('record_locator',{}))
                    item.source_path = manifest_path
            if item.source_record is None:
                item.context_issues.append('找不到 manifest 對應的原始裁判／公告／CSV 列')
        else:
            item.context_issues.append('無法辨識四種分析格式')
        items.append(item)
    return items, dict(path=str(path),sha256=sha256(path.read_bytes()),analysis_count=len(items),
                       status='loaded' if items else 'empty_analysis_not_evidence_of_no_risk')


def discover(root):
    root = Path(root)
    selected = []
    for path in sorted(root.rglob('*.json')):
        if path.name in RESULT_NAMES or path.name=='analysis_results.json':
            selected.append(path)
        elif path.name=='news.json' and not (path.parent/'analysis_results.json').exists():
            try:
                data=read_json(path)
                if any(a.get('analysis_status')=='success' and a.get('analysis') is not None for a in data.get('articles',[])):
                    selected.append(path)
            except (ValueError,OSError,AttributeError):
                pass
    return selected
