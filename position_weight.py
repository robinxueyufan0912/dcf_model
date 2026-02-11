from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

from finviz_sector_ticker_ps_pe_rev import sector_to_tickers


DEFAULT_CSV = Path("Portfolio_Positions_Jan-15-2026.csv")
EXPECTED_COLUMNS = [
    "Account Number",
    "Account Name",
    "Symbol",
    "Description",
    "Quantity",
    "Last Price",
    "Last Price Change",
    "Current Value",
    "Today's Gain/Loss Dollar",
    "Today's Gain/Loss Percent",
    "Total Gain/Loss Dollar",
    "Total Gain/Loss Percent",
    "Percent Of Account",
    "Cost Basis Total",
    "Average Cost Basis",
    "Type",
]
PREFIX_GROUPS = [
    ("aerospace", "aerospace - *"),
    ("data center", "data center - *"),
    ("Fin", "Fin - *"),
    ("cons disc", "cons disc - *"),
    ("Industry", "Industry - *"),
    ("TMT", "TMT - *"),
]


def parse_money(series: pd.Series) -> pd.Series:
    cleaned = series.astype(str).str.strip()
    cleaned = cleaned.str.replace(r"[$,]", "", regex=True)
    cleaned = cleaned.str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    return pd.to_numeric(cleaned, errors="coerce")


def read_csv_split_commas(path: Path) -> pd.DataFrame:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=",", quotechar='"')
        rows = [row for row in reader if row and any(cell.strip() for cell in row)]

    if not rows:
        raise ValueError("CSV file is empty.")

    header = [col.strip() for col in rows[0]]
    while header and header[-1] == "":
        header.pop()

    cleaned_rows: list[list[str]] = []
    for row in rows[1:]:
        while len(row) > len(header) and row[-1] == "":
            row = row[:-1]
        if len(row) > len(header):
            row = row[: len(header)]
        elif len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        cleaned_rows.append(row)

    df = pd.DataFrame(cleaned_rows, columns=header)
    if set(EXPECTED_COLUMNS).issubset(df.columns):
        df = df.loc[:, EXPECTED_COLUMNS]
    return df


def load_positions(path: Path) -> pd.DataFrame:
    df = read_csv_split_commas(path)
    if "Symbol" not in df.columns or "Current Value" not in df.columns:
        raise ValueError("Expected columns 'Symbol' and 'Current Value' not found.")
    df = df[df["Symbol"].notna()]
    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    df = df[df["Symbol"].ne("")]
    df = df[df["Symbol"].str.casefold().ne("pending activity")]
    df["current_value"] = parse_money(df["Current Value"])
    df = df[df["current_value"].notna()]
    return df


def compute_ticker_weights(df: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    aggregated = df.groupby("Symbol", as_index=False)["current_value"].sum()
    total_value = aggregated["current_value"].sum()
    if total_value == 0:
        raise ValueError("Total current value is zero after aggregation.")
    aggregated["weight"] = aggregated["current_value"] / total_value
    aggregated["weight_pct"] = aggregated["weight"] * 100
    aggregated = aggregated.sort_values("weight", ascending=False)
    return aggregated, total_value


def compute_sector_weights(
    ticker_weights: pd.DataFrame,
) -> tuple[pd.DataFrame, list[str], dict[str, list[str]]]:
    weights = ticker_weights.set_index("Symbol")["weight"]
    sector_weights: dict[str, float] = {}
    for sector, tickers in sector_to_tickers.items():
        unique_tickers = list(dict.fromkeys(tickers))
        sector_weights[sector] = weights.reindex(unique_tickers).fillna(0).sum()

    sector_df = (
        pd.Series(sector_weights, name="weight")
        .to_frame()
        .assign(weight_pct=lambda d: d["weight"] * 100)
        .sort_values("weight", ascending=False)
    )

    all_sector_tickers = {t for tickers in sector_to_tickers.values() for t in tickers}
    unclassified = sorted(set(weights.index) - all_sector_tickers)

    ticker_to_sectors: dict[str, list[str]] = {}
    for sector, tickers in sector_to_tickers.items():
        for ticker in tickers:
            ticker_to_sectors.setdefault(ticker, []).append(sector)
    multi_sector = {t: s for t, s in ticker_to_sectors.items() if len(s) > 1}

    return sector_df, unclassified, multi_sector


def compute_prefix_aggregates(sector_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    index = sector_df.index.to_series()
    for prefix, label in PREFIX_GROUPS:
        mask = index.str.startswith(prefix)
        weight = sector_df.loc[mask, "weight"].sum()
        rows.append({"group": label, "weight": weight, "weight_pct": weight * 100})
    return pd.DataFrame(rows).set_index("group")


def print_report(
    ticker_df: pd.DataFrame,
    sector_df: pd.DataFrame,
    total_value: float,
    unclassified: list[str],
    multi_sector: dict[str, list[str]],
) -> None:
    print(f"Total current value: ${total_value:,.2f}")
    print("\nTicker weights:")
    print(
        ticker_df[["Symbol", "current_value", "weight_pct"]].to_string(
            index=False,
            formatters={
                "current_value": "{:,.2f}".format,
                "weight_pct": lambda v: f"{v:.2f}%",
            },
        )
    )

    print("\nSector weights (based on sector_to_tickers):")
    print(
        sector_df.to_string(
            index=True,
            formatters={"weight_pct": lambda v: f"{v:.2f}%"},
        )
    )

    prefix_df = compute_prefix_aggregates(sector_df)
    print("\nAggregated sector weights (prefix groups):")
    print(
        prefix_df.to_string(
            index=True,
            formatters={"weight_pct": lambda v: f"{v:.2f}%"},
        )
    )

    if unclassified:
        print("\nTickers not in sector_to_tickers:")
        print(", ".join(unclassified))

    in_portfolio = set(ticker_df["Symbol"])
    overlaps = {t: s for t, s in multi_sector.items() if t in in_portfolio}
    if overlaps:
        print("\nTickers appearing in multiple sectors (counted in each sector):")
        for ticker in sorted(overlaps):
            print(f" - {ticker}: {', '.join(overlaps[ticker])}")


def main(path: Path = DEFAULT_CSV) -> None:
    df = load_positions(path)
    ticker_df, total_value = compute_ticker_weights(df)
    sector_df, unclassified, multi_sector = compute_sector_weights(ticker_df)
    print_report(ticker_df, sector_df, total_value, unclassified, multi_sector)


if __name__ == "__main__":
    main()
