import numpy as np
import pandas as pd

from typing import Dict, List, Optional, Union

from dcf_input import DCFInputs


# ----------------------------
# 2) Core DCF engine (FCFF)
# ----------------------------
def _validate_inputs(wacc: float, g: float):
    if wacc <= g:
        raise ValueError(f"WACC must be > terminal g. Got WACC={wacc:.4f}, g={g:.4f}")


def build_margin_path(m_start: float, m_terminal: float, years: int) -> np.ndarray:
    """
    Linear path from m_start at year1 to m_terminal at yearN.
    """
    return np.linspace(m_start, m_terminal, years)


def project_fcff(
        inp: DCFInputs,
        growth_rates: Union[List[float], np.ndarray],
        ebit_margin_terminal: float
) -> pd.DataFrame:
    """
    Returns a dataframe with yearly Revenue, EBIT, NOPAT, D&A, Capex, ΔNWC, FCFF.
    growth_rates: length must equal inp.years (year1..yearN)
    """
    growth_rates = np.asarray(growth_rates, dtype=float)
    if growth_rates.shape[0] != inp.years:
        raise ValueError(f"growth_rates length must be {inp.years}, got {growth_rates.shape[0]}")

    years = inp.years
    rev = np.empty(years + 1)  # include rev0
    rev[0] = inp.rev0
    for t in range(1, years + 1):
        rev[t] = rev[t - 1] * (1.0 + growth_rates[t - 1])

    margin_path = build_margin_path(inp.ebit_margin_start, ebit_margin_terminal, years)
    ebit = rev[1:] * margin_path
    nopat = ebit * (1.0 - inp.tax_rate)

    da = rev[1:] * inp.da_pct
    capex = rev[1:] * inp.capex_pct

    delta_rev = rev[1:] - rev[:-1]
    delta_nwc = delta_rev * inp.nwc_pct_of_delta_rev

    fcff = nopat + da - capex - delta_nwc

    df = pd.DataFrame({
        "Year": np.arange(1, years + 1),
        "Revenue": rev[1:],
        "EBIT_margin": margin_path,
        "EBIT": ebit,
        "NOPAT": nopat,
        "D&A": da,
        "Capex": capex,
        "ΔNWC": delta_nwc,
        "FCFF": fcff
    })
    return df


def dcf_valuation(
        inp: DCFInputs,
        wacc: float,
        growth_rates: Union[List[float], np.ndarray],
        ebit_margin_terminal: float,
        terminal_g: Optional[float] = None,
        return_detail: bool = False
) -> Dict[str, Union[float, pd.DataFrame]]:
    """
    FCFF DCF valuation:
    EV = PV(FCFF_1..N) + PV(Terminal Value)
    Equity = EV - NetDebt
    PerShare = Equity / shares
    """
    g = inp.terminal_g if terminal_g is None else terminal_g
    _validate_inputs(wacc, g)

    df = project_fcff(inp, growth_rates, ebit_margin_terminal)

    years = inp.years
    discount_factors = 1.0 / (1.0 + wacc) ** df["Year"].values
    pv_fcff = df["FCFF"].values * discount_factors
    pv_explicit = pv_fcff.sum()

    fcff_n = df["FCFF"].values[-1]
    tv = fcff_n * (1.0 + g) / (wacc - g)
    pv_tv = tv / (1.0 + wacc) ** years

    ev = pv_explicit + pv_tv
    equity = ev - inp.net_debt
    per_share = equity / inp.shares

    out = {
        "EV": float(ev),
        "Equity": float(equity),
        "PerShare": float(per_share),
        "PV_Explicit": float(pv_explicit),
        "PV_Terminal": float(pv_tv),
        "TerminalValue": float(tv),
    }
    if return_detail:
        df = df.copy()
        df["DiscountFactor"] = discount_factors
        df["PV_FCFF"] = pv_fcff
        out["Forecast"] = df
    return out


# ----------------------------
# 3) Scenario helpers for revenue growth
# ----------------------------
def growth_shift(base_growth: Union[List[float], np.ndarray], shift_pp: float,
                 floor: float = -0.9, cap: float = 2.0) -> np.ndarray:
    """
    Shift every year's growth by +/-(percentage points).
    Example: shift_pp=0.05 means +5pp to each year's growth.
    """
    g = np.asarray(base_growth, dtype=float) + shift_pp
    return np.clip(g, floor, cap)


def growth_scale(base_growth: Union[List[float], np.ndarray], scale: float,
                 floor: float = -0.9, cap: float = 2.0) -> np.ndarray:
    """
    Multiply every year's growth by a scalar.
    Example: scale=1.2 means growth rates * 1.2
    """
    g = np.asarray(base_growth, dtype=float) * scale
    return np.clip(g, floor, cap)


# ----------------------------
# 4) Sensitivity matrices
# ----------------------------
def sensitivity_matrix_2d(
        inp: DCFInputs,
        wacc_grid: List[float],
        terminal_margin_grid: List[float],
        growth_rates: Union[List[float], np.ndarray],
        terminal_g: Optional[float] = None
) -> pd.DataFrame:
    """
    Returns a 2D matrix: rows=terminal EBIT margin, cols=WACC, values=PerShare
    """
    rows = []
    for m in terminal_margin_grid:
        row = []
        for w in wacc_grid:
            res = dcf_valuation(inp, w, growth_rates, m, terminal_g=terminal_g, return_detail=False)
            row.append(res["PerShare"])
        rows.append(row)

    mat = pd.DataFrame(rows, index=[f"{m:.0%}" for m in terminal_margin_grid],
                       columns=[f"{w:.1%}" for w in wacc_grid])
    mat.index.name = "Terminal EBIT Margin"
    mat.columns.name = "WACC"
    return mat


def sensitivity_matrix_3d(
        inp: DCFInputs,
        wacc_grid: List[float],
        terminal_margin_grid: List[float],
        growth_scenarios: Dict[str, Union[List[float], np.ndarray]],
        terminal_g: Optional[float] = None
) -> Dict[str, pd.DataFrame]:
    """
    Returns a dict of 2D matrices keyed by scenario name.
    Each matrix: rows=terminal EBIT margin, cols=WACC, values=PerShare
    """
    out = {}
    for name, gvec in growth_scenarios.items():
        out[name] = sensitivity_matrix_2d(inp, wacc_grid, terminal_margin_grid, gvec, terminal_g=terminal_g)
    return out


def sensitivity_matrix_multiindex(
        inp: DCFInputs,
        wacc_grid: List[float],
        terminal_margin_grid: List[float],
        growth_scenarios: Dict[str, Union[List[float], np.ndarray]],
        terminal_g: Optional[float] = None
) -> pd.DataFrame:
    """
    3D output in one table via MultiIndex:
    index = (Scenario, TerminalMargin), columns = WACC
    """
    frames = []
    for name, gvec in growth_scenarios.items():
        mat = sensitivity_matrix_2d(inp, wacc_grid, terminal_margin_grid, gvec, terminal_g=terminal_g)
        mat.index = pd.MultiIndex.from_product([[name], mat.index], names=["Scenario", "Terminal EBIT Margin"])
        frames.append(mat)
    return pd.concat(frames)


# ----------------------------
# 5) Example: LITE baseline + generate valuation matrices
# ----------------------------
if __name__ == "__main__":
    # Configure pandas to display full rows without truncation
    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', None)
    pd.set_option('display.max_colwidth', None)
    pd.set_option('display.expand_frame_repr', False)

    from LITE import dcf_inputs as company

    # 2D matrix for Base scenario
    mat_base = sensitivity_matrix_2d(
        inp=company,
        wacc_grid=company.wacc_grid,
        terminal_margin_grid=company.terminal_margin_grid,
        growth_rates=company.rev_growth
    )
    print(f"\n=== {company.company_name}: Base growth | PerShare matrix (Terminal EBIT margin x WACC) ===")
    print(mat_base.round(1))

    # Growth scenarios (you can add more)
    growth_scenarios = {
        "Bear(-5pp)": growth_shift(company.rev_growth, -0.05),
        "Base": company.rev_growth,
        "Bull(+5pp)": growth_shift(company.rev_growth, +0.05),
        "Scale x0.8": growth_scale(company.rev_growth, 0.8),
        "Scale x1.2": growth_scale(company.rev_growth, 1.2),
    }

    # 3D: multiple scenario matrices
    mats = sensitivity_matrix_3d(
        inp=company,
        wacc_grid=company.wacc_grid,
        terminal_margin_grid=company.terminal_margin_grid,
        growth_scenarios=growth_scenarios
    )

    # Combine into one big table (MultiIndex)
    mat_all = sensitivity_matrix_multiindex(
        inp=company,
        wacc_grid=company.wacc_grid,
        terminal_margin_grid=company.terminal_margin_grid,
        growth_scenarios=growth_scenarios
    )
    print("\n=== LITE: MultiIndex table (Scenario, TerminalMargin) x WACC ===")
    print(mat_all.round(1))

    # Optional: export to Excel
    with pd.ExcelWriter("LITE_DCF_sensitivity.xlsx") as writer:
        mat_base.to_excel(writer, sheet_name="Base_2D")
        for name, m in mats.items():
            sheet = name[:31]  # Excel sheet name limit
            m.to_excel(writer, sheet_name=sheet)
        mat_all.to_excel(writer, sheet_name="All_MultiIndex")

    print("\nSaved: LITE_DCF_sensitivity.xlsx")
