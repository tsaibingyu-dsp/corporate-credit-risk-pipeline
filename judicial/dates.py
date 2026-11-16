import re
from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup


def two_year_window(*, as_of=None, start=None, end=None):
    today = as_of or datetime.now(timezone(timedelta(hours=8))).date()
    try:
        cutoff = today.replace(year=today.year - 2)
    except ValueError:
        cutoff = today.replace(year=today.year - 2, day=28)
    first = date.fromisoformat(start) if isinstance(start, str) else start or cutoff
    last = date.fromisoformat(end) if isinstance(end, str) else end or today
    if not cutoff <= first <= last <= today:
        raise ValueError(f'裁判日期必須在近兩年 {cutoff} 至 {today} 之內，且起日不得晚於迄日')
    return first, last


def parse_labeled_date(text):
    match = re.search(r'裁判日期\s*[:：]?\s*(?:民國\s*)?(\d{2,4})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*日?', text)
    if not match:
        return None
    year, month, day = map(int, match.groups())
    if year < 1912:
        year += 1911
    try:
        return date(year, month, day)
    except ValueError:
        return None


def judgment_date(content, *, is_html=True):
    if not is_html:
        return parse_labeled_date(content)
    soup = BeautifulSoup(content, 'html.parser')
    # The official metadata is outside .jud_content; never use cited case dates.
    for label in soup.select('#jud .col-th, .int-table .col-th'):
        if label.get_text(strip=True).rstrip('：:') == '裁判日期':
            value = label.find_next_sibling(class_='col-td')
            if value:
                return parse_labeled_date('裁判日期：' + value.get_text(' ', strip=True))
    return None
