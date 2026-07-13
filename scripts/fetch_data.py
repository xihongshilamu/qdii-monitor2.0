"""
Fetch QDII fund data from Eastmoney APIs and write data.json.
Runs via GitHub Actions on a daily schedule.

Step 1: Get full QDII fund list + purchase status from mobile API
Step 2: Scrape detail pages for exact purchase limit amounts
"""
import concurrent.futures
import html
import io
import json, math, re, subprocess, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional

API = 'https://fundmobapi.eastmoney.com/FundMNewApi/FundMNNetNewList'
DETAIL_URL = 'https://fund.eastmoney.com/{code}.html'
F10_FEE_URL = 'https://fundf10.eastmoney.com/jjfl_{code}.html'
ANN_LIST_URL = 'https://api.fund.eastmoney.com/f10/JJGG'
ANN_CONTENT_URL = 'https://np-cnotice-fund.eastmoney.com/api/content/ann'
ANN_PDF_URL = 'https://pdf.dfcfw.com/pdf/H2_{article_id}_1.pdf'
PAGE_SIZE = 30
MAX_PAGES = 30
MAX_WORKERS = 5
RETRY = 2
ANN_PAGE_SIZE = 12

HEADERS_API = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                  'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148',
}
HEADERS_WEB = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Referer': 'https://fund.eastmoney.com/',
}

# Matches: 单日累计购买上限10元, 日累计申购限额</td><td>10.00元, 购买上限100.00万元
LIMIT_PATTERNS = (
    re.compile(r'单日累计购买上限\s*([\d,]+(?:\.\d{1,2})?)\s*(万?元)'),
    re.compile(r'日累计申购限额.*?([\d,]+(?:\.\d{1,2})?)\s*(万?元)', re.S),
    re.compile(r'购买上限\s*([\d,]+(?:\.\d{1,2})?)\s*(万?元)'),
    re.compile(r'限额\s*([\d,]+(?:\.\d{1,2})?)\s*(万?元)'),
)
ANN_KEYWORDS = ('大额申购', '申购限额', '业务限额', '业务上限', '金额上限', '金额限制', '限制申购', '暂停大额', '调整大额')
ANN_LIMIT_PATTERNS = (
    re.compile(r'限制申购金额（单位：元）.{0,80}?([\d,]+(?:\.\d{1,2})?)'),
    re.compile(r'限制转换转入金额（单位：元）.{0,80}?([\d,]+(?:\.\d{1,2})?)'),
    re.compile(r'(?:业务限额为|业务上限为|金额上限调整为|上限调整为|限额调整为|限制申购金额为|申购限额为|单日限额为|金额应不超过|金额不得超过|限额至|限额为)\s*([\d,]+(?:\.\d{1,2})?)\s*(万美元|万港币|万?元|万元|人民币元|美元|美金|港币)'),
    re.compile(r'(?:申请金额大于|金额超过|超过)\s*([\d,]+(?:\.\d{1,2})?)\s*(万美元|万港币|万?元|万元|人民币元|美元|美金|港币).{0,60}?(?:确认失败|有权确认失败|拒绝|不予确认)'),
)
MONEY_TOKEN_RE = re.compile(r'-|[\d,]+(?:\.\d{1,2})?\s*(?:万美元|万港币|人民币元|万元|万?元|美元|美金|港币)?')
# Broader: 交易状态 section with status text
STATUS_RE = re.compile(r'交易状态.*?</div>', re.S)


def fetch_page(page: int) -> dict:
    url = (f'{API}?fundtype=6&SortColumn=DWJZ&Sort=desc'
           f'&pageIndex={page}&pageSize={PAGE_SIZE}'
           f'&deviceid=wap&plat=Iphone&product=EFund&version=6.3.1')
    req = urllib.request.Request(url, headers=HEADERS_API)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


def extract(data: dict) -> list[dict]:
    datas = data.get('Datas') or []
    funds = []
    for group in datas:
        items = group if isinstance(group, list) else [group]
        for f in items:
            if not isinstance(f, dict) or not f.get('FCODE'):
                continue
            sgzt = (f.get('SGZT') or '').strip()
            if '开放' in sgzt:
                status, text = 'open', '开放申购'
            elif '限大额' in sgzt or '限制大额' in sgzt:
                status, text = 'limit', sgzt
            elif '暂停' in sgzt:
                status, text = 'closed', '暂停申购'
            elif '封闭' in sgzt:
                status, text = 'closed', '封闭期'
            elif '场内' in sgzt:
                status, text = 'closed', '场内交易'
            elif sgzt == '':
                status, text = 'closed', '未知'
            else:
                status, text = 'closed', sgzt
            funds.append({
                'code': f['FCODE'],
                'name': f.get('SHORTNAME', ''),
                'nav': f.get('DWJZ', '--'),
                'navDate': f.get('FSRQ', '--'),
                'dayChange': f.get('RZDF', '--'),
                'status': status,
                'statusText': text,
                'limitAmount': None,
                'limitAmountSource': None,
            })
    return funds


def _parse_limit(amount_str: str, unit: str) -> str:
    amount_str = re.sub(r'<[^>]+>', '', amount_str).strip()
    m = re.search(r'\d[\d,]*(?:\.\d{1,2})?', amount_str)
    amount = (m.group(0) if m else amount_str).replace(',', '')
    try:
        val = float(amount)
    except ValueError:
        return f'{amount}{unit}'
    if '美' in unit:
        return f'{val:g}万美元' if '万' in unit else f'{val:g}美元'
    if '港' in unit:
        return f'{val:g}万港币' if '万' in unit else f'{val:g}港币'
    if '万' in unit:
        return f'{val:g}万元'
    if val >= 10000:
        return f'{val / 10000:g}万元'
    return f'{val:g}元'


def _fetch_text(url: str, headers: Optional[dict] = None) -> str:
    req = urllib.request.Request(url, headers=headers or HEADERS_WEB)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode('utf-8', errors='ignore')
    except Exception:
        cmd = ['curl', '-4', '-fsSL', '--connect-timeout', '8', '--max-time', '20']
        for key, value in (headers or HEADERS_WEB).items():
            cmd.extend(['-H', f'{key}: {value}'])
        cmd.append(url)
        return subprocess.check_output(cmd, timeout=25, stderr=subprocess.DEVNULL).decode('utf-8', errors='ignore')


def _fetch_bytes(url: str, headers: Optional[dict] = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or HEADERS_WEB)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read()
    except Exception:
        cmd = ['curl', '-4', '-fL', '--connect-timeout', '8', '--max-time', '30']
        for key, value in (headers or HEADERS_WEB).items():
            cmd.extend(['-H', f'{key}: {value}'])
        cmd.append(url)
        return subprocess.check_output(cmd, timeout=35, stderr=subprocess.DEVNULL)


def _fetch_json(url: str, headers: Optional[dict] = None) -> dict:
    return json.loads(_fetch_text(url, headers=headers))


def _find_limit(html: str) -> Optional[str]:
    for pattern in LIMIT_PATTERNS:
        m = pattern.search(html)
        if m:
            return _parse_limit(m.group(1), m.group(2))
    return None


def scrape_limit_from_pages(code: str) -> Optional[str]:
    """Scrape purchase limit amount from fund fee/detail pages, with retry."""
    urls = (F10_FEE_URL.format(code=code), DETAIL_URL.format(code=code))
    for attempt in range(RETRY + 1):
        try:
            pages = [_fetch_text(url) for url in urls]
            for html in pages:
                amount = _find_limit(html)
                if amount:
                    return amount

            # No amount found — check if page confirms 限大额 without amount
            for html in pages:
                m_status = STATUS_RE.search(html)
                if m_status:
                    status_text = re.sub(r'<[^>]+>', '', m_status.group(0))
                    if '限大额' in status_text:
                        return None  # Confirmed no amount on page
            return None
        except Exception:
            if attempt < RETRY:
                time.sleep(0.5 * (attempt + 1))
            continue
    return None


def _announcement_headers(code: str) -> dict:
    headers = HEADERS_WEB.copy()
    headers['Referer'] = f'https://fundf10.eastmoney.com/jjgg_{code}.html'
    return headers


def _clean_notice(text: str) -> str:
    text = html.unescape(text or '')
    text = re.sub(r'<[^>]+>', ' ', text)
    text = text.replace('\u3000', ' ')
    return re.sub(r'\s+', '', text)


def _token_to_amount(token: str, default_unit: str = '元') -> Optional[str]:
    token = (token or '').strip()
    if not token or token == '-':
        return None
    m = re.match(r'([\d,]+(?:\.\d{1,2})?)\s*(万美元|万港币|人民币元|万元|万?元|美元|美金|港币)?', token)
    if not m:
        return None
    unit = m.group(2) or default_unit
    return _parse_limit(m.group(1), unit)


def _fund_currency_unit(fund_name: str) -> str:
    if '美元' in fund_name:
        return '美元'
    if '港币' in fund_name:
        return '港币'
    return '元'


def _find_code_table_limit(text: str, code: str, fund_name: str) -> Optional[str]:
    code_row = re.search(r'(?:下属.{0,30}?(?:交易代码|代码)|基金份额的代码)(.{0,220}?)(?:该基金|该分级基金|该基金份额|下属基金|下属分级基金|限制申购金额|限制金额)', text)
    if not code_row:
        return None
    codes = re.findall(r'\d{6}', code_row.group(1))
    if code not in codes:
        return None
    idx = codes.index(code)

    row_re = re.compile(r'(?:限制申购金额|该基金份额的限制金额|限制金额)(?:（单位：(?P<unit>[^）]+)）)?(?P<values>.{0,220}?)(?:限制定期|限制转换|注：|2\.|2、|其他需要|$)')
    for row in row_re.finditer(text):
        unit = row.group('unit') or _fund_currency_unit(fund_name)
        values = row.group('values')
        tokens = MONEY_TOKEN_RE.findall(values)
        tokens = [t for t in tokens if t and (t == '-' or re.search(r'\d', t))]
        if len(tokens) <= idx:
            continue
        amount = _token_to_amount(tokens[idx], unit)
        if amount:
            return amount
    return None


def _find_channel_limit(text: str, fund_name: str) -> Optional[str]:
    unit = _fund_currency_unit(fund_name)
    if unit == '美元':
        patterns = (
            re.compile(r'代销机构.{0,180}?美元.{0,120}?(?:金额均应不超过|金额应不超过|金额不得超过|金额上限调整为|金额上限仍为|上限调整为|上限仍为|上限为|限额为)\s*([\d,]+(?:\.\d{1,2})?)\s*(美元|美金)'),
            re.compile(r'美元.{0,180}?(?:金额均应不超过|金额应不超过|金额不得超过|金额上限调整为|金额上限仍为|上限调整为|上限仍为|上限为|限额为)\s*([\d,]+(?:\.\d{1,2})?)\s*(美元|美金)'),
        )
    elif unit == '港币':
        patterns = (
            re.compile(r'代销机构.{0,180}?港币.{0,120}?(?:金额均应不超过|金额应不超过|金额不得超过|上限调整为|上限为|限额为)\s*([\d,]+(?:\.\d{1,2})?)\s*(港币)'),
            re.compile(r'港币.{0,160}?(?:金额均应不超过|金额应不超过|金额不得超过|上限调整为|上限为|限额为)\s*([\d,]+(?:\.\d{1,2})?)\s*(港币)'),
        )
    else:
        patterns = ()
    for pattern in patterns:
        m = pattern.search(text)
        if m:
            return _parse_limit(m.group(1), m.group(2))
    return None


def _find_explicit_channel_limit_details(content: str, fund_name: str) -> list[dict]:
    text = _clean_notice(content)
    unit = _fund_currency_unit(fund_name)
    amount_phrase = r'(?:金额各类别均应不超过|金额均应不超过|金额应不超过|金额不得超过|单笔或多笔累计高于|金额高于|高于|上限调整为|上限仍为|上限为|限额为)'
    direct_keys = ('直销机构', '直销渠道', '直销柜台', '网上直销', '直销平台')
    agency_keys = ('代销机构', '销售机构')
    if unit == '美元':
        amount_re = re.compile(amount_phrase + r'\s*(?:人民币)?([\d,]+(?:\.\d{1,2})?)\s*(美元|美金|万美元)')
        unit_key = '美元'
    elif unit == '港币':
        amount_re = re.compile(amount_phrase + r'\s*([\d,]+(?:\.\d{1,2})?)\s*(港币|万港币)')
        unit_key = '港币'
    else:
        amount_re = re.compile(amount_phrase + r'\s*(?:人民币)?([\d,]+(?:\.\d{1,2})?)\s*(人民币元|万元|万?元)')
        unit_key = ''
    details = []
    seen = set()
    for segment in re.split(r'[；。]', text):
        if unit_key and unit_key not in segment:
            continue
        channel = None
        if any(k in segment for k in direct_keys):
            channel = '直销'
        elif any(k in segment for k in agency_keys):
            channel = '代销'
        if not channel or channel in seen:
            continue
        m = amount_re.search(segment)
        if not m:
            continue
        details.append({'channel': channel, 'amount': _parse_limit(m.group(1), m.group(2))})
        seen.add(channel)
    return details


def _find_announcement_limit(content: str, code: str = '', fund_name: str = '') -> Optional[str]:
    text = _clean_notice(content)
    if code:
        amount = _find_code_table_limit(text, code, fund_name)
        if amount:
            return amount
    amount = _find_channel_limit(text, fund_name)
    if amount:
        return amount
    for pattern in ANN_LIMIT_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        unit = m.group(2) if len(m.groups()) >= 2 and m.group(2) else '元'
        fund_unit = _fund_currency_unit(fund_name)
        if fund_unit == '美元' and '美' not in unit:
            continue
        if fund_unit == '港币' and '港' not in unit:
            continue
        amount = _parse_limit(m.group(1), unit)
        # Ignore obviously wrong tiny dates/codes caught by loose table scans.
        if amount:
            return amount
    return None


def _announcement_channel(title: str, content: str) -> str:
    clean_title = _clean_notice(title)
    text = _clean_notice(content)
    if any(k in clean_title for k in ('直销渠道', '直销机构', '直销柜台', '网上直销', '直销平台')):
        return '直销'
    if any(k in clean_title for k in ('代销机构', '销售机构', '天天基金')):
        return '代销'
    if '本限制仅针对' in text and any(k in text for k in ('直销渠道', '直销机构', '直销柜台', '网上直销', '直销平台')):
        return '直销'
    if any(k in text for k in ('代销机构', '销售机构', '天天基金')) and not any(k in text for k in ('直销渠道', '直销机构', '直销柜台', '网上直销', '直销平台')):
        return '代销'
    return '公告'


def _append_limit_detail(details: list[dict], detail: dict) -> None:
    key = (detail.get('channel'), detail.get('amount'), detail.get('announcementId'), detail.get('source'))
    for existing in details:
        existing_key = (
            existing.get('channel'),
            existing.get('amount'),
            existing.get('announcementId'),
            existing.get('source'),
        )
        if existing_key == key:
            return
    details.append(detail)


def _extract_pdf_text(pdf_data: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_data))
        return '\n'.join(page.extract_text() or '' for page in reader.pages)
    except Exception:
        pass
    try:
        from pdfminer.high_level import extract_text
        return extract_text(io.BytesIO(pdf_data)) or ''
    except Exception:
        return ''


def _fetch_announcement_text(code: str, article_id: str) -> str:
    params = urllib.parse.urlencode({
        'client_source': 'web_fund',
        'show_all': 1,
        'art_code': article_id,
    })
    try:
        content_data = _fetch_json(f'{ANN_CONTENT_URL}?{params}')
        notice = content_data.get('data') or {}
        content = notice.get('notice_content') or ''
        if content:
            return content
    except Exception:
        pass

    pdf_url = ANN_PDF_URL.format(article_id=article_id)
    pdf_data = _fetch_bytes(pdf_url, headers=_announcement_headers(code))
    return _extract_pdf_text(pdf_data)


def _fetch_sales_announcements(code: str) -> list[dict]:
    params = urllib.parse.urlencode({
        'fundcode': code,
        'pageIndex': 1,
        'pageSize': ANN_PAGE_SIZE,
        'type': 5,  # 基金销售
    })
    data = _fetch_json(f'{ANN_LIST_URL}?{params}', headers=_announcement_headers(code))
    return data.get('Data') or []


def scrape_limit_details_from_announcements(code: str, fund_name: str = '') -> list[dict]:
    """Read 天天基金【基金销售】announcements and parse official purchase limits."""
    for attempt in range(RETRY + 1):
        try:
            announcements = _fetch_sales_announcements(code)
            details = []
            seen_channels = set()
            for ann in announcements:
                title = ann.get('TITLE') or ''
                if not any(k in title for k in ANN_KEYWORDS):
                    continue
                article_id = ann.get('ID')
                if not article_id:
                    continue
                content = _fetch_announcement_text(code, article_id)
                channel_details = _find_explicit_channel_limit_details(content, fund_name)
                added_channel_detail = False
                for channel_detail in channel_details:
                    channel = channel_detail['channel']
                    if channel in seen_channels:
                        continue
                    seen_channels.add(channel)
                    added_channel_detail = True
                    _append_limit_detail(details, {
                        'amount': channel_detail['amount'],
                        'source': 'announcement',
                        'channel': channel,
                        'announcementId': article_id,
                        'announcementTitle': title,
                        'announcementDate': (ann.get('PUBLISHDATEDesc') or ann.get('PUBLISHDATE') or '')[:10],
                        'announcementUrl': f'https://fund.eastmoney.com/gonggao/{code},{article_id}.html',
                    })
                if added_channel_detail:
                    continue
                amount = _find_announcement_limit(content, code, fund_name)
                if not amount:
                    continue
                channel = _announcement_channel(title, content)
                if channel in seen_channels:
                    continue
                seen_channels.add(channel)
                _append_limit_detail(details, {
                    'amount': amount,
                    'source': 'announcement',
                    'channel': channel,
                    'announcementId': article_id,
                    'announcementTitle': title,
                    'announcementDate': (ann.get('PUBLISHDATEDesc') or ann.get('PUBLISHDATE') or '')[:10],
                    'announcementUrl': f'https://fund.eastmoney.com/gonggao/{code},{article_id}.html',
                })
            return details
        except Exception:
            if attempt < RETRY:
                time.sleep(0.5 * (attempt + 1))
            continue
    return []


def scrape_limit_from_announcements(code: str, fund_name: str = '') -> Optional[dict]:
    details = scrape_limit_details_from_announcements(code, fund_name)
    return details[0] if details else None


def scrape_limit_info(code: str, fund_name: str = '') -> Optional[dict]:
    amount = scrape_limit_from_pages(code)
    details = scrape_limit_details_from_announcements(code, fund_name)
    if amount:
        _append_limit_detail(details, {
            'amount': amount,
            'source': 'eastmoney',
            'channel': '天天基金',
        })
    if not details:
        return None
    primary = details[0].copy()
    primary['details'] = details
    return primary


def _currency_key(name: str) -> str:
    if '美元现钞' in name:
        return '美元现钞'
    if '美元现汇' in name:
        return '美元现汇'
    if '美元' in name:
        return '美元'
    if '港币' in name:
        return '港币'
    return '人民币'


def _canonical_name(name: str) -> str:
    s = re.sub(r'\s+', '', name)
    s = s.replace('纳斯达克100', '纳指100')
    s = re.sub(r'\((人民币|美元现汇|美元现钞|美元|港币)份额?\)', '', s)
    s = re.sub(r'\((人民币|美元现汇|美元现钞|美元|港币)\)', '', s)
    s = re.sub(r'(人民币|美元现汇|美元现钞|美元|港币)份额?', '', s)
    s = re.sub(r'(A|B|C|D|E|F|H|I)$', '', s)
    return s


def infer_sibling_limits(funds: list[dict]) -> int:
    """Fill missing limits only when sibling share classes have one unanimous amount."""
    groups: dict[tuple[str, str], set[str]] = {}
    for f in funds:
        amount = f.get('limitAmount')
        if not amount:
            continue
        key = (_canonical_name(f.get('name', '')), _currency_key(f.get('name', '')))
        groups.setdefault(key, set()).add(amount)

    inferred = 0
    for f in funds:
        if f.get('status') != 'limit' or f.get('limitAmount'):
            continue
        key = (_canonical_name(f.get('name', '')), _currency_key(f.get('name', '')))
        amounts = groups.get(key) or set()
        if len(amounts) != 1:
            continue
        amount = next(iter(amounts))
        f['limitAmount'] = amount
        f['limitAmountSource'] = 'inferred'
        f['limitAmountNote'] = '同基金其他份额限额一致，供参考'
        inferred += 1
    return inferred


def main() -> None:
    # --- Step 1: Fetch full fund list ---
    print('Fetching page 1...', flush=True)
    first = fetch_page(1)
    total = first.get('TotalCount', 0)
    print(f'TotalCount={total}', flush=True)
    if total == 0:
        print('ERROR: API returned TotalCount=0, aborting.', flush=True)
        sys.exit(1)

    all_funds = extract(first)
    pages = min(math.ceil(total / PAGE_SIZE), MAX_PAGES)
    for p in range(2, pages + 1):
        print(f'Fetching page {p}/{pages}...', flush=True)
        try:
            data = fetch_page(p)
            all_funds.extend(extract(data))
        except Exception as e:
            print(f'  Warning: page {p} failed: {e}', flush=True)

    print(f'Total funds extracted: {len(all_funds)}', flush=True)

    # --- Step 2: Scrape purchase limits ---
    limit_funds = [f for f in all_funds if f['status'] == 'limit']
    print(f'\nScraping purchase limits for {len(limit_funds)} funds...', flush=True)

    code_to_fund = {f['code']: f for f in all_funds}

    def _do_scrape(fund: dict) -> tuple[str, Optional[dict]]:
        return fund['code'], scrape_limit_info(fund['code'], fund.get('name', ''))

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_do_scrape, f): f for f in limit_funds}
        for future in concurrent.futures.as_completed(futures):
            code, info = future.result()
            if info:
                f = code_to_fund[code]
                amount = info['amount']
                f['limitAmount'] = amount
                f['limitAmountSource'] = info['source']
                f['limitDetails'] = info.get('details') or []
                if info['source'] == 'announcement':
                    f['limitAnnouncementId'] = info.get('announcementId')
                    f['limitAnnouncementTitle'] = info.get('announcementTitle')
                    f['limitAnnouncementDate'] = info.get('announcementDate')
                    f['limitAnnouncementUrl'] = info.get('announcementUrl')
                f['statusText'] = f'限购{amount}'
            done += 1
            if done % 50 == 0:
                print(f'  Progress: {done}/{len(limit_funds)}', flush=True)

    inferred = 0
    with_amount = sum(1 for f in all_funds if f['limitAmount'])
    by_source = {}
    for f in all_funds:
        src = f.get('limitAmountSource')
        if src:
            by_source[src] = by_source.get(src, 0) + 1
    source_text = ', '.join(f'{k}={v}' for k, v in sorted(by_source.items()))
    print(f'Limits found: {with_amount}/{len(limit_funds)} ({source_text})', flush=True)

    # --- Step 3: Write JSON ---
    bj_tz = timezone(timedelta(hours=8))
    now = datetime.now(bj_tz).strftime('%Y-%m-%d %H:%M')

    output = {
        'updateTime': now,
        'total': len(all_funds),
        'funds': all_funds,
    }
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, separators=(',', ':'))
    print(f'Written data.json ({len(all_funds)} funds, updated {now})', flush=True)


if __name__ == '__main__':
    main()
