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
FARSIDE_URL = "https://farside.co.uk/btc/"
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
# Coin Metrics Community API — PriceUSD only.
# https://docs.coinmetrics.io/api/v4/ — community endpoints need no API key,
# rate limit ~10 req / 6s per IP.
#
# VERIFIED AGAINST LIVE API (see README "已知的坑"): CapMrktCurUSD and SplyCur
# are free, but CapRealUSD (realized cap) returns 403
# "not available with supplied credentials" on the community tier — it is
# gated behind Coin Metrics' paid plans. That makes it impossible to compute
# MVRV-Z or realized price from Coin Metrics without a paid key, contrary to
# the original plan. See fetch_mvrv_z_bitcoindata() / fetch_realized_price_bitcoindata()
# below for the substitute free source. Coin Metrics is kept only for the
# full daily price history used in the 200w MA and realized-vol calcs.
# ---------------------------------------------------------------------------
def fetch_coinmetrics_price_series():
    url = f"{COINMETRICS_BASE}/timeseries/asset-metrics"
    params = {
        "assets": "btc",
        "metrics": "PriceUSD",
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
            clean.append({"date": row["time"][:10], "price": float(row["PriceUSD"])})
        except (TypeError, KeyError, ValueError):
            continue
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


def fetch_realized_price_bitcoindata():
    r = requests.get(f"{BITCOIN_DATA_BASE}/realized-price/last", headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    row = r.json()
    return {"value": round(float(row["realizedPrice"]), 2), "asof": row["d"]}


def compute_ma_200w(series):
    """Resample daily closes to weekly (last obs of each week), rolling 200w mean."""
    weekly = {}
    for r in series:
        d = datetime.strptime(r["date"], "%Y-%m-%d")
        year, week, _ = d.isocalendar()
        weekly[(year, week)] = r["price"]  # overwritten by later days -> last obs
    weekly_vals = [v for _, v in sorted(weekly.items())]
    if len(weekly_vals) < 200:
        raise RuntimeError("not enough weekly data for 200w MA")
    ma = sum(weekly_vals[-200:]) / 200
    return {"value": round(ma, 2), "asof": series[-1]["date"]}


def compute_realized_vol(series):
    """30d realized vol (annualized) from daily log returns, plus its
    percentile rank against the full history of rolling-30d realized vol."""
    prices = [r["price"] for r in series]
    log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    if len(log_returns) < 400:
        raise RuntimeError("not enough return history for realized vol")

    def ann_vol(window):
        mean = sum(window) / len(window)
        var = sum((x - mean) ** 2 for x in window) / (len(window) - 1)
        return math.sqrt(var) * math.sqrt(365)

    rolling = []
    for i in range(30, len(log_returns) + 1):
        rolling.append(ann_vol(log_returns[i - 30:i]))

    current = rolling[-1]
    rank = sum(1 for v in rolling if v <= current) / len(rolling)
    return {
        "value": round(current, 4),
        "asof": series[-1]["date"],
        "percentile": round(rank, 4),
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
# Farside — no official API, scrape the HTML table. Structure may change;
# this is a best-effort parse guarded end-to-end by try/except in main().
# ---------------------------------------------------------------------------
def fetch_farside_etf_flows():
    import pandas as pd

    r = requests.get(FARSIDE_URL, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    tables = pd.read_html(r.text)
    if not tables:
        raise RuntimeError("no tables found on farside page")
    # pick the largest table on the page — that's the daily flow table
    df = max(tables, key=lambda t: t.shape[0])
    df = df.dropna(how="all")

    # last column is usually "Total"; first column is the date
    date_col = df.columns[0]
    total_col = df.columns[-1]

    def to_num(x):
        if isinstance(x, str):
            x = x.replace(",", "").replace("(", "-").replace(")", "").strip()
            if x in ("", "-", "nan"):
                return None
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    rows = []
    for _, row in df.iterrows():
        date_val = str(row[date_col])
        total_val = to_num(row[total_col])
        if total_val is None:
            continue
        if any(k in date_val.lower() for k in ["total", "average", "nan"]):
            continue
        rows.append((date_val, total_val))

    if not rows:
        raise RuntimeError("could not parse any daily flow rows from farside table")

    trailing_5d = rows[-5:]
    cumulative = sum(v for _, v in rows)
    latest_date, latest_val = rows[-1]
    return {
        "value": latest_val,
        "asof": latest_date,
        "trailing_5d": [{"date": d, "value": v} for d, v in trailing_5d],
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
# CoinGecko — spot price / ATH / drawdown / 24h volume (also used client-side)
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
    volume = md["total_volume"]["usd"]
    drawdown = (price - ath) / ath
    return {
        "price": price,
        "ath": ath,
        "ath_date": ath_date,
        "drawdown_from_ath": round(drawdown, 4),
        "volume": volume,
    }


def compute_volume_percentile(current_volume, cg_api_key):
    """Percentile using CoinGecko's daily market-chart history.

    VERIFIED AGAINST LIVE API: /coins/bitcoin/market_chart now returns 401
    without a (free, registration-required) CoinGecko Demo API key — the
    fully-keyless free tier no longer covers historical chart data. Optional
    via COINGECKO_API_KEY secret; if absent this is skipped (volume.value
    itself, from /coins/bitcoin, is unaffected and still keyless).
    """
    if not cg_api_key:
        raise RuntimeError("COINGECKO_API_KEY not set — skipping volume percentile")
    url = f"{COINGECKO_BASE}/coins/bitcoin/market_chart"
    params = {"vs_currency": "usd", "days": "1825", "interval": "daily"}
    headers = dict(UA_HEADERS)
    headers["x-cg-demo-api-key"] = cg_api_key
    r = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    volumes = [v for _, v in r.json().get("total_volumes", [])]
    if not volumes:
        raise RuntimeError("no volume history from coingecko")
    rank = sum(1 for v in volumes if v <= current_volume) / len(volumes)
    return round(rank, 4)


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

    ma_200w_result, realized_vol_result = None, None
    if cm_series:
        ma_200w_result, _ = safe("200w ma compute", compute_ma_200w, cm_series)
        realized_vol_result, _ = safe("realized vol compute", compute_realized_vol, cm_series)

    mvrv_z_result, _ = safe("mvrv-z (bitcoin-data.com)", fetch_mvrv_z_bitcoindata)
    realized_price_result, _ = safe("realized price (bitcoin-data.com)", fetch_realized_price_bitcoindata)

    cg_snapshot, _ = safe("coingecko snapshot", fetch_coingecko_snapshot)

    cg_api_key = os.environ.get("COINGECKO_API_KEY")
    volume_pct = None
    if cg_snapshot:
        volume_pct, _ = safe("volume percentile", compute_volume_percentile, cg_snapshot["volume"], cg_api_key)

    farside_result, _ = safe("farside etf flows", fetch_farside_etf_flows)

    fred_results = {}
    if fred_key:
        for label, series_id in [("nominal_10y", "DGS10"), ("real_10y", "DFII10"), ("dxy", "DTWEXBGS")]:
            fred_results[label], _ = safe(f"fred {series_id}", fetch_fred_series, series_id, fred_key)
    else:
        log("FRED_API_KEY not set — skipping macro fetch, keeping prior values")

    gold_result = None
    if cg_snapshot:
        gold_result, _ = safe("gold (yfinance)", fetch_gold, cg_snapshot["price"])

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
    out["ma_200w"] = merge_field(
        data.get("ma_200w"),
        {"value": ma_200w_result["value"], "asof": ma_200w_result["asof"], "source": "computed"}
        if ma_200w_result else None,
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
                      "dvol_fallback": dvol_result["value"] if dvol_result else None},
        } if realized_vol_result else None,
        stale=True,
    )

    out["volume"] = merge_field(
        data.get("volume"),
        {
            "value": cg_snapshot["volume"], "asof": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "source": "coingecko", "extra": {"percentile": volume_pct},
        } if cg_snapshot else None,
        stale=True,
    )

    out["etf_flow"] = merge_field(
        data.get("etf_flow"),
        {
            "value": farside_result["value"], "asof": farside_result["asof"], "source": "farside",
            "extra": {"trailing_5d": farside_result["trailing_5d"], "cumulative": farside_result["cumulative"]},
        } if farside_result else None,
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

    # saylor_holdings is manual — never overwritten by this script
    out["saylor_holdings"] = data.get("saylor_holdings", {
        "value": None, "asof": None,
        "note": "手填，参考 bitcointreasuries.net / strategy.com 持仓披露",
    })

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
