"""
vix_options_flow.py — risk-off 期权流量模块(精简版)
====================================================
目标单一: 捕捉 risk-off 信号。只追踪两条"恐惧需求"主线:
    [L1] SPX put(保护区桶)  -> 机构对指数下行的保护需求
    [L2] VIX call(VX1/VX2关联)-> 机构对波动率尖峰的保护需求
    附带免费校验项: VIX 端 C/P 比与 hedge_net 符号(由 put 侧最小量计算,
    不单独成特征, 仅用于确认买盘方向是否单边)。

原理:
    L1: 机构对冲指数下行 -> 买 SPX put -> 推高隐波 -> VIX 被"算"高。
        SPX 全链 ~64% 是 0DTE 日内噪音, 必须按"保护区桶"过滤:
        DTE 21-90 天 + 虚值 3%-20% 的 put(教科书式崩盘保护的栖息地)。
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

import certifi
import numpy as np
import pandas as pd

CBOE_DELAYED_QUOTES_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{root}.json"
HEDGE_MULTIPLIER_RATIO = 0.1  # VIX期权$100 / VX期货$1000 -> 每张期权对冲需 delta/10 张VX

# ---------------- 报警阈值 ----------------
CALL_VOL_ARMED_PERCENTILE = 90.0  # VX1对应 VIX call 量分位 >= 90 -> 波动率保护爆量
HEDGE_NET_ARMED_PERCENTILE = 80.0  # 净对冲流分位 >= 80 -> dealer 被迫买 VX 放大器
PROT_PUT_ARMED_PERCENTILE = 90.0  # SPX 保护区 put 量分位 >= 90 -> 指数保护爆量
PROT_PREMIUM_ARMED_PERCENTILE = 90.0  # 保护区权利金支出分位 >= 90 -> 保险价格被抢高
FLOW_PERCENTILE_LOOKBACK = 252
FLOW_MIN_ROWS = 60  # 冷启动期不报警

# ---------------- SPX 保护区桶定义 ----------------
PROT_DTE_MIN, PROT_DTE_MAX = 21, 90
PROT_PUT_OTM_MIN, PROT_PUT_OTM_MAX = -0.20, -0.03
PROT_CALL_OTM_MIN, PROT_CALL_OTM_MAX = 0.00, 0.20  # 桶内P/C的对照call
SPX_OPTION_MULTIPLIER = 100


# ================================================================ 通用抓取


def _fetch_chain(root: str, timeout: int = 120) -> tuple[list[dict], str, dict]:
    url = CBOE_DELAYED_QUOTES_URL.format(root=root)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"})
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    raw = json.loads(body)
    meta = {k: v for k, v in raw["data"].items() if k != "options"}
    return raw["data"]["options"], str(raw.get("timestamp", "")), meta


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
        call_vol, put_vol = c["volume"].sum(), p["volume"].sum()  # at the same expiry, sum volume of call/put cross different strike.
        call_oi = int(c["open_interest"].sum())  # at the same expiry, sum each call's open_interest through different strike.
        hedge_buy = float((c["volume"] * c["delta"] * HEDGE_MULTIPLIER_RATIO).sum())
        hedge_sell = float((p["volume"] * p["delta"].abs() * HEDGE_MULTIPLIER_RATIO).sum())
        top_call = c.loc[c["volume"].idxmax()] if len(c) else None
        rows.append(
            {
                "expiry": expiry,
                "opt_call_vol": int(call_vol),
                "opt_put_vol": int(put_vol),
                "opt_cp_ratio": call_vol / put_vol if put_vol > 0 else np.inf,  # >1 单边买call
                "opt_call_oi": call_oi,
                "hedge_net_vx": round(hedge_buy - hedge_sell),  # >0 dealer净买VX
                "top_call_strike": float(top_call["strike"]) if top_call is not None else np.nan,
                "top_call_vol": int(top_call["volume"]) if top_call is not None else 0,
            }
        )
    return pd.DataFrame(rows).sort_values("expiry").reset_index(drop=True)


def link_to_vx(flow: pd.DataFrame, vx_settlement_dates: list[str | dt.date]) -> pd.DataFrame:
    fsd = pd.to_datetime([pd.Timestamp(x) for x in vx_settlement_dates]).sort_values()
    today = pd.Timestamp(dt.date.today())
    future_fsd = fsd[fsd >= today].tolist()
    labels = {pd.Timestamp(d): (f"VX{i + 1}" if i < 2 else "VX3+") for i, d in enumerate(future_fsd)}
    out = flow.copy()
    out["vx_link"] = out["expiry"].map(lambda e: labels.get(pd.Timestamp(e), "VX3+"))
    return out


def pivot_vix_for_signal(flow_linked: pd.DataFrame, trade_date: str) -> dict:
    row: dict = {"Trade Date": trade_date}
    for tag in ["VX1", "VX2"]:
        sub = flow_linked[flow_linked["vx_link"] == tag]
        if sub.empty:
            continue
        s = sub.iloc[0]
        row[f"{tag.lower()}_opt_call_vol"] = s["opt_call_vol"]
        row[f"{tag.lower()}_opt_cp_ratio"] = round(s["opt_cp_ratio"], 2)
        row[f"{tag.lower()}_hedge_net_vx"] = s["hedge_net_vx"]
        row[f"{tag.lower()}_top_call_strike"] = s["top_call_strike"]
    row["opt_total_hedge_net_vx"] = int(flow_linked["hedge_net_vx"].sum())
    return row


def vix_daily_snapshot(history_path: str | Path, vx_settlement_dates: list[str | dt.date], *, trade_date: str | None = None) -> pd.DataFrame:
    history_path = Path(history_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    trade_date = trade_date or dt.date.today().isoformat()

    chain, ts = fetch_vix_options_chain()
    # print("/vix_opt_chain")
    # print(chain.head(30).to_string(index=False))

    vix_calls_by_expiry = aggregate_vix_calls(chain)
    # print("/vix_opt_groupby_expiry")
    # print(vix_calls_by_expiry.to_string(index=False))

    flow = link_to_vx(vix_calls_by_expiry, vx_settlement_dates)
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


def aggregate_spx_protection(chain: pd.DataFrame, spot: float, trade_date: str) -> dict:
    """机构保护区桶: 21<=DTE<=90 且 虚值3%-20% 的 put(对照call: 同DTE, 平值~虚值20%)。"""
    d = chain.copy()
    d["dte"] = (d["expiry"] - pd.Timestamp(trade_date)).dt.days
    d["moneyness"] = d["strike"] / spot - 1.0
    d["mid"] = np.where((d["bid"] > 0) & (d["ask"] > 0), (d["bid"] + d["ask"]) / 2.0, np.nan)

    zone = d[(d["dte"] >= PROT_DTE_MIN) & (d["dte"] <= PROT_DTE_MAX)]
    zp = zone[(zone["cp"] == "P") & (zone["moneyness"] >= PROT_PUT_OTM_MIN) & (zone["moneyness"] <= PROT_PUT_OTM_MAX)]
    zc = zone[(zone["cp"] == "C") & (zone["moneyness"] >= PROT_CALL_OTM_MIN) & (zone["moneyness"] <= PROT_CALL_OTM_MAX)]

    premium = float((zp["volume"] * zp["mid"].fillna(0.0) * SPX_OPTION_MULTIPLIER).sum())
    total_vol = float(d["volume"].sum())
    top = zp.loc[zp["volume"].idxmax()] if len(zp) and zp["volume"].max() > 0 else None
    return {
        "Trade Date": trade_date,
        "spx_close": spot,
        "spx_prot_put_vol": int(zp["volume"].sum()),
        "spx_prot_put_oi": int(zp["open_interest"].sum()),
        "spx_prot_premium_usd": round(premium),
        "spx_prot_pc_ratio": round(zp["volume"].sum() / zc["volume"].sum(), 2) if zc["volume"].sum() > 0 else np.inf,
        "spx_0dte_share": round(float(d.loc[d["dte"] == 0, "volume"].sum()) / total_vol, 3) if total_vol > 0 else np.nan,
        "spx_prot_top_strike": float(top["strike"]) if top is not None else np.nan,
        "spx_prot_top_expiry": str(top["expiry"].date()) if top is not None else "",
        "spx_prot_top_oi": int(top["open_interest"]) if top is not None else 0,
    }


def spx_daily_snapshot(history_path: str | Path, *, trade_date: str | None = None) -> pd.DataFrame:
    history_path = Path(history_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    trade_date = trade_date or dt.date.today().isoformat()

    chain, ts, meta = fetch_spx_options_chain()
    # print("/spx_opt_chain")
    # print(chain.head(30).to_string(index=False))

    spot = float(meta.get("close") or meta.get("current_price"))
    row = aggregate_spx_protection(chain, spot, trade_date)

    # print("/spx_opt_chain")
    # print(pd.DataFrame([row]).head(30).to_string(index=False))

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


# (源列, 分位列名, 报警阈值) —— 全部是 risk-off 方向: call 爆量 / put 爆量 / 权利金爆涨 / 对冲放大
_FLOW_FEATURE_SPECS = [
    ("vx1_opt_call_vol", "vx1_opt_call_vol_pct", CALL_VOL_ARMED_PERCENTILE),
    ("vx1_hedge_net_vx", "vx1_hedge_net_pct", HEDGE_NET_ARMED_PERCENTILE),
    ("spx_prot_put_vol", "spx_prot_put_vol_pct", PROT_PUT_ARMED_PERCENTILE),
    ("spx_prot_premium_usd", "spx_prot_premium_pct", PROT_PREMIUM_ARMED_PERCENTILE),
]


def add_flow_features(hist: pd.DataFrame, *, lookback: int = FLOW_PERCENTILE_LOOKBACK, min_rows: int = FLOW_MIN_ROWS) -> pd.DataFrame:
    """
    risk-off 分位特征与统一红绿灯:
      四个报警源任一触发 -> opt_flow_risk_off_level = RED:
        vx1_opt_call_vol_pct  (VIX call 爆量)
        vx1_hedge_net_pct     (dealer 被迫买 VX 放大)
        spx_prot_put_vol_pct  (SPX 保护区 put 爆量)
        spx_prot_premium_pct  (保护价格被抢高)
      另附 spx_prot_put_oi_chg(保护区OI日变化, 正=净新增对冲)。
    """
    out = hist.copy().sort_values("Trade Date").reset_index(drop=True)
    armed_any = pd.Series(False, index=out.index)
    for col, new, thresh in _FLOW_FEATURE_SPECS:
        if col not in out.columns:
            continue
        out[new] = _trailing_pct(pd.to_numeric(out[col], errors="coerce"), lookback, min_rows).round(1)
        armed_any = armed_any | (out[new] >= thresh).fillna(False)
    if "spx_prot_put_oi" in out.columns:
        out["spx_prot_put_oi_chg"] = pd.to_numeric(out["spx_prot_put_oi"], errors="coerce").diff()
    out["opt_flow_risk_off_level"] = np.where(armed_any, "RED", "GREEN")
    return out


def merge_into_signal(vx_features: pd.DataFrame, *flow_tables: pd.DataFrame) -> pd.DataFrame:
    out = vx_features
    for ft in flow_tables:
        cols = [
            c
            for c in ft.columns
            if c == "Trade Date" or c.endswith(("_pct", "_vol", "_vx", "_ratio", "_level", "_strike", "_usd", "_oi", "_oi_chg", "_share"))
        ]
        out = out.merge(ft[cols], on="Trade Date", how="left")
    return out


# ---------------------------------------------------------------- 与 vix_data_signal.py 的 score 接法(参考)
#   opt_flow_risk_off_level == RED            -> +1~2 (四源任一, 已去重)
#   或分项: spx_prot_put_vol_pct>=90 -> +1 (指数保护) | vx1_opt_call_vol_pct>=90 -> +1 (波动率保护)
#           spx_prot_premium_pct>=90 -> +1 (保险价格) | vx1_hedge_net_pct>=80 -> +1 (对冲放大)
#   持续性: spx_prot_put_oi_chg 连续为正 = 建仓型保护(加权); 单日脉冲+OI不动 = 事件型(标注衰减)
#   方向校验(免费): vx1_opt_cp_ratio >> 1 且 hedge_net_vx > 0 = 单边买call确认

if __name__ == "__main__":
    # cron 21:30 UTC 每日一次。VX结算日用 vix_data_signal.py 的
    # build_vx_monthly_schedule(...) -> [x["fsd"] for x in schedule] 生成。
    here = Path(__file__).resolve().parent
    vix_hist_file = here / "data" / "vix_call_flow_history.csv"
    spx_hist_file = here / "data" / "spx_put_flow_history.csv"

    demo_fsd = ["2026-07-22", "2026-08-19", "2026-09-16", "2026-10-21"]
    vix_hist = vix_daily_snapshot(vix_hist_file, demo_fsd)
    spx_hist = spx_daily_snapshot(spx_hist_file)
    print(add_flow_features(vix_hist).tail(3).to_string(index=False))
    print(add_flow_features(spx_hist).tail(3).to_string(index=False))
