import contextlib
import datetime as dt
import re
import ssl
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

pd.set_option("display.max_rows", None)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", None)


# 月份码（如果你后面要生成 VXH5 之类符号会用到）
MONTH_CODE = {
    1: "F",
    2: "G",
    3: "H",
    4: "J",
    5: "K",
    6: "M",
    7: "N",
    8: "Q",
    9: "U",
    10: "V",
    11: "X",
    12: "Z",
}


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


def vix_monthly_final_settlement(
    contract_year: int, contract_month: int, holidays: set[dt.date] | None = None
) -> dt.date:
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


def build_vx_monthly_schedule(
    start_date: dt.date, end_date: dt.date, holidays: set[dt.date]
) -> list[dict]:
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
        schedule.append(
            {
                "contract_month": f"{y:04d}-{m:02d}",
                "fsd": fsd,
            }
        )
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


def is_business_day(d: dt.date, holidays: set[dt.date]) -> bool:
    return (d.weekday() < 5) and (d not in holidays)


def vxcurrent_map(
    start_date: dt.date,
    end_date: dt.date,
    holidays: set[dt.date],
    business_days_only: bool = True,
) -> dict[dt.date, str]:
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


def vx_expiry_table(
    x: dt.date, n_months_back: int, holidays: set[dt.date] | None = None
):
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
    cboe_vx_futures_hlocv_data: dict[str, str],
    data_dir: str | Path | None = None,
    *,
    verify_ssl: bool = True,
    cafile: str | None = None,
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


def contract_month_to_futures_label(contract_month: str) -> str:
    """Convert YYYY-MM to VX futures label like 'J (Apr 2025)'."""
    year, month = (int(x) for x in contract_month.split("-"))
    month_label = dt.date(year, month, 1).strftime("%b %Y")
    return f"{MONTH_CODE[month]} ({month_label})"


def rows_for_vxcurrent_map(
    m: dict[dt.date, str],
    all_data: pd.DataFrame,
    *,
    strict: bool = True,
) -> pd.DataFrame:
    """Return rows from all_data matching each trade date and its VXCurrent futures label."""
    rows = []
    for trade_date, contract_month in m.items():
        trade_date_str = trade_date.isoformat()
        futures_label = contract_month_to_futures_label(contract_month)
        match = all_data.loc[
            (all_data["Trade Date"] == trade_date_str)
            & (all_data["Futures"] == futures_label)
        ]
        if match.empty:
            if strict:
                raise LookupError(
                    "No matching row for "
                    f"Trade Date={trade_date_str}, Futures={futures_label}"
                )
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
    lookback_rows: int = 365,
    ma_window: int = 50,
) -> pd.DataFrame:
    out = df.copy()
    out[date_col] = pd.to_datetime(out[date_col], errors="coerce")
    out[volume_col] = pd.to_numeric(out[volume_col], errors="coerce")
    out = out.sort_values(date_col).reset_index(drop=True)

    out["Close_MA20"] = out["Close"].rolling(20, min_periods=20).mean()

    out["Volume_MA50"] = (
        out[volume_col].rolling(ma_window, min_periods=ma_window).mean()
    )
    out["Volume/MA50"] = out[volume_col] / out["Volume_MA50"]

    def pct_rank_in_window(arr: np.ndarray) -> float:
        arr = arr.astype(float)
        arr = arr[~np.isnan(arr)]
        if len(arr) < lookback_rows:
            return np.nan
        v = arr[-1]  # 当日值（窗口最后一个）
        return float(np.mean(arr <= v) * 100.0)

    out["volume_pct"] = (
        out[volume_col]
        .rolling(window=lookback_rows, min_periods=lookback_rows)
        .apply(pct_rank_in_window, raw=True)
    )
    out["volume_pct_ge90_count_last_5"] = (
        out["volume_pct"]
        .rolling(window=5, min_periods=5)
        .apply(lambda arr: float(np.sum(arr >= 90.0)), raw=True)
    )
    out["volume_pct_ge90_count_last_10"] = (
        out["volume_pct"]
        .rolling(window=10, min_periods=10)
        .apply(lambda arr: float(np.sum(arr >= 90.0)), raw=True)
    )

    cond_0 = out["volume_pct"]>=90
    cond_1 = out["volume_pct_ge90_count_last_5"] >= 3 
    cond_2 = out["volume_pct_ge90_count_last_10"] >= 3
    cond_3 = out["volume_pct_ge90_count_last_5"] >= 2
    cond_4 = out["volume_pct_ge90_count_last_10"] >= 2
    # out["SELL"] = cond_0 & ((cond_1| cond_2) | ( cond_3 & cond_4))
    out["SELL"] = cond_0 & (cond_1| cond_2)
    
    out = out.reset_index(drop=True)
    return out

from io import StringIO


def read_text_over_https(
    url: str,
    *,
    verify_ssl: bool = True,
    cafile: str | None = None,
    timeout: int = 30,
) -> str:
    """
    Read text from https URL with custom SSL context.
    """
    if not verify_ssl:
        ctx = ssl._create_unverified_context()
    else:
        if cafile is None:
            try:
                import certifi  # type: ignore

                cafile = certifi.where()
            except Exception:
                cafile = None
        ctx = ssl.create_default_context(cafile=cafile)

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        final_url = resp.geturl()
        data = resp.read()

    if not data.strip():
        raise ValueError(f"Empty response while reading text from {final_url}")
    return data.decode("utf-8", errors="replace")


def parse_stooq_history_html(html_text: str) -> pd.DataFrame:
    """
    Extract the actual price-history table from a Stooq historical-data page.
    """
    required_cols = {"Date", "Open", "High", "Low", "Close"}
    date_pattern = r"\d{1,2} [A-Za-z]{3} \d{4}"
    best_df: pd.DataFrame | None = None
    best_rows = -1

    for table in pd.read_html(StringIO(html_text)):
        cols = {str(col) for col in table.columns}
        if not required_cols.issubset(cols):
            continue

        candidate = table.copy()
        candidate = candidate[
            candidate["Date"]
            .astype(str)
            .str.fullmatch(date_pattern, na=False)
        ]
        if candidate.empty:
            continue

        keep_cols = ["Date", "Open", "High", "Low", "Close"]
        if "Volume" in candidate.columns:
            keep_cols.append("Volume")
        candidate = candidate[keep_cols]

        if len(candidate) >= best_rows:
            best_df = candidate
            best_rows = len(candidate)

    if best_df is None:
        raise ValueError("Unable to locate SPX price table in Stooq HTML page.")

    return best_df.reset_index(drop=True)


def extract_stooq_last_page(html_text: str) -> int:
    """
    Extract the last pagination page number from a Stooq historical-data page.
    """
    page_nums = [
        int(page)
        for page in re.findall(r"q/d/\?s=[^&]+&i=d&l=(\d+)", html_text, flags=re.IGNORECASE)
    ]
    return max(page_nums, default=1)


def normalize_spx_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize OHLC data from cached CSV or Stooq HTML table.
    """
    df = df.copy()

    required_cols = {"Date", "Open", "High", "Low", "Close"}
    missing_cols = sorted(required_cols.difference(df.columns))
    if missing_cols:
        raise ValueError(
            f"SPX OHLC data is missing required columns {missing_cols}. "
            f"Available columns: {list(df.columns)}"
        )

    if "Volume" not in df.columns:
        df["Volume"] = np.nan

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["Date", "Open", "High", "Low", "Close"])
    df = df.drop_duplicates(subset=["Date"], keep="last")
    df = df.sort_values("Date").reset_index(drop=True)
    if df.empty:
        raise ValueError("SPX OHLC dataset is empty after parsing.")
    return df


def fetch_stooq_spx_history_html(
    *,
    page: int | None = None,
    verify_ssl: bool = True,
    cafile: str | None = None,
) -> str:
    """
    Fetch Stooq's HTML historical-data page for ^SPX.
    """
    url = "https://stooq.com/q/d/?s=%5Espx&i=d"
    if page is not None and page > 1:
        url = f"{url}&l={page}"
    return read_text_over_https(url, verify_ssl=verify_ssl, cafile=cafile)


def download_spx_ohlc_stooq_html(
    *,
    cached_df: pd.DataFrame | None = None,
    verify_ssl: bool = True,
    cafile: str | None = None,
) -> pd.DataFrame:
    """
    Download ^SPX daily OHLC from Stooq's historical HTML pages.
    - If cache exists, fetch enough recent pages to overlap with the cache and merge.
    - If cache is missing, bootstrap the full history by walking all pages once.
    """
    first_html = fetch_stooq_spx_history_html(
        verify_ssl=verify_ssl,
        cafile=cafile,
    )
    first_page_df = normalize_spx_ohlc(parse_stooq_history_html(first_html))
    last_page = extract_stooq_last_page(first_html)

    if cached_df is not None and (not cached_df.empty):
        cached_df = normalize_spx_ohlc(cached_df)
        cached_last_date = cached_df["Date"].max()

        frames = [first_page_df]
        oldest_fetched_date = first_page_df["Date"].min()
        page = 2

        while oldest_fetched_date > cached_last_date and page <= last_page:
            page_df = normalize_spx_ohlc(
                parse_stooq_history_html(
                    fetch_stooq_spx_history_html(
                        page=page,
                        verify_ssl=verify_ssl,
                        cafile=cafile,
                    )
                )
            )
            frames.append(page_df)
            oldest_fetched_date = min(oldest_fetched_date, page_df["Date"].min())
            page += 1

        return normalize_spx_ohlc(pd.concat([cached_df] + frames, ignore_index=True))

    print(f"[info] Bootstrapping SPX OHLC from Stooq HTML across {last_page} pages.")
    frames = [first_page_df]
    for page in range(2, last_page + 1):
        page_df = normalize_spx_ohlc(
            parse_stooq_history_html(
                fetch_stooq_spx_history_html(
                    page=page,
                    verify_ssl=verify_ssl,
                    cafile=cafile,
                )
            )
        )
        frames.append(page_df)

    return normalize_spx_ohlc(pd.concat(frames, ignore_index=True))


def load_spx_ohlc_stooq(
    *,
    cache_path: str | Path = "spx_stooq.csv",
    refresh: bool = False,
    verify_ssl: bool = True,
    cafile: str | None = None,
) -> pd.DataFrame:
    """
    Download ^SPX daily OHLC from Stooq's HTML history page and cache locally.
    """
    cache_path = Path(cache_path)

    if refresh or (not cache_path.exists()):
        try:
            cached_df = pd.read_csv(cache_path) if cache_path.exists() else None
            df = download_spx_ohlc_stooq_html(
                cached_df=cached_df,
                verify_ssl=verify_ssl,
                cafile=cafile,
            )
        except Exception as exc:
            if not cache_path.exists():
                raise RuntimeError(
                    "Failed to refresh SPX OHLC from Stooq HTML pages and "
                    f"cache {cache_path} does not exist."
                ) from exc
            print(
                f"[warn] Failed to refresh SPX OHLC from Stooq ({exc}). "
                f"Falling back to cached file {cache_path}."
            )
            df = pd.read_csv(cache_path)
        else:
            cache_path.write_text(df.to_csv(index=False), encoding="utf-8")
    else:
        df = pd.read_csv(cache_path)

    return normalize_spx_ohlc(df)


def compute_spx_forward_metrics(
    spx_ohlc: pd.DataFrame,
    horizons: tuple[int, ...] = (1, 5, 10),
) -> pd.DataFrame:
    """
    给每个交易日计算：
      - fwd_ret_{h}: (Close[t+h]/Close[t]-1)
      - max_dd_{h}: min_{i=1..h} (Low[t+i]/Close[t]-1)   # 用未来h天内最低Low做最大回撤
    返回 DataFrame index=Date
    """
    df = spx_ohlc.copy()
    df = df.rename(
        columns={
            "Date": "date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
        }
    )
    df = (
        df.dropna(subset=["date", "close", "low"])
        .sort_values("date")
        .reset_index(drop=True)
    )
    df = df.set_index("date")

    closes = df["close"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    n = len(df)

    out = df[["close", "low"]].copy()

    for h in horizons:
        # forward return
        fwd = np.full(n, np.nan, dtype=float)
        fwd[:-h] = closes[h:] / closes[:-h] - 1.0
        out[f"fwd_ret_{h}"] = fwd

        # max drawdown over next h trading days using LOW
        mdd = np.full(n, np.nan, dtype=float)
        for i in range(n - h):
            window_min_low = np.min(lows[i + 1 : i + 1 + h])  # 未来h天（不含当天）
            mdd[i] = window_min_low / closes[i] - 1.0
        out[f"max_dd_{h}"] = mdd * 100

    return out


def align_signal_dates_to_spx(
    signal_dates: list[pd.Timestamp],
    spx_index: pd.DatetimeIndex,
    *,
    mode: str = "next",  # "next" or "exact"
) -> list[pd.Timestamp]:
    """
    把信号日对齐到 SPX 交易日：
      - exact：必须是交易日，否则丢弃
      - next：若不是交易日，则对齐到下一个交易日（一般你的信号日来自VX，通常本来就是交易日）
    """
    aligned = []
    for d in signal_dates:
        d = pd.Timestamp(d).normalize()
        if d in spx_index:
            aligned.append(d)
        elif mode == "next":
            pos = spx_index.searchsorted(d)
            if pos < len(spx_index):
                aligned.append(spx_index[pos])
        # else exact: drop
    return aligned


def event_study_spx_drawdown(
    df_signals: pd.DataFrame,
    spx_metrics: pd.DataFrame,
    *,
    date_col: str = "Trade Date",
    horizons: tuple[int, ...] = (1, 5, 10),
    dd_hit_threshold: float = -0.01,  # hit: max drawdown <= -1%
    align_mode: str = "next",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    返回：
      1) per-event 明细表（每个触发日的 fwd_ret/max_dd）
      2) summary（分布 + hit rate）
    """
    # signals -> timestamps
    sig_ts = (
        pd.to_datetime(df_signals[date_col], errors="coerce").dropna().dt.normalize()
    )
    sig_list = sig_ts.tolist()

    spx_idx = spx_metrics.index
    aligned = align_signal_dates_to_spx(sig_list, spx_idx, mode=align_mode)

    # per-event table
    rows = []
    for d in aligned:
        row = {
            "signal_date": d.date(),
            "spx_close_t0": float(spx_metrics.loc[d, "close"]),
        }
        for h in horizons:
            row[f"fwd_ret_{h}"] = (
                float(spx_metrics.loc[d, f"fwd_ret_{h}"])
                if pd.notna(spx_metrics.loc[d, f"fwd_ret_{h}"])
                else np.nan
            )
            row[f"max_dd_{h}"] = (
                float(spx_metrics.loc[d, f"max_dd_{h}"])
                if pd.notna(spx_metrics.loc[d, f"max_dd_{h}"])
                else np.nan
            )
        rows.append(row)

    per_event = pd.DataFrame(rows).dropna(subset=[f"fwd_ret_{horizons[0]}"], how="all")

    # summary: distribution + hit rate
    summary_rows = []
    for h in horizons:
        s_ret = per_event[f"fwd_ret_{h}"].dropna()
        s_dd = per_event[f"max_dd_{h}"].dropna()

        summary_rows.append(
            {
                "horizon_days": h,
                "n": int(min(len(s_ret), len(s_dd))),
                # forward return distribution
                "ret_mean": float(s_ret.mean()) if len(s_ret) else np.nan,
                "ret_p10": float(s_ret.quantile(0.10)) if len(s_ret) else np.nan,
                "ret_p50": float(s_ret.quantile(0.50)) if len(s_ret) else np.nan,
                "ret_p90": float(s_ret.quantile(0.90)) if len(s_ret) else np.nan,
                # max drawdown distribution
                "mdd_mean": float(s_dd.mean()) if len(s_dd) else np.nan,
                "mdd_p10": float(s_dd.quantile(0.10)) if len(s_dd) else np.nan,
                "mdd_p50": float(s_dd.quantile(0.50)) if len(s_dd) else np.nan,
                "mdd_p90": float(s_dd.quantile(0.90)) if len(s_dd) else np.nan,
                # hit rates
                "hit_ret_neg": float((s_ret < 0).mean()) if len(s_ret) else np.nan,
                "hit_mdd_le_thresh": float((s_dd <= dd_hit_threshold).mean())
                if len(s_dd)
                else np.nan,
            }
        )

    summary = pd.DataFrame(summary_rows)
    return per_event, summary

import subprocess

def imessage(to, message):
    safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "Messages"
        set targetService to 1st service whose service type is iMessage
        set targetBuddy to buddy "{to}" of targetService
        send "{safe_message}" to targetBuddy
    end tell
    '''
    subprocess.run(["osascript", "-e", script], check=False)

def imessage_file(to, file_path: str) -> None:
    safe_path = file_path.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "Messages"
        activate
        set targetService to 1st service whose service type is iMessage
        set targetBuddy to buddy "{to}" of targetService
        set theFile to POSIX file "{safe_path}" as alias
        try
            set targetChat to 1st chat whose participants contains targetBuddy
            send theFile to targetChat
        on error
            send theFile to targetBuddy
        end try
    end tell
    '''
    subprocess.run(["osascript", "-e", script], check=False)

def _df_to_pipe_table(df: pd.DataFrame) -> str:
    # preferred_cols = ["Date", "%", "%90_L5", "%90_L10", "SELL"]
    # if all(col in df.columns for col in preferred_cols):
    #     df = df.loc[:, preferred_cols]
    cols = list(df.columns)
    rows = df.values.tolist()
    str_rows = [
        ["" if pd.isna(val) else str(val) for val in row]
        for row in rows
    ]

    widths = []
    for i, col in enumerate(cols):
        max_len = len(str(col))
        for row in str_rows:
            if len(row[i]) > max_len:
                max_len = len(row[i])
        widths.append(max_len)

    def _fmt_row(row_vals):
        return "| " + " | ".join(
            row_vals[i].ljust(widths[i]) for i in range(len(cols))
        ) + " |"

    header = _fmt_row([str(c) for c in cols])
    sep = "| " + " | ".join("-" * widths[i] for i in range(len(cols))) + " |"
    body = "\n".join(_fmt_row(r) for r in str_rows)
    return "\n".join([header, sep, body]) if body else "\n".join([header, sep])


def _vx_table_for_imessage(
    df: pd.DataFrame, max_rows: int = 10
) -> str:
    cols = [
        "Trade Date",
        "Futures",
        "Close",
        "Change",
        "Total Volume",
        "Volume/MA50",
        "volume_pct",
        "volume_pct_ge90_count_last_5",
        "volume_pct_ge90_count_last_10",
        "SELL",
    ]
    view = df.loc[:, cols].tail(max_rows).copy()
    view = view.rename(
        columns={
            "Trade Date": "Date",
            "Futures": "Fut",
            "Close": "Clo",
            "Change": "Chg",
            "Total Volume": "Vol",
            "Volume/MA50": "/m50",
            "volume_pct": "%",
            "volume_pct_ge90_count_last_5": "90%L5",
            "volume_pct_ge90_count_last_10": "90%L10",
        }
    )

    def _fmt_float_trim(val) -> str:
        if pd.isna(val):
            return ""
        return f"{val:.2f}".rstrip("0").rstrip(".")

    def _fmt_int(val) -> str:
        return "" if pd.isna(val) else f"{int(val):,}"

    def _fmt_date(val) -> str:
        if pd.isna(val):
            return ""
        dt_val = pd.to_datetime(val, errors="coerce")
        if pd.isna(dt_val):
            return str(val)
        return dt_val.strftime("%m-%d")

    def _fmt_fut(val) -> str:
        if pd.isna(val):
            return ""
        text = str(val).strip()
        match = re.match(r"^([A-Za-z]+)\s*\(([^)]+)\)$", text)
        if not match:
            return text.replace("(", "").replace(")", "")
        code = match.group(1).strip()
        inside = match.group(2).strip()
        parts = inside.split()
        if len(parts) < 2 or not parts[-1].isdigit():
            return text.replace("(", "").replace(")", "")
        month = parts[0][:3].title()
        year = int(parts[-1])
        return f"{month}/{year % 100:02d}"

    def _fmt_float_1(val) -> str:
        if pd.isna(val):
            return ""
        return f"{val:.1f}"

    def _fmt_vol_k(val) -> str:
        if pd.isna(val):
            return ""
        try:
            num = int(float(val))
        except (TypeError, ValueError):
            return str(val)
        if abs(num) < 1000:
            return f"{num}"
        return f"{num // 1000}k"

    view["Date"] = view["Date"].map(_fmt_date)
    view["Fut"] = view["Fut"].map(_fmt_fut)
    view["Clo"] = view["Clo"].map(_fmt_float_1)
    view["Chg"] = view["Chg"].map(_fmt_float_trim)
    view["/m50"] = view["/m50"].map(_fmt_float_trim)
    view["%"] = view["%"].map(_fmt_float_trim)
    view["Vol"] = view["Vol"].map(_fmt_vol_k)
    view["90%L5"] = view["90%L5"].map(_fmt_int)
    view["90%L10"] = view["90%L10"].map(_fmt_int)
    view["SELL"] = view["SELL"].map(lambda x: "Y" if bool(x) else "N")

    return _df_to_pipe_table(view)


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
    table = vx_expiry_table(
        x, n_months_back=12, holidays=holidays
    )  # holidays 你可以传入Cboe options holiday日期集合
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
    }
    download_cboe_vx_csvs(cboe_vx_futures_hlocv_data)

    # ===== Create Dataframe, for each trading day, the VXCurrent and its HLOCV, plus features =====
    data_dir = Path(__file__).resolve().parent / "data"
    all_data = load_vx_csvs(data_dir)
    df_vxcurrent_hlocv = rows_for_vxcurrent_map(m, all_data, strict=False)
    # print("/vxcurrent hlocv:")
    # print(df_vxcurrent_hlocv.tail(10))
    # df_vxcurrent_hlocv.to_csv("vxcurrent_hlocv.csv")

    # ===== Derive features to predict draw down =====
    df_vxcurrent_hlocv_features = add_volume_metrics_rows_incl_today_strict(
        df_vxcurrent_hlocv, lookback_rows=300, ma_window=50
    )
    df_vxcurrent_hlocv_features.to_csv(
        f"vix_sell_signal/{end_date}_vxcurrent_hlocv_features.csv"
    )

    # ===== VXCurrent draw down (sell) signal =====
    date_filter = "2023-01-01" < df_vxcurrent_hlocv_features["Trade Date"]
    # data_filter &= df_vxcurrent_vol_pct["Trade Date"] < "2025-01-01"

    df_vx_days_trigger_sell_signal = df_vxcurrent_hlocv_features[
        df_vxcurrent_hlocv_features["SELL"] & date_filter
    ]
    print(
        "/vxcurrent sell signal, 1) volume_pct>90, 2) count(last5daysVolPct>90)>=3 or count(last10daysVolPct>90)>=3:"
    )
    print(df_vx_days_trigger_sell_signal)
    df_vx_days_trigger_sell_signal.to_csv(
        f"vix_sell_signal/{end_date}_vxcurrent_days_trigger_sell_signal.csv"
    )

    # ===== SPX event study =====
    # when drawdown (sell) signal occurs, what's the max drawdown in next (1, 5, 10, 15, 20, 25, 30) days
    spx_ohlc = load_spx_ohlc_stooq(
        cache_path="spx_stooq.csv", refresh=True, verify_ssl=False
    )

    horizons = (1, 5, 10, 15, 20, 25, 30)
    spx_metrics = compute_spx_forward_metrics(spx_ohlc, horizons=horizons)

    # 触发日事件研究：t+(1, 5, 10, 15, 20, 25, 30) 的 forward ret + max drawdown
    per_event, summary = event_study_spx_drawdown(
        df_vx_days_trigger_sell_signal,
        spx_metrics,
        date_col="Trade Date",
        horizons=horizons,
        dd_hit_threshold=-0.01,  # 你可以换成 -0.02 看 2% 回撤 hit rate
        align_mode="next",
    )

    print("\n/SPX per-event:")
    select_col = [f"max_dd_{i}" for i in horizons]
    select_col = ["signal_date"] + select_col
    print(per_event[select_col])

    # print("\n/SPX summary:")
    # print(summary)

    # per_event.to_csv("spx_eventstudy_per_signal.csv", index=False)
    # summary.to_csv("spx_eventstudy_summary.csv", index=False)

    # ===== VXCurrent =====
    print("/vxcurrent this month:")
    print(df_vxcurrent_hlocv_features.tail(30))
    print(
        f"Today is {end_date}, vxcurrent sell signal: \n"
        "- today's volume_pct>=90\n"
        "- volume_pct_ge90_count_last_5>=3 or volume_pct_ge90_count_last_10>=3"
    )

    vx_table = _vx_table_for_imessage(df_vxcurrent_hlocv_features, max_rows=10)
    imessage_txt = f"{vx_table}\nToday is {end_date}"
    print("/imessage:")
    print(imessage_txt)
    imessage(to="+14155187720", message=imessage_txt)
    

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

    with report_path.open("w", encoding="utf-8") as report_file, contextlib.redirect_stdout(
        _Tee(sys.stdout, report_file)
    ):
        run_vx_eod_report(end_date)

    # imessage_file(to="robin-xue@qq.com", file_path=str(report_path))
