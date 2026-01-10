import datetime as dt
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import certifi
import io

# =========================
# Config
# =========================

SEC_USER_AGENT = "RobinResearch/1.0 (email: youremail@example.com)"
SEC_RATE_LIMIT_SLEEP = 0.2  # seconds

STOOQ_BASE = "https://stooq.com/q/d/l/"

REVENUE_TAG_CANDIDATES = [
    "Revenues",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
]

# Net earnings
NET_INCOME_TAG_CANDIDATES = [
    "NetIncomeLoss",
    "ProfitLoss",
]

# EBIT (closest practical in US GAAP for most: Operating Income)
OPERATING_INCOME_TAG_CANDIDATES = [
    "OperatingIncomeLoss",
    # 你也可扩展更多候选
]

SHARES_TAG_CANDIDATES = [
    "EntityCommonStockSharesOutstanding",
    "CommonStockSharesOutstanding",
]


# =========================
# Helpers
# =========================

def _sec_get_json(url: str, host: str = "data.sec.gov") -> dict:
    headers = {
        "User-Agent": SEC_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json",
    }
    # 有些 SEC URL 不在 data.sec.gov（如 company_tickers.json 在 www.sec.gov）
    if host:
        headers["Host"] = host

    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    time.sleep(SEC_RATE_LIMIT_SLEEP)
    return r.json()

def _as_date(x) -> dt.date:
    if isinstance(x, dt.date):
        return x
    return dt.datetime.strptime(str(x), "%Y-%m-%d").date()

def _pick_first_existing_tag(companyfacts: dict, candidates: List[str]) -> Optional[str]:
    facts = companyfacts.get("facts", {})
    for taxonomy in facts.values():  # e.g., us-gaap
        for tag in candidates:
            if tag in taxonomy:
                return tag
    return None

def _get_fact_table(companyfacts: dict, tag: str) -> Optional[pd.DataFrame]:
    facts = companyfacts.get("facts", {})
    for taxonomy_name, taxonomy in facts.items():
        if tag not in taxonomy:
            continue

        units = taxonomy[tag].get("units", {})
        if not units:
            return None

        # 优先常见单位
        unit_key = None
        for k in ["USD", "shares"]:
            if k in units:
                unit_key = k
                break
        if unit_key is None:
            unit_key = list(units.keys())[0]

        df = pd.DataFrame(units[unit_key])

        # 统一 parse 日期字段
        for col in ["start", "end", "filed"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce").dt.date

        return df
    return None

def _duration_days(row) -> Optional[int]:
    s = row.get("start", None)
    e = row.get("end", None)
    if isinstance(s, dt.date) and isinstance(e, dt.date):
        return (e - s).days
    return None


# =========================
# SEC: Ticker -> CIK
# =========================

def ticker_to_cik(ticker: str) -> str:
    ticker = ticker.upper().strip()
    url = "https://www.sec.gov/files/company_tickers.json"
    data = _sec_get_json(url, host="www.sec.gov")

    for _, row in data.items():
        if str(row.get("ticker", "")).upper() == ticker:
            cik_int = int(row["cik_str"])
            return f"{cik_int:010d}"
    raise ValueError(f"SEC mapping 找不到 ticker={ticker} 的 CIK。")


def get_companyfacts(cik_10: str) -> dict:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_10}.json"
    return _sec_get_json(url, host="data.sec.gov")


# =========================
# Point-in-time quarterly selection
# =========================

@dataclass
class PeriodRecord:
    fy: int
    fp: str           # Q1/Q2/Q3/FY (and we will derive Q4)
    end: dt.date
    filed: dt.date
    val: float
    start: Optional[dt.date] = None
    duration: Optional[int] = None

def _choose_best_record_for_fp(group: pd.DataFrame, fp: str) -> Optional[pd.Series]:
    """
    同一 (fy, fp) 可能有季度值 + YTD值（尤其 fp=Q2/Q3），我们需要挑“季度值”：
    - Q1/Q2/Q3：选 duration 最短（更像单季度），若无 start 则退化用 filed 最新
    - FY：选 duration 最长（更像全年），若无 start 则退化用 filed 最新
    """
    g = group.copy()

    # duration
    if "start" in g.columns and "end" in g.columns:
        g["duration"] = g.apply(lambda r: _duration_days(r), axis=1)
    else:
        g["duration"] = None

    # 如果 duration 全是 NA，直接选 filed 最新
    if g["duration"].isna().all():
        g = g.sort_values("filed")
        return g.iloc[-1]

    # 有 duration：FY 选最大，其它选最小
    if fp == "FY":
        # 先按 duration，再按 filed
        g = g.sort_values(["duration", "filed"])
        return g.iloc[-1]
    else:
        # Q1/Q2/Q3：选最小 duration；如果多个同 duration，选 filed 最新
        min_d = g["duration"].min()
        gg = g[g["duration"] == min_d].sort_values("filed")
        return gg.iloc[-1]

def build_quarter_records_point_in_time(
    fact_df: pd.DataFrame,
    asof: dt.date,
) -> List[PeriodRecord]:
    """
    生成“as-of 可见”的季度记录列表：
    - 从 facts 里挑出每个 (fy, fp) 最合适的一条（避免误取 YTD）
    - Q4 用 FY - (Q1+Q2+Q3) 推导（如果齐全）
    """
    if fact_df is None or fact_df.empty:
        return []

    df = fact_df.copy()

    # filed<=asof
    if "filed" not in df.columns:
        return []
    df = df[df["filed"].notna()]
    df = df[df["filed"] <= asof]
    if df.empty:
        return []

    # 只保留常见报表表单（你可扩展 20-F 等）
    if "form" in df.columns:
        df = df[df["form"].isin(["10-Q", "10-K", "10-Q/A", "10-K/A"])]
        if df.empty:
            return []

    needed = {"fy", "fp", "end", "val", "filed"}
    if not needed.issubset(set(df.columns)):
        return []

    # group by (fy, fp) then choose best record
    records_by_year: Dict[int, Dict[str, PeriodRecord]] = {}

    for (fy, fp), g in df.groupby(["fy", "fp"]):
        fy_i = int(fy)
        fp_s = str(fp)

        if fp_s not in ["Q1", "Q2", "Q3", "FY"]:
            continue

        best = _choose_best_record_for_fp(g, fp_s)
        if best is None:
            continue

        rec = PeriodRecord(
            fy=fy_i,
            fp=fp_s,
            end=best["end"],
            filed=best["filed"],
            val=float(best["val"]),
            start=best.get("start", None),
            duration=int(best["duration"]) if pd.notna(best.get("duration", None)) else None,
        )
        records_by_year.setdefault(fy_i, {})[fp_s] = rec

    # build final list with Q4 derived when possible
    out: List[PeriodRecord] = []

    for fy, mp in records_by_year.items():
        # add Q1-Q3 as-is
        for fp in ["Q1", "Q2", "Q3"]:
            if fp in mp:
                out.append(mp[fp])

        # derive Q4 if FY and Q1-Q3 exist
        if "FY" in mp and all(q in mp for q in ["Q1", "Q2", "Q3"]):
            fy_rec = mp["FY"]
            qsum = mp["Q1"].val + mp["Q2"].val + mp["Q3"].val
            q4_val = fy_rec.val - qsum
            out.append(PeriodRecord(
                fy=fy,
                fp="Q4",
                end=fy_rec.end,
                filed=fy_rec.filed,      # Q4 视为随着 10-K 披露
                val=float(q4_val),
                start=None,
                duration=None,
            ))

    # 去重：同一个 end 可能来自不同 fy（极少），按 end 排序后保留最后一个
    out = sorted(out, key=lambda r: (r.end, r.filed))
    dedup: Dict[dt.date, PeriodRecord] = {}
    for r in out:
        dedup[r.end] = r
    return list(dedup.values())


@dataclass
class TTMResult:
    ttm_value: float
    quarter_ends_used: List[dt.date]
    quarter_values_used: List[float]

def compute_ttm_from_quarter_records(qrecs: List[PeriodRecord]) -> Optional[TTMResult]:
    if not qrecs or len(qrecs) < 4:
        return None
    qrecs_sorted = sorted(qrecs, key=lambda r: r.end)
    last4 = qrecs_sorted[-4:]
    vals = [r.val for r in last4]
    ends = [r.end for r in last4]
    return TTMResult(ttm_value=sum(vals), quarter_ends_used=ends, quarter_values_used=vals)

def latest_quarter_and_yoy(
    qrecs: List[PeriodRecord],
) -> Tuple[Optional[PeriodRecord], Optional[float]]:
    """
    返回最新季度记录以及 YoY%（按 fy-1 + 同 fp 匹配）
    YoY% = (current / prev - 1)*100
    """
    if not qrecs:
        return None, None

    q_sorted = sorted(qrecs, key=lambda r: r.end)
    latest = q_sorted[-1]

    # 找上一年同季度
    prev = None
    for r in qrecs:
        if r.fp == latest.fp and r.fy == latest.fy - 1:
            prev = r
            break

    if prev is None or prev.val == 0:
        return latest, None

    yoy = (latest.val / prev.val - 1.0) * 100.0
    return latest, yoy


# =========================
# Shares / Price
# =========================

def get_latest_shares_outstanding(companyfacts: dict, asof: dt.date) -> Optional[Tuple[float, dt.date]]:
    tag = _pick_first_existing_tag(companyfacts, SHARES_TAG_CANDIDATES)
    if not tag:
        return None
    df = _get_fact_table(companyfacts, tag)
    if df is None or df.empty:
        return None
    if "filed" not in df.columns:
        return None
    df = df[df["filed"].notna()]
    df = df[df["filed"] <= asof]
    if df.empty:
        return None

    # 同 end 取 filed 最新
    df = df.sort_values(["end", "filed"]).drop_duplicates(subset=["end"], keep="last")
    last = df.sort_values("filed").iloc[-1]
    return float(last["val"]), last["filed"]

def get_stooq_close_price_us(ticker: str, asof: dt.date) -> float:
    sym = f"{ticker.lower()}.us"
    url = f"{STOOQ_BASE}?s={sym}&i=d"

    resp = requests.get(url, timeout=30, verify=certifi.where())
    resp.raise_for_status()

    df = pd.read_csv(io.StringIO(resp.text))
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    row = df[df["Date"] == asof]
    if row.empty:
        raise ValueError(f"Stooq 找不到 {ticker} 在 {asof} 的数据（可能非交易日或代码不同）。")
    return float(row.iloc[0]["Close"])


# =========================
# Main
# =========================

@dataclass
class ValuationAndYoYResult:
    ticker: str
    asof: dt.date

    price: float
    shares_out: Optional[float]
    shares_filed_date: Optional[dt.date]
    market_cap: Optional[float]

    revenue_tag: Optional[str]
    opinc_tag: Optional[str]
    netinc_tag: Optional[str]

    revenue_ttm: Optional[TTMResult]
    netinc_ttm: Optional[TTMResult]

    ps_ttm: Optional[float]
    pe_ttm: Optional[float]

    latest_quarter_end: Optional[dt.date]
    latest_quarter_fp: Optional[str]
    latest_quarter_fy: Optional[int]

    revenue_q: Optional[float]
    revenue_yoy_pct: Optional[float]

    opinc_q: Optional[float]
    opinc_yoy_pct: Optional[float]

    netinc_q: Optional[float]
    netinc_yoy_pct: Optional[float]


def compute_all(
    ticker: str,
    asof: str,
) -> ValuationAndYoYResult:
    asof_d = _as_date(asof)

    cik = ticker_to_cik(ticker)
    facts = get_companyfacts(cik)

    # tags
    rev_tag = _pick_first_existing_tag(facts, REVENUE_TAG_CANDIDATES)
    op_tag = _pick_first_existing_tag(facts, OPERATING_INCOME_TAG_CANDIDATES)
    ni_tag = _pick_first_existing_tag(facts, NET_INCOME_TAG_CANDIDATES)

    # quarter records
    rev_qrecs = []
    op_qrecs = []
    ni_qrecs = []

    if rev_tag:
        rev_df = _get_fact_table(facts, rev_tag)
        rev_qrecs = build_quarter_records_point_in_time(rev_df, asof_d)

    if op_tag:
        op_df = _get_fact_table(facts, op_tag)
        op_qrecs = build_quarter_records_point_in_time(op_df, asof_d)

    if ni_tag:
        ni_df = _get_fact_table(facts, ni_tag)
        ni_qrecs = build_quarter_records_point_in_time(ni_df, asof_d)

    # TTM
    rev_ttm = compute_ttm_from_quarter_records(rev_qrecs)
    ni_ttm = compute_ttm_from_quarter_records(ni_qrecs)

    # Latest quarter as-of (用 revenue 的最新季度作为“最新财报季度”的锚)
    latest_rev, rev_yoy = latest_quarter_and_yoy(rev_qrecs)
    latest_end = latest_rev.end if latest_rev else None
    latest_fp = latest_rev.fp if latest_rev else None
    latest_fy = latest_rev.fy if latest_rev else None

    # 对 op income / net income：取同一季度（fy/fp）对应值与yoy
    def _find_same_quarter_value_and_yoy(qrecs: List[PeriodRecord], fy: int, fp: str) -> Tuple[Optional[float], Optional[float]]:
        if fy is None or fp is None:
            return None, None
        cur = None
        prev = None
        for r in qrecs:
            if r.fy == fy and r.fp == fp:
                cur = r
            if r.fy == fy - 1 and r.fp == fp:
                prev = r
        if cur is None:
            return None, None
        if prev is None or prev.val == 0:
            return float(cur.val), None
        yoy = (cur.val / prev.val - 1.0) * 100.0
        return float(cur.val), yoy

    op_q, op_yoy = _find_same_quarter_value_and_yoy(op_qrecs, latest_fy, latest_fp) if latest_fy and latest_fp else (None, None)
    ni_q, ni_yoy = _find_same_quarter_value_and_yoy(ni_qrecs, latest_fy, latest_fp) if latest_fy and latest_fp else (None, None)

    # price
    price = get_stooq_close_price_us(ticker, asof_d)

    # shares & market cap
    shares_info = get_latest_shares_outstanding(facts, asof_d)
    shares_out, shares_filed = (shares_info if shares_info else (None, None))
    market_cap = price * shares_out if shares_out else None

    # ratios
    ps = None
    pe = None
    if market_cap and rev_ttm and rev_ttm.ttm_value != 0:
        ps = market_cap / rev_ttm.ttm_value
    if market_cap and ni_ttm and ni_ttm.ttm_value != 0:
        pe = market_cap / ni_ttm.ttm_value

    return ValuationAndYoYResult(
        ticker=ticker.upper(),
        asof=asof_d,

        price=price,
        shares_out=shares_out,
        shares_filed_date=shares_filed,
        market_cap=market_cap,

        revenue_tag=rev_tag,
        opinc_tag=op_tag,
        netinc_tag=ni_tag,

        revenue_ttm=rev_ttm,
        netinc_ttm=ni_ttm,

        ps_ttm=ps,
        pe_ttm=pe,

        latest_quarter_end=latest_end,
        latest_quarter_fp=latest_fp,
        latest_quarter_fy=latest_fy,

        revenue_q=float(latest_rev.val) if latest_rev else None,
        revenue_yoy_pct=rev_yoy,

        opinc_q=op_q,
        opinc_yoy_pct=op_yoy,

        netinc_q=ni_q,
        netinc_yoy_pct=ni_yoy,
    )


def pretty_print(res: ValuationAndYoYResult) -> None:
    print(f"Ticker: {res.ticker}")
    print(f"As-of date: {res.asof}")
    print(f"Close price: {res.price:,.4f}")

    if res.shares_out:
        print(f"Shares outstanding (latest filed<=asof): {res.shares_out:,.0f} (filed {res.shares_filed_date})")
        print(f"Market cap: {res.market_cap/1e9:,.3f} B")
    else:
        print("Shares outstanding: N/A")

    print("\nTTM Ratios (as-of):")
    print(f"  P/S (TTM): {res.ps_ttm:,.4f}x" if res.ps_ttm is not None else "  P/S (TTM): N/A")
    print(f"  P/E (TTM): {res.pe_ttm:,.4f}x" if res.pe_ttm is not None else "  P/E (TTM): N/A")

    if res.revenue_ttm:
        print(f"\nRevenue tag: {res.revenue_tag}")
        print(f"Revenue TTM: {res.revenue_ttm.ttm_value/1e9:,.6f} B")
        print("Revenue quarters used:")
        for d, v in zip(res.revenue_ttm.quarter_ends_used, res.revenue_ttm.quarter_values_used):
            print(f"  {d}: {v/1e6:,.3f} M")

    if res.netinc_ttm:
        print(f"\nNet income tag: {res.netinc_tag}")
        print(f"Net income TTM: {res.netinc_ttm.ttm_value/1e9:,.6f} B")
        print("Net income quarters used:")
        for d, v in zip(res.netinc_ttm.quarter_ends_used, res.netinc_ttm.quarter_values_used):
            print(f"  {d}: {v/1e6:,.3f} M")

    print("\nLatest reported quarter visible as-of this date:")
    if res.latest_quarter_end:
        print(f"  Quarter: FY{res.latest_quarter_fy} {res.latest_quarter_fp} (end {res.latest_quarter_end})")
    else:
        print("  N/A (no quarterly revenue available)")

    def _fmt_yoy(x):
        return "N/A" if x is None else f"{x:,.2f}%"

    print("\nYoY% (latest quarter):")
    print(f"  Revenue YoY: {_fmt_yoy(res.revenue_yoy_pct)}")
    print(f"  Operating income YoY: {_fmt_yoy(res.opinc_yoy_pct)}")
    print(f"  Net earnings YoY: {_fmt_yoy(res.netinc_yoy_pct)}")

    # 也把当季绝对值打印出来（方便 sanity check）
    if res.latest_quarter_end:
        print("\nLatest quarter absolute values (for sanity check):")
        if res.revenue_q is not None:
            print(f"  Revenue: {res.revenue_q/1e6:,.3f} M")
        if res.opinc_q is not None:
            print(f"  Operating income: {res.opinc_q/1e6:,.3f} M")
        if res.netinc_q is not None:
            print(f"  Net earnings: {res.netinc_q/1e6:,.3f} M")


if __name__ == "__main__":
    # Example: APP on 2025-08-01
    r = compute_all("APP", "2025-08-01")
    pretty_print(r)
