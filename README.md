# BTC 周期位置看板

单一用途的静态看板：BTC 现在处在周期什么相位。没有后端，托管在 GitHub Pages。

设计哲学见任务说明；简单说——克制（8–10 个核心指标）、估值层最大权重、每格给"极端程度 + 一句解读"而不是裸数字，且这些是 12–18 个月尺度的信号，别拿来做短线择时。

## 文件结构

- `index.html` / `styles.css` / `app.js` — 前端页面。`app.js` 读取同目录的 `data.json`，并客户端实时请求恐惧贪婪指数、Binance 资金费率/OI、Deribit DVOL（这几个失败时会退回 `data.json` 里 `realtime_fallback` 的上次快照，并标记"陈旧"）。
- `fetch.py` + `requirements.txt` — 构建期抓取脚本，由 GitHub Actions 每日跑一次：从 bitcoin-data.com 取 MVRV Z-Score/实现价，自算 200 周均线和已实现波动率（用 Coin Metrics 免费价格历史），抓 FRED 宏观数据和 TFTC 的 ETF 净流入数据集，写回 `data.json` 并 commit。
- `.github/workflows/update.yml` — 定时任务（每天 UTC 06:17，可手动触发）。
- `data.json` — 数据契约，见下方结构说明。当前是占位空值，需要跑一次 `fetch.py`（本地或 Action）才有真实数据。

## 部署步骤

1. **建 GitHub 仓库**，把这个目录的内容 push 上去（这个目录本身已经是独立的 git 仓库，见下方）。
2. **设置 FRED API key（必需，否则宏观三项拿不到数据）**：去 https://fred.stlouisfed.org/docs/api/api_key.html 免费申请一个 key，然后在仓库 `Settings → Secrets and variables → Actions` 里新建 secret，名字必须是 `FRED_API_KEY`。没有这个 key，宏观三项（实际10Y/名义10Y/DXY）会保持上次的值并标记陈旧，其余指标不受影响。
3. **开 GitHub Pages**：`Settings → Pages`，Source 选 `Deploy from a branch`，branch 选 `main` / `(root)`。
4. **手动跑一次 Action** 把真实数据填进 `data.json`：`Actions → Update BTC cycle data → Run workflow`。跑完会自动 commit，Pages 会在几分钟内更新。
5. 打开 `app.js` 顶部把 `GITHUB_REPO_URL` 换成你自己的仓库地址（只影响页脚"源码"链接，不影响功能）。

没有其他可选 key 了——ETF 流入(TFTC)、成交量(Coin Metrics)都是全自动、免费、无需注册的源，FRED 是唯一一个需要你自己申请的。

## `data.json` 契约

每个字段统一结构 `{ value, asof, source, stale, extra }`。`stale: true` 表示这是失败后保留的上一次已知值，前端会在 as-of 日期旁加"陈旧"标记。

```
mvrv_z          — MVRV Z-Score，extra.mvrv_ratio、extra.history(稀疏化的日线序列，供 sparkline)
realized_price  — 实现价(当前值，来自 bitcoin-data.com 每日序列的最新一条)
ma_200w         — 200周均线(当前值，取自 cost_basis_history 的最后一周)
price           — 现价，extra.ath / ath_date / drawdown_from_ath
cost_basis_history — 价格/实现价/200周线三条线的周线历史，供成本线卡片画图：
                     { dates, price, realized_price, ma_200w } 四个等长数组(周频，2010 至今)。
                     realized_price 在 bitcoin-data.com 覆盖范围(~2022)之前、ma_200w 在满
                     200 周之前都是 null，前端按 spanGaps 处理，直接从有数据的地方开始画
realized_vol    — 已实现波动率(30d, 年化)，extra.percentile、extra.dvol_fallback、extra.history(滚动30d序列)
volume          — 现货成交量(Coin Metrics volume_reported_spot_usd_1d，跨交易所汇总口径)，extra.percentile、extra.history
etf_flow        — ETF 净流入(百万美元)，extra.history([date,value] 对，最多保留 90 天)、extra.cumulative
macro           — { nominal_10y, real_10y, dxy, gold(含 extra.btc_gold_ratio) }
saylor_holdings — 手填字段，fetch.py 不会覆盖它。参考 bitcointreasuries.net / strategy.com 公开披露自行更新
realtime_fallback — { fng, funding_rate, open_interest, dvol } 的最近一次快照，仅用作客户端实时请求失败时的兜底
```

历史类字段统一用 `[date, value]` 数对数组（`cost_basis_history` 因为是多条线共享一套日期，例外用了 `{dates:[], series1:[], series2:[]}` 的并行数组结构）。

## 已知的坑

- **Coin Metrics Community API 不含 `CapRealUSD`（实现市值）**：实测直接调用会返回 `403 not available with supplied credentials`——这是付费指标，免费社区层拿不到，跟最初设想的不一样。`MVRV Z-Score` 和`实现价`因此改用 [bitcoin-data.com](https://bitcoin-data.com) 的免费社区镜像 API（`/v1/mvrv-zscore`、`/v1/realized-price/last`，无需 key）。这个源history 只回溯到 2022 年左右（不是 BTC 全历史），且**免费层限速 10 请求/小时**——`fetch.py` 每次只打 2 个请求，一天一次的定时任务完全够用，但不要在本地循环反复手动跑它去测试，会被限速。Z-Score 数值口径是该源自己的算法（标准的"市值偏离实现市值的标准差数"定义，跟 0/7 常见阈值对得上），我们没有自己重新计算标准差。
- **成交量最终改用 Coin Metrics，中间绕了两次弯路**：CoinGecko 的历史成交量端点(`/coins/bitcoin/market_chart`)现在要求付费的 Demo API key，实测 `401`（当前现价/ATH 用的 `/coins/bitcoin` 端点本身仍免费，不受影响）。查了 CoinMarketCap 想换源，免费层同样不含历史数据（历史数据从 $79/月的 Startup 档才开放）。改用 Binance 公开的 `/api/v3/klines` 本地测试完全没问题，一推到 GitHub Actions 才发现 `api.binance.com` 对 runner 的 IP 段也返回 `451`(地域限制)——跟资金费率/OI 那个 `fapi.binance.com` 是同一类问题，只是这次挡住的是核心历史字段，不是无关紧要的 fallback。最后发现其实不用舍近求远：Coin Metrics 社区免费层本来就有 `volume_reported_spot_usd_1d`（跨交易所汇总口径，2010-07-18 至今，`catalog-v2` 确认 `"community": true`），干脆跟 `PriceUSD` 一起在同一个请求里拉回来，不再多打一次 API。比 Binance 方案还更好——是全市场口径而不是单交易所，也不会有地域限制的风险。
- **`fapi.binance.com` 对部分云厂商 IP 返回 451（地域限制）**：GitHub Actions 的 runner IP 段偶尔会被打上这个标签，导致 `fetch.py` 里的资金费率/OI 快照抓取失败——这不影响主线数据，只影响 `realtime_fallback` 里那份兜底快照，且 `fetch.py` 对每个源都做了 try/except，失败不会污染其他字段。前端用户自己浏览器发出的实时请求走的是用户自己的 IP，通常不受影响。
- **ETF 净流入改用 TFTC 而不是直接抓 Farside**：Farside 官网(`farside.co.uk`)本身挡在 Cloudflare 的 JS 挑战后面——实测无论加什么 User-Agent/Accept 头都拿到 `403` + "Just a moment..." 挑战页，纯 `requests`/`pandas.read_html` 抓不到，需要无头浏览器（Playwright 等）才能过，超出本项目"零依赖静态站+轻量 Action"的范围。改用 [tftc.io](https://www.tftc.io/bitcoin-etf-flows) 的公开 JSON 数据集(`https://www.tftc.io/bitcoin-etf-flows/data.json`)：CC BY 4.0 协议、无需 key、`Access-Control-Allow-Origin: *`（甚至能前端直接 fetch，只是目前仍走构建期抓取以保持架构一致），数据本身就是从 SoSoValue + Farside 披露的数字整理来的，覆盖 2024-01-11 ETF 上市至今，每天更新。`value`/`extra.history` 里的数值单位是百万美元(把 TFTC 原始的美元数值 ÷1e6 存的)。
- **checkonchain 的 iframe** 理论上没有设 `X-Frame-Options`（否则这个方案从一开始就不成立），如果未来对方加了限制，页面会在 iframe 触发 `error` 事件时自动换成"在新标签页打开原图"的占位链接。
- **MVRV Z-Score 口径**：见上面 bitcoin-data.com 那条——现在是消费第三方已经算好的值，不是本项目自己用全历史累计标准差重新计算的（免费数据源拿不到算这个所需的原始 `CapRealUSD`）。跟某些其他第三方版本数值对不上，属于口径/数据源差异，不是 bug。

## 本地跑一次抓取脚本

```bash
pip install -r requirements.txt
FRED_API_KEY=你的key python fetch.py
```

会直接改写本目录下的 `data.json`。
