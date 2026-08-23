"use strict";

const GITHUB_REPO_URL = "https://github.com/Badkido/btc-cycle-dashboard";

const FETCH_TIMEOUT_MS = 8000;
const IFRAME_TIMEOUT_MS = 6000;

/* ---------------- formatters ---------------- */
const fmtUSD = (v, digits = 0) =>
  v == null ? "—" : "$" + Number(v).toLocaleString("en-US", { maximumFractionDigits: digits, minimumFractionDigits: digits });
const fmtUSDCompact = (v) =>
  v == null ? "—" : "$" + Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(v);
const fmtPct = (v, digits = 1) => (v == null ? "—" : (v * 100).toFixed(digits) + "%");
const fmtPctSigned = (v, digits = 1) => (v == null ? "—" : (v >= 0 ? "+" : "") + (v * 100).toFixed(digits) + "%");
const fmtNum = (v, digits = 2) => (v == null ? "—" : Number(v).toFixed(digits));
const fmtBTC = (v) => (v == null ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 0 }) + " BTC");

function daysStale(asof) {
  if (!asof) return Infinity;
  const d = new Date(asof);
  if (isNaN(d)) return Infinity;
  return (Date.now() - d.getTime()) / 86400000;
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function setDot(id, state) {
  const el = document.getElementById(id);
  if (el) el.dataset.state = state || "neutral";
}

function setAsof(id, asof, stale) {
  const el = document.getElementById(id);
  if (!el) return;
  const label = asof ? `截至 ${asof}` : "暂无数据";
  el.innerHTML = stale ? `${label} <span class="stale-flag">· 陈旧</span>` : label;
}

/* ---------------- fetch helpers ---------------- */
async function fetchWithTimeout(url, opts = {}, timeout = FETCH_TIMEOUT_MS) {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeout);
  try {
    const res = await fetch(url, { ...opts, signal: controller.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(id);
  }
}

async function loadDataJson() {
  try {
    const res = await fetch("./data.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (e) {
    console.error("data.json load failed", e);
    return null;
  }
}

/* ============================================================
 * LAYER 1 — 估值/成本
 * ============================================================ */
function renderMvrvZ(data) {
  const f = data?.mvrv_z;
  if (!f || f.value == null) {
    setText("val-mvrv-z", "—");
    setDot("dot-mvrv-z", "neutral");
    setAsof("asof-mvrv-z", null, true);
    return;
  }
  const z = f.value;
  setText("val-mvrv-z", z.toFixed(2));
  setText("sub-mvrv-z", f.extra?.mvrv_ratio != null ? `MVRV 比率 ${f.extra.mvrv_ratio.toFixed(2)}x` : "");
  setAsof("asof-mvrv-z", f.asof, f.stale);

  let state = "neutral";
  if (z < 0) state = "deep-green";
  else if (z < 2) state = "green";
  else if (z < 4) state = "neutral";
  else if (z < 6) state = "orange";
  else state = "red";
  setDot("dot-mvrv-z", state);

  drawSparkline("spark-mvrv-z", f.extra?.history || []);
}

let mvrvChart = null;
function drawSparkline(canvasId, history) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !window.Chart || !history || history.length < 2) return;
  const labels = history.map((h) => h[0]);
  const values = history.map((h) => h[1]);
  if (mvrvChart) mvrvChart.destroy();
  mvrvChart = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          data: values,
          borderColor: "#f2a900",
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.15,
          fill: false,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { intersect: false },
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: {
        x: { display: false },
        y: {
          display: false,
          grid: { display: false },
        },
      },
      elements: { line: { capBezierPoints: false } },
    },
  });
}

function renderCostBasis(data) {
  const price = data?.price?.value;
  const realized = data?.realized_price?.value;
  const ma200w = data?.ma_200w?.value;

  setText("val-price-cb", fmtUSD(price));
  setText("val-realized", fmtUSD(realized));
  setText("val-200w", fmtUSD(ma200w));

  const asof = data?.price?.asof || data?.realized_price?.asof;
  const stale = !!(data?.price?.stale || data?.realized_price?.stale || data?.ma_200w?.stale);
  setAsof("asof-cost-basis", asof, stale);

  if (price == null || realized == null || ma200w == null) {
    setDot("dot-cost-basis", "neutral");
    return;
  }

  const floor = Math.max(realized, ma200w);
  let state = "neutral";
  if (price < Math.min(realized, ma200w)) state = "deep-green";
  else if (price < floor * 1.1) state = "green";
  setDot("dot-cost-basis", state);

  // position markers along [0, floor*1.6] range, clamped
  const rangeMax = floor * 1.6;
  const pos = (v) => Math.max(2, Math.min(98, (v / rangeMax) * 100));
  const priceEl = document.getElementById("marker-price");
  const realizedEl = document.getElementById("marker-realized");
  const ma200wEl = document.getElementById("marker-200w");
  if (priceEl) priceEl.style.left = pos(price) + "%";
  if (realizedEl) realizedEl.style.left = pos(realized) + "%";
  if (ma200wEl) ma200wEl.style.left = pos(ma200w) + "%";
}

function renderLthSth() {
  ["lth", "sth"].forEach((key) => {
    const iframe = document.getElementById(`iframe-${key}`);
    const fallback = document.getElementById(`fallback-${key}`);
    if (!iframe) return;
    let loaded = false;
    iframe.addEventListener("load", () => {
      loaded = true;
    });
    setTimeout(() => {
      // heuristic: if iframe never fired load (blocked/network error), show fallback
      if (!loaded) {
        try {
          // accessing contentWindow.location on a blocked cross-origin frame still
          // works (it's same-origin-policy opaque, not an error) — load event is
          // the reliable signal here.
        } catch (e) {
          /* ignore */
        }
      }
    }, IFRAME_TIMEOUT_MS);
    iframe.addEventListener("error", () => {
      iframe.hidden = true;
      if (fallback) fallback.hidden = false;
    });
  });
}

/* ============================================================
 * LAYER 2 — 波动率
 * ============================================================ */
function renderVol(data, rtDvol) {
  const rv = data?.realized_vol;
  setText("val-realized-vol", rv?.value != null ? fmtPct(rv.value, 1) : "—");
  if (rv?.extra?.percentile != null) {
    setText("pct-realized-vol", `历史分位 ${(rv.extra.percentile * 100).toFixed(0)}%`);
  }
  const dvolVal = rtDvol?.value ?? rv?.extra?.dvol_fallback;
  setText("val-dvol", dvolVal != null ? dvolVal.toFixed(1) + "%" : "—");
  setAsof("asof-vol", rv?.asof, rv?.stale);

  let state = "neutral";
  if (rv?.extra?.percentile != null && rv.extra.percentile < 0.2) state = "signal";
  setDot("dot-vol", state);
}

/* ============================================================
 * LAYER 3 — 流动性/参与度
 * ============================================================ */
function renderVolume(data) {
  const v = data?.volume;
  setText("val-volume", v?.value != null ? fmtUSDCompact(v.value) : "—");
  setAsof("asof-volume", v?.asof, v?.stale);
  const pct = v?.extra?.percentile;
  const fill = document.getElementById("fill-volume");
  if (fill) fill.style.width = pct != null ? Math.round(pct * 100) + "%" : "0%";
  setText("caption-volume", pct != null ? `历史分位 ${(pct * 100).toFixed(0)}%` : "");

  let state = "neutral";
  if (pct != null && pct < 0.2) {
    state = "green";
    fill && (fill.style.background = "var(--green)");
  } else if (fill) {
    fill.style.background = "var(--signal)";
  }
  setDot("dot-volume", state);
}

function renderEtf(data) {
  const e = data?.etf_flow;
  setText("val-etf", e?.value != null ? fmtUSDCompact(e.value * 1e6) : "—");
  setText("val-etf-cum", e?.extra?.cumulative != null ? fmtUSDCompact(e.extra.cumulative * 1e6) : "—");
  setAsof("asof-etf", e?.asof, e?.stale);

  const bars = document.getElementById("etf-bars");
  if (bars) {
    bars.innerHTML = "";
    const trailing = e?.extra?.trailing_5d || [];
    const maxAbs = Math.max(1, ...trailing.map((d) => Math.abs(d.value)));
    trailing.forEach((d) => {
      const bar = document.createElement("div");
      bar.className = "etf-bar " + (d.value >= 0 ? "pos" : "neg");
      bar.style.height = Math.max(4, (Math.abs(d.value) / maxAbs) * 44) + "px";
      bar.title = `${d.date}: ${d.value >= 0 ? "+" : ""}${d.value}`;
      bars.appendChild(bar);
    });
  }

  let state = "neutral";
  if (e?.value != null) state = e.value >= 0 ? "green" : "orange";
  setDot("dot-etf", state);
}

/* ============================================================
 * LAYER 4 — 情绪/杠杆
 * ============================================================ */
const FNG_LABELS = { "Extreme Fear": "极度恐惧", "Fear": "恐惧", "Neutral": "中性", "Greed": "贪婪", "Extreme Greed": "极度贪婪" };

function renderFng(rt) {
  if (!rt || rt.value == null) {
    setText("val-fng", "—");
    setDot("dot-fng", "neutral");
    return;
  }
  setText("val-fng", String(rt.value));
  setText("label-fng", FNG_LABELS[rt.classification] || rt.classification || "");
  setAsof("asof-fng", rt.asof, false);
  let state = "neutral";
  if (rt.value < 25) state = "green";
  else if (rt.value > 75) state = "orange";
  setDot("dot-fng", state);
}

function renderLeverage(rtFunding, rtOi) {
  setText("val-funding", rtFunding?.value != null ? fmtPctSigned(rtFunding.value, 4) : "—");
  setText("val-oi", rtOi?.value != null ? fmtBTC(rtOi.value) : "—");
  const asof = rtFunding?.asof || rtOi?.asof;
  setAsof("asof-leverage", asof, false);

  let state = "neutral";
  if (rtFunding?.value != null) {
    if (rtFunding.value < 0) state = "green";
    else if (rtFunding.value > 0.0005) state = "orange";
  }
  setDot("dot-leverage", state);
}

/* ============================================================
 * LAYER 5 — 宏观
 * ============================================================ */
function renderMacro(data) {
  const m = data?.macro || {};
  setText("val-real10y", m.real_10y?.value != null ? m.real_10y.value.toFixed(2) + "%" : "—");
  setText("val-nom10y", m.nominal_10y?.value != null ? m.nominal_10y.value.toFixed(2) + "%" : "—");
  setText("val-dxy", m.dxy?.value != null ? m.dxy.value.toFixed(2) : "—");
  const asof = m.real_10y?.asof || m.nominal_10y?.asof;
  const stale = !!(m.real_10y?.stale || m.nominal_10y?.stale || m.dxy?.stale);
  setAsof("asof-rates", asof, stale);

  setText("val-gold", m.gold?.value != null ? fmtUSD(m.gold.value) : "—");
  setText("val-btc-gold", m.gold?.extra?.btc_gold_ratio != null ? m.gold.extra.btc_gold_ratio.toFixed(1) + "x" : "—");
  setAsof("asof-gold", m.gold?.asof, m.gold?.stale);
}

/* ============================================================
 * top background strip
 * ============================================================ */
function renderTopBar(data) {
  setText("bg-price", data?.price?.value != null ? fmtUSD(data.price.value) : "—");
  const dd = data?.price?.extra?.drawdown_from_ath;
  setText("bg-drawdown", dd != null ? fmtPctSigned(dd, 1) : "—");
  const s = data?.saylor_holdings;
  setText("bg-saylor", s?.value != null ? fmtBTC(s.value) + (s.asof ? ` (${s.asof})` : "") : "未填写");
}

/* ============================================================
 * client-side realtime fetches (A 类源), 兜底用 data.json.realtime_fallback
 * ============================================================ */
async function fetchFngLive(fallback) {
  try {
    const j = await fetchWithTimeout("https://api.alternative.me/fng/?limit=1");
    const d = j?.data?.[0];
    if (!d) throw new Error("empty");
    const asof = new Date(Number(d.timestamp) * 1000).toISOString().slice(0, 10);
    return { value: Number(d.value), classification: d.value_classification, asof, stale: false };
  } catch (e) {
    console.warn("F&G live fetch failed, using fallback", e);
    return fallback ? { ...fallback, stale: true } : null;
  }
}

async function fetchFundingOiLive(fallbackFunding, fallbackOi) {
  try {
    const [premium, oi] = await Promise.all([
      fetchWithTimeout("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"),
      fetchWithTimeout("https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"),
    ]);
    const today = new Date().toISOString().slice(0, 10);
    return {
      funding: { value: Number(premium.lastFundingRate), asof: today, stale: false },
      oi: { value: Number(oi.openInterest), asof: today, stale: false },
    };
  } catch (e) {
    console.warn("funding/OI live fetch failed, using fallback", e);
    return {
      funding: fallbackFunding ? { ...fallbackFunding, stale: true } : null,
      oi: fallbackOi ? { ...fallbackOi, stale: true } : null,
    };
  }
}

async function fetchDvolLive(fallback) {
  try {
    const nowMs = Date.now();
    const startMs = nowMs - 2 * 24 * 3600 * 1000;
    const url = `https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC&start_timestamp=${startMs}&end_timestamp=${nowMs}&resolution=3600`;
    const j = await fetchWithTimeout(url);
    const rows = j?.result?.data;
    if (!rows || !rows.length) throw new Error("empty");
    const last = rows[rows.length - 1];
    const asof = new Date(last[0]).toISOString().slice(0, 10);
    return { value: Number(last[4]), asof, stale: false };
  } catch (e) {
    console.warn("DVOL live fetch failed, using fallback", e);
    return fallback ? { ...fallback, stale: true } : null;
  }
}

/* ============================================================
 * boot
 * ============================================================ */
async function boot() {
  const repoLink = document.getElementById("footer-repo-link");
  if (repoLink) repoLink.href = GITHUB_REPO_URL;

  const data = await loadDataJson();

  renderTopBar(data || {});
  renderMvrvZ(data || {});
  renderCostBasis(data || {});
  renderLthSth();
  renderVolume(data || {});
  renderEtf(data || {});
  renderMacro(data || {});

  if (data?.generated_at) {
    const el = document.getElementById("footer-generated");
    if (el) el.textContent = new Date(data.generated_at).toLocaleString("zh-CN", { hour12: false });
  }

  const rt = data?.realtime_fallback || {};

  const [fng, fundingOi, dvol] = await Promise.all([
    fetchFngLive(rt.fng),
    fetchFundingOiLive(rt.funding_rate, rt.open_interest),
    fetchDvolLive(rt.dvol),
  ]);

  renderFng(fng);
  renderLeverage(fundingOi.funding, fundingOi.oi);
  renderVol(data || {}, dvol);
}

document.addEventListener("DOMContentLoaded", boot);
