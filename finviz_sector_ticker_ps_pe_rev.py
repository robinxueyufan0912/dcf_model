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
    "TPU": ["WULF", "CIFT", "GOOG", "AVGO", "TSM", "TTMI", "FLEX", "CLS", "MRVL", "APH", "LITE", "ADI", "FN", "VICR"],
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
    "rare earth": ["MP", "CRML", "UCU"],
    "material": ["AA", "ALB", "LAC", "FLNC"],
    "quatum": ["IBM", "IONQ", "RGTI"],
    "TMT - SAAS" : ["ORCL", "SAP", "CRM", "NOW", "ADBE", "INTU", "ADP", "WDAY", "ADSK", "TEAM", "HUBS", "TWLO", "ZM", "DOCU", "BILL", "GDDY", "WIX", "PATH"],
    "Fin - bank" : ["JPM", "BAC", "C", "BK", "MS", "GS"],
    "Fin - credit": ["AXP", "COF", "AFRM", "KLAR", "SOFI", "UPST", "FICO"],
    "Fin - payment": ["V", "MA", "FISV", "PYPL", "XYZ", "TOST"],
    "Fin - broker": ["SCHW", "IBKR", "HOOD", "COIN"],
    "aerospace & defense":["PLTR", "KTOS", "AVAV", "ONDS", "RTX", "LMT", "NOC", "GD", "HII", "CW", "BWXT", "LDOS", "BAH", "CACI"],
    "aerospace aftermarket": ["BA", "GE", "HWM", "TDG", "HEI", "CRS", "FTAI"],
    "police equipment": ["MSI", "AXON"],
    "auto manufactueres" :["TSLA", "RACE", "GM", "F", "CRPT"],
    "residential construction": ["DHI", "TOL", "KBH", "BLD", "IBP"],
    "home improvement retial": ["HD", "LOW", "BLDR", "W", "WSM"],
    "loging": ["MAR", "HLT", "H"],
    "airline": ["DAL", "UAL", "LUV", "AAL"],
    "resort & casinos": ["LVS", "WYNN", "MGM", "CZR"],
    "travel service": ["BKNG", "ABNB", "RCL", "EXPE", "MMYR", "YOU"],
    "gambling": ["FLUT", "DKNG"],
    "restaurants": ["MCD", "SBUX", "CMG", "DRI", "DPZ", "TXRH", "WING", "CAVA", "SG"],
    "telecom service": ["TMUS", "VZ", "T", "CMCSA", "ERIC"], 
    "discount stores": ["WMT", "COST", "TGT", "SFM"],
    "household & personal products": ["PG", "PM", "UL", "CL", "KMB", "KVUE", "ULTA", "ELF", "HIMS"],
    "utilities - regulated electric": ["NEE", "SO", "HE"],
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

def fetch_rev_ebit(ticker: str):
    pass

def main():
    asof = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()

    rows = []
    with requests.Session() as s:
        i = 1
        num_of_tickers = 0
        for _, tickers in sector_to_tickers.items():
            num_of_tickers += len(tickers)

        for sector, tickers in sector_to_tickers.items():
            for t in tickers:
                ticker, pe, ps = fetch_pe_ps(s, t)
                _ = fetch_rev_ebit(s)
                rows.append({
                    "sector" : sector,
                    "ticker": ticker,
                    "date": asof,
                    "TTM P/E (GAAP)": pe,
                    "TTM P/S (GAAP)": ps,
                })
                if i % 25 == 0:
                    print(f"Fetched {i}/{num_of_tickers}...")
                i += 1
            if i >= 25:
                break 

    df = pd.DataFrame(rows)
    output_dir = Path("ticker_ps_pe_revyoy_ebityoy_eyoy")
    output_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_dir / f"{asof}_.csv", index=False)
    df.to_csv(output_dir / f"{asof}_.tsv", sep="\t", index=False)
    # markdown table
    print(df.to_markdown(index=False))

if __name__ == "__main__":
    main()
