import argparse
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from .pipeline import run
from .config import PROJECT


def parse_amount(value):
    text=str(value).strip()
    if not re.fullmatch(r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?',text):
        raise ValueError('授信額度請輸入正數金額（元），可用千分位，最多兩位小數')
    amount=Decimal(text.replace(',',''))
    if not 0<amount<=Decimal('1000000000000000000'):
        raise ValueError('申請額度必須大於零且不超過 10^18 元')
    return amount.quantize(Decimal('0.01'))


def main(argv=None):
    parser=argparse.ArgumentParser(description='近兩年企業授信決策模擬：非正式核准或實際利率報價；只分析保存原文，不啟動爬蟲')
    parser.add_argument('--company-code',help='公司代號；未指定時互動輸入')
    parser.add_argument('--amount',help='申請授信額度，單位新臺幣元；未指定時互動輸入')
    parser.add_argument('--data-root',type=Path,default=PROJECT/'data')
    parser.add_argument('--out',type=Path,help='預設 data/integrated/公司代號')
    parser.add_argument('--inputs',type=Path,help='明確指定各模組既有來源路徑的 JSON；未指定時自動尋找本機資料')
    parser.add_argument('--financials',type=Path,help='已查核的財務、償債能力及可選定價資料 JSON')
    parser.add_argument('--policy',type=Path,default=PROJECT/'config/credit_policy.json')
    parser.add_argument('--as-of',type=date.fromisoformat,help='固定稽核基準日期 YYYY-MM-DD')
    parser.add_argument('--saved-analyses-only',action='store_true',help='只使用已成功保存的分析，不重跑模組；評分建議仍可呼叫 Gemini')
    args=parser.parse_args(argv)
    try:
        code=(args.company_code or input('請輸入公司代號：')).strip()
        if not re.fullmatch(r'\d{4,6}',code):
            raise ValueError('公司代號須為 4～6 位數字')
        amount=parse_amount(args.amount if args.amount is not None else input('請輸入申請授信額度（新臺幣元）：'))
    except (ValueError,EOFError) as exc:
        parser.error(str(exc))
    output=args.out or args.data_root/'integrated'/code
    try:
        report=run(code,amount,data_root=args.data_root,output=output,policy_path=args.policy,
            inputs_path=args.inputs,financials_path=args.financials,as_of=args.as_of,saved_analyses_only=args.saved_analyses_only)
    except (ValueError,OSError,TypeError) as exc:
        print(f'整合設定或輸入失敗：{exc}')
        return 1
    print(f'JSON {(output / "integrated_risk_report.json").resolve()}')
    print(f'MARKDOWN {(output / "integrated_risk_report.md").resolve()}')
    print(f'授信決策模擬，非正式核准或實際利率報價；狀態={report["credit_recommendation"].get("assessment_status","待確認")} 分數={report["risk_score"]} 風險等級={report["risk_level"]} 模擬准駁={report["credit_recommendation"]["credit_decision"]} 需覆核={report["credit_recommendation"]["review_required"]}')
    return 2 if report['credit_recommendation']['review_required'] else 0
