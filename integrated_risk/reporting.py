import html
import json
from pathlib import Path


def write_json(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str,allow_nan=False),encoding='utf-8')
    temp.replace(path)


def text(value):
    if value is None:
        return '待確認（null）'
    return html.escape(str(value)).replace('|','\\|').replace('\n',' ')


def markdown(report):
    c=report['company']
    r=report['credit_recommendation']
    lines=['# 企業授信決策模擬報告','',
        '**授信決策模擬，非正式核准或實際利率報價。**','',
        f'- 公司：{text(c["company_code"])} {text(c["company_name"])}',
        f'- 申請額度：{text(report["requested_credit_amount"])} {text(report["currency"])}',
        f'- 評估狀態：{text(r["assessment_status"])}；provisional：{r["provisional"]}',
        f'- 有效評分依據：{r["evidence_basis"]["valid_evidence_count"]} 筆分析紀錄',
        f'- 綜合風險分數：{text(r["risk_score"])}／100；風險等級：{text(r["risk_level"])}',
        f'- 已確認扣分所對應等級：{text(r["observed_risk_level"])}；資料及評分完整：{r["scoring_complete"]}',
        f'- 模擬准駁結果：{text(r["credit_decision"])}',
        f'- 模擬核准額度：{text(r["recommended_approved_amount"])} {text(report["currency"])}',
        f'- 模擬年利率（%）：{text(r["recommended_interest_rate_percent"])}',
        f'- 需要人工覆核：{r["review_required"]}','',
        '分數僅由已驗證且去重的扣分計算。完全沒有有效證據時為無法評估，不能把空資料當 100 分；資料不完整但有有效評分依據時提供暫定模擬。','',
        '## 模組執行狀態','', '| 模組 | 狀態 | 原文筆數 | AI 成功 | 失敗 | 驗證失敗 | 涵蓋確認 |',
        '| --- | --- | ---: | ---: | ---: | ---: | --- |']
    for module,s in report['module_statuses'].items():
        labels=dict(judicial='司法裁判',sfb='證期局',environment='環境裁罰 CSV',news='企業新聞',mops='MOPS')
        states=dict(success='成功',partial='部分完成',missing_source='缺少來源',failed='失敗',no_records='本次來源無紀錄')
        lines.append(f'| {labels[module]} | {text(states.get(s["status"],s["status"]))} | {s["input_records"]} | {s["success"]} | {s["failed"]} | {s["validation_failed"]} | {s["coverage_confirmed"]} |')
    lines+=['','## 模擬計算過程','', '```json',json.dumps(r['calculation'],ensure_ascii=False,indent=2),'```','',
        '## 日期與資料覆蓋','']
    for module,s in report['module_statuses'].items():
        scope=s.get('date_scope',{})
        lines.append(f'- {module}：{text(scope.get("start"))}～{text(scope.get("end"))}；範圍外排除 {scope.get("excluded_count",0)} 筆，日期不明 {scope.get("unknown_date_count",0)} 筆。')
        if scope.get('missing_years'):
            lines.append('- 尚缺 MOPS 年度來源：'+text(', '.join(map(str,scope['missing_years']))))
    for item in report.get('source_assessments',[]):
        lines.append(f'- {text(item["module"])}：已完成查詢，未發現符合條件裁罰；扣分 {item["applied_deduction"]}。{text(item["reason"])}')
    for override in r.get('overrides',[]):
        lines.append('- 重大事件覆寫：'+text(override['rule'])+' → '+text(override['decision']))
    lines+=['','## 類別扣分','', '| 類別 | 已採用扣分 | 評分原因 |','| --- | ---: | --- |']
    for item in report['category_deductions']:
        lines.append(f'| {text(item["category"])} | {item["applied_deduction"]} | {text("；".join(item["reasons"]))} |')
    lines+=['','## 事件明細','']
    for i,event in enumerate(report['events'],1):
        lines += [f'### {i}. {text(event["risk_category"])}','',
            f'- 事件：{text(event["event_content"])}',
            f'- 狀態：{text(event["status"])}；風險程度：{text(event["risk_level"])}',
            f'- AI 建議扣分：{text(event["proposed_deduction"])}；實際採用：{event["applied_deduction"]}',
            f'- 評分原因：{text(event["score_reason"])}',
            f'- 來源：{text(event["source"]["url"])}；檔案：{text(event["source"]["file"])}']
        for factor in event['risk_factors']:
            lines.append('- 判斷因子：'+text(factor['factor']))
        if event.get('recency_adjustment'):
            a=event['recency_adjustment']
            lines.append(f'- 新聞時間權重：發布 {text(a["publication_date"])}，距基準日 {text(a["age_days"])} 天；原始扣分 {a["original_deduction"]} × 權重 {text(a["weight"])} = {text(a["weighted_deduction"])} 分（同事件仍僅扣一次）。')
        for quote in event['evidence_quotes']:
            excerpt=quote[:400]+('（節錄；完整引文見 JSON）' if len(quote)>400 else '')
            lines+=['','> '+text(excerpt)]
        lines.append('')
    lines+=['## 人工覆核事項','']
    for item in r['review_items']:
        lines.append(f'- {text(item["reason"])}'+('（阻擋准駁建議）' if item['blocks_decision'] else ''))
    for condition in r['conditions']:
        lines.append('- 條件：'+text(condition))
    lines+=['','## 規則與限制','',f'- 評分 CSV SHA-256：{report["scoring_table"]["sha256"]}',
        f'- 政策版本：{text(report["policy"]["version"])}',
        '- 相同事件只採用一筆已驗證建議扣分；跨類別或跨來源不加總。疑似重複未釐清前暫不扣分。',
        '- 金額以十進位字串保存，避免額度浮點誤差；利率單位為年百分比。',
        '- 未發現事件只代表本次資料範圍；本機原文及雜湊一致仍不保證事件現況或模型解讀正確。','']
    return '\n'.join(lines)


def save_reports(root,report):
    root=Path(root)
    write_json(root/'integrated_risk_report.json',report)
    (root/'integrated_risk_report.md').write_text(markdown(report),encoding='utf-8')
