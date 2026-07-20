import contextlib
import datetime as dt
import ssl
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from vix_options_flow import add_flow_features, spx_daily_snapshot, vix_daily_snapshot

pd.set_option("display.max_rows", None)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", None)


# 月份码（如果你后面要生成 VXH5 之类符号会用到）
MONTH_CODE = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M", 7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}
CBOE_INDEX_HISTORY_URLS = {
    "VIX": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    "VIXEQ": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIXEQ_History.csv",
    "VXN": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VXN_History.csv",
    "VXSMH": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VXSMH_History.csv",
}
VIXEQ_VIX_SPREAD_ARMED_PERCENTILE = 85.0
VIXEQ_VIX_SPREAD_MIN_ROWS = 252
VXSMH_PERCENTILE_LOOKBACK = 252
VXSMH_RISK_OFF_PERCENTILE = 85.0
# VIXEQ_RHO_ARMED_THRESHOLD = 0.15


def third_friday(year: int, month: int) -> dt.date:
    """该月第三个周五"""
    d0 = dt.date(year, month, 1)
    # weekday: Mon=0..Sun=6, Fri=4
    first_friday = d0 + dt.timedelta(days=(4 - d0.weekday()) % 7)
    return first_friday + dt.timedelta(days=14)


def prev_business_day(d: dt.date, holidays: set[dt.date]) -> dt.date:
    """若 d 是周末/假日，则不断向前滚到最近工作日"""
    while d.weekday() >= 5 or d in holidays:  # 5=Sat,6=Sun
        d -= dt.timedelta(days=1)
    return d


def vix_monthly_final_settlement(contract_year: int, contract_month: int, holidays: set[dt.date] | None = None) -> dt.date:
    """
    月度VX合约最终结算日（Final Settlement Date）：
    = 下一自然月第三个周五(如遇假日先向前找工作日) - 30天
    并可选：若计算得到的结算日是周末/假日，则向前滚到最近工作日
    """
    holidays = holidays or set()

    # next month
    if contract_month == 12:
        ny, nm = contract_year + 1, 1
    else:
        ny, nm = contract_year, contract_month + 1

    tf = third_friday(ny, nm)
    tf_adj = prev_business_day(tf, holidays)  # 第三个周五如遇假日，向前找工作日
    fsd = tf_adj - dt.timedelta(days=30)  # 标准规则：往前30天（通常是周三）

    # 结算日本身如果是周末/假日，也向前滚到工作日（更稳健）
    fsd_adj = prev_business_day(fsd, holidays)
    return fsd_adj


def add_months(y: int, m: int, k: int) -> tuple[int, int]:
    """(y,m) 加 k 个月，k可为负"""
    total = y * 12 + (m - 1) + k
    ny = total // 12
    nm = total % 12 + 1
    return ny, nm


def build_vx_monthly_schedule(start_date: dt.date, end_date: dt.date, holidays: set[dt.date]) -> list[dict]:
    """
    生成 [start_date, end_date] 覆盖范围内的“合约月份 -> Final Settlement Date”列表。
    为了保证边界日期也能找到 current，需要向前/向后扩展几个月。
    """
    # buffer：多算前后几个月，避免 start/end 落在边界导致查不到
    buf_months = 6

    def add_months_local(y: int, m: int, k: int) -> tuple[int, int]:
        total = y * 12 + (m - 1) + k
        return total // 12, total % 12 + 1

    y0, m0 = add_months_local(start_date.year, start_date.month, -buf_months)
    y1, m1 = add_months_local(end_date.year, end_date.month, +buf_months)

    # 生成从 (y0,m0) 到 (y1,m1) 的每个月合约月份及其到期日
    schedule = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        fsd = vix_monthly_final_settlement(y, m, holidays)
        schedule.append({"contract_month": f"{y:04d}-{m:02d}", "fsd": fsd})
        y, m = add_months_local(y, m, +1)

    # 按到期日排序（保证“找最早未到期”）
    schedule.sort(key=lambda x: x["fsd"])
    return schedule


def vxcurrent_contract_month_for_date(d: dt.date, schedule: list[dict]) -> str:
    """
    给定日期 d，返回该日期对应的 VXCurrent 合约月份（YYYY-MM）。
    逻辑：找 fsd >= d 的最小那一个合约月份。
    """
    for item in schedule:
        if item["fsd"] >= d:
            return item["contract_month"]
    raise ValueError(f"No VXCurrent found for date={d}; schedule range too small.")


def vxnext_contract_month_for_date(d: dt.date, schedule: list[dict]) -> str:
    """
    给定日期 d，返回下一个月的 VX 合约月份（YYYY-MM）。
    逻辑：找 fsd >= d 的第二个合约月份。
    """
    found = 0
    for item in schedule:
        if item["fsd"] >= d:
            found += 1
            if found == 2:
                return item["contract_month"]
    raise ValueError(f"No VXNext found for date={d}; schedule range too small.")


def is_business_day(d: dt.date, holidays: set[dt.date]) -> bool:
    return (d.weekday() < 5) and (d not in holidays)


def vxcurrent_map(start_date: dt.date, end_date: dt.date, holidays: set[dt.date], business_days_only: bool = True) -> dict[dt.date, str]:
    """
    返回 dict: {date -> VXCurrent contract_month}
    - business_days_only=True: 只返回交易日（排除周末和holiday）
    - business_days_only=False: 区间内每天都返回
    """
    if end_date < start_date:
        raise ValueError("end_date must be >= start_date")

    schedule = build_vx_monthly_schedule(start_date, end_date, holidays)

    out = {}
    d = start_date
    while d <= end_date:
        if (not business_days_only) or is_business_day(d, holidays):
            out[d] = vxcurrent_contract_month_for_date(d, schedule)
        d += dt.timedelta(days=1)

    return out


def vxnext_map(start_date: dt.date, end_date: dt.date, holidays: set[dt.date], business_days_only: bool = True) -> dict[dt.date, str]:
    """
    返回 dict: {date -> VXNext contract_month} (第二近月合约)
    """
    if end_date < start_date:
        raise ValueError("end_date must be >= start_date")

    schedule = build_vx_monthly_schedule(start_date, end_date, holidays)

    out = {}
    d = start_date
    while d <= end_date:
        if (not business_days_only) or is_business_day(d, holidays):
            out[d] = vxnext_contract_month_for_date(d, schedule)
        d += dt.timedelta(days=1)

    return out


def vx_expiry_table(x: dt.date, n_months_back: int, holidays: set[dt.date] | None = None):
    """
    输出从 (x 往前 n 个月) 到 x 的每个月度VX合约（按合约月份）对应的最终结算日
    """
    holidays = holidays or set()
    y0, m0 = add_months(x.year, x.month, -n_months_back)

    rows = []
    y, m = y0, m0
    while (y, m) <= (x.year, x.month):
        fsd = vix_monthly_final_settlement(y, m, holidays)
        rows.append(
            {
                "contract_month": f"{y:04d}-{m:02d}",
                "vx_symbol_2y": f"VX{MONTH_CODE[m]}{y % 100:02d}",  # 例如 VXH25
                "final_settlement": fsd.isoformat(),
            }
        )
        y, m = add_months(y, m, +1)

    return rows


def parse_holidays(date_strs: list[str]) -> set[dt.date]:
    return {dt.date.fromisoformat(s) for s in date_strs}


def load_vx_csvs(data_dir: str | Path) -> pd.DataFrame:
    """Load all VX CSVs under data_dir into a single DataFrame."""
    data_dir = Path(data_dir)
    csv_files = sorted(data_dir.glob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No CSV files found under {data_dir}")
    dfs = [pd.read_csv(p) for p in csv_files]
    return pd.concat(dfs, ignore_index=True)


def download_cboe_vx_csvs(
    cboe_vx_futures_hlocv_data: dict[str, str], data_dir: str | Path | None = None, *, verify_ssl: bool = True, cafile: str | None = None
) -> list[Path]:
    """Download CSVs to data_dir using dict keys as filenames."""
    if data_dir is None:
        data_dir = Path(__file__).resolve().parent / "data"
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    if not verify_ssl:
        context = ssl._create_unverified_context()
    else:
        if cafile is None:
            try:
                import certifi  # type: ignore
            except ImportError:
                certifi = None
            if certifi is not None:
                cafile = certifi.where()
        context = ssl.create_default_context(cafile=cafile)

    saved_paths = []
    for name, url in cboe_vx_futures_hlocv_data.items():
        out_path = data_dir / f"{name}.csv"
        with urllib.request.urlopen(url, context=context) as resp:
            out_path.write_bytes(resp.read())
        saved_paths.append(out_path)
    return saved_paths


def download_cboe_index_csvs(
    index_history_urls: dict[str, str], data_dir: str | Path | None = None, *, verify_ssl: bool = True, cafile: str | None = None
) -> dict[str, Path]:
    """Download Cboe index history CSVs to data_dir using index tickers as filenames."""
    if data_dir is None:
        data_dir = Path(__file__).resolve().parent / "data" / "indices"
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    if not verify_ssl:
        context = ssl._create_unverified_context()
    else:
        if cafile is None:
            try:
                import certifi  # type: ignore
            except ImportError:
                certifi = None
            if certifi is not None:
                cafile = certifi.where()
        context = ssl.create_default_context(cafile=cafile)

    saved_paths = {}
    for name, url in index_history_urls.items():
        out_path = data_dir / f"{name}_History.csv"
        with urllib.request.urlopen(url, context=context) as resp:
            out_path.write_bytes(resp.read())
        saved_paths[name] = out_path
    return saved_paths


def load_cboe_index_history_csv(path: str | Path, index_name: str) -> pd.DataFrame:
    """Load a Cboe index history CSV and normalize to Trade Date plus one numeric value column."""
    path = Path(path)
    raw = pd.read_csv(path)
    if "DATE" not in raw.columns:
        raise ValueError(f"{path} is missing DATE column")

    index_name = index_name.upper()
    if index_name == "VIX":
        source_col = "CLOSE"
        value_col = "vix"
    elif index_name == "VIXEQ":
        source_col = "VIXEQ"
        value_col = "vixeq"
    else:
        source_col = "CLOSE" if "CLOSE" in raw.columns else index_name
        value_col = index_name.lower()

    if source_col not in raw.columns:
        raise ValueError(f"{path} is missing {source_col} column")

    out = pd.DataFrame(
        {"Trade Date": pd.to_datetime(raw["DATE"], format="%m/%d/%Y", errors="coerce"), value_col: pd.to_numeric(raw[source_col], errors="coerce")}
    )
    out = out.dropna(subset=["Trade Date", value_col]).sort_values("Trade Date").reset_index(drop=True)
    out["Trade Date"] = out["Trade Date"].dt.strftime("%Y-%m-%d")
    return out


def add_vxn_ma20_signal(vxn_df: pd.DataFrame, *, ma_window: int = 20) -> pd.DataFrame:
    """Add VXN level and 20-trading-day trend features."""
    required_cols = {"Trade Date", "vxn"}
    missing_cols = required_cols.difference(vxn_df.columns)
    if missing_cols:
        raise ValueError(f"vxn_df is missing required columns: {sorted(missing_cols)}")
    if ma_window < 1:
        raise ValueError("ma_window must be at least 1")

    out = vxn_df.copy()
    out["vxn"] = pd.to_numeric(out["vxn"], errors="coerce")
    out = out.sort_values("Trade Date").reset_index(drop=True)
    out["vxn_ma20"] = out["vxn"].rolling(ma_window, min_periods=ma_window).mean()
    out["vxn_gt_ma20"] = out["vxn"] > out["vxn_ma20"]
    out["vxn_ma20_rising"] = out["vxn_ma20"].diff() > 0
    vxn_risk_off = out["vxn_gt_ma20"] & out["vxn_ma20_rising"]
    out["vxn_risk_off_level"] = np.where(vxn_risk_off, "RED", "GREEN")
    return out


def add_vxsmh_percentile_signal(
    vxsmh_df: pd.DataFrame, *, lookback_rows: int = VXSMH_PERCENTILE_LOOKBACK, risk_off_percentile: float = VXSMH_RISK_OFF_PERCENTILE
) -> pd.DataFrame:
    """Add VXSMH, its trailing percentile, and the percentile-based risk level."""
    required_cols = {"Trade Date", "vxsmh"}
    missing_cols = required_cols.difference(vxsmh_df.columns)
    if missing_cols:
        raise ValueError(f"vxsmh_df is missing required columns: {sorted(missing_cols)}")
    if lookback_rows < 1:
        raise ValueError("lookback_rows must be at least 1")
    if not 0.0 <= risk_off_percentile <= 100.0:
        raise ValueError("risk_off_percentile must be between 0 and 100")

    out = vxsmh_df.copy()
    out["vxsmh"] = pd.to_numeric(out["vxsmh"], errors="coerce")
    out = out.sort_values("Trade Date").reset_index(drop=True)

    def pct_rank_in_window(arr: np.ndarray) -> float:
        arr = arr.astype(float)
        arr = arr[~np.isnan(arr)]
        current_value = arr[-1]
        return float(np.mean(arr <= current_value) * 100.0)

    out["vxsmh_pct"] = out["vxsmh"].rolling(window=lookback_rows, min_periods=1).apply(pct_rank_in_window, raw=True).round(1)
    out["vxsmh_risk_off_level"] = np.where(out["vxsmh_pct"] > risk_off_percentile, "RED", "GREEN")
    return out


def add_vixeq_vix_spread_signal(
    vixeq_df: pd.DataFrame,
    vix_df: pd.DataFrame,
    *,
    percentile_threshold: float = VIXEQ_VIX_SPREAD_ARMED_PERCENTILE,
    # rho_threshold: float = VIXEQ_RHO_ARMED_THRESHOLD,
    lookback_rows: int | None = None,
    min_rows: int = VIXEQ_VIX_SPREAD_MIN_ROWS,
) -> pd.DataFrame:
    """
    Build VIX vs VIXEQ structural risk-off features.

    Percentile is inclusive of the current row. By default it uses all history
    available up to each date, so a record-wide spread prints as 100.
    """
    out = vixeq_df.merge(vix_df, on="Trade Date", how="inner")
    out = out.sort_values("Trade Date").reset_index(drop=True)
    out["vixeq_vix_spread"] = out["vixeq"] - out["vix"]

    def pct_rank_in_window(arr: np.ndarray) -> float:
        arr = arr.astype(float)
        arr = arr[~np.isnan(arr)]
        if len(arr) < min_rows:
            return np.nan
        v = arr[-1]
        return float(np.mean(arr <= v) * 100.0)

    if lookback_rows is None:
        spread_pct = out["vixeq_vix_spread"].expanding(min_periods=min_rows).apply(pct_rank_in_window, raw=True)
    else:
        min_periods = min(min_rows, lookback_rows)
        spread_pct = out["vixeq_vix_spread"].rolling(window=lookback_rows, min_periods=min_periods).apply(pct_rank_in_window, raw=True)

    out["spread_pct"] = spread_pct.round(1)
    spread_armed = spread_pct > percentile_threshold
    # rho is the standard correlation symbol. This is a VIXEQ-derived proxy, not official Cboe COR1M/COR3M.
    # out["rho"] = np.where(out["vixeq"] > 0, (out["vix"] / out["vixeq"]) ** 2, np.nan)
    # out["rho_armed"] = out["rho"] < rho_threshold
    out["vixeq_risk_off_level"] = np.select([spread_armed.fillna(False)], ["RED"], default="GREEN")
    return out


def contract_month_to_futures_label(contract_month: str) -> str:
    """Convert YYYY-MM to VX futures label like 'J (Apr 2025)'."""
    year, month = (int(x) for x in contract_month.split("-"))
    month_label = dt.date(year, month, 1).strftime("%b %Y")
    return f"{MONTH_CODE[month]} ({month_label})"


def rows_for_vxcurrent_map(m: dict[dt.date, str], all_data: pd.DataFrame, *, strict: bool = True) -> pd.DataFrame:
    """Return rows from all_data matching each trade date and its VXCurrent futures label."""
    rows = []
    for trade_date, contract_month in m.items():
        trade_date_str = trade_date.isoformat()
        futures_label = contract_month_to_futures_label(contract_month)
        match = all_data.loc[(all_data["Trade Date"] == trade_date_str) & (all_data["Futures"] == futures_label)]
        if match.empty:
            if strict:
                raise LookupError(f"No matching row for Trade Date={trade_date_str}, Futures={futures_label}")
            continue
        rows.append(match)
    if not rows:
        return all_data.iloc[0:0].copy()
    return pd.concat(rows, ignore_index=True)


def add_volume_metrics_rows_incl_today_strict(
    df: pd.DataFrame,
    *,
    date_col: str = "Trade Date",
    volume_col: str = "Total Volume",
    lookback_rows: int = 252,  # 252 # 365
    ma_window: int = 50,
) -> pd.DataFrame:
    out = df.copy()
    out[date_col] = pd.to_datetime(out[date_col], errors="coerce")
    out[volume_col] = pd.to_numeric(out[volume_col], errors="coerce")
    out = out.sort_values(date_col).reset_index(drop=True)

    out["Close_MA20"] = out["Close"].rolling(20, min_periods=20).mean()
    out["Close_MA50"] = out["Close"].rolling(50, min_periods=50).mean()

    # Price signals
    out["close_gt_ma50"] = out["Close"] > out["Close_MA50"]
    out["ma20_rising"] = out["Close_MA20"].diff() > 0
    out["close_gt_ma20"] = out["Close"] > out["Close_MA20"]

    # Volume signals
    out["Volume_MA50"] = out[volume_col].rolling(ma_window, min_periods=ma_window).mean()
    out["Volume/MA50"] = out[volume_col] / out["Volume_MA50"]

    def pct_rank_in_window(arr: np.ndarray) -> float:
        arr = arr.astype(float)
        arr = arr[~np.isnan(arr)]
        if len(arr) < lookback_rows:
            return np.nan
        v = arr[-1]  # 当日值（窗口最后一个）
        return float(np.mean(arr <= v) * 100.0)

    out["volume_pct"] = out[volume_col].rolling(window=lookback_rows, min_periods=lookback_rows).apply(pct_rank_in_window, raw=True)

    out["vol_ge_1.85x_ma50"] = out["Volume/MA50"] >= 1.85
    out["vol_ge_90pct"] = out["volume_pct"] >= 90

    out["vol_ge_90pct_last_5days"] = out["volume_pct"].rolling(window=5, min_periods=5).apply(lambda arr: float(np.sum(arr >= 90.0)), raw=True)
    out["vol_ge_90pct_last_10days"] = out["volume_pct"].rolling(window=10, min_periods=10).apply(lambda arr: float(np.sum(arr >= 90.0)), raw=True)

    # risk_off_score uses VX current + VX next price/volume conditions only.
    risk_off_components = [
        out["close_gt_ma50"],
        out["close_gt_ma20"],
        out["ma20_rising"],
        out["vol_ge_1.85x_ma50"],
        out["vol_ge_90pct"],
        out["vol_ge_90pct_last_5days"] >= 2,
        out["vol_ge_90pct_last_10days"] >= 3,
    ]

    score = pd.Series(0, index=out.index, dtype="int64")
    for component in risk_off_components:
        score = score + component.fillna(False).astype(bool).astype(int)
    out["risk_off_score"] = score
    out["risk_off_level"] = pd.cut(out["risk_off_score"], bins=[-1, 1, 3, 5, np.inf], labels=["GREEN", "YELLOW", "ORANGE", "RED"])

    out = out.reset_index(drop=True)
    return out


def print_date_range(df: pd.DataFrame, start: str, end: str, cols: list[str] | None = None, label: str = "") -> None:
    """Print rows of df where Trade Date is between [start, end]."""
    mask = (df["Trade Date"] >= start) & (df["Trade Date"] <= end)
    tag = label or df.columns[1]  # fallback to second col name as label
    print(f"\n/{tag} [{start}, {end}]:")
    if cols:
        print(df.loc[mask, cols])
    else:
        print(df.loc[mask])


def run_options_flow_snapshots(end_date: dt.date, holidays: set[dt.date], data_dir: str | Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Save and print the daily VIX-call and SPX-put flow features."""
    data_dir = Path(data_dir)
    option_schedule = build_vx_monthly_schedule(end_date, end_date + dt.timedelta(days=365), holidays)
    vx_settlement_dates = [item["fsd"] for item in option_schedule if item["fsd"] >= end_date]

    vix_hist = vix_daily_snapshot(data_dir / "vix_call_flow_history.csv", vx_settlement_dates)
    spx_hist = spx_daily_snapshot(data_dir / "spx_put_flow_history.csv")
    vix_flow_features = add_flow_features(vix_hist)
    spx_flow_features = add_flow_features(spx_hist)

    print("/VIX call options flow latest:")
    print(vix_flow_features.tail(3).to_string(index=False))
    print("/SPX put protection flow latest:")
    print(spx_flow_features.tail(3).to_string(index=False))
    return vix_flow_features, spx_flow_features


def run_vx_eod_report(end_date: dt.date) -> None:
    HOLIDAYS_2023 = [
        "2023-01-02",  # New Year's Day (observed)
        "2023-01-16",  # Martin Luther King, Jr. Day
        "2023-02-20",  # Presidents' Day (Washington’s Birthday)
        "2023-04-07",  # Good Friday
        "2023-05-29",  # Memorial Day
        "2023-06-19",  # Juneteenth National Independence Day
        "2023-07-04",  # Independence Day
        "2023-09-04",  # Labor Day
        "2023-11-23",  # Thanksgiving Day
        "2023-12-25",  # Christmas Day
    ]

    HOLIDAYS_2024 = [
        "2024-01-01",  # New Year's Day
        "2024-01-15",  # Martin Luther King, Jr. Day
        "2024-02-19",  # Presidents' Day (Washington’s Birthday)
        "2024-03-29",  # Good Friday
        "2024-05-27",  # Memorial Day
        "2024-06-19",  # Juneteenth National Independence Day
        "2024-07-04",  # Independence Day
        "2024-09-02",  # Labor Day
        "2024-11-28",  # Thanksgiving Day
        "2024-12-25",  # Christmas Day
    ]

    holidays = parse_holidays(
        HOLIDAYS_2023
        + HOLIDAYS_2024
        + [
            "2025-01-01",
            "2025-01-09",
            "2025-01-20",
            "2025-02-17",
            "2025-04-18",
            "2025-05-26",
            "2025-06-19",
            "2025-07-04",
            "2025-09-01",
            "2025-11-27",
            "2025-12-25",
            "2026-01-01",
            "2026-01-19",
            "2026-02-16",
            "2026-04-03",
            "2026-05-25",
            "2026-06-19",
            "2026-07-03",
            "2026-09-07",
            "2026-11-26",
            "2026-12-25",
        ]
    )
    x = dt.date(2025, 2, 24)
    table = vx_expiry_table(x, n_months_back=12, holidays=holidays)  # holidays 你可以传入Cboe options holiday日期集合
    for r in table:
        print(r)

    # ===== find the current VXCurrent contract month for each trading day [start_date, end_date] =====
    start_date = dt.date(2021, 1, 1)

    m = vxcurrent_map(start_date, end_date, holidays, business_days_only=True)

    print("/trade_date to VXCurrent contract month:")
    # 打印看看
    for k in sorted(m.keys())[-10:]:
        print(k, "->", m[k])

    # ===== download the latest VX futures HLOCV data =====
    cboe_vx_futures_hlocv_data = {
        "CFE_VX_F4_2024": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2024-01-17.csv",
        "CFE_VX_G4_2024": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2024-02-14.csv",
        "CFE_VX_H4_2024": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2024-03-20.csv",
        "CFE_VX_J4_2024": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2024-04-17.csv",
        "CFE_VX_K4_2024": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2024-05-22.csv",
        "CFE_VX_M4_2024": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2024-06-18.csv",
        "CFE_VX_N4_2024": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2024-07-17.csv",
        "CFE_VX_Q4_2024": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2024-08-21.csv",
        "CFE_VX_U4_2024": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2024-09-18.csv",
        "CFE_VX_V4_2024": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2024-10-16.csv",
        "CFE_VX_X4_2024": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2024-11-20.csv",
        "CFE_VX_Z4_2024": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2024-12-18.csv",
        "CFE_VX_F5_2025": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2025-01-22.csv",
        "CFE_VX_G5_2025": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2025-02-19.csv",
        "CFE_VX_H5_2025": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2025-03-18.csv",
        "CFE_VX_J5_2025": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2025-04-16.csv",
        "CFE_VX_K5_2025": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2025-05-21.csv",
        "CFE_VX_M5_2025": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2025-06-18.csv",
        "CFE_VX_N5_2025": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2025-07-16.csv",
        "CFE_VX_Q5_2025": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2025-08-20.csv",
        "CFE_VX_U5_2025": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2025-09-17.csv",
        "CFE_VX_V5_2025": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2025-10-22.csv",
        "CFE_VX_X5_2025": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2025-11-19.csv",
        "CFE_VX_Z5_2025": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2025-12-17.csv",
        "CFE_VX_F6_2026": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2026-01-21.csv",
        "CFE_VX_G6_2026": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2026-02-18.csv",
        "CFE_VX_H6_2026": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2026-03-18.csv",
        "CFE_VX_J6_2026": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2026-04-15.csv",
        "CFE_VX_K6_2026": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2026-05-19.csv",
        "CFE_VX_M6_2026": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2026-06-17.csv",
        "CFE_VX_N6_2026": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2026-07-22.csv",
        "CFE_VX_Q6_2026": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2026-08-19.csv",
        "CFE_VX_U6_2026": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2026-09-16.csv",
        "CFE_VX_V6_2026": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2026-10-21.csv",
        "CFE_VX_X6_2026": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2026-11-18.csv",
        "CFE_VX_Z6_2026": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2026-12-16.csv",
    }
    root_dir = Path(__file__).resolve().parent
    data_dir = root_dir / "data"
    index_data_dir = data_dir / "indices"

    download_cboe_vx_csvs(cboe_vx_futures_hlocv_data, data_dir=data_dir)
    index_paths = download_cboe_index_csvs(CBOE_INDEX_HISTORY_URLS, data_dir=index_data_dir)
    df_vixeq = load_cboe_index_history_csv(index_paths["VIXEQ"], "VIXEQ")
    df_vix = load_cboe_index_history_csv(index_paths["VIX"], "VIX")
    df_vxn = load_cboe_index_history_csv(index_paths["VXN"], "VXN")
    df_vxn_features = add_vxn_ma20_signal(df_vxn)
    df_vxsmh = load_cboe_index_history_csv(index_paths["VXSMH"], "VXSMH")
    df_vxsmh_features = add_vxsmh_percentile_signal(df_vxsmh)
    df_vixeq_vix_spread_features = add_vixeq_vix_spread_signal(df_vixeq, df_vix)

    # ===== Create Dataframe, for each trading day, the VXCurrent and its HLOCV, plus features =====
    print_tail_num_rows = 30
    all_data = load_vx_csvs(data_dir)
    df_vxcurrent_hlocv = rows_for_vxcurrent_map(m, all_data, strict=False)

    # ===== VXNext (second nearest month) HLOCV =====
    m_next = vxnext_map(start_date, end_date, holidays, business_days_only=True)
    df_vxnext_hlocv = rows_for_vxcurrent_map(m_next, all_data, strict=False)

    next_cols_rename = {
        "Futures": "next_Futures",
        "Open": "next_Open",
        "High": "next_High",
        "Low": "next_Low",
        "Close": "next_Close",
        "Settle": "next_Settle",
        "Change": "next_Change",
        "Total Volume": "next_Total Volume",
        "EFP": "next_EFP",
        "Open Interest": "next_Open Interest",
    }
    df_vxnext_hlocv = df_vxnext_hlocv[["Trade Date"] + list(next_cols_rename.keys())]
    df_vxnext_hlocv = df_vxnext_hlocv.rename(columns=next_cols_rename)

    df_vxcurrent_vxnext_hlocv = df_vxcurrent_hlocv.merge(df_vxnext_hlocv, on="Trade Date", how="left")
    df_vxcurrent_vxnext_hlocv = df_vxcurrent_vxnext_hlocv.merge(df_vixeq_vix_spread_features, on="Trade Date", how="left")
    df_vxcurrent_vxnext_hlocv = df_vxcurrent_vxnext_hlocv.merge(df_vxn_features, on="Trade Date", how="left")
    df_vxcurrent_vxnext_hlocv = df_vxcurrent_vxnext_hlocv.merge(df_vxsmh_features, on="Trade Date", how="left")

    df_vxcurrent_vxnext_hlocv["front_next_OI"] = pd.to_numeric(df_vxcurrent_vxnext_hlocv["Open Interest"], errors="coerce").fillna(0) + pd.to_numeric(
        df_vxcurrent_vxnext_hlocv["next_Open Interest"], errors="coerce"
    ).fillna(0)
    df_vxcurrent_vxnext_hlocv = df_vxcurrent_vxnext_hlocv.sort_values("Trade Date").reset_index(drop=True)
    df_vxcurrent_vxnext_hlocv["front_next_OI_delta"] = df_vxcurrent_vxnext_hlocv["front_next_OI"].diff()
    # NaN on contract roll dates — the delta is meaningless when front/next contracts change
    roll_mask = df_vxcurrent_vxnext_hlocv["Futures"] != df_vxcurrent_vxnext_hlocv["Futures"].shift(1)
    df_vxcurrent_vxnext_hlocv.loc[roll_mask, "front_next_OI_delta"] = np.nan

    print("/df_vxcurrent_vxnext_hlocv:")
    print(df_vxcurrent_vxnext_hlocv.tail(print_tail_num_rows))
    # df_vxcurrent_vxnext_hlocv.to_csv("df_vxcurrent_vxnext_hlocv.csv")

    ### Print selected date range for df_vxcurrent_vxnext_hlocv
    # 2025-11-03 - 11-04, 2025 fed rate not reduce drawdown
    # print_date_range(df_vxcurrent_vxnext_hlocv, "2024-06-01", "2024-09-01", label="df_vxcurrent_vxnext_hlocv") # 2024-07-18, 2024 jpy carry trade drawdown
    # print_date_range(df_vxcurrent_vxnext_hlocv, "2025-01-01", "2025-04-30", label="df_vxcurrent_vxnext_hlocv") # 2025-02-21, 2025 libration day drawdown
    # print_date_range(df_vxcurrent_vxnext_hlocv, "2026-02-01", "2026-04-01", label="df_vxcurrent_vxnext_hlocv") # 2026-03-02, 2026 US Iran war drawdown

    # ===== Derive features to predict draw down =====
    df_vxcurrent_vxnext_hlocv_features = add_volume_metrics_rows_incl_today_strict(
        df_vxcurrent_vxnext_hlocv,
        lookback_rows=252,  # one year has 252 trading days
        ma_window=50,
    )

    # ===== df_vxcurrent_vxnext_hlocv_features =====
    print(f"/df_vxcurrent_vxnext_hlocv_features: {len(df_vxcurrent_vxnext_hlocv_features.columns)} columns")
    print("/df_vxcurrent_vxnext_hlocv_features this month:")
    # fmt: off
    # yapf: disable
    selected_col = [
        "Trade Date", "Futures",
        "close_gt_ma20", "ma20_rising", "close_gt_ma50",  # VX Price signal
        "volume_pct", "vol_ge_1.85x_ma50", "vol_ge_90pct", "vol_ge_90pct_last_5days", "vol_ge_90pct_last_10days",  # VX Volume signal
        "risk_off_score", "risk_off_level",  # VX current+next risk_off_score and risk_off_level
        "vixeq", "vix", "vixeq_vix_spread", "spread_pct",  # VIXEQ-VIX dispersion signal
        # "rho", "rho_armed",  # rho proxy disabled for now
        "vixeq_risk_off_level",  # VIXEQ risk_off_level
        "vxn", "vxn_gt_ma20", "vxn_ma20_rising", "vxn_risk_off_level",  # Nasdaq-100 volatility signal
        "vxsmh", "vxsmh_pct", "vxsmh_risk_off_level",  # Semiconductor ETF volatility signal
        # "front_next_OI_delta",
    ]
    # yapf: enable
    # fmt: on

    print_col_aliases = {
        "Trade Date": "Date",
        "Futures": "VX1",
        "close_gt_ma20": "VX1>MA20",
        "ma20_rising": "VX1_MA20+",
        "close_gt_ma50": "VX1>MA50",
        "volume_pct": "VX1_VolPct",
        "vol_ge_1.85x_ma50": "Vol1.85",
        "vol_ge_90pct": "VolP90",
        "vol_ge_90pct_last_5days": "P90_l5d",
        "vol_ge_90pct_last_10days": "P90_l10d",
        "risk_off_score": "Score",
        "risk_off_level": "VX1_Lvl",
        "vixeq": "VIXEQ",
        "vix": "VIX",
        "vixeq_vix_spread": "EQ-VIX",
        "spread_pct": "SprdPct",
        "vixeq_risk_off_level": "EQ_Lvl",
        "vxn": "VXN",
        "vxn_gt_ma20": "VXN>MA20",
        "vxn_ma20_rising": "VXN_MA20+",
        "vxn_risk_off_level": "VXN_Lvl",
        "vxsmh": "VXSMH",
        "vxsmh_pct": "VXSMH_Pct",
        "vxsmh_risk_off_level": "VXSMH_Lvl",
    }
    print_bool_cols = ["close_gt_ma20", "ma20_rising", "close_gt_ma50", "vol_ge_1.85x_ma50", "vol_ge_90pct", "vxn_gt_ma20", "vxn_ma20_rising"]
    feature_display = df_vxcurrent_vxnext_hlocv_features[selected_col].tail(print_tail_num_rows).copy().rename(columns=print_col_aliases)
    for source_col in print_bool_cols:
        display_col = print_col_aliases[source_col]
        feature_display[display_col] = feature_display[display_col].map(lambda value: "?" if pd.isna(value) else ("Y" if bool(value) else "-"))
    for source_col in ["vol_ge_90pct_last_5days", "vol_ge_90pct_last_10days"]:
        display_col = print_col_aliases[source_col]
        feature_display[display_col] = pd.to_numeric(feature_display[display_col], errors="coerce").round().astype("Int64")
    feature_display["VX1_VolPct"] = pd.to_numeric(feature_display["VX1_VolPct"], errors="coerce").round(1)

    print("# Y=True, empty=False, ?=missing")
    print(feature_display.to_string(index=False))
    df_vxcurrent_vxnext_hlocv_features[selected_col].tail(100).to_csv(f"vix_sell_signal/{end_date}_vxcurrent_hlocv_features.csv")

    print(
        '原理:\n'
        '    L1: 机构对冲指数下行 -> 买 SPX put -> 推高隐波 -> VIX 被"算"高。\n'
        '        SPX 全链 ~64% 是 0DTE 日内噪音, 必须按"保护区桶"过滤:\n'
        '        DTE 21-90 天 + 虚值 3%-20% 的 put(教科书式崩盘保护的栖息地)。\n'
        '    L2: 机构买 VIX call -> dealer 卖 call -> 为 delta neutral 买同结算日 VX 期货。\n'
        '        对冲量 = sum(量 x delta / 10)(期权$100/期货$1000)。'
    )
    # print("/VIXEQ-VIX spread signal latest:")
    # print(
    #     df_vxcurrent_vxnext_hlocv_features[
    #         [
    #             "Trade Date",
    #             "vixeq",
    #             "vix",
    #             "vixeq_vix_spread",
    #             "spread_pct",
    #             # "rho",
    #             # "rho_armed",
    #             "vixeq_risk_off_level",
    #         ]
    #     ]
    #     .dropna(subset=["vixeq_vix_spread"])
    #     .tail(10)
    # )

    ### Print selected date range for df_vxcurrent_vxnext_hlocv_features
    # 2025-11-03 - 11-04, 2025 fed rate not reduce drawdown
    # print_date_range(df_vxcurrent_vxnext_hlocv_features, "2024-06-01", "2024-09-01", cols=selected_col, label="vxcurrent_features") # 2024-07-18, 2024 jpy carry trade drawdown
    # print_date_range(df_vxcurrent_vxnext_hlocv_features, "2025-01-01", "2025-04-30", cols=selected_col, label="vxcurrent_features") # 2025-02-21, 2025 libration day drawdown
    # print_date_range(df_vxcurrent_vxnext_hlocv_features, "2026-02-01", "2026-04-01", cols=selected_col, label="vxcurrent_features") # 2026-03-02, 2026 US Iran war drawdown

    print(f"Today is {end_date}")
    # print("# risk_off_score (0-7): VX current+next price/volume signals")
    # print("# risk_off_level: categorical - GREEN (0-1), YELLOW (2-3), ORANGE (4-5), RED (>=6)")
    # print(f"# vixeq_risk_off_level: RED when spread_pct > {VIXEQ_VIX_SPREAD_ARMED_PERCENTILE:.0f}, else GREEN")
    # print("# vxn_gt_ma20: VXN close is above its 20-trading-day moving average")
    # print("# vxn_ma20_rising: VXN MA20 is higher than on the prior trading day")
    # print("# vxn_risk_off_level: RED when vxn_gt_ma20 and vxn_ma20_rising are both True, else GREEN")
    # print(f"# vxsmh_pct: VXSMH current-inclusive percentile over up to the trailing {VXSMH_PERCENTILE_LOOKBACK} trading days")
    # print(f"# vxsmh_risk_off_level: RED when vxsmh_pct > {VXSMH_RISK_OFF_PERCENTILE:.0f}, else GREEN")
    # print(f"# rho_armed: (VIX/VIXEQ)^2 < {VIXEQ_RHO_ARMED_THRESHOLD:.2f}")

    # ===== VIX call / SPX put options flow snapshots =====
    run_options_flow_snapshots(end_date, holidays, data_dir)


# 示例
if __name__ == "__main__":
    end_date = dt.date.today()
    report_dir = Path(__file__).resolve().parent / "vix_sell_signal"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{end_date}_vx_eod_report.txt"

    class _Tee:
        def __init__(self, *files):
            self._files = files

        def write(self, data: str) -> None:
            for file in self._files:
                file.write(data)

        def flush(self) -> None:
            for file in self._files:
                file.flush()

    with report_path.open("w", encoding="utf-8") as report_file, contextlib.redirect_stdout(_Tee(sys.stdout, report_file)):
        run_vx_eod_report(end_date)
