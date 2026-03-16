## NYSE Harbor Plotter

Detects sloped exhaustion / mean-reversion "harbor" lines from swing chains on 1h NYSE data.  
Fades the break (reversal signal) after activation delay.

## Quick start

```bash
pip install yfinance mplfinance pandas numpy matplotlib argparse
python nyse_harbor_plot.py --symbol SMCI --days-back 60 --debug
