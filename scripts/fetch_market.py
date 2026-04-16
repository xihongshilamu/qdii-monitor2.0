"""
Fetch market data (US indices, VIX, Gold) from Yahoo Finance v8 Chart API.
Writes market.json for the static site.
"""
import json, sys, urllib.request
from datetime import datetime, timezone, timedelta

SYMBOLS = {
    'SPX':  {'yahoo': '%5EGSPC', 'name': 'S&P 500'},
    'NDX':  {'yahoo': '%5EIXIC', 'name': 'Nasdaq'},
    'DJI':  {'yahoo': '%5EDJI',  'name': 'Dow Jones'},
    'VIX':  {'yahoo': '%5EVIX',  'name': 'VIX'},
    'GOLD': {'yahoo': 'GC%3DF',  'name': 'Gold'},
}

CHART_URL = (
    'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
    '?interval=1d&range=6mo'
)
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
}


def fetch_chart(symbol_key: str) -> dict | None:
    cfg = SYMBOLS[symbol_key]
    url = CHART_URL.format(symbol=cfg['yahoo'])
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f'  Warning: {symbol_key} fetch failed: {e}')
        return None

    result = (data.get('chart') or {}).get('result', [None])[0]
    if not result:
        print(f'  Warning: {symbol_key} no chart result')
        return None

    meta = result.get('meta', {})
    timestamps = result.get('timestamp') or []
    quote = (result.get('indicators') or {}).get('quote', [{}])[0]
    opens = quote.get('open') or []
    highs = quote.get('high') or []
    lows = quote.get('low') or []
    closes = quote.get('close') or []
    volumes = quote.get('volume') or []

    candles = []
    for i, ts in enumerate(timestamps):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        candles.append({
            'time': datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d'),
            'open':  round(opens[i], 2)  if i < len(opens) and opens[i] else round(c, 2),
            'high':  round(highs[i], 2)  if i < len(highs) and highs[i] else round(c, 2),
            'low':   round(lows[i], 2)   if i < len(lows) and lows[i] else round(c, 2),
            'close': round(c, 2),
        })

    # Deduplicate by date (keep last)
    seen = {}
    for candle in candles:
        seen[candle['time']] = candle
    candles = list(seen.values())
    candles.sort(key=lambda x: x['time'])

    return {
        'symbol': symbol_key,
        'name': cfg['name'],
        'price': meta.get('regularMarketPrice'),
        'prevClose': meta.get('previousClose'),
        'currency': meta.get('currency', 'USD'),
        'candles': candles,
    }


def main() -> None:
    output = {}
    for key in SYMBOLS:
        print(f'Fetching {key}...')
        chart = fetch_chart(key)
        if chart:
            print(f'  {key}: price={chart["price"]}, candles={len(chart["candles"])}')
            output[key] = chart
        else:
            print(f'  {key}: FAILED')

    bj_tz = timezone(timedelta(hours=8))
    now = datetime.now(bj_tz).strftime('%Y-%m-%d %H:%M')

    result = {
        'updateTime': now,
        'data': output,
    }
    with open('market.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, separators=(',', ':'))
    print(f'Written market.json (updated {now})')


if __name__ == '__main__':
    main()
