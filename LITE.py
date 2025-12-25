from dcf_input import DCFInputs
import numpy as np

# --- LITE baseline inputs (adjust freely) ---
dcf_inputs = DCFInputs(
    company_name="LITE",
    tax_rate=0.18,
    da_pct=0.06,
    capex_pct=0.07,
    nwc_pct_of_delta_rev=0.02,
    net_debt=2120.0,  # $m
    shares=78.3,  # m
    terminal_g=0.03,
    years=10,

    rev0=1645.0,  # $m
    rev_growth=np.array([0.30, 0.25, 0.20, 0.15, 0.12, 0.10, 0.08, 0.07, 0.06, 0.05]),

    # WACC & terminal margin grids
    wacc_grid=[0.075, 0.080, 0.085, 0.090, 0.095, 0.100, 0.105],

    ebit_margin_start=0.20,
    terminal_margin_grid=[0.26, 0.28, 0.30, 0.32, 0.34, 0.36, 0.38],
)
