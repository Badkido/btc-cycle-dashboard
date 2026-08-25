#!/usr/bin/env python3
"""
构建期数据抓取脚本 — 由 GitHub Actions 每日定时跑一次。

抓取/计算所有 B 类（构建期）指标，写入 data.json。
每个数据源独立 try/except：单个源失败不影响其余字段，
失败时保留 data.json 里的上一次已知值并标记 stale: true。

字段契约见 README.md 和项目根目录的 data.json。
"""

import json
import math
import os
import re
import sys
import traceback
from datetime import datetime, timezone

import requests

UTC_NOW = datetime.now(timezone.utc).isoformat()
DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

COINMETRICS_BASE = "https://community-api.coinmetrics.io/v4"
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
FNG_URL = "https://api.alternative.me/fng/"
BINANCE_FAPI_BASE = "https://fapi.binance.com"
DERIBIT_BASE = "https://www.deribit.com/api/v2"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

REQUEST_TIMEOUT = 20
UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; btc-cycle-dashboard/1.0; +https://github.com/)"
}


def log(msg):
    print(f"[fetch.py] {msg}", file=sys.stderr)


def load_existing():
    try:
        with open(DATA_PATH, "r") as f:
            return json.load(f)
    except Exception:
        log("no existing data.json found, starting from empty shell")
        return {}


def safe(label, fn, *args, **kwargs):
    """Run fn, return (result, error). Never raises."""
    try:
        result = fn(*args, **kwargs)
        log(f"OK   {label}")
        return result, None
    except Exception as e:
        log(f"FAIL {label}: {e}")
        traceback.print_exc(file=sys.stderr)
        return None, str(e)


# ---------------------------------------------------------------------------
# Coin Metrics Community API — PriceUSD + volume_reported_spot_usd_1d.
# https://docs.coinmetrics.io/api/v4/ — community endpoints need no API key,
# rate limit ~10 req / 6s per IP.
#
# VERIFIED AGAINST LIVE API (see README "已知的坑"): CapMrktCurUSD and SplyCur
# are free, but CapRealUSD (realized cap) returns 403
# "not available with supplied credentials" on the community tier — it is
# gated behind Coin Metrics' paid plans. That makes it impossible to compute
# MVRV-Z or realized price from Coin Metrics without a paid key, contrary to
# the original plan. See fetch_mvrv_z_bitcoindata() / fetch_realized_price_history_bitcoindata()
# below for the substitute free source.
#
# volume_reported_spot_usd_1d, by contrast, IS free on the community tier
# (catalog confirms "community": true, full history since 2010-07-18) — an
# aggregate reported spot volume across exchanges, which is both a better
# methodology and a more reliable source than the alternatives tried here:
# CoinGecko's historical endpoint now needs a paid Demo API key (401
# without one); CoinMarketCap's free Basic tier excludes historical data
# entirely; Binance's public klines are free and keyless but 451-blocked
# from GitHub Actions' runner IPs (same block already hit on the futures
# funding-rate endpoint). Coin Metrics has neither problem and we're
# already calling it for price, so volume rides along in the same request.
# ---------------------------------------------------------------------------
def fetch_coinmetrics_price_series():
    url = f"{COINMETRICS_BASE}/timeseries/asset-metrics"
    params = {
        "assets": "btc",
        "metrics": "PriceUSD,volume_reported_spot_usd_1d",
        "frequency": "1d",
        "page_size": 10000,
        "start_time": "2010-07-01T00:00:00Z",
    }
    out = []
    next_params = params
    for _ in range(10):  # pagination safety cap
        r = requests.get(url, params=next_params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        out.extend(payload.get("data", []))
        token = payload.get("next_page_token")
        if not token:
            break
        next_params = dict(params)
        next_params["next_page_token"] = token
    if not out:
        raise RuntimeError("coinmetrics returned no data")
    clean = []
    for row in out:
        try:
            entry = {"date": row["time"][:10], "price": float(row["PriceUSD"])}
        except (TypeError, KeyError, ValueError):
            continue
        vol = row.get("volume_reported_spot_usd_1d")
        entry["volume"] = float(vol) if vol not in (None, "") else None
        clean.append(entry)
    if len(clean) < 1500:
        raise RuntimeError(f"coinmetrics returned too few clean rows ({len(clean)})")
    clean.sort(key=lambda r: r["date"])
    return clean


def _thin_history(history, recent_days=730, step=7):
    if len(history) <= recent_days:
        return history
    older, recent = history[:-recent_days], history[-recent_days:]
    return older[::step] + recent


# ---------------------------------------------------------------------------
# bitcoin-data.com — free, unofficial community mirror API (no key), used
# only for MVRV-Z and realized price since Coin Metrics gates CapRealUSD
# behind a paid plan. Rate limit observed: 10 requests/hour on the free tier,
# so fetch.py must make at most a couple of calls here — fine for a
# once-a-day cron, NOT fine for repeated manual/local testing in a loop.
# History only goes back to ~2022 (not full BTC history), which is enough
# for a meaningful sparkline but shorter than the original all-time-history
# design. Z-score methodology is the source's own (standard "deviation from
# realized cap in stdevs of market cap" definition, matches the commonly
# cited 0/7 thresholds) — we do not recompute it ourselves.
# ---------------------------------------------------------------------------
BITCOIN_DATA_BASE = "https://bitcoin-data.com/v1"


def fetch_mvrv_z_bitcoindata():
    r = requests.get(f"{BITCOIN_DATA_BASE}/mvrv-zscore", headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("bitcoin-data.com mvrv-zscore returned no data")
    rows.sort(key=lambda row: row["d"])
    history = [[row["d"], round(float(row["mvrvZscore"]), 4)] for row in rows]
    latest = rows[-1]
    return {
        "value": round(float(latest["mvrvZscore"]), 4),
        "asof": latest["d"],
        "history": _thin_history(history),
    }


def fetch_realized_price_history_bitcoindata():
    """Full history in one call (its last row doubles as the latest value),
    to stay well under bitcoin-data.com's 10 req/hour free-tier limit."""
    r = requests.get(f"{BITCOIN_DATA_BASE}/realized-price", headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("bitcoin-data.com realized-price returned no data")
    rows.sort(key=lambda row: row["d"])
    return [(row["d"], float(row["realizedPrice"])) for row in rows]


def fetch_lth_mvrv_bitcoindata():
    r = requests.get(f"{BITCOIN_DATA_BASE}/lth-mvrv", headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("bitcoin-data.com lth-mvrv returned no data")
    rows.sort(key=lambda row: row["d"])
    return [(row["d"], float(row["lthMvrv"])) for row in rows]


def fetch_sth_mvrv_bitcoindata():
    r = requests.get(f"{BITCOIN_DATA_BASE}/sth-mvrv", headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("bitcoin-data.com sth-mvrv returned no data")
    rows.sort(key=lambda row: row["d"])
    return [(row["d"], float(row["sthMvrv"])) for row in rows]


def compute_lth_sth_mvrv_history(lth_pairs, sth_pairs):
    """Both series cover the same ~4-year window (bitcoin-data.com's history
    depth), so this is a straight union-of-dates align rather than the
    weekly-resample trick cost-basis needed to bridge full-history price
    against a shorter realized-price range."""
    lth_map = dict(lth_pairs)
    sth_map = dict(sth_pairs)
    dates = sorted(set(lth_map) | set(sth_map))
    if len(dates) < 30:
        raise RuntimeError("not enough overlapping LTH/STH-MVRV history")
    lth_series = [round(lth_map[d], 4) if d in lth_map else None for d in dates]
    sth_series = [round(sth_map[d], 4) if d in sth_map else None for d in dates]

    if len(dates) <= 730:
        return {"dates": dates, "lth_mvrv": lth_series, "sth_mvrv": sth_series}
    cut = len(dates) - 730
    return {
        "dates": dates[:cut][::7] + dates[cut:],
        "lth_mvrv": lth_series[:cut][::7] + lth_series[cut:],
        "sth_mvrv": sth_series[:cut][::7] + sth_series[cut:],
    }


def _weekly_resample(daily_pairs):
    """[(date_str, value), ...] -> {(iso_year, iso_week): (date_str, value)},
    keeping the last value seen in each ISO week."""
    weekly = {}
    for date_str, value in daily_pairs:
        key = datetime.strptime(date_str, "%Y-%m-%d").isocalendar()[:2]
        weekly[key] = (date_str, value)
    return weekly


def compute_cost_basis_history(cm_series, realized_pairs):
    """Weekly-aligned {dates, price, realized_price, ma_200w} for the cost-basis
    chart. Weekly cadence (not daily) so a rolling 200-period mean is a real
    200-WEEK moving average, and so the three series — full BTC price history
    since 2010, realized price only from ~2022 (bitcoin-data.com's range) —
    can share one x-axis without a daily-resolution alignment headache.
    realized_price/ma_200w are null for weeks before each series has enough
    history; the frontend just skips drawing those segments."""
    price_weekly = _weekly_resample([(r["date"], r["price"]) for r in cm_series])
    realized_weekly = _weekly_resample(realized_pairs)

    weeks = sorted(price_weekly.keys())
    if len(weeks) < 210:
        raise RuntimeError("not enough weekly price data for cost-basis history")

    dates = [price_weekly[w][0] for w in weeks]
    prices = [round(price_weekly[w][1], 2) for w in weeks]

    ma_200w_series = [None] * len(prices)
    window_sum = 0.0
    for i, p in enumerate(prices):
        window_sum += p
        if i >= 200:
            window_sum -= prices[i - 200]
        if i >= 199:
            ma_200w_series[i] = round(window_sum / 200, 2)

    realized_series = [
        round(realized_weekly[w][1], 2) if w in realized_weekly else None
        for w in weeks
    ]

    return {
        "dates": dates,
        "price": prices,
        "realized_price": realized_series,
        "ma_200w": ma_200w_series,
    }


def compute_realized_vol(series):
    """30d realized vol (annualized) from daily log returns, its percentile
    rank against the full history of rolling-30d realized vol, and the
    rolling series itself for a sparkline."""
    prices = [r["price"] for r in series]
    dates = [r["date"] for r in series]
    log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    if len(log_returns) < 400:
        raise RuntimeError("not enough return history for realized vol")

    def ann_vol(window):
        mean = sum(window) / len(window)
        var = sum((x - mean) ** 2 for x in window) / (len(window) - 1)
        return math.sqrt(var) * math.sqrt(365)

    rolling = []
    rolling_dates = []
    for k in range(30, len(log_returns) + 1):
        rolling.append(ann_vol(log_returns[k - 30:k]))
        rolling_dates.append(dates[k])

    current = rolling[-1]
    rank = sum(1 for v in rolling if v <= current) / len(rolling)
    history = [[d, round(v, 4)] for d, v in zip(rolling_dates, rolling)]
    return {
        "value": round(current, 4),
        "asof": rolling_dates[-1],
        "percentile": round(rank, 4),
        "history": _thin_history(history),
    }


# ---------------------------------------------------------------------------
# FRED — 10Y nominal (DGS10), 10Y real / TIPS (DFII10), broad dollar (DTWEXBGS)
# https://fred.stlouisfed.org/docs/api/fred/series_observations.html
# Free API key required, stored as FRED_API_KEY secret.
# ---------------------------------------------------------------------------
def fetch_fred_series(series_id, api_key):
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 10,
    }
    r = requests.get(FRED_BASE, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    obs = r.json().get("observations", [])
    for o in obs:  # FRED uses "." for missing values; walk back to latest real print
        if o["value"] not in (".", "", None):
            return {"value": round(float(o["value"]), 4), "asof": o["date"]}
    raise RuntimeError(f"no valid observation for {series_id}")


# ---------------------------------------------------------------------------
# ETF flows — TFTC's open dataset, not Farside directly.
#
# VERIFIED AGAINST LIVE SITE: farside.co.uk sits behind a Cloudflare JS
# challenge ("Just a moment...") that returns 403 to any plain HTTP client —
# curl, requests, pandas.read_html, all of it. No amount of User-Agent/Accept
# header tweaking gets past it; it needs an actual JS-executing browser
# (Playwright headless etc.), which is out of scope for a lightweight daily
# cron. TFTC (tftc.io) publishes a CC BY 4.0 JSON mirror of US spot BTC ETF
# flows — compiled from SoSoValue + issuer disclosures as tabulated by
# Farside itself — with an open CORS header and no auth. Same underlying
# data, actually reachable. https://www.tftc.io/bitcoin-etf-flows
# ---------------------------------------------------------------------------
TFTC_ETF_URL = "https://www.tftc.io/bitcoin-etf-flows/data.json"


def fetch_tftc_etf_flows():
    r = requests.get(TFTC_ETF_URL, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    days = payload.get("days", [])
    if not days:
        raise RuntimeError("tftc.io returned no daily flow rows")

    rows = [(d["date"], round(d["netFlowUsd"] / 1e6, 2)) for d in days if d.get("netFlowUsd") is not None]
    if not rows:
        raise RuntimeError("tftc.io rows had no usable netFlowUsd")
    rows.sort(key=lambda r: r[0])

    cumulative = sum(v for _, v in rows)
    latest_date, latest_val = rows[-1]
    return {
        "value": latest_val,
        "asof": latest_date,
        "history": [[d, v] for d, v in rows[-90:]],
        "cumulative": round(cumulative, 1),
    }


# ---------------------------------------------------------------------------
# Gold (optional) — yfinance GC=F futures
# ---------------------------------------------------------------------------
def fetch_gold(btc_price):
    import yfinance as yf

    hist = yf.Ticker("GC=F").history(period="5d")
    if hist.empty:
        raise RuntimeError("yfinance returned no gold data")
    latest = hist.iloc[-1]
    price = float(latest["Close"])
    asof = hist.index[-1].strftime("%Y-%m-%d")
    ratio = (btc_price / price) if btc_price else None
    return {"value": round(price, 2), "asof": asof, "btc_gold_ratio": round(ratio, 4) if ratio else None}


# ---------------------------------------------------------------------------
# Saylor / Strategy (MSTR) BTC holdings — no official API, so this scrapes
# the rendered number off bitcointreasuries.net's Strategy page. Unlike
# Farside, this page is NOT behind a Cloudflare challenge — plain requests
# get a normal 200. The BTC count is anchored to a stable, distinctive
# marker (the "₿" balance figure), independent of the site's page layout
# text around it. The average cost basis (bonus context for judging
# "forced seller" risk) comes from an inline data blob further down the
# page keyed to that same balance figure; if the site's markup changes
# enough to break that second regex, the core BTC count still comes
# through fine on its own — each extraction fails independently.
# ---------------------------------------------------------------------------
def fetch_saylor_holdings():
    url = "https://bitcointreasuries.net/public-companies/strategy"
    r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    # The server doesn't send a charset in Content-Type, so requests falls back
    # to ISO-8859-1 per the HTTP spec default and mangles the ₿ character —
    # verified: r.encoding comes back "ISO-8859-1" while r.apparent_encoding
    # (chardet's actual sniff) correctly says "utf-8". Force it, or the regex
    # below silently never matches.
    r.encoding = "utf-8"
    html = r.text

    m = re.search(r'font-btc[^>]*>₿</span>([\d,]+).{0,120}?As of ([A-Za-z]+ \d{1,2}, \d{4})', html, re.DOTALL)
    if not m:
        raise RuntimeError("could not find BTC balance + as-of-date marker on bitcointreasuries.net")
    balance = int(m.group(1).replace(",", ""))
    asof = datetime.strptime(m.group(2), "%b %d, %Y").strftime("%Y-%m-%d")

    avg_cost = None
    m2 = re.search(
        r'balance:' + str(balance) + r',date:"[\d-]+",cost_basis:\{value:app\.decode\(.BigDecimal., "(\d+)"',
        html,
    )
    if m2:
        avg_cost = round(float(m2.group(1)) / balance, 2)

    return {"value": balance, "asof": asof, "avg_cost": avg_cost}


# ---------------------------------------------------------------------------
# CoinGecko — spot price / ATH / drawdown (keyless, still fine as of last check)
# ---------------------------------------------------------------------------
def fetch_coingecko_snapshot():
    url = f"{COINGECKO_BASE}/coins/bitcoin"
    params = {"localization": "false", "tickers": "false", "community_data": "false", "developer_data": "false"}
    r = requests.get(url, params=params, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    md = r.json()["market_data"]
    price = md["current_price"]["usd"]
    ath = md["ath"]["usd"]
    ath_date = md["ath_date"]["usd"][:10]
    drawdown = (price - ath) / ath
    return {
        "price": price,
        "ath": ath,
        "ath_date": ath_date,
        "drawdown_from_ath": round(drawdown, 4),
    }


def compute_volume_percentile(cm_series):
    """Uses volume_reported_spot_usd_1d already pulled alongside price in
    fetch_coinmetrics_price_series() — see that function's docstring for why
    this replaced the CoinGecko/CoinMarketCap/Binance attempts."""
    rows = [(r["date"], r["volume"]) for r in cm_series if r.get("volume") is not None]
    if not rows:
        raise RuntimeError("no volume data in coinmetrics series")
    volumes = [v for _, v in rows]
    current = volumes[-1]
    latest_date = rows[-1][0]
    rank = sum(1 for v in volumes if v <= current) / len(volumes)
    hist_pairs = [[d, round(v, 0)] for d, v in rows]
    return {
        "value": round(current, 0),
        "asof": latest_date,
        "percentile": round(rank, 4),
        "history": _thin_history(hist_pairs),
    }


# ---------------------------------------------------------------------------
# Realtime snapshot (fallback copy for client-side A-class fetches)
# ---------------------------------------------------------------------------
def fetch_fng_snapshot():
    r = requests.get(FNG_URL, params={"limit": 1}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    d = r.json()["data"][0]
    asof = datetime.fromtimestamp(int(d["timestamp"]), tz=timezone.utc).strftime("%Y-%m-%d")
    return {"value": int(d["value"]), "classification": d["value_classification"], "asof": asof}


def fetch_binance_funding_oi():
    r1 = requests.get(f"{BINANCE_FAPI_BASE}/fapi/v1/premiumIndex", params={"symbol": "BTCUSDT"}, timeout=REQUEST_TIMEOUT)
    r1.raise_for_status()
    funding = float(r1.json()["lastFundingRate"])

    r2 = requests.get(f"{BINANCE_FAPI_BASE}/fapi/v1/openInterest", params={"symbol": "BTCUSDT"}, timeout=REQUEST_TIMEOUT)
    r2.raise_for_status()
    oi = float(r2.json()["openInterest"])

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (
        {"value": funding, "asof": today},
        {"value": oi, "asof": today},
    )


def fetch_dvol_snapshot():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - 2 * 24 * 3600 * 1000
    r = requests.get(
        f"{DERIBIT_BASE}/public/get_volatility_index_data",
        params={"currency": "BTC", "start_timestamp": start_ms, "end_timestamp": now_ms, "resolution": "3600"},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()["result"]["data"]
    if not data:
        raise RuntimeError("no dvol data returned")
    last = data[-1]  # [timestamp, open, high, low, close]
    asof = datetime.fromtimestamp(last[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return {"value": round(float(last[4]), 2), "asof": asof}


# ---------------------------------------------------------------------------
def merge_field(existing_field, new_value, stale):
    """Preserve prior value on failure, mark stale accordingly."""
    if new_value is not None:
        new_value["stale"] = False
        return new_value
    if existing_field:
        existing_field["stale"] = True
        return existing_field
    return {"value": None, "asof": None, "stale": True}


def main():
    data = load_existing()
    fred_key = os.environ.get("FRED_API_KEY")

    cm_series, _ = safe("coinmetrics price series", fetch_coinmetrics_price_series)

    realized_vol_result = None
    if cm_series:
        realized_vol_result, _ = safe("realized vol compute", compute_realized_vol, cm_series)

    mvrv_z_result, _ = safe("mvrv-z (bitcoin-data.com)", fetch_mvrv_z_bitcoindata)
    realized_pairs, _ = safe("realized price history (bitcoin-data.com)", fetch_realized_price_history_bitcoindata)
    realized_price_result = None
    if realized_pairs:
        latest_date, latest_val = realized_pairs[-1]
        realized_price_result = {"value": round(latest_val, 2), "asof": latest_date}

    cost_basis_result = None
    if cm_series and realized_pairs:
        cost_basis_result, _ = safe("cost-basis history compute", compute_cost_basis_history, cm_series, realized_pairs)

    lth_pairs, _ = safe("lth-mvrv (bitcoin-data.com)", fetch_lth_mvrv_bitcoindata)
    sth_pairs, _ = safe("sth-mvrv (bitcoin-data.com)", fetch_sth_mvrv_bitcoindata)
    lth_sth_result = None
    if lth_pairs and sth_pairs:
        lth_sth_result, _ = safe("lth/sth-mvrv history compute", compute_lth_sth_mvrv_history, lth_pairs, sth_pairs)

    cg_snapshot, _ = safe("coingecko snapshot", fetch_coingecko_snapshot)

    volume_result = None
    if cm_series:
        volume_result, _ = safe("volume percentile compute", compute_volume_percentile, cm_series)

    etf_result, _ = safe("etf flows (tftc.io)", fetch_tftc_etf_flows)

    fred_results = {}
    if fred_key:
        for label, series_id in [("nominal_10y", "DGS10"), ("real_10y", "DFII10"), ("dxy", "DTWEXBGS")]:
            fred_results[label], _ = safe(f"fred {series_id}", fetch_fred_series, series_id, fred_key)
    else:
        log("FRED_API_KEY not set — skipping macro fetch, keeping prior values")

    gold_result = None
    if cg_snapshot:
        gold_result, _ = safe("gold (yfinance)", fetch_gold, cg_snapshot["price"])

    saylor_result, _ = safe("saylor/strategy holdings", fetch_saylor_holdings)

    fng_result, _ = safe("fear & greed snapshot", fetch_fng_snapshot)
    funding_oi, _ = safe("binance funding/oi", fetch_binance_funding_oi)
    funding_result, oi_result = funding_oi if funding_oi else (None, None)
    dvol_result, _ = safe("deribit dvol snapshot", fetch_dvol_snapshot)

    out = dict(data)
    out["generated_at"] = UTC_NOW

    mvrv_ratio = None
    if cg_snapshot and realized_price_result and realized_price_result["value"]:
        mvrv_ratio = round(cg_snapshot["price"] / realized_price_result["value"], 4)

    out["mvrv_z"] = merge_field(
        data.get("mvrv_z"),
        {"value": mvrv_z_result["value"], "asof": mvrv_z_result["asof"], "source": "bitcoin-data.com",
         "extra": {"mvrv_ratio": mvrv_ratio, "history": mvrv_z_result["history"]}}
        if mvrv_z_result else None,
        stale=True,
    )
    out["realized_price"] = merge_field(
        data.get("realized_price"),
        {"value": realized_price_result["value"], "asof": realized_price_result["asof"], "source": "bitcoin-data.com"}
        if realized_price_result else None,
        stale=True,
    )

    ma_200w_latest, ma_200w_asof = None, None
    if cost_basis_result:
        for d, v in zip(reversed(cost_basis_result["dates"]), reversed(cost_basis_result["ma_200w"])):
            if v is not None:
                ma_200w_latest, ma_200w_asof = v, d
                break
    out["ma_200w"] = merge_field(
        data.get("ma_200w"),
        {"value": ma_200w_latest, "asof": ma_200w_asof, "source": "computed"}
        if ma_200w_latest is not None else None,
        stale=True,
    )

    out["cost_basis_history"] = merge_field(
        data.get("cost_basis_history"),
        {
            "value": None, "asof": cost_basis_result["dates"][-1] if cost_basis_result else None,
            "source": "coinmetrics + bitcoin-data.com, weekly",
            "extra": cost_basis_result,
        } if cost_basis_result else None,
        stale=True,
    )

    out["lth_sth_mvrv_history"] = merge_field(
        data.get("lth_sth_mvrv_history"),
        {
            "value": None, "asof": lth_sth_result["dates"][-1] if lth_sth_result else None,
            "source": "bitcoin-data.com",
            "extra": lth_sth_result,
        } if lth_sth_result else None,
        stale=True,
    )

    price_extra_prev = (data.get("price") or {}).get("extra", {})
    out["price"] = merge_field(
        data.get("price"),
        {
            "value": cg_snapshot["price"], "asof": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "source": "coingecko",
            "extra": {
                "ath": cg_snapshot["ath"], "ath_date": cg_snapshot["ath_date"],
                "drawdown_from_ath": cg_snapshot["drawdown_from_ath"],
            },
        } if cg_snapshot else None,
        stale=True,
    )
    if out["price"].get("extra") is None:
        out["price"]["extra"] = price_extra_prev

    out["realized_vol"] = merge_field(
        data.get("realized_vol"),
        {
            "value": realized_vol_result["value"], "asof": realized_vol_result["asof"], "source": "computed",
            "extra": {"percentile": realized_vol_result["percentile"],
                      "dvol_fallback": dvol_result["value"] if dvol_result else None,
                      "history": realized_vol_result["history"]},
        } if realized_vol_result else None,
        stale=True,
    )

    out["volume"] = merge_field(
        data.get("volume"),
        {
            "value": volume_result["value"], "asof": volume_result["asof"], "source": "coinmetrics",
            "extra": {"percentile": volume_result["percentile"], "history": volume_result["history"]},
        } if volume_result else None,
        stale=True,
    )

    out["etf_flow"] = merge_field(
        data.get("etf_flow"),
        {
            "value": etf_result["value"], "asof": etf_result["asof"], "source": "tftc.io",
            "extra": {"history": etf_result["history"], "cumulative": etf_result["cumulative"]},
        } if etf_result else None,
        stale=True,
    )

    macro = dict(data.get("macro", {}))
    for label, series_id in [("nominal_10y", "DGS10"), ("real_10y", "DFII10"), ("dxy", "DTWEXBGS")]:
        r = fred_results.get(label)
        macro[label] = merge_field(
            macro.get(label),
            {"value": r["value"], "asof": r["asof"], "source": f"fred:{series_id}"} if r else None,
            stale=True,
        )
    gold_extra_prev = (macro.get("gold") or {}).get("extra", {})
    macro["gold"] = merge_field(
        macro.get("gold"),
        {"value": gold_result["value"], "asof": gold_result["asof"], "source": "yfinance:GC=F",
         "extra": {"btc_gold_ratio": gold_result["btc_gold_ratio"]}} if gold_result else None,
        stale=True,
    )
    if macro["gold"].get("extra") is None:
        macro["gold"]["extra"] = gold_extra_prev
    out["macro"] = macro

    out["saylor_holdings"] = merge_field(
        data.get("saylor_holdings"),
        {
            "value": saylor_result["value"], "asof": saylor_result["asof"], "source": "bitcointreasuries.net",
            "extra": {"avg_cost": saylor_result["avg_cost"]},
        } if saylor_result else None,
        stale=True,
    )

    prev_rt = data.get("realtime_fallback", {})
    out["realtime_fallback"] = {
        "fng": fng_result or prev_rt.get("fng", {"value": None, "classification": None, "asof": None}),
        "funding_rate": funding_result or prev_rt.get("funding_rate", {"value": None, "asof": None}),
        "open_interest": oi_result or prev_rt.get("open_interest", {"value": None, "asof": None}),
        "dvol": dvol_result or prev_rt.get("dvol", {"value": None, "asof": None}),
    }

    with open(DATA_PATH, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    log(f"wrote {DATA_PATH}")


if __name__ == "__main__":
    main()
