"""Conservative conversion of explicit source amounts, never sums separate claims."""
import re
import unicodedata
from decimal import Decimal

from .company import normalize_name

DIGITS = dict(zip('零〇一壹二貳贰兩两三參叁四肆五伍六陸陆七柒八捌九玖',
                  [0, 0, 1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 4, 4, 5, 5, 6, 6, 6, 7, 7, 8, 8, 9, 9]))
UNITS = {'十': 10, '拾': 10, '百': 100, '佰': 100, '千': 1000, '仟': 1000, '萬': 10000, '億': 100000000}
CURRENCIES = {'TWD': ['新臺幣', '新台幣', '臺幣', '台幣', 'TWD', 'NT$'],
              'USD': ['美元', '美金', 'USD', 'US$'], 'JPY': ['日圓', '日元', '日幣', 'JPY'],
              'CNY': ['人民幣', 'CNY', 'RMB'], 'EUR': ['歐元', 'EUR'], 'HKD': ['港幣', '港元', 'HKD']}


def currency_code(value):
    for code, labels in CURRENCIES.items():
        if value in labels:
            return code
    return value if value and re.fullmatch('[A-Z]{3}', value) else None


def source_number(original):
    text = re.sub(r'[\s,]', '', unicodedata.normalize('NFKC', original))
    for labels in CURRENCIES.values():
        for label in labels:
            if text.startswith(label):
                text = text[len(label):]
                break
    text = re.sub(r'(?:元整|圓整|元|圓|整)$', '', text)
    if not text or not re.fullmatch(r'[\d.零〇一壹二貳贰兩两三參叁四肆五伍六陸陆七柒八捌九玖十拾百佰千仟萬億]+', text):
        return None
    if re.fullmatch(r'\d+(?:\.\d+)?', text):
        return Decimal(text)
    tokens = re.findall(r'\d+(?:\.\d+)?|[^\d.]', text)
    if ''.join(tokens) != text:
        return None
    total, section, number = Decimal(0), Decimal(0), None
    last_small, last_large = 10000, 10**16
    last_unit, explicit_zero = 1, False
    for token in tokens:
        if token in UNITS:
            unit = UNITS[token]
            if unit < 10000:
                if unit >= last_small:
                    return None
                section += (number if number is not None else Decimal(1)) * unit
                last_small = unit
            else:
                if unit >= last_large:
                    return None
                if not section and number is None:
                    return None
                total += (section + (number or 0)) * unit
                section, last_small, last_large = Decimal(0), 10000, unit
            number = None
            last_unit, explicit_zero = unit, False
        elif token in DIGITS:
            digit = DIGITS[token]
            if digit == 0:
                if number not in (None, 0):
                    return None
                number = None
                explicit_zero = True
            elif number is not None:
                return None
            else:
                number = Decimal(digit)
        else:
            if number is not None:
                return None
            number = Decimal(token)
    if number is not None and 0 < number < 10 and last_unit > 10 and not explicit_zero:
        # Shorthand such as 一百二／一萬五 is ambiguous; never guess its multiplier.
        return None
    return total + section + (number or 0)


def supported_amounts(extraction, company, aliases):
    names = {normalize_name(n) for n in [company, *aliases]}
    candidates = {'claim': [], 'award': []}
    ambiguous = False
    for amount in extraction['amounts']:
        kind = amount['purpose']
        if kind not in candidates:
            continue
        participants = {normalize_name(n) for n in [amount['payer'], amount['payee']] if n}
        if not participants.intersection(names):
            ambiguous = True
            continue
        value, currency = source_number(amount['amount_original']), currency_code(amount['currency'])
        if currency in CURRENCIES and not any(label in '\n'.join(amount['quotes']) for label in CURRENCIES[currency]):
            currency = None
        if value is None or currency is None:
            ambiguous = True
        candidates[kind].append((value, currency, amount['quotes']))
    currencies = {c for values in candidates.values() for _, c, _ in values if c}
    selected_currency = next(iter(currencies)) if len(currencies) == 1 else None
    output = dict(claim_amount=None, awarded_amount=None, currency=selected_currency,
                  ambiguous=ambiguous or len(currencies) > 1, quotes={'claim_amount': [], 'awarded_amount': []})
    for purpose, field in [('claim', 'claim_amount'), ('award', 'awarded_amount')]:
        values = candidates[purpose]
        # Even equal amounts in separate source entries may be distinct claims.
        if len(values) == 1 and selected_currency is not None:
            value, currency, quotes = values[0]
            if value is not None and currency == selected_currency:
                output[field] = int(value) if value == value.to_integral_value() else float(value)
                output['quotes'][field] = quotes
        elif values:
            output['ambiguous'] = True
    return output
