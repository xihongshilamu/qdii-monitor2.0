"""
Fetch QDII fund data from Eastmoney APIs and write data.json.
Runs via GitHub Actions on a daily schedule.

Step 1: Get full QDII fund list + purchase status from mobile API
Step 2: Scrape detail pages for exact purchase limit amounts
"""
import concurrent.futures
import json, math, re, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

API = 'https://fundmobapi.eastmoney.com/FundMNewApi/FundMNNetNewList'
DETAIL_URL = 'https://fund.eastmoney.com/{code}.html'
PAGE_SIZE = 30
MAX_PAGES = 30
MAX_WORKERS = 5
RETRY = 2

HEADERS_API = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                  'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148',
}
HEADERS_WEB = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Referer': 'https://fund.eastmoney.com/',
}

# Matches: 购买上限300.00元, 购买上限5,000.00元, 购买上限100.00万元
LIMIT_RE = re.compile(r'购买上限\s*([\d,.]+)\s*(万?元)')
# Fallback: 限额 1000 元
LIMIT_RE2 = re.compile(r'限额\s*([\d,.]+)\s*(万?元)')
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
            })
    return funds


def _parse_limit(amount_str: str, unit: str) -> str:
    amount = amount_str.replace(',', '')
    try:
        val = float(amount)
    except ValueError:
        return f'{amount}{unit}'
    if '万' in unit:
        return f'{val:g}万元'
    if val >= 10000:
        return f'{val / 10000:g}万元'
    return f'{val:g}元'


def scrape_limit(code: str) -> str | None:
    """Scrape purchase limit amount from fund detail page, with retry."""
    url = DETAIL_URL.format(code=code)
    for attempt in range(RETRY + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS_WEB)
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode('utf-8', errors='ignore')

            for pattern in (LIMIT_RE, LIMIT_RE2):
                m = pattern.search(html)
                if m:
                    return _parse_limit(m.group(1), m.group(2))

            # No amount found — check if page confirms 限大额 without amount
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


def main() -> None:
    # --- Step 1: Fetch full fund list ---
    print('Fetching page 1...')
    first = fetch_page(1)
    total = first.get('TotalCount', 0)
    print(f'TotalCount={total}')
    if total == 0:
        print('ERROR: API returned TotalCount=0, aborting.')
        sys.exit(1)

    all_funds = extract(first)
    pages = min(math.ceil(total / PAGE_SIZE), MAX_PAGES)
    for p in range(2, pages + 1):
        print(f'Fetching page {p}/{pages}...')
        try:
            data = fetch_page(p)
            all_funds.extend(extract(data))
        except Exception as e:
            print(f'  Warning: page {p} failed: {e}')

    print(f'Total funds extracted: {len(all_funds)}')

    # --- Step 2: Scrape purchase limits ---
    limit_funds = [f for f in all_funds if f['status'] == 'limit']
    print(f'\nScraping purchase limits for {len(limit_funds)} funds...')

    code_to_fund = {f['code']: f for f in all_funds}

    def _do_scrape(fund: dict) -> tuple[str, str | None]:
        return fund['code'], scrape_limit(fund['code'])

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_do_scrape, f): f for f in limit_funds}
        for future in concurrent.futures.as_completed(futures):
            code, amount = future.result()
            if amount:
                f = code_to_fund[code]
                f['limitAmount'] = amount
                f['statusText'] = f'限购{amount}'
            done += 1
            if done % 50 == 0:
                print(f'  Progress: {done}/{len(limit_funds)}')

    with_amount = sum(1 for f in all_funds if f['limitAmount'])
    print(f'Limits found: {with_amount}/{len(limit_funds)}')

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
    print(f'Written data.json ({len(all_funds)} funds, updated {now})')


if __name__ == '__main__':
    main()
