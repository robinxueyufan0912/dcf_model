#!/bin/zsh
set -e
set -u
set -o pipefail

cd /Users/yxue/workspace/dcf_model

source /Users/yxue/workspace/dcf_model/.venv/bin/activate
python /Users/yxue/workspace/dcf_model/vix_data_signal.py
python /Users/yxue/workspace/dcf_model/finviz_sector_ticker_ps_pe_rev.py
