"""Deterministic zero deductions backed by completed, scoped searches."""
import hashlib
from pathlib import Path


def empty_assessments(bundle, *, snapshot_path=None, start=None, end=None):
    results = {}
    expected = set(bundle.get('company_name_profile', {}).get('search_names', []))
    expected.add(bundle.get('company'))
    expected.discard(None)
    for code, module in [('sfb', 'sfb'), ('prtr', 'environment')]:
        reports = [r for r in bundle.get('sources', []) if r.get('source') == code]
        failures = [r for r in bundle.get('failures', []) if r.get('source') in (None, code)]
        if (not reports or not expected or failures
                or any(r.get('source') == code for r in bundle.get('documents', []))
                or not expected.issubset({r.get('query') for r in reports})):
            continue
        valid = True
        for report in reports:
            scope = report.get('date_scope', {})
            begin, finish = scope.get('start'), scope.get('end')
            if (report.get('state') != 'complete' or report.get('errors')
                    or report.get('collected') != 0 or report.get('limit_reached')
                    or scope.get('unknown_date_count', 0) or not begin or not finish
                    or (start and begin > str(start)) or (end and finish < str(end))):
                valid = False
            if code == 'prtr':
                p = report.get('provenance', {})
                try:
                    for item in p.get('files',[p]):
                        valid = valid and hashlib.sha256(Path(item['file_path']).read_bytes()).hexdigest() == item['file_sha256']
                except (KeyError, OSError):
                    valid = False
        if not valid:
            continue
        reason = ('本次查詢已完成，指定期間及來源內未發現符合條件的裁罰，扣分為 0。'
                  + ('僅限使用者提供的 CSV 快照，不代表所有環保機關或最新資料均無裁罰。' if code == 'prtr' else '不代表企業在其他來源或期間沒有違規。'))
        results[module] = dict(module=module, status='no_records_confirmed', scoring_eligible=True,
            applied_deduction=0, reason=reason, review_required=False,
            scope='supplied_csv_only' if code == 'prtr' else 'completed_sfb_search',
            queries=[r['query'] for r in reports], date_scopes=[r['date_scope'] for r in reports],
            source_file=str(Path(snapshot_path).resolve()) if snapshot_path else None,
            source_sha256=hashlib.sha256(Path(snapshot_path).read_bytes()).hexdigest() if snapshot_path else None)
    return results
