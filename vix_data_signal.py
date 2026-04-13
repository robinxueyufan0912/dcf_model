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
    lookback_rows: int = 252, # 252 # 365
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

    out["vol_ge_1.85x_ma50"] = out["Volume/MA50"] >= 1.85
    out["vol_ge_90pct"] = out["volume_pct"] >= 90

    out["vol_ge_90pct_last_5days"] = (
        out["volume_pct"]
        .rolling(window=5, min_periods=5)
        .apply(lambda arr: float(np.sum(arr >= 90.0)), raw=True)
    )
    out["vol_ge_90pct_last_10days"] = (
        out["volume_pct"]
        .rolling(window=10, min_periods=10)
        .apply(lambda arr: float(np.sum(arr >= 90.0)), raw=True)
    )

    # Risk-off score (0-7)
    # risk_off_score (0-7): sum of all 7 boolean signals
    # risk_off_level: categorical - GREEN (0-1), YELLOW (2-3), ORANGE (4-5), RED (6-7)
    out["risk_off_score"] = (
        out["close_gt_ma50"].astype(int)
        + out["close_gt_ma20"].astype(int)
        + out["ma20_rising"].astype(int)
        + out["vol_ge_1.85x_ma50"].astype(int)
        + out["vol_ge_90pct"].astype(int)
        + (out["vol_ge_90pct_last_5days"] >= 2).astype(int)
        + (out["vol_ge_90pct_last_10days"] >= 3).astype(int)
    )
    out["risk_off_level"] = pd.cut(
        out["risk_off_score"],
        bins=[-1, 1, 3, 5, 7],
        labels=["GREEN", "YELLOW", "ORANGE", "RED"],
    )

    out = out.reset_index(drop=True)
    return out

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
    download_cboe_vx_csvs(cboe_vx_futures_hlocv_data)

    # ===== Create Dataframe, for each trading day, the VXCurrent and its HLOCV, plus features =====
    print_tail_num_rows = 100
    data_dir = Path(__file__).resolve().parent / "data"
    all_data = load_vx_csvs(data_dir)
    df_vxcurrent_hlocv = rows_for_vxcurrent_map(m, all_data, strict=False)
    print("/df_vxcurrent_hlocv:")
    print(df_vxcurrent_hlocv.tail(print_tail_num_rows))
    # df_vxcurrent_hlocv.to_csv("vxcurrent_hlocv.csv")

    # ===== Derive features to predict draw down =====
    df_vxcurrent_hlocv_features = add_volume_metrics_rows_incl_today_strict(
        df_vxcurrent_hlocv, 
        lookback_rows=252, # one year has 252 trading days
        ma_window=50
    )
    df_vxcurrent_hlocv_features.to_csv(
        f"vix_sell_signal/{end_date}_vxcurrent_hlocv_features.csv"
    )

    # ===== VXCurrent =====
    print("/vxcurrent this month:")
    print(df_vxcurrent_hlocv_features.columns)
    # "Volume/MA50", "volume_pct", 
    # risk_off_score (0-7): sum of all 7 boolean signals
    # risk_off_level: categorical - GREEN (0-1), YELLOW (2-3), ORANGE (4-5), RED (6-7)
    selected_col = ["Trade Date", "Futures", "Close", "Change", "close_gt_ma50", "ma20_rising", "close_gt_ma20", "vol_ge_1.85x_ma50", "vol_ge_90pct", "vol_ge_90pct_last_5days", "vol_ge_90pct_last_10days", "risk_off_score", "risk_off_level"]
    print(df_vxcurrent_hlocv_features[selected_col].tail(print_tail_num_rows))

    # Print selected date range
    # 2024-07-18, 2024 jpy carry trade drawdown
    # 2025-02-21, 2025 libration day drawdown
    # 2025-11-03 - 11-04, 2025 fed rate not reduce drawdown
    # 2026-03-02, 2026 US Iran war drawdown
    # print_range_start, print_range_end = "2024-06-01", "2024-09-01"
    # print_range_start, print_range_end = "2025-01-01", "2025-04-30"
    print_range_start, print_range_end = "2026-02-01", "2025-04-01"
    date_mask = (df_vxcurrent_hlocv_features["Trade Date"] >= print_range_start) & (df_vxcurrent_hlocv_features["Trade Date"] <= print_range_end)
    print(f"\n/vxcurrent [{print_range_start}, {print_range_end}]:")
    print(df_vxcurrent_hlocv_features.loc[date_mask, selected_col])

    print(f"Today is {end_date}")


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
