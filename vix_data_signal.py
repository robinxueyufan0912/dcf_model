import contextlib
import datetime as dt
import json
import re
import ssl
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from market_time import NEW_YORK_TZ, los_angeles_today, to_los_angeles_time
from vix_options_flow import (
    add_flow_features,
    fetch_spx_options_chain,
    flow_table_to_string,
    spx_daily_implied_moves,
    spx_daily_snapshot,
    spx_implied_move_table_to_string,
    vix_daily_snapshot,
)

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
CBOE_INDEX_LATEST_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_{symbol}.json"
VIXEQ_VIX_SPREAD_ARMED_PERCENTILE = 85.0
VIXEQ_VIX_SPREAD_MIN_ROWS = 252
VXSMH_PERCENTILE_LOOKBACK = 252
VXSMH_RISK_OFF_PERCENTILE = 85.0
# VIXEQ_RHO_ARMED_THRESHOLD = 0.15

# cdn.cboe.com 在 Cloudflare 后面：不带 UA 或短时间大量请求会被临时封 IP(403)。
# 所以下载统一带浏览器 UA、加请求间隔，且已过期合约的不可变 CSV 不重复下载。
_MONTH_CODE_REVERSE = {code: month for month, code in MONTH_CODE.items()}
_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "*/*",
}
DOWNLOAD_REQUEST_DELAY = 1.0  # seconds between cboe requests


def _make_ssl_context(verify_ssl: bool = True, cafile: str | None = None) -> ssl.SSLContext:
    if not verify_ssl:
        return ssl._create_unverified_context()
    if cafile is None:
        try:
            import certifi  # type: ignore
        except ImportError:
            certifi = None
        if certifi is not None:
            cafile = certifi.where()
    return ssl.create_default_context(cafile=cafile)


def _url_download(url: str, context: ssl.SSLContext, *, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=_REQUEST_HEADERS)
    with urllib.request.urlopen(req, context=context, timeout=timeout) as resp:
        return resp.read()


def vx_contract_month_from_name(name: str) -> tuple[int, int] | None:
    """Parse 'CFE_VX_N6_2026' -> (2026, 7). None if the name does not match."""
    m = re.fullmatch(r"CFE_VX_([FGHJKMNQUVXZ])\d_(\d{4})", name)
    if not m:
        return None
    return int(m.group(2)), _MONTH_CODE_REVERSE[m.group(1)]


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


def vx1_contract_month_for_date(d: dt.date, schedule: list[dict]) -> str:
    """
    给定日期 d，返回该日期对应的 VX1 合约月份（YYYY-MM）。
    逻辑：找 fsd >= d 的最小那一个合约月份。
    """
    for item in schedule:
        if item["fsd"] >= d:
            return item["contract_month"]
    raise ValueError(f"No VX1 found for date={d}; schedule range too small.")


def vx2_contract_month_for_date(d: dt.date, schedule: list[dict]) -> str:
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
    raise ValueError(f"No VX2 found for date={d}; schedule range too small.")


def is_business_day(d: dt.date, holidays: set[dt.date]) -> bool:
    return (d.weekday() < 5) and (d not in holidays)


def vx1_map(start_date: dt.date, end_date: dt.date, holidays: set[dt.date], business_days_only: bool = True) -> dict[dt.date, str]:
    """
    返回 dict: {date -> VX1 contract_month}
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
            out[d] = vx1_contract_month_for_date(d, schedule)
        d += dt.timedelta(days=1)

    return out


def vx2_map(start_date: dt.date, end_date: dt.date, holidays: set[dt.date], business_days_only: bool = True) -> dict[dt.date, str]:
    """
    返回 dict: {date -> VX2 contract_month} (第二近月合约)
    """
    if end_date < start_date:
        raise ValueError("end_date must be >= start_date")

    schedule = build_vx_monthly_schedule(start_date, end_date, holidays)

    out = {}
    d = start_date
    while d <= end_date:
        if (not business_days_only) or is_business_day(d, holidays):
            out[d] = vx2_contract_month_for_date(d, schedule)
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
    csv_files = sorted(data_dir.glob("CFE_VX_*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No CSV files found under {data_dir}")
    dfs = [pd.read_csv(p) for p in csv_files]
    return pd.concat(dfs, ignore_index=True)


def download_cboe_vx_csvs(
    cboe_vx_futures_hlocv_data: dict[str, str], data_dir: str | Path | None = None, *, verify_ssl: bool = True, cafile: str | None = None
) -> list[Path]:
    """Download CSVs to data_dir using dict keys as filenames.

    Skips contracts that already settled (their CSV is immutable) and files
    already refreshed today. On download failure, keeps the existing local
    file so the report can still run on cached data.
    """
    if data_dir is None:
        data_dir = Path(__file__).resolve().parent / "data"
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    context = _make_ssl_context(verify_ssl, cafile)

    today = los_angeles_today()
    saved_paths = []
    for name, url in cboe_vx_futures_hlocv_data.items():
        out_path = data_dir / f"{name}.csv"
        contract_ym = vx_contract_month_from_name(name)
        settled = contract_ym is not None and contract_ym < (today.year, today.month)
        fresh_today = out_path.exists() and dt.date.fromtimestamp(out_path.stat().st_mtime) >= today
        if out_path.exists() and (settled or fresh_today):
            continue
        try:
            payload = _url_download(url, context)
        except Exception as exc:
            if out_path.exists():
                print(f"[vx csv] {name} download failed ({exc}); keeping existing file", file=sys.stderr)
                continue
            raise
        out_path.write_bytes(payload)
        saved_paths.append(out_path)
        time.sleep(DOWNLOAD_REQUEST_DELAY)
    return saved_paths


def download_cboe_index_csvs(
    index_history_urls: dict[str, str], data_dir: str | Path | None = None, *, verify_ssl: bool = True, cafile: str | None = None
) -> dict[str, Path]:
    """Download Cboe index history CSVs to data_dir using index tickers as filenames.

    Index histories are mutable (updated daily), so they are always refreshed;
    on download failure the existing local file is kept.
    """
    if data_dir is None:
        data_dir = Path(__file__).resolve().parent / "data" / "indices"
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    context = _make_ssl_context(verify_ssl, cafile)

    saved_paths = {}
    for name, url in index_history_urls.items():
        out_path = data_dir / f"{name}_History.csv"
        try:
            payload = _url_download(url, context)
        except Exception as exc:
            if out_path.exists():
                print(f"[index csv] {name} download failed ({exc}); keeping existing file", file=sys.stderr)
                saved_paths[name] = out_path
                continue
            raise
        out_path.write_bytes(payload)
        saved_paths[name] = out_path
        time.sleep(DOWNLOAD_REQUEST_DELAY)
    return saved_paths


def download_cboe_index_latest_quotes(index_names: list[str], *, verify_ssl: bool = True, cafile: str | None = None) -> dict[str, dict[str, object]]:
    """Download current Cboe index quotes without making the report depend on them."""
    context = _make_ssl_context(verify_ssl, cafile)

    quotes: dict[str, dict[str, object]] = {}
    for index_name in index_names:
        symbol = index_name.upper()
        url = CBOE_INDEX_LATEST_QUOTE_URL.format(symbol=symbol)
        try:
            req = urllib.request.Request(url, headers=_REQUEST_HEADERS)
            with urllib.request.urlopen(req, context=context, timeout=15) as resp:
                payload = json.load(resp)
            if isinstance(payload, dict):
                quotes[symbol] = payload
        except (OSError, ValueError) as exc:
            print(f"[index quote] {symbol} unavailable: {exc}", file=sys.stderr)
        time.sleep(DOWNLOAD_REQUEST_DELAY)
    return quotes


def append_cboe_latest_index_quote(
    history: pd.DataFrame, index_name: str, quote: dict[str, object] | None, *, max_date: dt.date | None = None
) -> pd.DataFrame:
    """Append a newer quote date only when the official daily history has not published it."""
    if not quote:
        return history

    value_col = index_name.lower()
    if value_col not in history.columns:
        raise ValueError(f"history is missing {value_col} column")

    data = quote.get("data")
    if not isinstance(data, dict):
        return history

    # Prefer the quote's actual market time. Cboe's top-level timestamp is UTC
    # without an offset and can already be on the next date when Los Angeles is
    # still on the current trading date.
    quote_ts = to_los_angeles_time(data.get("last_trade_time"), naive_timezone=NEW_YORK_TZ)
    if quote_ts is None:
        quote_ts = to_los_angeles_time(quote.get("timestamp"))
    quote_value = pd.to_numeric(data.get("close", data.get("current_price")), errors="coerce")
    if quote_ts is None or pd.isna(quote_value):
        return history

    quote_date = quote_ts.date()
    if max_date is not None and quote_date > max_date:
        return history

    out = history.copy()
    history_dates = pd.to_datetime(out["Trade Date"], errors="coerce")
    if history_dates.notna().any() and quote_date <= history_dates.max().date():
        return out

    latest = pd.DataFrame({"Trade Date": [quote_date.isoformat()], value_col: [float(quote_value)]})
    return pd.concat([out, latest], ignore_index=True).sort_values("Trade Date").reset_index(drop=True)


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


def rows_for_vx_map(m: dict[dt.date, str], all_data: pd.DataFrame, *, strict: bool = True) -> pd.DataFrame:
    """Return rows from all_data matching each trade date and its VX futures label."""
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
    out["Close"] = pd.to_numeric(out["Close"], errors="coerce")
    out[volume_col] = pd.to_numeric(out[volume_col], errors="coerce")
    out = out.sort_values(date_col).reset_index(drop=True)

    # Index histories can publish one day before VX futures CSVs. Calculate VX
    # rolling features only on rows that actually have VX data, then join them
    # back so an index-only placeholder does not consume a rolling-window row.
    vx = out.loc[out["Close"].notna() & out[volume_col].notna()].copy()
    vx["Close_MA20"] = vx["Close"].rolling(20, min_periods=20).mean()
    vx["Close_MA50"] = vx["Close"].rolling(50, min_periods=50).mean()

    # Price signals
    vx["close_gt_ma50"] = vx["Close"] > vx["Close_MA50"]
    vx["ma20_rising"] = vx["Close_MA20"].diff() > 0
    vx["close_gt_ma20"] = vx["Close"] > vx["Close_MA20"]

    # Volume signals
    vx["Volume_MA50"] = vx[volume_col].rolling(ma_window, min_periods=ma_window).mean()
    vx["Volume/MA50"] = vx[volume_col] / vx["Volume_MA50"]

    def pct_rank_in_window(arr: np.ndarray) -> float:
        arr = arr.astype(float)
        arr = arr[~np.isnan(arr)]
        if len(arr) < lookback_rows:
            return np.nan
        v = arr[-1]  # 当日值（窗口最后一个）
        return float(np.mean(arr <= v) * 100.0)

    vx["volume_pct"] = vx[volume_col].rolling(window=lookback_rows, min_periods=lookback_rows).apply(pct_rank_in_window, raw=True)

    vx["vol_ge_1.85x_ma50"] = vx["Volume/MA50"] >= 1.85
    vx["vol_ge_90pct"] = vx["volume_pct"] >= 90

    vx["vol_ge_90pct_last_5days"] = vx["volume_pct"].rolling(window=5, min_periods=5).apply(lambda arr: float(np.sum(arr >= 90.0)), raw=True)
    vx["vol_ge_90pct_last_10days"] = vx["volume_pct"].rolling(window=10, min_periods=10).apply(lambda arr: float(np.sum(arr >= 90.0)), raw=True)

    # risk_off_score currently uses VX1 price/volume conditions only.
    risk_off_components = [
        vx["close_gt_ma50"],
        vx["close_gt_ma20"],
        vx["ma20_rising"],
        vx["vol_ge_1.85x_ma50"],
        vx["vol_ge_90pct"],
        vx["vol_ge_90pct_last_5days"] >= 2,
        vx["vol_ge_90pct_last_10days"] >= 3,
    ]

    score = pd.Series(0, index=vx.index, dtype="int64")
    for component in risk_off_components:
        score = score + component.fillna(False).astype(bool).astype(int)
    vx["risk_off_score"] = score
    vx["risk_off_level"] = pd.cut(vx["risk_off_score"], bins=[-1, 1, 3, 5, np.inf], labels=["GREEN", "YELLOW", "ORANGE", "RED"])

    feature_cols = [
        "Close_MA20",
        "Close_MA50",
        "close_gt_ma50",
        "ma20_rising",
        "close_gt_ma20",
        "Volume_MA50",
        "Volume/MA50",
        "volume_pct",
        "vol_ge_1.85x_ma50",
        "vol_ge_90pct",
        "vol_ge_90pct_last_5days",
        "vol_ge_90pct_last_10days",
        "risk_off_score",
        "risk_off_level",
    ]
    out = out.join(vx[feature_cols])

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


def run_options_flow_snapshots(end_date: dt.date, holidays: set[dt.date], data_dir: str | Path) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """Save and print the daily VIX-call and SPX-put flow features.

    Options do not trade on weekends/holidays. Cboe's delayed-quote JSON still
    returns the last session's chain then, so a weekend cron run would save a
    fake row dated on the weekend (and with DTE buckets shifted by the weekend
    date). On non-trading days: no fetch and no new history row, but still
    print the tables from the saved history.
    """
    data_dir = Path(data_dir)
    vix_history_path = data_dir / "vix_call_flow_history.csv"
    spx_history_path = data_dir / "spx_put_flow_history.csv"
    spx_implied_moves = None

    if is_business_day(end_date, holidays):
        option_schedule = build_vx_monthly_schedule(end_date, end_date + dt.timedelta(days=365), holidays)
        vx_settlement_dates = [item["fsd"] for item in option_schedule if item["fsd"] >= end_date]
        vix_hist = vix_daily_snapshot(vix_history_path, vx_settlement_dates)
        spx_fetched = fetch_spx_options_chain()
        spx_chain, _spx_ts, spx_meta = spx_fetched
        spx_spot = float(spx_meta.get("close") or spx_meta.get("current_price"))
        spx_implied_moves = spx_daily_implied_moves(spx_chain, spx_spot, end_date)
        spx_hist = spx_daily_snapshot(spx_history_path, trade_date=end_date.isoformat(), fetched=spx_fetched)
        save = True
    else:
        print(f"[options flow] {end_date} is not a trading day; showing last saved snapshots")
        vix_hist = pd.read_csv(vix_history_path, dtype={"Trade Date": str}) if vix_history_path.exists() else pd.DataFrame()
        spx_hist = pd.read_csv(spx_history_path, dtype={"Trade Date": str}) if spx_history_path.exists() else pd.DataFrame()
        save = False

    if vix_hist.empty or spx_hist.empty:
        print("[options flow] no flow history yet")
        return None

    vix_flow_features = add_flow_features(vix_hist)
    spx_flow_features = add_flow_features(spx_hist)
    if save:
        vix_flow_features.to_csv(vix_history_path, index=False)
        spx_flow_features.to_csv(spx_history_path, index=False)

    print("/VIX call options flow latest:")
    print(flow_table_to_string(vix_flow_features))
    print("/SPX put protection flow latest:")
    print(flow_table_to_string(spx_flow_features))
    print("/SPX daily implied move next 5 expiries:")
    if spx_implied_moves is None:
        print("(unavailable: no fresh SPX chain on a non-trading day)")
    else:
        print(spx_implied_move_table_to_string(spx_implied_moves))
        print("# DayMove = 相邻到期 ATM straddle/spot 的方差差分; 是预期绝对 move 代理, 不是 1-sigma.")
        print("# GapD > 1 包含周末/假日风险.")
        print("# DayMove 空白表示累计 straddle 方差不单调; 保留为空而不强制归零.")
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

    # ===== find the VX1 contract month for each trading day [start_date, end_date] =====
    start_date = dt.date(2021, 1, 1)

    vx1_by_date = vx1_map(start_date, end_date, holidays, business_days_only=True)

    # print("/trade_date to VX1 contract month:")
    # # 打印看看
    # for k in sorted(vx1_by_date.keys())[-10:]:
    #     print(k, "->", vx1_by_date[k])

    # ===== download the latest VX futures HLOCV data =====
    cboe_vx_futures_hlocv_data = {
        "CFE_VX_V1_2021": "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2021-10-20.csv",
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
    latest_index_quotes = download_cboe_index_latest_quotes(list(CBOE_INDEX_HISTORY_URLS))
    df_vixeq = load_cboe_index_history_csv(index_paths["VIXEQ"], "VIXEQ")
    df_vix = load_cboe_index_history_csv(index_paths["VIX"], "VIX")
    df_vxn = load_cboe_index_history_csv(index_paths["VXN"], "VXN")
    df_vixeq = append_cboe_latest_index_quote(df_vixeq, "VIXEQ", latest_index_quotes.get("VIXEQ"), max_date=end_date)
    df_vix = append_cboe_latest_index_quote(df_vix, "VIX", latest_index_quotes.get("VIX"), max_date=end_date)
    df_vxn = append_cboe_latest_index_quote(df_vxn, "VXN", latest_index_quotes.get("VXN"), max_date=end_date)
    df_vxn_features = add_vxn_ma20_signal(df_vxn)
    df_vxsmh = load_cboe_index_history_csv(index_paths["VXSMH"], "VXSMH")
    df_vxsmh = append_cboe_latest_index_quote(df_vxsmh, "VXSMH", latest_index_quotes.get("VXSMH"), max_date=end_date)
    df_vxsmh_features = add_vxsmh_percentile_signal(df_vxsmh)
    df_vixeq_vix_spread_features = add_vixeq_vix_spread_signal(df_vixeq, df_vix)

    # ===== Create DataFrame with VX1 and VX2 HLOCV for each trading day =====
    print_tail_num_rows = 30
    all_data = load_vx_csvs(data_dir)
    df_vx1_hlocv = rows_for_vx_map(vx1_by_date, all_data, strict=False)

    # ===== VX2 (second nearest month) HLOCV =====
    vx2_by_date = vx2_map(start_date, end_date, holidays, business_days_only=True)
    df_vx2_hlocv = rows_for_vx_map(vx2_by_date, all_data, strict=False)

    vx2_cols_rename = {
        "Futures": "vx2_Futures",
        "Open": "vx2_Open",
        "High": "vx2_High",
        "Low": "vx2_Low",
        "Close": "vx2_Close",
        "Settle": "vx2_Settle",
        "Change": "vx2_Change",
        "Total Volume": "vx2_Total Volume",
        "EFP": "vx2_EFP",
        "Open Interest": "vx2_Open Interest",
    }
    df_vx2_hlocv = df_vx2_hlocv[["Trade Date"] + list(vx2_cols_rename.keys())]
    df_vx2_hlocv = df_vx2_hlocv.rename(columns=vx2_cols_rename)

    # Use the union of VX and index dates. Cboe index histories can publish the
    # latest session before the VX futures CSVs; that session should still be
    # shown with index signals while VX1/VX2 remain empty until the next refresh.
    trade_dates = pd.concat(
        [
            df_vx1_hlocv["Trade Date"],
            df_vx2_hlocv["Trade Date"],
            df_vixeq_vix_spread_features["Trade Date"],
            df_vxn_features["Trade Date"],
            df_vxsmh_features["Trade Date"],
        ],
        ignore_index=True,
    )
    trade_dates = pd.to_datetime(trade_dates, errors="coerce").dropna().drop_duplicates().sort_values()
    trade_dates = trade_dates[(trade_dates.dt.date >= start_date) & (trade_dates.dt.date <= end_date)]
    report_dates = pd.DataFrame({"Trade Date": trade_dates.dt.strftime("%Y-%m-%d")})

    df_vx1_vx2_hlocv = report_dates.merge(df_vx1_hlocv, on="Trade Date", how="left", validate="one_to_one")
    df_vx1_vx2_hlocv = df_vx1_vx2_hlocv.merge(df_vx2_hlocv, on="Trade Date", how="left", validate="one_to_one")
    df_vx1_vx2_hlocv = df_vx1_vx2_hlocv.merge(df_vixeq_vix_spread_features, on="Trade Date", how="left", validate="one_to_one")
    df_vx1_vx2_hlocv = df_vx1_vx2_hlocv.merge(df_vxn_features, on="Trade Date", how="left", validate="one_to_one")
    df_vx1_vx2_hlocv = df_vx1_vx2_hlocv.merge(df_vxsmh_features, on="Trade Date", how="left", validate="one_to_one")

    df_vx1_vx2_hlocv["vx1_vx2_OI"] = pd.to_numeric(df_vx1_vx2_hlocv["Open Interest"], errors="coerce") + pd.to_numeric(
        df_vx1_vx2_hlocv["vx2_Open Interest"], errors="coerce"
    )
    df_vx1_vx2_hlocv = df_vx1_vx2_hlocv.sort_values("Trade Date").reset_index(drop=True)
    df_vx1_vx2_hlocv["vx1_vx2_OI_delta"] = df_vx1_vx2_hlocv["vx1_vx2_OI"].diff()
    # NaN on contract roll dates — the delta is meaningless when VX1/VX2 contracts change
    roll_mask = df_vx1_vx2_hlocv["Futures"] != df_vx1_vx2_hlocv["Futures"].shift(1)
    df_vx1_vx2_hlocv.loc[roll_mask, "vx1_vx2_OI_delta"] = np.nan

    # print("/df_vx1_vx2_hlocv:")
    # print(df_vx1_vx2_hlocv.tail(print_tail_num_rows))
    # df_vx1_vx2_hlocv.to_csv("df_vx1_vx2_hlocv.csv")

    ### Print selected date range for df_vx1_vx2_hlocv
    # 2025-11-03 - 11-04, 2025 fed rate not reduce drawdown
    # print_date_range(df_vx1_vx2_hlocv, "2024-06-01", "2024-09-01", label="df_vx1_vx2_hlocv") # 2024-07-18, 2024 jpy carry trade drawdown
    # print_date_range(df_vx1_vx2_hlocv, "2025-01-01", "2025-04-30", label="df_vx1_vx2_hlocv") # 2025-02-21, 2025 libration day drawdown
    # print_date_range(df_vx1_vx2_hlocv, "2026-02-01", "2026-04-01", label="df_vx1_vx2_hlocv") # 2026-03-02, 2026 US Iran war drawdown

    # ===== Derive features to predict draw down =====
    df_vx1_vx2 = add_volume_metrics_rows_incl_today_strict(
        df_vx1_vx2_hlocv,
        lookback_rows=252,  # one year has 252 trading days
        ma_window=50,
    )

    # ===== df_vx1_vx2 =====
    print(f"/df_vx1_vx2: {len(df_vx1_vx2.columns)} columns")
    print("/df_vx1_vx2 this month:")
    # fmt: off
    # yapf: disable
    selected_col = [
        "Trade Date", "Futures",
        "close_gt_ma20", "ma20_rising", "close_gt_ma50",  # VX Price signal
        "volume_pct", "vol_ge_1.85x_ma50", "vol_ge_90pct", "vol_ge_90pct_last_5days", "vol_ge_90pct_last_10days",  # VX Volume signal
        "risk_off_score", "risk_off_level",  # VX1 risk_off_score and risk_off_level
        "vixeq", "vix", "vixeq_vix_spread", "spread_pct",  # VIXEQ-VIX dispersion signal
        # "rho", "rho_armed",  # rho proxy disabled for now
        "vixeq_risk_off_level",  # VIXEQ risk_off_level
        "vxn", "vxn_gt_ma20", "vxn_ma20_rising", "vxn_risk_off_level",  # Nasdaq-100 volatility signal
        "vxsmh", "vxsmh_pct", "vxsmh_risk_off_level",  # Semiconductor ETF volatility signal
        # "vx1_vx2_OI_delta",
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
    feature_display = df_vx1_vx2[selected_col].tail(print_tail_num_rows).copy().rename(columns=print_col_aliases)
    for source_col in print_bool_cols:
        display_col = print_col_aliases[source_col]
        feature_display[display_col] = feature_display[display_col].map(lambda value: "" if pd.isna(value) else ("Y" if bool(value) else "-"))
    for source_col in ["vol_ge_90pct_last_5days", "vol_ge_90pct_last_10days"]:
        display_col = print_col_aliases[source_col]
        count_values = pd.to_numeric(feature_display[display_col], errors="coerce").round()
        feature_display[display_col] = count_values.map(lambda value: "" if pd.isna(value) else str(int(value)))
    feature_display["VX1_VolPct"] = pd.to_numeric(feature_display["VX1_VolPct"], errors="coerce").round(1)

    print("# Y=True, -=False, empty=missing")
    print(feature_display.to_string(index=False, na_rep=""))
    df_vx1_vx2[selected_col].tail(100).to_csv(f"vix_sell_signal/{end_date}_vx1_hlocv_features.csv")

    print(
        "Risk-off原理:\n"
        "    L0: (VX1)      机构直接对冲买入(养老金/宏观直接买期货)、快速建仓平仓VX1, 战术性、流动性优先的对冲. \n"
        '    L1: (SPX Put)  机构对冲指数下行 -> 买 SPX put -> 推高隐波 -> VIX 被"算"高。\n'
        "        spx put tac: 1-22  DTE 战术桶 -> 未来1天至3周的事件性对冲(周末风险/数据周/战事), 衰减快; 自带彩票churn噪声, 解读时优先看OI变化. \n"
        "        spx put vix: 23-37 DTE VIX窗 -> 官方VIX计算唯一使用的期限段(近月>23天、次月<37天, 插值出恒定30天波动率), 只有这一桶直接进入VIX公式. \n"
        "    L2: (VIX call) VIX call 拉高VX volume原理: 机构买 VIX call -> dealer 卖 call -> 为 delta neutral 买同结算日 VX 期货对冲。\n"
        "        VX 期货对冲量 = sum(VIX call量 x delta / 10)(期权$100/期货$1000)。\n"
        "        VX交易量(来自VIX 期权 dealer 对冲): 平时 ~4%，极端日可达 10-20%。"
    )

    ### Print selected date range for df_vx1_vx2
    # 2025-11-03 - 11-04, 2025 fed rate not reduce drawdown
    # print_date_range(df_vx1_vx2, "2024-06-01", "2024-09-01", cols=selected_col, label="vx1_features") # 2024-07-18, 2024 jpy carry trade drawdown
    # print_date_range(df_vx1_vx2, "2025-01-01", "2025-04-30", cols=selected_col, label="vx1_features") # 2025-02-21, 2025 fed rate not reduce drawdown
    # print_date_range(df_vx1_vx2, "2026-02-01", "2026-04-01", cols=selected_col, label="vx1_features") # 2026-03-02, 2026 US Iran war drawdown

    # print(f"Today is {end_date}")
    # print("# risk_off_score (0-7): VX1 price/volume signals")
    # print("# risk_off_level: categorical - GREEN (0-1), YELLOW (2-3), ORANGE (4-5), RED (>=6)")
    # print(f"# vixeq_risk_off_level: RED when spread_pct > {VIXEQ_VIX_SPREAD_ARMED_PERCENTILE:.0f}, else GREEN")
    # print("# vxn_gt_ma20: VXN close is above its 20-trading-day moving average")
    # print("# vxn_ma20_rising: VXN MA20 is higher than on the prior trading day")
    # print("# vxn_risk_off_level: RED when vxn_gt_ma20 and vxn_ma20_rising are both True, else GREEN")
    # print(f"# vxsmh_pct: VXSMH current-inclusive percentile over up to the trailing {VXSMH_PERCENTILE_LOOKBACK} trading days")
    # print(f"# vxsmh_risk_off_level: RED when vxsmh_pct > {VXSMH_RISK_OFF_PERCENTILE:.0f}, else GREEN")
    # print(f"# rho_armed: (VIX/VIXEQ)^2 < {VIXEQ_RHO_ARMED_THRESHOLD:.2f}")

    # ===== VIX call / SPX put options flow snapshots =====
    # A blocked/failed options download must not kill the main report.
    try:
        run_options_flow_snapshots(end_date, holidays, data_dir / "vix_call_spx_put")
    except Exception as exc:
        print(f"[options flow] snapshot failed: {exc}", file=sys.stderr)


# 示例
if __name__ == "__main__":
    end_date = los_angeles_today()
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
