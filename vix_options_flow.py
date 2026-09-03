"""
vix_options_flow.py — risk-off 期权流量模块(精简版)
====================================================
目标单一: 捕捉 risk-off 信号。只追踪两条"恐惧需求"主线:
    [L1] SPX put(保护区桶)  -> 机构对指数下行的保护需求
    [L2] VIX call(VX1/VX2关联)-> 机构对波动率尖峰的保护需求
    附带免费校验项: VIX 端 C/P 比与 hedge_net 符号(由 put 侧最小量计算,
    不单独成特征, 仅用于确认买盘方向是否单边)。

原理:
    L1: 机构对冲指数下行 -> 买 SPX put。按期限分三桶解读(见 SPX_BUCKETS):
        战术桶(1-22DTE)=近端事件对冲; VIX窗(23-37DTE)=唯一直接进入VIX计算的
        期限段(官方方法论: >23天且<37天, 插值出恒定30天波动率);
        结构桶(38-90DTE)=中期战略保护。SPX全链~64%是0DTE噪音, 已排除。
    L2: 机构买 VIX call -> dealer 卖 call -> 为 delta neutral 买同结算日 VX 期货。
        对冲量 = sum(量 x delta / 10)(期权$100/期货$1000)。

数据源: Cboe 延迟报价 JSON(免费, 无需 key, SPX 为 Cboe 独家即全市场)
    https://cdn.cboe.com/api/global/delayed_quotes/options/_VIX.json   (~0.3MB)
    https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json   (~13MB, 含SPXW)

运行: 每日美股收盘后一次(cron 21:30 UTC), 幂等落盘, 历史自动累积。
    免费源无逐日历史; 回填或客户方向拆分需付费 Cboe DataShop Open-Close。
    OI 滞后一个交易日, OI 日变化按 T-1 口径解读。

对接: merge_into_signal() 按 Trade Date 左连到 vix_data_signal.py 的输出表。
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import ssl
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from market_time import format_cboe_timestamp, los_angeles_today


def _make_ssl_context() -> ssl.SSLContext:
    """优先用 certifi 的 CA 包(修复 macOS python.org 版 Python 缺根证书的问题)。"""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL_CTX = _make_ssl_context()

CBOE_DELAYED_QUOTES_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{root}.json"
HEDGE_MULTIPLIER_RATIO = 0.1  # VIX期权$100 / VX期货$1000 -> 每张期权对冲需 delta/10 张VX

# ---------------- 报警阈值 ----------------
CALL_VOL_ARMED_PERCENTILE = 90.0  # VX1/VX2 对应 VIX call 量分位 >= 90 -> 波动率保护爆量
PROT_PUT_ARMED_PERCENTILE = 90.0  # SPX 保护区 put 量分位 >= 90 -> 指数保护爆量
FLOW_PERCENTILE_LOOKBACK = 252
FLOW_MIN_ROWS = 60  # 冷启动期不报警
SPX_IMPLIED_MOVE_EXPIRIES = 5

# ---------------- SPX put 分桶定义 ----------------
# 三个期限桶, 语义不同, 分开计分(避免单一21-90桶把"近端事件对冲"和"中期仓位"混在一起):
#   tac: 1-22 DTE  战术桶  -> 未来1天至3周的事件性对冲(周末风险/数据周/战事), 衰减快;
#                            自带彩票churn噪声, 解读时优先看OI变化
#   vix: 23-37 DTE VIX窗   -> 官方VIX计算唯一使用的期限段(近月>23天、次月<37天,
#                            插值出恒定30天波动率), 只有这一桶直接进入VIX公式
#   str: 38-90 DTE 结构桶  -> 中期战略保护/波动率期限结构仓位, 与VIX现货联系弱
SPX_BUCKETS = {
    "tac": (1, 22),
    "vix": (23, 37),
    # "str": (38, 90)
}
PROT_PUT_OTM_MIN, PROT_PUT_OTM_MAX = -0.20, -0.03  # 虚值 3%-20% 的 put

DEBUG = False
# ================================================================ 通用抓取


def _fetch_chain(root: str, timeout: int = 120) -> tuple[list[dict], str, dict]:
    url = CBOE_DELAYED_QUOTES_URL.format(root=root)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" not in str(e):
            raise
        print(
            "[warn] 本地CA证书缺失, 本次临时跳过证书校验。"
            "彻底修复: 运行 '/Applications/Python 3.x/Install Certificates.command' 或 'pip install -U certifi'"
        )
        resp = urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context())
    body = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    raw = json.loads(body)
    meta = {k: v for k, v in raw["data"].items() if k != "options"}
    return raw["data"]["options"], format_cboe_timestamp(raw.get("timestamp")), meta


# ================================================================ L2: VIX call 流


def fetch_vix_options_chain(root: str = "_VIX", timeout: int = 30) -> tuple[pd.DataFrame, str]:
    records, ts, _meta = _fetch_chain(root, timeout)
    df = pd.DataFrame(records)
    sym = df["option"].astype(str)
    df = df[sym.str[3] != "W"].copy()  # we got VIX and VIWX, filter out VIXW
    sym = df["option"].astype(str)
    df["expiry"] = pd.to_datetime("20" + sym.str[3:5] + "-" + sym.str[5:7] + "-" + sym.str[7:9])  # ex:"option":"VIX260722C00010000",
    df["cp"] = sym.str[9]  # ex:"option":"VIX260722C00010000", sym.str[9] is C or P
    df["strike"] = sym.str[10:].astype(float) / 1000.0
    for col in ["volume", "open_interest", "delta", "bid", "ask", "iv"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df, ts


def aggregate_vix_calls(chain: pd.DataFrame) -> pd.DataFrame:
    """按到期月聚合, 聚焦 call 侧; put 侧仅保留 C/P 比与对冲流符号所需的最小量。"""
    rows = []
    for expiry, g in chain.groupby("expiry"):
        c = g[g["cp"] == "C"]
        p = g[g["cp"] == "P"]
        call_vol, put_vol = c["volume"].sum(), p["volume"].sum()
        call_oi, put_oi = c["open_interest"].sum(), p["open_interest"].sum()
        hedge_buy = float((c["volume"] * c["delta"] * HEDGE_MULTIPLIER_RATIO).sum())
        hedge_sell = float((p["volume"] * p["delta"].abs() * HEDGE_MULTIPLIER_RATIO).sum())
        top_call = c.loc[c["volume"].idxmax()] if len(c) else None
        rows.append(
            {
                "expiry": expiry,
                "call_vol": int(call_vol),
                "cp_vol_ratio": call_vol / put_vol if put_vol > 0 else np.inf,  # >1 单边买call
                "call_oi": int(call_oi),
                "put_oi": int(put_oi),
                "cp_oi_ratio": call_oi / put_oi if put_oi > 0 else np.inf,
                "hedge_net_vx": round(hedge_buy - hedge_sell),  # >0 dealer净买VX
                "top_call_strike": float(top_call["strike"]) if top_call is not None else np.nan,
                "top_call_vol": int(top_call["volume"]) if top_call is not None else 0,
            }
        )
    return pd.DataFrame(rows).sort_values("expiry").reset_index(drop=True)


def link_to_vx(flow: pd.DataFrame, vx_settlement_dates: list[str | dt.date], *, asof_date: str | dt.date | None = None) -> pd.DataFrame:
    fsd = pd.to_datetime([pd.Timestamp(x) for x in vx_settlement_dates]).sort_values()
    asof = pd.Timestamp(asof_date or los_angeles_today())
    future_fsd = fsd[fsd >= asof].tolist()
    labels = {pd.Timestamp(d): (f"VX{i + 1}" if i < 2 else "VX3+") for i, d in enumerate(future_fsd)}
    out = flow.copy()
    out["vx_link"] = out["expiry"].map(lambda e: "EXPIRED" if pd.Timestamp(e) < asof else labels.get(pd.Timestamp(e), "VX3+"))
    return out


def pivot_vix_for_signal(flow_linked: pd.DataFrame, trade_date: str) -> dict:
    row: dict = {"Trade Date": trade_date}
    for tag in ["VX1", "VX2"]:
        sub = flow_linked[flow_linked["vx_link"] == tag]
        if sub.empty:
            continue
        s = sub.iloc[0]
        row[f"{tag.lower()}_call_vol"] = s["call_vol"]
        row[f"{tag.lower()}_expiry"] = pd.Timestamp(s["expiry"]).date().isoformat()
        row[f"{tag.lower()}_cp_vol_ratio"] = round(s["cp_vol_ratio"], 2)
        row[f"{tag.lower()}_cp_oi_ratio"] = round(s["cp_oi_ratio"], 2)
        row[f"{tag.lower()}_hedge_net_vx"] = s["hedge_net_vx"]
        row[f"{tag.lower()}_top_call_strike"] = s["top_call_strike"]
    return row


def vix_daily_snapshot(history_path: str | Path, vx_settlement_dates: list[str | dt.date], *, trade_date: str | None = None) -> pd.DataFrame:
    history_path = Path(history_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    trade_date = trade_date or los_angeles_today().isoformat()

    chain, ts = fetch_vix_options_chain()

    if DEBUG:
        print("/vix_opt_chain")
        print(chain.head(30).to_string(index=False))

    vix_calls_by_expiry = aggregate_vix_calls(chain)

    if DEBUG:
        print("/vix_opt_groupby_expiry")
        print(vix_calls_by_expiry.to_string(index=False))

    flow = link_to_vx(vix_calls_by_expiry, vx_settlement_dates, asof_date=trade_date)
    row = pivot_vix_for_signal(flow, trade_date)

    hist = pd.read_csv(history_path, dtype={"Trade Date": str}) if history_path.exists() else pd.DataFrame()
    if not hist.empty:
        hist = hist[hist["Trade Date"] != trade_date]
    hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True).sort_values("Trade Date").reset_index(drop=True)
    hist.to_csv(history_path, index=False)
    print(f"[vix snapshot] {trade_date} saved (source ts={ts}), history rows={len(hist)}")
    return hist


# ================================================================ L1: SPX put 保护区流


def fetch_spx_options_chain(root: str = "_SPX", timeout: int = 120) -> tuple[pd.DataFrame, str, dict]:
    # {
    #     "option": "SPX260821C00200000",
    #     "bid": 7231.3,
    #     "bid_size": 0,
    #     "ask": 7252.3,
    #     "ask_size": 0,
    #     "iv": 0,
    #     "open_interest": 2385,
    #     "volume": 578,
    #     "delta": 0.9997,
    #     "gamma": 0,
    #     "vega": 0.0008,
    #     "theta": -0.0029,
    #     "rho": 0.196,
    #     "theo": 7248.1666,
    #     "change": -42.7401,
    #     "open": 7249.96,
    #     "high": 7283.73,
    #     "low": 7249.83,
    #     "tick": "up",
    #     "last_trade_price": 7263.61,
    #     "last_trade_time": "2026-07-17T11:25:31",
    #     "percent_change": -0.584972,
    #     "prev_day_close": 7306.35009765625
    #   },
    records, ts, meta = _fetch_chain(root, timeout)
    df = pd.DataFrame(records)
    sym = df["option"].astype(str)  # "option": "SPX260821C00200000",
    is_w = sym.str.startswith("SPXW")
    off = np.where(is_w, 4, 3)
    df["expiry"] = pd.to_datetime([f"20{s[i : i + 2]}-{s[i + 2 : i + 4]}-{s[i + 4 : i + 6]}" for s, i in zip(sym, off)])
    df["cp"] = [s[i + 6] for s, i in zip(sym, off)]
    df["strike"] = pd.to_numeric([s[i + 7 :] for s, i in zip(sym, off)]) / 1000.0
    for col in ["volume", "open_interest", "delta", "bid", "ask", "iv"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df, ts, meta


def _spx_atm_straddle_mid(chain_for_expiry: pd.DataFrame, spot: float) -> tuple[float, float] | None:
    """Return (ATM strike, call-mid + put-mid), preferring PM-settled SPXW when both series exist."""
    candidate_groups: list[pd.DataFrame] = []
    if "option" in chain_for_expiry.columns:
        is_weekly = chain_for_expiry["option"].astype(str).str.startswith("SPXW")
        weekly = chain_for_expiry.loc[is_weekly]
        standard = chain_for_expiry.loc[~is_weekly]
        if not weekly.empty:
            candidate_groups.append(weekly)
        if not standard.empty:
            candidate_groups.append(standard)
    else:
        candidate_groups.append(chain_for_expiry)

    for group in candidate_groups:
        quotes = group.copy()
        for col in ["strike", "bid", "ask"]:
            quotes[col] = pd.to_numeric(quotes[col], errors="coerce")
        quotes = quotes[
            quotes["cp"].isin(["C", "P"])
            & quotes["strike"].notna()
            & quotes["bid"].notna()
            & quotes["ask"].notna()
            & (quotes["bid"] > 0)
            & (quotes["ask"] > 0)
            & (quotes["ask"] >= quotes["bid"])
        ].copy()
        if quotes.empty:
            continue

        quotes["mid"] = (quotes["bid"] + quotes["ask"]) / 2.0
        paired = quotes.pivot_table(index="strike", columns="cp", values="mid", aggfunc="first")
        if not {"C", "P"}.issubset(paired.columns):
            continue
        paired = paired.dropna(subset=["C", "P"])
        if paired.empty:
            continue

        strike = float(min(paired.index, key=lambda value: abs(float(value) - spot)))
        straddle_mid = float(paired.loc[strike, "C"] + paired.loc[strike, "P"])
        if np.isfinite(straddle_mid) and straddle_mid > 0:
            return strike, straddle_mid
    return None


def spx_daily_implied_moves(
    chain: pd.DataFrame,
    spot: float,
    trade_date: str | dt.date,
    *,
    num_expiries: int = SPX_IMPLIED_MOVE_EXPIRIES,
) -> pd.DataFrame:
    """Estimate forward one-session SPX moves from adjacent-expiry ATM straddles.

    CumMove is ATM-straddle midpoint / spot. DayMove removes the previous
    expiry's squared CumMove, treating the straddle move as proportional to
    expected absolute return and assuming variance adds across intervals.
    DayMove is therefore a comparable straddle-equivalent proxy, not a 1-sigma
    forecast. A Monday interval includes weekend risk.
    """
    columns = ["expiry", "dte", "gap_days", "atm_strike", "straddle_mid", "cum_move_pct", "day_move_pct", "day_move_points"]
    if num_expiries < 1:
        raise ValueError("num_expiries must be at least 1")
    if not np.isfinite(spot) or spot <= 0:
        raise ValueError("spot must be a positive finite number")
    required_cols = {"expiry", "cp", "strike", "bid", "ask"}
    missing_cols = required_cols.difference(chain.columns)
    if missing_cols:
        raise ValueError(f"SPX chain is missing required columns: {sorted(missing_cols)}")

    asof = pd.Timestamp(trade_date).normalize()
    work = chain.copy()
    work["expiry"] = pd.to_datetime(work["expiry"], errors="coerce").dt.normalize()
    future_expiries = work.loc[work["expiry"] > asof, "expiry"].dropna().drop_duplicates().sort_values()

    rows: list[dict] = []
    previous_expiry = asof
    previous_cum_move_pct = 0.0
    for expiry in future_expiries:
        atm = _spx_atm_straddle_mid(work.loc[work["expiry"] == expiry], spot)
        if atm is None:
            continue
        strike, straddle_mid = atm
        cum_move_pct = straddle_mid / spot * 100.0
        forward_move_variance = cum_move_pct**2 - previous_cum_move_pct**2
        day_move_pct = float(np.sqrt(forward_move_variance)) if forward_move_variance >= 0 else np.nan
        day_move_points = day_move_pct / 100.0 * spot if np.isfinite(day_move_pct) else np.nan
        rows.append(
            {
                "expiry": expiry.date().isoformat(),
                "dte": int((expiry - asof).days),
                "gap_days": int((expiry - previous_expiry).days),
                "atm_strike": strike,
                "straddle_mid": straddle_mid,
                "cum_move_pct": cum_move_pct,
                "day_move_pct": day_move_pct,
                "day_move_points": day_move_points,
            }
        )
        previous_expiry = expiry
        previous_cum_move_pct = cum_move_pct
        if len(rows) >= num_expiries:
            break
    return pd.DataFrame(rows, columns=columns)


def spx_implied_move_table_to_string(implied_moves: pd.DataFrame) -> str:
    """Format the next-expiry SPX implied-move table for the console report."""
    if implied_moves.empty:
        return "(no valid future SPX expiry quotes)"

    view = implied_moves.rename(
        columns={
            "expiry": "Expiry",
            "dte": "DTE",
            "gap_days": "GapD",
            "atm_strike": "ATM",
            "straddle_mid": "Straddle",
            "cum_move_pct": "CumMove",
            "day_move_pct": "DayMove",
            "day_move_points": "DayPts",
        }
    ).copy()
    view["Expiry"] = pd.to_datetime(view["Expiry"], errors="coerce").dt.strftime("%m-%d")

    def format_pct(value: float) -> str:
        return "" if pd.isna(value) else f"{value:.3f}%"

    formatters = {
        "ATM": lambda value: f"{value:.0f}",
        "Straddle": lambda value: f"{value:.1f}",
        "CumMove": format_pct,
        "DayMove": format_pct,
        "DayPts": lambda value: "" if pd.isna(value) else f"{value:.1f}",
    }
    return view.to_string(index=False, formatters=formatters, na_rep="")


def aggregate_spx_protection(chain: pd.DataFrame, spot: float, trade_date: str) -> dict:
    """
    SPX put 分桶聚合: 每桶取虚值 3%-20% 的 put，输出总 volume 和总 OI。
    桶定义见 SPX_BUCKETS。注意: 只有 vix 桶(23-37 DTE)直接进入 VIX 计算,
    tac/str 桶测量的是保护需求本身, 不应表述为"VIX 的计算输入"。
    """
    d = chain.copy()
    d["dte"] = (d["expiry"] - pd.Timestamp(trade_date)).dt.days
    d["moneyness"] = d["strike"] / spot - 1.0

    row: dict = {"Trade Date": trade_date, "spx_close": spot}
    puts = d[(d["cp"] == "P") & (d["moneyness"] >= PROT_PUT_OTM_MIN) & (d["moneyness"] <= PROT_PUT_OTM_MAX)]
    for tag, (lo, hi) in SPX_BUCKETS.items():
        b = puts[(puts["dte"] >= lo) & (puts["dte"] <= hi)]
        row[f"spx_{tag}_put_vol"] = int(b["volume"].sum())
        row[f"spx_{tag}_put_oi"] = int(b["open_interest"].sum())

    return row


def spx_daily_snapshot(
    history_path: str | Path,
    *,
    trade_date: str | None = None,
    fetched: tuple[pd.DataFrame, str, dict] | None = None,
) -> pd.DataFrame:
    history_path = Path(history_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    trade_date = trade_date or los_angeles_today().isoformat()

    chain, ts, meta = fetched if fetched is not None else fetch_spx_options_chain()
    spot = float(meta.get("close") or meta.get("current_price"))
    row = aggregate_spx_protection(chain, spot, trade_date)

    hist = pd.read_csv(history_path, dtype={"Trade Date": str}) if history_path.exists() else pd.DataFrame()
    if not hist.empty:
        hist = hist[hist["Trade Date"] != trade_date]
    hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True).sort_values("Trade Date").reset_index(drop=True)
    hist.to_csv(history_path, index=False)
    print(f"[spx snapshot] {trade_date} saved (source ts={ts}), history rows={len(hist)}")
    return hist


# ================================================================ 特征与信号


def _trailing_pct(s: pd.Series, lookback: int, min_rows: int) -> pd.Series:
    def _pct(arr: np.ndarray) -> float:
        arr = arr[~np.isnan(arr)]
        if len(arr) < min_rows:
            return np.nan
        return float(np.mean(arr <= arr[-1]) * 100.0)

    return s.rolling(lookback, min_periods=1).apply(_pct, raw=True)


# (源列, 分位列名, 报警阈值) —— 全部是 risk-off 方向; SPX结构桶(str)只做观察不报警(阈值None)
_FLOW_FEATURE_SPECS = [
    ("vx1_call_vol", "vx1_call_vol_pct", CALL_VOL_ARMED_PERCENTILE),
    ("vx2_call_vol", "vx2_call_vol_pct", CALL_VOL_ARMED_PERCENTILE),
    ("spx_tac_put_vol", "spx_tac_put_vol_pct", PROT_PUT_ARMED_PERCENTILE),
    ("spx_vix_put_vol", "spx_vix_put_vol_pct", PROT_PUT_ARMED_PERCENTILE),
    ("spx_str_put_vol", "spx_str_put_vol_pct", None),
]


def order_flow_columns(flow_features: pd.DataFrame) -> pd.DataFrame:
    """Return flow features in a stable, analysis-friendly column order."""
    if "spx_close" in flow_features.columns:
        preferred = ["Trade Date", "spx_close"]
        for tag in SPX_BUCKETS:
            preferred.extend(
                [
                    f"spx_{tag}_put_vol",
                    f"spx_{tag}_put_vol_chg_pct",
                    f"spx_{tag}_put_vol_pct",
                    f"spx_{tag}_put_oi",
                    f"spx_{tag}_put_oi_chg",
                    f"spx_{tag}_put_oi_chg_pct",
                    f"spx_{tag}_put_vol_oi_ratio",
                ]
            )
            if tag == "vix":
                preferred.extend(
                    [
                        "tac_vix_put_oi",
                        "tac_vix_put_oi_chg_pct",
                    ]
                )
    elif "vx1_call_vol" in flow_features.columns:
        preferred = [
            "Trade Date",
            "vx1_expiry",
            "vx1_call_vol",
            "vx1_call_vol_chg_pct",
            "vx1_call_vol_pct",
            "vx1_cp_vol_ratio",
            "vx1_cp_vol_ratio_chg_pct",
            "vx1_cp_oi_ratio",
            "vx1_cp_oi_ratio_chg_pct",
            "vx1_hedge_net_vx",
            "vx1_top_call_strike",
            "vx2_expiry",
            "vx2_call_vol",
            "vx2_call_vol_chg_pct",
            "vx2_call_vol_pct",
            "vx2_cp_vol_ratio",
            "vx2_cp_vol_ratio_chg_pct",
            "vx2_cp_oi_ratio",
            "vx2_cp_oi_ratio_chg_pct",
            "vx2_hedge_net_vx",
            "vx2_top_call_strike",
            "vx2_vx1_call_vol_ratio",
        ]
    else:
        preferred = ["Trade Date"]

    risk_col = "flow_risk_off_level"
    preferred = [col for col in preferred if col in flow_features.columns]
    remaining = [col for col in flow_features.columns if col not in preferred and col != risk_col]
    ordered = preferred + remaining
    if risk_col in flow_features.columns:
        ordered.append(risk_col)
    return flow_features[ordered]


def add_flow_features(hist: pd.DataFrame, *, lookback: int = FLOW_PERCENTILE_LOOKBACK, min_rows: int = FLOW_MIN_ROWS) -> pd.DataFrame:
    """
    risk-off 分位特征与统一红绿灯:
      四个报警项任一触发 -> flow_risk_off_level = RED:
        vx1_call_vol_pct      (VIX call 爆量)
        vx2_call_vol_pct      (次月 VIX call 爆量)
        spx_tac_put_vol_pct   (SPX 近端保护 put 爆量)
        spx_vix_put_vol_pct   (SPX VIX窗保护 put 爆量)
      另附各期限桶 put OI 日变化(正=净新增对冲)。
    """
    out = hist.copy().sort_values("Trade Date").reset_index(drop=True)
    armed_any = pd.Series(False, index=out.index)
    for col, new, thresh in _FLOW_FEATURE_SPECS:
        if col not in out.columns:
            continue
        out[new] = _trailing_pct(pd.to_numeric(out[col], errors="coerce"), lookback, min_rows).round(1)
        if thresh is not None:
            armed_any = armed_any | (out[new] >= thresh).fillna(False)

    if {"vx1_call_vol", "vx2_call_vol"}.issubset(out.columns):
        vx1_call_vol = pd.to_numeric(out["vx1_call_vol"], errors="coerce")
        vx2_call_vol = pd.to_numeric(out["vx2_call_vol"], errors="coerce")
        out["vx2_vx1_call_vol_ratio"] = vx2_call_vol.div(vx1_call_vol.where(vx1_call_vol != 0)).round(2)

    # C/P 比率的日变化百分比(相比前一交易日; inf 视为缺失, 避免除零/无穷传播)
    for col in ["vx1_cp_vol_ratio", "vx1_cp_oi_ratio", "vx2_cp_vol_ratio", "vx2_cp_oi_ratio"]:
        if col in out.columns:
            ratio = pd.to_numeric(out[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
            out[f"{col}_chg_pct"] = ratio.div(ratio.shift().where(ratio.shift() != 0)).sub(1).mul(100).round(1)

    # call 量的日变化百分比(相比前一交易日)
    for col in ["vx1_call_vol", "vx2_call_vol"]:
        if col in out.columns:
            vol = pd.to_numeric(out[col], errors="coerce")
            out[f"{col}_chg_pct"] = vol.div(vol.shift().where(vol.shift() != 0)).sub(1).mul(100).round(1)

    for tag in SPX_BUCKETS:
        oi_col = f"spx_{tag}_put_oi"
        if oi_col in out.columns:
            oi = pd.to_numeric(out[oi_col], errors="coerce")
            out[f"spx_{tag}_put_oi_chg"] = oi.diff()
            out[f"spx_{tag}_put_oi_chg_pct"] = oi.div(oi.shift().where(oi.shift() != 0)).sub(1).mul(100).round(1)

            vol_col = f"spx_{tag}_put_vol"
            if vol_col in out.columns:
                vol = pd.to_numeric(out[vol_col], errors="coerce")
                out[f"spx_{tag}_put_vol_chg_pct"] = vol.div(vol.shift().where(vol.shift() != 0)).sub(1).mul(100).round(1)
                out[f"spx_{tag}_put_vol_oi_ratio"] = vol.div(oi.where(oi != 0)).round(3)

    # 1-37 DTE 合计 OI，用于观察战术桶和 VIX 窗的整体保护仓位。
    tac_vix_oi_cols = ["spx_tac_put_oi", "spx_vix_put_oi"]
    if set(tac_vix_oi_cols).issubset(out.columns):
        tac_vix_oi = out[tac_vix_oi_cols].apply(pd.to_numeric, errors="coerce").sum(axis=1, min_count=2)
        out["tac_vix_put_oi"] = tac_vix_oi
        out["tac_vix_put_oi_chg_pct"] = (
            tac_vix_oi.div(tac_vix_oi.shift().where(tac_vix_oi.shift() != 0)).sub(1).mul(100).round(1)
        )
    out["flow_risk_off_level"] = np.where(armed_any, "RED", "GREEN")
    return order_flow_columns(out)


def flow_table_to_string(flow_features: pd.DataFrame, *, tail_rows: int = 10) -> str:
    """Compact volume/OI/VX counts as truncated thousands for console output only."""
    # 打印时隐藏的低信息列(CSV 中仍保留); 各 SPX 桶的 OI 绝对值变化隐藏, 只看百分比
    hidden_cols = {
        "vx1_hedge_net_vx",
        "vx2_hedge_net_vx",
        "vx1_top_call_strike",
        "vx2_top_call_strike",
        "vx2_vx1_call_vol_ratio",
        *(f"spx_{tag}_put_oi_chg" for tag in SPX_BUCKETS),
    }

    def compact_thousands(value: float) -> str:
        return f"{int(value / 1_000)}k" if abs(value) >= 1_000 else f"{value:.0f}"

    view = order_flow_columns(flow_features).tail(tail_rows)
    view = view.drop(columns=[c for c in view.columns if c in hidden_cols])
    # 分位列冷启动期全为 NaN, 整列不显示
    all_nan_pct_cols = [col for col in view.columns if col.endswith("_vol_pct") and view[col].isna().all()]
    view = view.drop(columns=all_nan_pct_cols)
    # 打印时列名缩短: vx1_ -> 1_, vx2_ -> 2_, spx_tac_ -> tac_, spx_vix_ -> vix_;
    # 仅保留 vx expiry 的完整名称(CSV 列名不变)。
    keep_full = {"vx1_expiry", "vx2_expiry"}

    def short_name(c: str) -> str:
        if c in keep_full:
            return c
        return (
            c.replace("vx1_", "1_")
            .replace("vx2_", "2_")
            .replace("spx_tac_", "tac_")
            .replace("spx_vix_", "vix_")
            .replace("_chg_pct", "_chg%")
        )

    view = view.rename(columns=short_name)

    compact_suffixes = ("_vol", "_oi", "_oi_chg", "_vx")
    formatters = {col: compact_thousands for col in view.columns if col.endswith(compact_suffixes)}
    formatters.update({col: lambda value: f"{value:.1f}%" for col in view.columns if col.endswith("_chg%")})
    price_cols = [col for col in view.columns if col == "spx_close" or col.endswith("_strike")]
    formatters.update({col: lambda value: str(int(value)) for col in price_cols})
    # expiry 只显示月-日(CSV 中仍是完整日期)
    formatters.update({col: lambda value: str(value)[5:] for col in view.columns if col.endswith("_expiry")})
    return view.to_string(index=False, formatters=formatters)


def merge_into_signal(vx_features: pd.DataFrame, *flow_tables: pd.DataFrame) -> pd.DataFrame:
    out = vx_features
    for ft in flow_tables:
        cols = [
            c for c in ft.columns if c == "Trade Date" or c.endswith(("_pct", "_vol", "_vx", "_ratio", "_level", "_strike", "_usd", "_oi", "_oi_chg"))
        ]
        out = out.merge(ft[cols], on="Trade Date", how="left")
    return out


# ---------------------------------------------------------------- 与 vix_data_signal.py 的 score 接法(参考)
#   flow_risk_off_level == RED                -> +1~2 (多源任一, 已去重)
#   或分项: vx1/vx2_call_vol_pct>=90 -> +1 (波动率保护)
#           spx_tac_*_pct>=90 -> +1 (近端事件对冲, 衰减快) | spx_vix_*_pct>=90 -> +1 (VIX窗保护)
#   持续性: 各桶 spx_*_put_oi_chg 连续为正 = 建仓型保护(加权); 单日脉冲+OI不动 = 事件型(标注衰减)
#   方向校验(免费): vx1_cp_vol_ratio >> 1 且 hedge_net_vx > 0 = 单边买call确认
#   语义注意: 只有 spx_vix_* 桶(23-37DTE)直接进入VIX计算; tac/str 桶是保护需求本身

if __name__ == "__main__":
    # cron 21:30 UTC 每日一次。VX结算日用 vix_data_signal.py 的
    # build_vx_monthly_schedule(...) -> [x["fsd"] for x in schedule] 生成。
    here = Path(__file__).resolve().parent
    flow_history_dir = here / "data" / "vix_call_spx_put"
    vix_hist_file = flow_history_dir / "vix_call_flow_history.csv"
    spx_hist_file = flow_history_dir / "spx_put_flow_history.csv"

    today = los_angeles_today()
    if today.weekday() >= 5:
        # 周末期权无交易: 不抓不存, 只打印已有历史(假日由 vix_data_signal 的主守卫覆盖)
        print(f"[options flow] {today} is a weekend; showing last saved snapshots")
        for hist_file in (vix_hist_file, spx_hist_file):
            if hist_file.exists():
                print(flow_table_to_string(add_flow_features(pd.read_csv(hist_file, dtype={"Trade Date": str}))))
        raise SystemExit(0)

    demo_fsd = ["2026-07-22", "2026-08-19", "2026-09-16", "2026-10-21"]
    vix_hist = vix_daily_snapshot(vix_hist_file, demo_fsd)
    spx_hist = spx_daily_snapshot(spx_hist_file)
    vix_flow_features = add_flow_features(vix_hist)
    spx_flow_features = add_flow_features(spx_hist)
    vix_flow_features.to_csv(vix_hist_file, index=False)
    spx_flow_features.to_csv(spx_hist_file, index=False)
    print(flow_table_to_string(vix_flow_features))
    print(flow_table_to_string(spx_flow_features))
