# QDII 美股基金额度助手

实时查看国内美股 QDII 基金的申购额度状态（开放申购 / 限大额 / 暂停申购）。

## 功能

- 打开页面自动加载全部 QDII 基金数据（700+ 只）
- 默认展示热门美股相关基金（纳指100、标普500 等）
- 按申购状态分组：可买入 / 限大额 / 暂停申购
- 支持搜索任意基金代码或名称，添加到"自选"
- 自选列表保存在浏览器本地（localStorage），下次打开自动恢复
- 手机、电脑均可使用（响应式布局）
- 自动适配深色模式

## 使用方式

### 本地 HTTP 服务

```bash
cd /Users/wangjinlan/Downloads/qdii-monitor-main
python3 -m http.server 8899
```

然后访问 http://localhost:8899

> 不建议直接双击 `index.html` 打开。现代浏览器会限制 `file://` 页面读取同目录下的 `data.json` 和 `market.json`，导致基金列表无法加载。

### 部署到 Gitee / GitHub Pages

1. 创建一个新的 Git 仓库
2. 将 `index.html` 推送到仓库
3. 在仓库设置中开启 Pages 服务
4. 得到公网链接，可分享给他人

## 数据来源

数据来自天天基金（东方财富）公开 API，每次打开页面实时获取最新数据。

- 接口：`fundmobapi.eastmoney.com`（支持 CORS，纯前端直连）
- 数据：QDII 基金列表、净值、申购状态
- 无后端、无数据库，所有逻辑在浏览器端完成

## 注意事项

- 数据仅供参考，实际申购限额以基金公司最新公告为准
- 天天基金接口非官方公开 API，请勿高频请求
- 如接口变更导致无法加载，需更新 `index.html` 中的请求逻辑

## 技术栈

- 纯 HTML + CSS + JavaScript，单文件，零依赖
- 天天基金 REST API（CORS `Access-Control-Allow-Origin: *`）
- localStorage 持久化用户自选

## 创建日期

2026-04-16
