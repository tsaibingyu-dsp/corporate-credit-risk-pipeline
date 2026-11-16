import csv
import hashlib
import io
import json
import re
from pathlib import Path

from pydantic import Field, StrictInt, StrictBool, model_validator
from typing import Literal
from judicial.schema import StrictModel
from credit_risk.assessment import INDICATOR_CODES, RiskLevel

PROJECT=Path(__file__).resolve().parents[1]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


class ScoreBand(StrictModel):
    minimum: StrictInt = Field(ge=0,le=100)
    risk_level: Literal['高','中','低']


class DecisionBand(StrictModel):
    minimum: StrictInt = Field(ge=0,le=100)
    decision: Literal['准核','條件式准核','婉拒']
    amount_ratio: float = Field(ge=0,le=1,allow_inf_nan=False)


class SimulationBand(StrictModel):
    minimum: StrictInt = Field(ge=0,le=100)
    maximum: StrictInt = Field(ge=0,le=100)
    decision: Literal['准核','條件式准核','婉拒']
    risk_level: Literal['高','中','低']
    ratio_min: float = Field(ge=0,le=1,allow_inf_nan=False)
    ratio_max: float = Field(ge=0,le=1,allow_inf_nan=False)
    rate_min: float | None = Field(ge=0,allow_inf_nan=False)
    rate_max: float | None = Field(ge=0,allow_inf_nan=False)

    @model_validator(mode='after')
    def bounds(self):
        if self.minimum>self.maximum or self.ratio_min>self.ratio_max:
            raise ValueError('模擬分數或額度比例區間倒置')
        if self.decision=='婉拒':
            if self.ratio_min!=0 or self.ratio_max!=0 or self.rate_min is not None or self.rate_max is not None:
                raise ValueError('婉拒區間須零額度且不報利率')
        elif self.rate_min is None or self.rate_max is None or self.rate_min>self.rate_max:
            raise ValueError('非婉拒區間須有有效利率上下限')
        return self


class SimulationOverride(StrictModel):
    name: str
    code: str
    minimum_level: Literal['低','中','高']
    evidence_patterns: list[str]
    decision: Literal['條件式准核','婉拒']
    max_amount_ratio: float = Field(ge=0,le=1,allow_inf_nan=False)
    minimum_rate_percent: float | None = Field(ge=0,allow_inf_nan=False)

    @model_validator(mode='after')
    def coherent(self):
        if self.code not in INDICATOR_CODES or not self.evidence_patterns:
            raise ValueError('重大事件覆寫須有有效類別及原文判斷模式')
        for pattern in self.evidence_patterns:
            if not pattern or len(pattern)>300:
                raise ValueError('覆寫原文模式無效或過長')
            re.compile(pattern)
        if self.decision=='婉拒' and (self.max_amount_ratio!=0 or self.minimum_rate_percent is not None):
            raise ValueError('重大事件婉拒覆寫須零額度且無利率')
        return self


class SimulationPolicy(StrictModel):
    maximum_amount: StrictInt = Field(gt=0,le=100000000)
    amount_decimal_places: Literal[2]
    rate_decimal_places: StrictInt = Field(ge=2,le=6)
    bands: list[SimulationBand]
    overrides: list[SimulationOverride]
    pending_critical_max_ratio: float = Field(ge=0,le=1,allow_inf_nan=False)
    pending_critical_rate_floor: float = Field(ge=0,allow_inf_nan=False)

    @model_validator(mode='after')
    def complete(self):
        values=[]
        for b in self.bands:
            values.extend(range(b.minimum,b.maximum+1))
        if sorted(values)!=list(range(101)):
            raise ValueError('模擬分數區間須完整且不重疊涵蓋 0～100')
        if not {'F02','F03','F07'}.issubset({r.code for r in self.overrides}):
            raise ValueError('模擬引擎須保留重大違約、破產及嚴重舞弊覆寫規則')
        ordered=sorted(self.bands,key=lambda b:b.minimum,reverse=True)
        priorities={'准核':0,'條件式准核':1,'婉拒':2}
        if [priorities[b.decision] for b in ordered]!=sorted(priorities[b.decision] for b in ordered):
            raise ValueError('分數較低的模擬區間不能反而更寬鬆')
        if [b.ratio_max for b in ordered]!=sorted((b.ratio_max for b in ordered),reverse=True):
            raise ValueError('分數較低的模擬區間不能反而有更高額度比例')
        return self


class NewsAgeBand(StrictModel):
    maximum_age_days: StrictInt = Field(ge=0)
    weight: float = Field(gt=0,le=1,allow_inf_nan=False)


class NewsRecencyPolicy(StrictModel):
    bands: list[NewsAgeBand] = Field(default_factory=lambda: [
        NewsAgeBand(maximum_age_days=d,weight=w) for d,w in [(90,1),(180,.9),(365,.75),(732,.5)]])

    @model_validator(mode='after')
    def ordered(self):
        days=[b.maximum_age_days for b in self.bands]
        weights=[b.weight for b in self.bands]
        if not days or days!=sorted(set(days)) or weights!=sorted(weights,reverse=True):
            raise ValueError('新聞時間權重須按天數遞增、權重遞減且不得重複')
        return self


class Policy(StrictModel):
    version: str
    description: str
    base_score: Literal[100]
    score_bands: list[ScoreBand]
    manual_review_below_score: StrictInt = Field(ge=0,le=100)
    mandatory_review_codes: list[str]
    major_governance_level: Literal['高','中','低']
    possible_duplicate_similarity: float = Field(gt=0,le=1,allow_inf_nan=False)
    require_complete_sources_for_decision: StrictBool
    financial_max_age_days: StrictInt = Field(ge=1)
    pricing_max_age_days: StrictInt = Field(ge=1)
    minimum_annual_operating_cash_flow: float = Field(allow_inf_nan=False)
    minimum_equity: float = Field(allow_inf_nan=False)
    financial_gate_failure_action: Literal['manual_review','decline']
    minimum_debt_service_coverage_ratio: float = Field(gt=0,allow_inf_nan=False)
    decision_bands: list[DecisionBand]
    rate_addon_percentage_points: dict[str,float]
    maximum_interest_rate_percent: float = Field(gt=0,allow_inf_nan=False)
    simulation: SimulationPolicy
    news_recency: NewsRecencyPolicy = Field(default_factory=NewsRecencyPolicy)

    @model_validator(mode='after')
    def coherent(self):
        for bands in (self.score_bands,self.decision_bands):
            mins=[b.minimum for b in bands]
            if not mins or mins!=sorted(set(mins),reverse=True) or mins[-1]!=0:
                raise ValueError('政策區間須由高至低、不得重複且涵蓋 0 分')
        if not set(self.mandatory_review_codes).issubset(INDICATOR_CODES):
            raise ValueError('強制覆核類別必須為 F01～F10')
        if set(self.rate_addon_percentage_points)!={'高','中','低'} or any(not 0<=v<=100 for v in self.rate_addon_percentage_points.values()):
            raise ValueError('利率加碼表須包含高／中／低且為有效百分點')
        rank={'低':0,'中':1,'高':2}
        if [rank[b.risk_level] for b in self.score_bands]!=sorted(rank[b.risk_level] for b in self.score_bands):
            raise ValueError('較低分不能對應較低風險')
        decisions={'准核':0,'條件式准核':1,'婉拒':2}
        if [decisions[b.decision] for b in self.decision_bands]!=sorted(decisions[b.decision] for b in self.decision_bands):
            raise ValueError('較低分不能對應更寬鬆准駁')
        if any(b.decision=='婉拒' and b.amount_ratio!=0 for b in self.decision_bands):
            raise ValueError('婉拒的額度比例必須為零')
        rates=[self.simulation.pending_critical_rate_floor,*[b.rate_max for b in self.simulation.bands if b.rate_max is not None],
               *[r.minimum_rate_percent for r in self.simulation.overrides if r.minimum_rate_percent is not None]]
        if any(r>self.maximum_interest_rate_percent for r in rates):
            raise ValueError('模擬利率或覆寫利率超過設定的整體檢核上限')
        return self


def load_policy(path=None):
    default=PROJECT/'config/credit_policy.json'
    selected=Path(path) if path is not None else default
    if not selected.exists() and selected.resolve()==default.resolve():
        from .default_policy import DEFAULT_POLICY
        return Policy.model_validate(DEFAULT_POLICY)
    return Policy.model_validate_json(selected.read_text(encoding='utf-8-sig'))


def load_scoring_table(path=None):
    path=Path(path or PROJECT/'new_key_words.csv').resolve()
    raw=path.read_bytes()
    for encoding in ('utf-8-sig','cp950'):
        try:
            text=raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('評分 CSV 編碼必須為 UTF-8 或 CP950')
    reader=csv.DictReader(io.StringIO(text))
    required={'風險類別','扣分','嚴重程度判定依據'}
    if not reader.fieldnames or len(set(reader.fieldnames))!=len(reader.fieldnames) or not required.issubset(reader.fieldnames):
        raise ValueError('評分 CSV 缺少風險類別、扣分或嚴重程度判定依據，或欄位重複')
    rules={}
    for row in reader:
        category=row['風險類別'].strip()
        code=category.split(' ',1)[0]
        match=re.fullmatch(r'\s*(\d+)\s*[～~–—\-至]\s*(\d+)\s*',row['扣分'])
        if code not in INDICATOR_CODES or code in rules or not match:
            raise ValueError('評分 CSV 類別遺漏／重複或扣分區間無法辨識')
        lower,upper=map(int,match.groups())
        if not 0<=lower<=upper<=100 or not row['嚴重程度判定依據'].strip():
            raise ValueError('扣分上下限須介於 0～100，且須有判定依據')
        rules[code]=dict(code=code,category=category,minimum_deduction=lower,maximum_deduction=upper,
            severity_criteria=row['嚴重程度判定依據'].strip(),risk_meaning=row.get('授信風險意義',''),
            negative_keywords=row.get('負面關鍵字',''))
    if set(rules)!=set(INDICATOR_CODES):
        raise ValueError('評分 CSV 須完整包含 F01～F10')
    return dict(path=str(path),sha256=hashlib.sha256(raw).hexdigest(),rules=rules)
