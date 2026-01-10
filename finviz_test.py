# pip install finvizfinance pandas

from finvizfinance.quote import Statements, finvizfinance
from finvizfinance.calendar import Calendar

import pandas as pd

pd.set_option("display.max_rows", None)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", None)

def calc_growth_pct(series, window):
    series_chrono = series.iloc[::-1]
    growth_chrono = series_chrono.rolling(window=window, min_periods=window).apply(
        lambda x: (x[-1] - x[0]) / x[0] if x[0] not in (0, None) else float("nan"),
        raw=True,
    )
    growth = growth_chrono.iloc[::-1]
    growth_pct = (growth * 100).round(0).astype("Int64")
    return growth_pct.map(lambda v: f"{v}%" if pd.notna(v) else pd.NA)


def calc_yoy_pct(series):
    return calc_growth_pct(series, window=5)


def calc_qoq_pct(series):
    return calc_growth_pct(series, window=2)


def add_growth_row(df, source_row, row_name, calc_func, insert_after=None):
    if source_row not in df.index:
        return df

    values = (
        df.loc[source_row]
        .replace({",": ""}, regex=True)
        .pipe(pd.to_numeric, errors="coerce")
    )

    growth = calc_func(values)

    df_out = df.copy()
    if row_name in df_out.index:
        df_out = df_out.drop(index=row_name)

    anchor = insert_after if insert_after in df_out.index else source_row
    if anchor not in df_out.index:
        return df_out
    insert_at = list(df_out.index).index(anchor) + 1
    upper = df_out.iloc[:insert_at]
    lower = df_out.iloc[insert_at:]
    return pd.concat([upper, pd.DataFrame([growth], index=[row_name]), lower])


def add_yoy_row(df, source_row, row_name, insert_after=None):
    return add_growth_row(df, source_row, row_name, calc_yoy_pct, insert_after=insert_after)


def add_qoq_row(df, source_row, row_name, insert_after=None):
    return add_growth_row(df, source_row, row_name, calc_qoq_pct, insert_after=insert_after)


s = Statements()

# Quarterly Income Statement
df_is_q = s.get_statements("AAPL", statement="I", timeframe="Q")
df_is_q = add_yoy_row(df_is_q, "Total Revenue", "rev yoy%", insert_after="Total Revenue")
df_is_q = add_yoy_row(df_is_q, "Net Income Before Taxes", "EBIT yoy", insert_after="Net Income Before Taxes")
df_is_q = add_yoy_row(df_is_q, "Net Income", "Net Income yoy", insert_after="Net Income")
df_is_q = add_yoy_row(df_is_q, "EBITDA", "EBITDA yoy", insert_after="EBITDA")
df_is_q = add_qoq_row(df_is_q, "Total Revenue", "rev qoq%", insert_after="rev yoy%")
df_is_q = add_qoq_row(df_is_q, "Net Income Before Taxes", "EBIT qoq", insert_after="EBIT yoy")
df_is_q = add_qoq_row(df_is_q, "Net Income", "Net Income qoq", insert_after="Net Income yoy")
df_is_q = add_qoq_row(df_is_q, "EBITDA", "EBITDA qoq", insert_after="EBITDA yoy")
print(df_is_q)

# 你也可以抓：Balance Sheet / Cash Flow
df_bs_q = s.get_statements("AAPL", statement="B", timeframe="Q")
df_cf_q = s.get_statements("AAPL", statement="C", timeframe="Q")

f = finvizfinance("AAPL")
# print(f.ticker_full_info())
# print("---")
print(f.ticker_fundament("series", output_format="series"))
print("---")

# c = Calendar()
# print(c.calendar())
