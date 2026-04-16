"""
Fetch QDII fund data from Eastmoney APIs and write data.json.
Runs via GitHub Actions on a daily schedule.
"""
import json, math, sys, urllib.request
from datetime import datetime, timezone, timedelta

API = 'https://fundmobapi.eastmoney.com/FundMNewApi/FundMNNetNewList'
PAGE_SIZE = 30
MAX_PAGES = 30

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                  'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148',
}


def fetch_page(page: int) -> dict:
    url = (f'{API}?fundtype=6&SortColumn=DWJZ&Sort=desc'
           f'&pageIndex={page}&pageSize={PAGE_SIZE}'
           f'&deviceid=wap&plat=Iphone&product=EFund&version=6.3.1')
    req = urllib.request.Request(url, headers=HEADERS)
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
            })
    return funds


def main() -> None:
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
