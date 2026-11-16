# 企業授信風險分析 Pipeline

整合司法裁判、證期局／環境部裁罰、企業新聞及 MOPS 重大訊息，使用 Gemini 分析、Python 驗證與計分，輸出授信決策模擬。

**模擬結果不代表正式核准或實際利率報價。** 本次報告包含資料缺漏與待覆核事件，請連同限制閱讀：[授信模擬報告](reports/integrated_risk_report.md)。

## 安裝

Python 3.10+，以下為 Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install "pydantic>=2.8,<3" "beautifulsoup4>=4.12,<5" "playwright>=1.48,<2" "google-genai>=1.50,<3" "pypdf>=5,<7"
.\.venv\Scripts\python.exe -m playwright install chromium
$env:GEMINI_API_KEY = "你的金鑰"
$env:GEMINI_MODEL = "你的可用 Gemini 模型 ID"
```

金鑰僅放環境變數；免費額度、模型可用性及服務狀態依供應商而定。來源預設瀏覽器為 Edge；沒有 Edge 時依各 CLI 的 `--help` 改用 Chromium。

## 執行流程

先跑四個來源，再執行 main。預設近兩年；以下以興富發 `2542` 為例：

```powershell
# 司法：蒐集全文 → 欄位萃取 → 風險分析
.\.venv\Scripts\python.exe .\02_company.py --company "興富發建設股份有限公司" --analyze

# 環境部：人工完成網站驗證、下載 CSV，放入此資料夾，檔名不限
New-Item -ItemType Directory -Force .\data\environment_imports

# 證期局網站＋環境部匯入 CSV → 分析
.\.venv\Scripts\python.exe .\03_regulatory.py --company "興富發建設股份有限公司" --sources sfb prtr --limit 100 --analyze

# Google News RSS＋中央社 → 初篩及兩輪分析
.\.venv\Scripts\python.exe .\04_news.py --company "興富發建設股份有限公司" --all

# MOPS：跨年度下載全文 → 分析
.\.venv\Scripts\python.exe .\05_mops.py --company 2542 --extract

# 可選：離線驗證
.\.venv\Scripts\python.exe .\06_validate.py --root .\data --output .\data\verification

# 整合既有成功分析，申請額度 1 億元
.\.venv\Scripts\python.exe .\main.py --company-code 2542 --amount 100000000 --saved-analyses-only
```

未指定 `--limit` 的司法與 MOPS 不限制本機筆數。新聞 `--all` 取消本機篇數限制，但來源回傳範圍不等於完整歷史。環境部使用目前 PRTR 人工下載的固定中文欄位格式；格式錯誤會明確報錯。

`--saved-analyses-only` 不重爬、不重做來源分析，但評分建議仍可能呼叫 Gemini。失敗或驗證不符者不扣分，保留覆核原因。新聞 `--resume` 可續跑，`--retry-second-round` 只重試第二輪失敗結果。不要同時執行多份相同輸出程序。

## 評分與輸出

- `new_key_words.csv`：F01～F10、關鍵字、扣分區間與嚴重度依據。
- `integrated_risk/default_policy.py`：公開版本的預設額度、利率、覆核、重大事件及時間權重規則。可另以 `--policy` 提供自訂 JSON，本庫不附 JSON。
- Gemini 分析與建議扣分；Python 驗證原文、公司歸責、金額、上下限及去重，再由 100 分扣至最低 0 分。
- 新聞發布 90 天內權重 1.0、91～180 天 0.9、181～365 天 0.75、其餘近兩年 0.5；原始扣分先符合 CSV，時間加權後可能低於原下限。
- 新聞先分析命中關鍵字者，再抽查未命中但符合條件者的 20%；第二輪只列中、高風險。RSS 是摘要，不能冒充已查證全文扣分。MOPS 符合日期且有全文者全部分析。
- 風險程度統一：高／中／低／無明確負面風險／待確認。重大、不明或證據不足事件依規則覆核；不因「模擬」兩字強制覆核。
- 日期、公司歸責、Schema、逐字引文及數值驗證不等於已獨立查證所有事件事實。

執行後產生 `data/`：

| 位置 | 內容 |
|---|---|
| `companies/<公司雜湊>/` | 裁判全文、欄位萃取、`judgment_analysis_results.json` |
| `regulatory/<公司雜湊>/` | 裁罰原始資料、證期局／環境部分析、評分標準 |
| `adverse_news/<公司雜湊>/` | `news.json` 全部狀態、成功分析、抽查及錯誤 |
| `mops/<代號>/<民國年度>/` | 公告全文、`analysis_results.json`、執行狀態 |
| `integrated/<代號>/` | `integrated_risk_report.json` 與 `.md` |

原始資料、匯入 CSV、API 金鑰及執行快取不隨本庫發布。示範報告只移除本機路徑，未重新分析或修改分數。

程式資料夾用途：`judicial` 司法、`regulatory` 裁罰、`adverse_news` 新聞、`mops_announcements` 重大訊息、`company_names` 公司名稱對照、`credit_risk` 共用日期及風險指標、`verification` 證據驗證、`integrated_risk` 整合評分。`reports` 只放本次模擬報告。

## 輸出欄位對照

四個模組共同的 `risk_assessment`：

| 欄位 | 中文 |
|---|---|
| `is_negative` / `negative_reason` | 是否負面／判斷理由 |
| `risk_level` / `risk_category` | 風險程度／主要 F 類別 |
| `risk_reason` / `risk_factors` | 判級理由／風險因子 |
| `evidence_quotes` / `risk_factor_evidence` | 原文引句／因子對應證據與事實或推論 |
| `review_required` | 是否需要人工覆核 |
| `risk_indicators` | F01～F10：代碼、類別、狀態、程度、理由、證據與覆核需求 |

各來源的主要分析欄位：

| 模組 | 欄位 | 中文 |
|---|---|---|
| 司法 | `company_name`, `is_target_company`, `company_role` | 目標公司、是否當事人、訴訟角色 |
| 司法 | `court`, `case_number`, `case_type`, `judgment_date` | 法院、案號、類型、裁判日期 |
| 司法 | `case_summary`, `court_findings`, `judgment_result`, `is_final` | 摘要、法院認定、裁判結果、是否確定 |
| 司法 | `claim_amount`, `awarded_amount`, `currency` | 請求金額、判賠金額、幣別 |
| 裁罰 | `source_type`, `source_id`, `company_name`, `company_match` | 來源類型、紀錄 ID、公司、身分匹配 |
| 裁罰 | `event_date`, `event_summary`, `event_status`, `risk_reasoning` | 日期、摘要、狀態、風險理由 |
| 裁罰 | `financial_impact`, `source_details`, `limitations` | 金額與付款、原始案件細節、限制 |
| 司法／裁罰 | `risk_category`, `severity`, `evidence_text`, `review_required` | 風險分類、程度、原文證據、覆核需求 |
| 新聞 | `company_name`, `is_relevant`, `relationship`, `company_match` | 公司、相關性、關係、身分匹配 |
| 新聞 | `event_detected`, `event_type`, `event_subject`, `event_summary` | 有無事件、類型、直接主體、摘要 |
| 新聞 | `target_company_role`, `attribution_status`, `negative_event_verified` | 公司角色、歸責、是否證實負面事件 |
| 新聞 | `is_negative`, `risk_category`, `risk_level`, `confidence` | 是否負面、類別、程度、模型信心 |
| 新聞 | `event_status`, `evidence`, `evidence_quote`, `reasoning` | 確認與解決狀態、引句、主要引句、理由 |
| 新聞 | `needs_manual_review`, `requires_human_review` | 人工覆核需求 |
| 新聞報告 | `article_id`, `news_url`, `published_at`, `analysis_round` | 新聞 ID、網址、發布時間、分析輪次 |
| MOPS | `company_id`, `company_name`, `announcement_date`, `announcement_title` | 公司代號、名稱、公告日期、標題 |
| MOPS | `is_negative`, `overall_severity`, `analysis_confidence`, `source_url` | 是否負面、整體程度、信心、來源 |
| MOPS | `risk_events[]` | 多事件：分類、描述、狀態、程度、財務／營運影響、公司回應、證據及覆核 |

環境部的金額、日期、繳款狀態及原始欄位由 Python 固定保留，Gemini 僅輸出判斷。

整合授信模擬報告：

| 欄位 | 中文 |
|---|---|
| `company`, `requested_credit_amount`, `currency` | 公司代號與名稱、申請額度、幣別 |
| `risk_score`, `risk_level` | 綜合風險分數、風險等級 |
| `credit_recommendation.credit_decision` | 准核／條件式准核／婉拒；無有效依據可為 null |
| `credit_recommendation.recommended_approved_amount` | 模擬額度：不超過申請額度及 1 億元 |
| `credit_recommendation.recommended_interest_rate_percent` | 模擬年利率百分比；婉拒或無法評估可為 null |
| `risk_factors`, `category_deductions`, `events`, `event_groups` | 風險因子、分類扣分、逐筆原因與證據、事件去重 |
| `module_statuses`, `score_calculation` | 來源狀態、排除原因、有效依據與計分過程 |
| `credit_recommendation.review_required`, `review_items` | 覆核需求、具體原因（均位於 credit_recommendation） |
| `provisional`, `simulation_notice` | 暫定結果、模擬聲明 |
