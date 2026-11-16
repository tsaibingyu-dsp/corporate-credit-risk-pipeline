import csv
import io
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictBool, model_validator
from judicial.schema import StrictModel


RiskLevel = Literal['高', '中', '低', '無明確負面風險', '待確認']
IndicatorCode = Literal['F01','F02','F03','F04','F05','F06','F07','F08','F09','F10']
INDICATOR_CODES = tuple(f'F{i:02d}' for i in range(1,11))


class RiskIndicator(StrictModel):
    code: IndicatorCode
    category: str
    status: Literal['有證據支持', '本筆未發現', '資料不足']
    risk_level: RiskLevel
    reason: str = Field(min_length=1)
    evidence_quotes: list[str]
    review_required: StrictBool

    @model_validator(mode='after')
    def coherent_status(self):
        if not self.reason.strip():
            raise ValueError('每類指標必須說明判斷理由')
        if self.status=='有證據支持':
            if self.risk_level not in ('高','中','低') or not self.evidence_quotes:
                raise ValueError('有證據支持須有高／中／低程度及原文證據')
        elif self.status=='本筆未發現' and self.risk_level!='無明確負面風險':
            raise ValueError('本筆未發現的程度只能是無明確負面風險')
        elif self.status=='資料不足' and (self.risk_level!='待確認' or not self.review_required):
            raise ValueError('資料不足須標待確認且要求人工覆核')
        if self.risk_level=='高' and not self.review_required:
            raise ValueError('高風險指標須要求人工覆核')
        return self


class FactorEvidence(StrictModel):
    factor: str
    statement_type: Literal['fact', 'inference']
    evidence_quotes: list[str]
    inference_reason: str | None


class RiskAssessment(StrictModel):
    is_negative: StrictBool | None
    negative_reason: str
    risk_level: RiskLevel
    risk_category: str | None
    risk_reason: str
    risk_factors: list[str]
    evidence_quotes: list[str]
    risk_factor_evidence: list[FactorEvidence]
    review_required: StrictBool
    risk_indicators: list[RiskIndicator] = Field(min_length=10,max_length=10)

    @model_validator(mode='after')
    def complete_indicators(self):
        codes=[i.code for i in self.risk_indicators]
        if len(set(codes))!=10 or set(codes)!=set(INDICATOR_CODES):
            raise ValueError('risk_indicators 必須完整包含 F01～F10，不能重複或遺漏')
        return self


ASSESSMENT_PROMPT = '''

【跨四項資料共通：必須解釋負面性、風險程度與原文證據】
輸出必須新增 risk_assessment 物件，不能省略，也不能只給分類或等級。
1. is_negative：這個事件對目標企業是否有負面授信風險；true／false，無法確認 null。不得把第三方事件直接歸責目標企業。
2. negative_reason：明確說明為何屬於目標企業的負面事件，或為何無明確負面風險／證據不足；區分事件主體、指控與已確認事實。
3. risk_level：高／中／低／無明確負面風險／待確認，須與本模組原有風險程度及負面判斷一致。不是信用評等，不產生扣分。
所有風險程度欄位（risk_level、severity、overall_severity 及每類指標）一律只用高／中／低／無明確負面風險／待確認，不輸出英文或其他程度。is_negative=false 時程度為無明確負面風險，is_negative=null 時程度為待確認。
4. risk_category：從輸入 credit_risk_taxonomy 挑選最主要的完整 category，例如 F02 信用違約。可多風險，但此欄只填最主要的一類，其他風險於 risk_factors 說明；無明確負面風險或無法合理分類時為 null。
5. risk_reason：具體回答為何判為此程度，尤其「中／高」須說明具體事件、可能影響財務／現金流／償債／營運／法遵／治理的途徑及資訊限制，不可只重述等級、罰鍰數字或命中關鍵字。
6. risk_factors：逐項列出支持判斷的因素，不編造金額、違約、流動性壓力或營運中斷。可能影響屬推論，必須明確標示可能性。
7. evidence_quotes：支持上述判斷的實際、逐字、連續原文片段陣列。只用本次原始輸入，不能引用提示、taxonomy 或示例；不得拼接不同欄位或省略括號。
8. risk_factor_evidence：每個 risk_factors 元素須有一筆對應，包含 factor（原樣回填因素）、statement_type（fact／inference）、evidence_quotes（從共通 evidence_quotes 中選取）及 inference_reason（推論時必須解釋由何原文如何推得；事實時可 null）。
9. review_required：歸屬不明、關鍵資訊缺漏、尚未證實或可能重大影響等須 true。
10. risk_indicators：必須逐一評估並輸出 F01～F10 共十筆，不能只列命中者。每筆必含 code、category（taxonomy 完整 category）、status、risk_level、reason、evidence_quotes、review_required。
status 僅可為「有證據支持」「本筆未發現」「資料不足」。有證據支持表示原文支持目標企業該類負面風險，須有逐字連續引文及高／中／低程度；不可因關鍵字、分類名稱或別家公司事件就認定有風險。
本筆未發現的程度固定「無明確負面風險」，理由限定為本筆原文未辨識到此類事件，不得推論企業完全不存在該風險；無對應引文可以 evidence_quotes=[]。
資料不足的程度固定「待確認」，review_required=true，說明不足或歸屬疑義，有可用原文才引用，沒有則 []，不得填假引文。高風險也須 review_required=true。
credit_risk_taxonomy 的 negative_keywords 及 risk_indicator_candidates 只提供初篩候選；命中、否認、指控及僅提及都須讀原文判斷，未命中也可由 AI 找出有證據的事件。所有十類都須各自給出具體理由，不強迫列為負面。
同一事件可同時支持多類，但每類須各有理由及原文支持；不產生分數。同事件跨來源後續彙整須去重。
is_negative=true 時至少一類有證據支持，主要 risk_category 必須是其中之一。整體高／中／低須等於已支持指標的最高程度；若另有關鍵未解疑義可用待確認並覆核。is_negative=false 或 null 時不可同時把某類列為有證據支持的目標企業負面風險；null 時至少一類資料不足。
所有非空負面判斷須有原文；負面事件至少一個風險因素及對應證據。資料不足用 null／待確認並說明原因，不為完成欄位虛構因素。
引文以原模組已使用的原文為準，優先逐字沿用原有有效引文。非原文字句只能放在 negative_reason、risk_reason 或明示 inference 的分析，不能充當 evidence_quotes。
F02 信用違約只在原文有實際債務支付／信用違約證據時使用；「遭裁罰」「被告」「財報違規」或別家公司違約不能自動判 F02。事件涉及信用風險不等於已確認違約。
例子中的「未依約償還到期債務」「可能面臨流動性壓力」不是預設答案；輸入未記載不得填入已發生事實，可能影響也須有可追溯依據與推論說明。
'''


def load_taxonomy():
    path = Path(__file__).resolve().parents[1] / 'new_key_words.csv'
    raw = path.read_bytes()
    for encoding in ('utf-8-sig', 'cp950'):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            pass
    else:
        raise ValueError('風險分類 CSV 編碼無法辨識')
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or any('風險類別' not in row for row in rows):
        raise ValueError('風險分類 CSV 缺少風險類別')
    taxonomy=[dict(category=row['風險類別'], risk_meaning=row.get('授信風險意義', ''),
                   negative_keywords=[k.strip() for k in re.split(r'[、,，;；\n]+',row.get('負面關鍵字','')) if k.strip()]) for row in rows]
    if len(taxonomy)!=10 or {r['category'].split(' ',1)[0] for r in taxonomy}!=set(INDICATOR_CODES):
        raise ValueError('風險分類 CSV 必須完整且唯一包含 F01～F10')
    return taxonomy


def indicator_candidates(source_texts, taxonomy):
    """Deterministic hints only. Never infer a risk finding from a keyword."""
    from adverse_news.screening import normalize_with_positions, normalize
    prepared=[(i,*normalize_with_positions(text),text) for i,text in enumerate(source_texts) if isinstance(text,str)]
    output=[]
    for row in taxonomy:
        hits=[]
        for keyword in row.get('negative_keywords',[]):
            needle=normalize(keyword)
            if not needle:
                continue
            for i,normalized,positions,text in prepared:
                for match in re.finditer(re.escape(needle),normalized):
                    left,right=match.span()
                    if needle[0].isascii() and needle[0].isalnum() and left and re.match(r'[a-z0-9_]',normalized[left-1]):
                        continue
                    if needle[-1].isascii() and needle[-1].isalnum() and right<len(normalized) and re.match(r'[a-z0-9_]',normalized[right]):
                        continue
                    start,end=positions[left],positions[right-1]+1
                    hits.append(dict(keyword=keyword,source_field=i,start=start,end=end,quote=text[start:end],
                                     context=text[max(0,start-60):min(len(text),end+60)]))
                    break
        output.append(dict(code=row['category'].split(' ',1)[0],category=row['category'],
                           keyword_hit_count=len(hits),keyword_hits=hits[:8],
                           candidate_only=True,hits_truncated=len(hits)>8))
    return output


def expected_level(is_negative, severity):
    if is_negative is False:
        return '無明確負面風險'
    if is_negative is None or severity in ('uncertain', '待確認'):
        return '待確認'
    return {'high':'高', 'medium':'中', 'low':'低', '高':'高', '中':'中', '低':'低', 'none':'無明確負面風險'}[severity]


def severity_negative(severity):
    return False if severity in ('none','無明確負面風險') else None if severity in ('uncertain','待確認') else True


def validate_assessment(assessment, source_texts, taxonomy, *, is_negative, severity):
    if assessment.is_negative is not is_negative:
        raise ValueError('risk_assessment 負面性與原模組判斷不一致')
    if assessment.risk_level != expected_level(is_negative, severity):
        raise ValueError('risk_assessment 風險程度與原模組不一致')
    allowed = {row['category'] for row in taxonomy}
    if assessment.risk_category is not None and assessment.risk_category not in allowed:
        raise ValueError('risk_assessment 分類不在提供的 F 類別清單中')
    if not assessment.negative_reason.strip() or not assessment.risk_reason.strip():
        raise ValueError('risk_assessment 必須解釋負面性與風險程度')
    sources = [s for s in source_texts if isinstance(s, str) and s.strip()]
    for quote in assessment.evidence_quotes:
        if not quote.strip() or not any(quote in source for source in sources):
            raise ValueError('risk_assessment 原文證據不存在或跨來源拼接')
    if is_negative is not None and not assessment.evidence_quotes:
        raise ValueError('risk_assessment 明確判斷缺少原文證據')
    if is_negative is True and (not assessment.risk_category or not assessment.risk_factors):
        raise ValueError('負面風險缺少 F 分類或具體風險因素')
    if assessment.risk_level in ('高', '中') and assessment.risk_reason.strip() in (
            assessment.risk_level, assessment.risk_level + '風險', assessment.risk_category):
        raise ValueError('中高風險理由不能只重述等級或分類名稱')
    if assessment.risk_category and assessment.risk_category.startswith('F02 '):
        if not re.search(r'債務|違約|逾期|跳票|退票|償還|償債|未償付|清償|到期借款|信用降評|信用評等.{0,6}(?:調降|下降)', '\n'.join(assessment.evidence_quotes)):
            raise ValueError('F02 信用違約分類缺少具體債務或信用事件原文，不能由裁罰推定違約')
    if is_negative is False and assessment.risk_category is not None:
        raise ValueError('無明確負面風險不應套用負面 F 分類')
    if any(not factor.strip() for factor in assessment.risk_factors) or len(set(assessment.risk_factors)) != len(assessment.risk_factors):
        raise ValueError('風險因素不得空白或重複')
    factors = [item.factor for item in assessment.risk_factor_evidence]
    if len(factors) != len(set(factors)) or set(factors) != set(assessment.risk_factors):
        raise ValueError('每個風險因素必須有唯一的原文對應')
    for item in assessment.risk_factor_evidence:
        if not item.evidence_quotes or any(q not in assessment.evidence_quotes for q in item.evidence_quotes):
            raise ValueError('風險因素的證據必須出現在共通原文證據清單')
        if item.statement_type == 'inference' and (not item.inference_reason or not item.inference_reason.strip()):
            raise ValueError('推論因素必須說明依據與推論途徑')
    if assessment.risk_level == '待確認':
        assessment.review_required = True
    validate_indicators(assessment,sources,taxonomy)


def validate_indicators(assessment,sources,taxonomy):
    categories={r['category'].split(' ',1)[0]:r['category'] for r in taxonomy}
    supported=[]
    for indicator in assessment.risk_indicators:
        if indicator.category!=categories.get(indicator.code):
            raise ValueError('指標代碼與 CSV 風險類別不一致')
        for quote in indicator.evidence_quotes:
            if not quote.strip() or not any(quote in source for source in sources):
                raise ValueError(f'{indicator.code} 指標引文不存在或跨欄位拼接')
        if indicator.status=='有證據支持':
            supported.append(indicator)
            if indicator.reason.strip() in (indicator.risk_level,indicator.category,indicator.risk_level+'風險'):
                raise ValueError('有證據支持的指標必須說明具體理由，不能只重述等級或分類')
            if indicator.code=='F02' and not re.search(r'違約|跳票|退票|逾期未|未.{0,8}(?:償還|清償|支付|償付)|無法.{0,6}(?:償還|清償|支付|償付)', '\n'.join(indicator.evidence_quotes)):
                raise ValueError('F02 指標缺少具體未償付或違約證據，不能把裁罰或一般信用疑慮當成違約')
        if indicator.review_required:
            assessment.review_required=True
    if assessment.is_negative is True:
        if not supported or assessment.risk_category not in {i.category for i in supported}:
            raise ValueError('整體負面判斷及主要分類須有對應的有證據支持指標')
        rank={'低':1,'中':2,'高':3}
        highest=max(supported,key=lambda i:rank[i.risk_level]).risk_level
        if assessment.risk_level!='待確認' and assessment.risk_level!=highest:
            raise ValueError('整體風險程度須與十類已支持指標的最高程度一致')
    elif supported:
        raise ValueError('整體無明確負面風險或待確認時，不可同時確認某類目標企業負面風險')
    if assessment.is_negative is None and not any(i.status=='資料不足' for i in assessment.risk_indicators):
        raise ValueError('整體資料不足時，十類指標須標明至少一項資訊缺口')
