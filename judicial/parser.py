from bs4 import BeautifulSoup


def parse_html(html: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    # #jud includes official case metadata, especially the judgment date.
    node = soup.select_one('#jud') or soup.select_one('.jud_content')
    if node is None:
        raise ValueError('找不到裁判正文；可能為查詢頁、驗證頁或網站版型已變更')
    for element in node.select('script, style'):
        element.decompose()
    text = node.get_text('\n', strip=True).replace('\r\n', '\n')
    if len(text) < 30:
        raise ValueError('裁判正文過短，拒絕視為成功')
    return text
