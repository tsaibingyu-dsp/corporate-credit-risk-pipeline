from typing import Literal
from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Fact(StrictModel):
    value: str | None
    status: Literal['stated', 'not_stated', 'not_applicable', 'uncertain']
    quotes: list[str]


class Party(StrictModel):
    name: Fact
    roles: list[str]
    entity_type: Literal['person', 'organization', 'unknown']
    anonymized: bool


class Finding(StrictModel):
    subject: str | None
    category: str
    description: str
    basis: Literal['allegation', 'court_finding', 'disposition', 'unknown']
    quotes: list[str]


class Money(StrictModel):
    amount_original: str
    currency: str | None
    purpose: Literal['claim', 'award', 'fine', 'cost', 'other']
    payer: str | None
    payee: str | None
    quotes: list[str]


class Judgment(StrictModel):
    court: Fact
    case_number: Fact
    case_category: Fact
    document_type: Fact
    judgment_date: Fact
    cause: Fact
    procedure: Fact
    parties: list[Party]
    disposition: Fact
    finality: Fact
    related_cases: list[Fact]
    legal_provisions: list[Fact]
    findings: list[Finding]
    amounts: list[Money]
    risk_signals: list[Finding]
    limitations: list[str]
