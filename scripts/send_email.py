"""
Send daily QDII fund quota email.
Reads data.json, builds a summary, and sends via SMTP.

Required environment variables:
  EMAIL_TO   — recipient email address
  SMTP_HOST  — SMTP server (e.g. smtp.qq.com)
  SMTP_USER  — SMTP login username
  SMTP_PASS  — SMTP password / app-specific password
  SMTP_PORT  — (optional) SMTP port, defaults to 465 (SSL)
"""
import json, os, sys
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta

BJ_TZ = timezone(timedelta(hours=8))

US_KW = ['纳斯达克', '纳指', '标普', '美国', '美股', '美元', 's&p', '500', 'nasdaq',
         '道琼斯', '全球科技', '全球互联', '信息科技', '生物科技', '消费品', '医疗保健',
         '油气', '石油', '原油', '全球精选', '全球成长', '移动互联', '全球制造', '全球高端', '全球领先']

DEFAULTS = {
    '160213', '040046', '270042', '006479', '012754', '014063', '014978',
    '050025', '161125', '013309', '007721', '008696',
    '000834', '040048', '003721', '005241', '012906',
    '486001', '162411', '070100', '118001', '164906',
    '001668', '006480', '000988', '013308', '014064',
    '002423', '010342', '017026', '017027',
    '002979', '008697', '501310', '006075',
    '000043', '000044', '161128', '012868',
    '539001', '012752', '023422', '096001', '008401',
    '519981', '016452', '016453', '018043', '018044',
    '019172', '019173', '017641', '019305',
    '019441', '019442', '019524', '019525',
    '018966', '018967', '019547', '019548',
    '017436', '017437', '162415', '009975',
    '016532', '016533', '016055', '016057',
    '015299', '015300', '018064', '018065',
    '017028', '017030', '019736', '019737',
    '100055', '002891', '000906',
}

IDX_NAMES = {
    'SPX': '标普 500', 'NDX': '纳斯达克综合', 'NDX100': '纳斯达克 100',
    'DJI': '道琼斯工业', 'VIX': 'VIX 恐慌指数', 'GOLD': '国际黄金', 'OIL': 'WTI 原油',
}


def is_us_fund(f: dict) -> bool:
    name = f.get('name', '').lower()
    return f['code'] in DEFAULTS or any(k.lower() in name for k in US_KW)


def fmt_pct(price, prev_close):
    if not price or not prev_close:
        return '--'
    pct = (price - prev_close) / prev_close * 100
    return f'{pct:+.2f}%'


def build_email(funds: list, market: dict, update_time: str) -> tuple[str, str]:
    """Build email subject and HTML body."""
    today = datetime.now(BJ_TZ).strftime('%Y-%m-%d')
    subject = f'QDII 基金额度日报 — {today}'

    vis = [f for f in funds if is_us_fund(f)]
    open_list = [f for f in vis if f['status'] == 'open']
    limit_list = [f for f in vis if f['status'] == 'limit']
    closed_list = [f for f in vis if f['status'] not in ('open', 'limit')]

    def fund_row(f):
        limit = f.get('limitAmount') or f.get('statusText') or '--'
        nav = f.get('nav', '--')
        return f'<tr><td>{f["name"]}</td><td style="font-family:monospace">{f["code"]}</td><td>{nav}</td><td><b>{limit}</b></td></tr>'

    def section(title, color, items):
        if not items:
            return ''
        rows = '\n'.join(fund_row(f) for f in sorted(items, key=lambda x: x['name']))
        return f'''
        <h3 style="color:{color};margin:16px 0 8px">{title}（{len(items)}只）</h3>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
        <tr style="background:#f0f4f8"><th style="text-align:left;padding:6px">名称</th><th style="padding:6px">代码</th><th style="padding:6px">净值</th><th style="padding:6px">限额</th></tr>
        {rows}
        </table>'''

    market_rows = ''
    for k in ['SPX', 'NDX', 'NDX100', 'DJI', 'OIL', 'VIX', 'GOLD']:
        d = market.get(k)
        if not d or d.get('price') is None:
            continue
        pct = fmt_pct(d['price'], d.get('prevClose'))
        color = '#16a34a' if pct.startswith('+') else '#dc2626' if pct.startswith('-') else '#666'
        market_rows += f'<tr><td style="padding:4px 8px">{IDX_NAMES.get(k, k)}</td><td style="padding:4px 8px;font-family:monospace">{d["price"]:,.2f}</td><td style="padding:4px 8px;font-family:monospace;color:{color}">{pct}</td></tr>'

    body = f'''
    <div style="max-width:640px;margin:0 auto;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#1a1a1a">
      <div style="background:linear-gradient(135deg,#0c1426,#162040);padding:20px 24px;border-radius:12px 12px 0 0">
        <h1 style="color:#fff;font-size:20px;margin:0">📊 QDII 基金额度日报</h1>
        <p style="color:rgba(255,255,255,.6);margin:6px 0 0;font-size:13px">{today} · 数据更新于 {update_time}</p>
      </div>
      <div style="padding:16px 24px;background:#fff;border:1px solid #e5e7eb;border-top:none">
        <div style="background:#f8fafc;border-radius:8px;padding:12px 16px;margin-bottom:16px">
          <h3 style="margin:0 0 8px;font-size:14px;color:#374151">📈 市场行情</h3>
          <table style="width:100%;border-collapse:collapse;font-size:13px">{market_rows}</table>
        </div>

        <div style="display:flex;gap:12px;margin-bottom:16px">
          <div style="flex:1;background:#f0fdf4;border-radius:8px;padding:12px;text-align:center">
            <div style="font-size:24px;font-weight:800;color:#16a34a">{len(open_list)}</div>
            <div style="font-size:11px;color:#666">可买入</div>
          </div>
          <div style="flex:1;background:#fffbeb;border-radius:8px;padding:12px;text-align:center">
            <div style="font-size:24px;font-weight:800;color:#d97706">{len(limit_list)}</div>
            <div style="font-size:11px;color:#666">限大额</div>
          </div>
          <div style="flex:1;background:#fef2f2;border-radius:8px;padding:12px;text-align:center">
            <div style="font-size:24px;font-weight:800;color:#dc2626">{len(closed_list)}</div>
            <div style="font-size:11px;color:#666">已暂停</div>
          </div>
          <div style="flex:1;background:#f0f4f8;border-radius:8px;padding:12px;text-align:center">
            <div style="font-size:24px;font-weight:800;color:#374151">{len(vis)}</div>
            <div style="font-size:11px;color:#666">合计</div>
          </div>
        </div>

        {section('✅ 开放申购', '#16a34a', open_list)}
        {section('⚠️ 限制大额', '#d97706', limit_list)}
        {section('🚫 暂停申购', '#dc2626', closed_list)}
      </div>
      <div style="background:#f8fafc;padding:12px 24px;border-radius:0 0 12px 12px;border:1px solid #e5e7eb;border-top:none;text-align:center">
        <p style="margin:0;font-size:11px;color:#9ca3af">QDII 盯盘助手 · 数据来源：天天基金 / Yahoo Finance · 仅供参考，不构成投资建议</p>
      </div>
    </div>'''
    return subject, body


def main():
    email_to = os.environ.get('EMAIL_TO', '').strip()
    smtp_host = os.environ.get('SMTP_HOST', '').strip()
    smtp_user = os.environ.get('SMTP_USER', '').strip()
    smtp_pass = os.environ.get('SMTP_PASS', '').strip()
    smtp_port = int(os.environ.get('SMTP_PORT', '465'))

    if not email_to:
        print('EMAIL_TO not set, skipping email send.')
        return
    if not smtp_host or not smtp_user or not smtp_pass:
        print('SMTP credentials not fully configured, skipping.')
        return

    try:
        with open('data.json', 'r', encoding='utf-8') as f:
            fund_data = json.load(f)
    except FileNotFoundError:
        print('data.json not found')
        sys.exit(1)

    try:
        with open('market.json', 'r', encoding='utf-8') as f:
            market_data = json.load(f)
    except FileNotFoundError:
        market_data = {'data': {}}

    funds = fund_data.get('funds', [])
    market = market_data.get('data', {})
    update_time = fund_data.get('updateTime', '--')

    subject, html_body = build_email(funds, market, update_time)

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = smtp_user
    msg['To'] = email_to
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    print(f'Sending email to {email_to} via {smtp_host}:{smtp_port}...')
    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [email_to], msg.as_string())
        print('Email sent successfully!')
    except Exception as e:
        print(f'Failed to send email: {e}')
        sys.exit(1)


if __name__ == '__main__':
    main()
