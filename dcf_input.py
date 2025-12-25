from dataclasses import dataclass
import numpy as np
from typing import List


# ----------------------------
# 1) Model inputs
# ----------------------------
@dataclass
class DCFInputs:
    company_name: str

    rev0: float  # base year revenue (same units throughout, e.g., $m)
    tax_rate: float  # effective cash tax rate, e.g. 0.18
    da_pct: float  # D&A as % of revenue, e.g. 0.06
    capex_pct: float  # Capex as % of revenue, e.g. 0.07
    nwc_pct_of_delta_rev: float  # ΔNWC = (ΔRevenue) * this%, e.g. 0.02
    ebit_margin_start: float  # year1 EBIT margin start (or base), e.g. 0.20
    net_debt: float  # EV -> Equity adjustment (Net Debt), same units as revenue ($m)
    shares: float  # diluted shares (m)
    terminal_g: float = 0.03  # terminal growth, e.g. 0.03
    years: int = 10  # explicit forecast horizon

    rev_growth: np.ndarray = None
    ebit_margin_terminal: float = None
    wacc_grid: List[float] = None
    terminal_margin_grid: List[float] = None
