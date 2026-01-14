from __future__ import annotations

from re import L
import time
from datetime import datetime
from pathlib import Path
from typing import OrderedDict
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
import pandas as pd

pd.set_option("display.max_rows", None)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", None)

from finvizfinance.quote import Statements, finvizfinance

sector_to_tickers = {
    "TMT - mag7": ["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA"],
    "TMT - e-commerce": ["UI", "SE", "CPNG"],
    "Luxury": [],
    "TMT - consumer discretionary": [
        "UBER", "DASH", "CART", "SHOP", "EBAY", "ETSY", "NFLX",
        "DIS", "SPOT", "RBLX", "RDDT", "PINS", "SNAP", "MTCH",
        "APP", "TTD", "ROKU", "U", "DUOL", "COUR", "RKT",
        "Z", "IOT", 
    ],
    "TMT - cybersecurity": [
        "PANW", "CRWD", "FTNT", "ZS", "NET", "CYBR", "OKTA", "RBRK", "CVLT", "NTNX", 
        "SNOW", "MDB", "DDOG", "DT", 
        ],
    "TMT - devops": ["GTLB", "FROG", "DBX",],
    "TPU": ["WULF", "CIFR", "GOOG", "AVGO", "TSM", "TTMI", "FLEX", "CLS", "MRVL", "APH", "LITE", "ADI", "FN", "VICR"],
    "data center": ["NOK", ],
    "data center - hyper scaler": ["ORCL", "CRWV", "NBIS", "IREN", "CIFR", ],
    "data center - semi": ["NVDA", "MU", "AVGO", "AMD"],
    "data center - package":["TSM", "ASX", "AMKR",],
    "data center - test":["TER", ],
    "data center - router" :["ANET", "MRVL", ],
    "data center - optics" :[ "CRDO", "MTSI", "SMTC", "COHR", "CIEN", "CSCO", "GLW", ],
    "data center - NAND/SSD": ["STX", "WDC", "SNDK"],
    "data center - semi equip": [ "ASML", "LRCX", "KLAC", ],
    "data center - server":[ "DELL", "SMCI"],
    "data center - PDU":["MPWR", "NVT", "VRT", "ETN", ],
    "data center - onsite power generator":[ "CAT", "CMI", "BE",],
    "data center - construction builder":["EME", "FIX",],
    "data center - medium volt transformer":["POWL", "GEV", "PWR",],
    "data center - new IPP":["TLN", "BWXT", "OKLO", "SMR",],
    "data center - IPP": ["CEG", "VST", "NRG",],
    "data center - uranium": ["URI", "CCJ", "UEC", "LEU", ],
    "data center - HVAC":[ "TT", "CARR", "JCI", "SPXC"],
    "chips": ["QCOM", "INTC", "ARM"],
    "robotic": ["PH", "ALNT", "ROK", "SNPS", "SYM", "ATI"],
    "rare earth": ["MP", "CRML"],
    "material": ["AA", "SCCO", "ALB", "LAC", "FLNC"], # aluminum, copper, lithium   
    "quatum": ["IBM", "IONQ", "RGTI"],
    "TMT - SAAS" : ["ORCL", "SAP", "CRM", "NOW", "ADBE", "INTU", "ADP", "WDAY", "ADSK", "TEAM", "HUBS", "TWLO", "ZM", "DOCU", "BILL", "GDDY", "WIX", "PATH"],
    "Fin - bank" : ["JPM", "BAC", "C", "BK", "MS", "GS"],
    "Fin - credit": ["AXP", "COF", "AFRM", "KLAR", "SOFI", "UPST", "FICO"],
    "Fin - payment": ["V", "MA", "FISV", "PYPL", "XYZ", "TOST"],
    "Fin - broker": ["SCHW", "IBKR", "HOOD", "COIN"],
    "farm machinery" : ["DE", "PCAR"],
    "aerospace & defense":["PLTR", "KTOS", "AVAV", "ONDS", "RTX", "LMT", "NOC", "GD", "HII", "CW", "BWXT", "LDOS", "BAH", "CACI"],
    "aerospace aftermarket": ["BA", "GE", "HWM", "TDG", "HEI", "CRS", "FTAI"],
    "police equipment": ["MSI", "AXON"],
    "auto manufactueres" :["TSLA", "RACE", "GM", "F", "CPRT"],
    "residential construction": ["DHI", "TOL", "KBH", "BLD", "IBP"],
    "home improvement retial": ["HD", "LOW", "BLDR", "W", "WSM"],
    "loging": ["MAR", "HLT", "H"],
    "airline": ["DAL", "UAL", "LUV", "AAL"],
    "resort & casinos": ["LVS", "WYNN", "MGM", "CZR"],
    "travel service": ["BKNG", "ABNB", "RCL", "EXPE", "MMYT", "YOU"],
    "gambling": ["FLUT", "DKNG"],
    "restaurants": ["MCD", "SBUX", "CMG", "DRI", "DPZ", "TXRH", "WING", "CAVA", "SG"],
    "apparel" :["TJX", "NKE", "RL", "AS", "LULU", "ONON", "DECK", "VFC", "CROX", "GOOS"],
    "telecom service": ["TMUS", "VZ", "T", "CMCSA", "ERIC"], 
    "discount stores": ["WMT", "COST", "TGT", "SFM"],
    "household & personal products": ["PG", "PM", "UL", "CL", "KMB", "KVUE", "ULTA", "ELF", "HIMS"],
    "utilities - regulated electric": ["NEE", "SO", "DUK","AEP","D","PEG","PCG","HE"],
    "utilities - waste management": ["WM", "RSG", "WCN"],
    "insurance - diversified": ["BRK-B", "AIG"],
    "insurance - property & casualty": ["PGR", "ALL", "CB","TRV", "HIG", "WRB", "GWRE"],
    "insurance - brokers": ["AON", "AJG", "BRO", "ERIE"],
    "beverages - Non-alcohol": ["KO", "PEP", "KDP", "COKE", "MNST", "CELH", "PRMB"],
    "packaged food": ["KHC", "GIS", "MKC", "CPB", "PPC", "POST", "BRBR" , "FRPT"],
    "farm products": ["ADM", "TSN", "CALM", "VITL"],
    "specialty retail": ["ORLY", "AZO", "TSCO", "DKS"],
    "drug - general": ["LLY", "JNJ", "ABBV", "MRK", "AZN", "AMGN", "PFE","BMY", "GILD"],
    "biotech": ["NVO", "VRTX", "REGN", "MRNA", "UTHR"],
    "diagnostics" : ["TMO", "ILMN"],
    "healthcare plans": ["UNH", "CVS", "ELV", "CI", "HUM", "OSCR", "ALHC"],
    "hospital": ["HCA", "THC", "UHS", "DVA"],
    "health informaction service": ["GEHC", "VEEV"],
    "medical devices": ["ABT", "SYK", "BSX", "MDT", "EW"],
    "medical instruments": ["ISRG", "BDX", "ALC"],
    "industrial dis": ["GWW", "FAST", "CNM"],
    "marine shipping": ["KEX", "MATX"],
    "specialty chemicals": ["LIN", "SHW", "ECL"],
    "IT servies": ["IBM", "ACN", "INFY", "IT", "EPAM", "GLOB"],
    "oil & gas integrated": ["XOM", "CVX", "SHEL"],
}


FINVIZ = "https://finviz.com/quote.ashx?t={}"


# statement API: https://finviz.com/api/statement.ashx?t=zs&so=F&s=IQ

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def parse_snapshot_metrics(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="snapshot-table2")
    if not table:
        return {}
    tds = [td.get_text(strip=True) for td in table.find_all("td")]
    # Finviz uses alternating label/value cells
    return dict(zip(tds[0::2], tds[1::2]))

def norm(x: str | None) -> str:
    if not x:
        return "NA"
    x = x.strip()
    if x in {"-", "N/A"}:
        return "NA"
    return x

def fetch_pe_ps(session: requests.Session, ticker: str, sleep_s: float = 0.15) -> tuple[str, str, str]:
    url = FINVIZ.format(ticker)
    try:
        r = session.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return ticker, "NA", "NA"
        m = parse_snapshot_metrics(r.text)
        pe = norm(m.get("P/E"))
        ps = norm(m.get("P/S"))
        time.sleep(sleep_s)  # be nice to the site
        return ticker, pe, ps
    except Exception:
        return ticker, "NA", "NA"


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


def at_or_na(df, row, col, default="N/A"):
    try:
        return df.at[row, col]
    except KeyError:
        return default

def fetch_rev_ebit(ticker: str):
    s = Statements()
    # Quarterly Income Statement
    df_is_q = s.get_statements(ticker, statement="I", timeframe="Q")
    
    df_is_q = add_yoy_row(df_is_q, "Total Revenue", "rev yoy%", insert_after="Total Revenue")
    df_is_q = add_yoy_row(df_is_q, "Net Income Before Taxes", "EBIT yoy", insert_after="Net Income Before Taxes")
    df_is_q = add_yoy_row(df_is_q, "Net Income", "Net Income yoy", insert_after="Net Income")
    df_is_q = add_yoy_row(df_is_q, "EBITDA", "EBITDA yoy", insert_after="EBITDA")
    df_is_q = add_qoq_row(df_is_q, "Total Revenue", "rev qoq%", insert_after="rev yoy%")
    df_is_q = add_qoq_row(df_is_q, "Net Income Before Taxes", "EBIT qoq", insert_after="EBIT yoy")
    df_is_q = add_qoq_row(df_is_q, "Net Income", "Net Income qoq", insert_after="Net Income yoy")
    df_is_q = add_qoq_row(df_is_q, "EBITDA", "EBITDA qoq", insert_after="EBITDA yoy")
    print(df_is_q)
    print(at_or_na(df_is_q, 'rev yoy%', 0))
    print(at_or_na(df_is_q, 'EBIT yoy', 0))
    print(at_or_na(df_is_q, 'Net Income yoy', 0))
    print(at_or_na(df_is_q, 'EBITDA yoy', 0))
    print(at_or_na(df_is_q, 'rev qoq%', 0))
    print(at_or_na(df_is_q, 'EBIT qoq', 0))
    print(at_or_na(df_is_q, 'Net Income qoq', 0))
    print(at_or_na(df_is_q, 'EBITDA qoq', 0))

    return at_or_na(df_is_q, 'rev yoy%', 0), at_or_na(df_is_q, 'EBIT yoy', 0), at_or_na(df_is_q, 'Net Income yoy', 0), at_or_na(df_is_q, 'EBITDA yoy', 0), at_or_na(df_is_q, 'rev qoq%', 0), at_or_na(df_is_q, 'EBIT qoq', 0), at_or_na(df_is_q, 'Net Income qoq', 0), at_or_na(df_is_q, 'EBITDA qoq', 0)


def main():
    asof = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()

    columns = [
        "sector",
        "ticker",
        "date",
        "TTM P/E (GAAP)",
        "TTM P/S (GAAP)",
        "rev yoy%",
        "EBIT yoy",
        "EBITDA yoy",
        "Net Income yoy",
    ]
    empty_row = {col: "" for col in columns}

    rows = []
    with requests.Session() as s:
        i = 1
        num_of_tickers = 0
        for _, tickers in sector_to_tickers.items():
            num_of_tickers += len(tickers)

        for sector, tickers in sector_to_tickers.items():
            for t in tickers:
                print(t)
                ticker, pe, ps = fetch_pe_ps(s, t)
                rev_yoy, ebit_yoy, net_income_yoy, ebitda_yoy, rev_qoq, ebit_qoq, net_income_qoq, ebitda_qoq = fetch_rev_ebit(t)
                rows.append({
                    "sector" : sector,
                    "ticker": ticker,
                    "date": asof,
                    "TTM P/E (GAAP)": pe,
                    "TTM P/S (GAAP)": ps,
                    "rev yoy%": rev_yoy,
                    "EBIT yoy": ebit_yoy,
                    "EBITDA yoy": ebitda_yoy,
                    "Net Income yoy": net_income_yoy,
                    # "rev qoq%": rev_qoq,
                    # "EBIT qoq": ebit_qoq,
                    # "Net Income qoq": net_income_qoq,
                    # "EBITDA qoq": ebitda_qoq,
                })
                if i % 25 == 0:
                    print(f"Fetched {i}/{num_of_tickers}...")
                i += 1
            rows.append(empty_row.copy())
            # if i >= 25:
            #     break 

    df = pd.DataFrame(rows, columns=columns)
    output_dir = Path("ticker_ps_pe_revyoy_ebityoy_eyoy")
    output_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_dir / f"{asof}_.csv", index=False)
    df.to_csv(output_dir / f"{asof}_.tsv", sep="\t", index=False)
    # markdown table
    print(df.to_markdown(index=False))

if __name__ == "__main__":
    main()
