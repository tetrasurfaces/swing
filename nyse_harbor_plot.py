# PRIVATE AND CONFIDENTIAL
# Copyright (c) 2025 Tetrasurfaces. All Rights Reserved.
#
# This file and all associated intellectual property (including harbor lines,
# nested sailfish pattern recognition, swing chain logic, multi-timeframe
# reversal systems, and trading models) are strictly confidential.
#
# Unauthorized distribution, disclosure, copying, or use is prohibited.
# Mutual Non-Disclosure Agreement applies to all collaborators.
#
# For internal backup and development use only.
# Last updated: 2026-04-23

#!/usr/bin/env python3
"""
NYSE 1h harbor plotter - with activation offsets + reversal signals
"""
import argparse
import pandas as pd
import yfinance as yf
import mplfinance as mpf
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

# ==================== Parameters ====================
MAN_OFFSET_SHORT    = 5
EIGHTH_OFFSET_SHORT = 9
MAN_OFFSET_LONG     = 22
EIGHTH_OFFSET_LONG  = 36

# Minimum chain lengths before we even consider the structure
MIN_CHAIN_SHORT     = 12
MIN_CHAIN_LONG      = 40
activation_offset_short = MIN_CHAIN_SHORT // 4
activation_offset_long  = MIN_CHAIN_LONG // 4
# ==================== Helpers ====================
def triplet_swings(df):
    highs = df["high"].values
    lows  = df["low"].values
    up_swings, down_swings = [], []

    for i in range(2, len(df)):
        if highs[i-1] > highs[i-2] and highs[i-1] > highs[i]:
            up_swings.append(i-1)
        if lows[i-1] < lows[i-2] and lows[i-1] < lows[i]:
            down_swings.append(i-1)

    return up_swings, down_swings


def get_harbor_lines(df, debug=False):
    lines = []
    _, down_swings = triplet_swings(df)

    if debug:
        print(f"[PARAMS] min_chain_short={MIN_CHAIN_SHORT}, act_offset_short={activation_offset_short}")
        print(f"[PARAMS] min_chain_long ={MIN_CHAIN_LONG},  act_offset_long ={activation_offset_long}")

    # ─── Short bias harbors (resistance → potential fade long) ──────────────
    short_noses = [
        i-1 for i in range(2, len(df))
        if df['high'].iloc[i-1] > df['high'].iloc[i-2] and
           df['high'].iloc[i-1] > df['high'].iloc[i]
    ]

    for nose_idx in short_noses:
        downs_after = [idx for idx in down_swings if idx > nose_idx]
        if len(downs_after) < MIN_CHAIN_SHORT:
            continue

        man_idx    = downs_after[MAN_OFFSET_SHORT]
        eighth_idx = downs_after[EIGHTH_OFFSET_SHORT]

        if eighth_idx >= len(df):
            continue

        if df['close'].iloc[eighth_idx] >= df['open'].iloc[eighth_idx]:
            continue  # prefer red candle

        a1_t = df.index[man_idx]
        a1_p = float(df['high'].iloc[man_idx])
        a2_t = df.index[eighth_idx]
        a2_p = float(df['close'].iloc[eighth_idx])

        delta_h = (a2_t - a1_t).total_seconds() / 3600.0
        if delta_h <= 0:
            continue

        slope = (a2_p - a1_p) / delta_h
        if slope >= -0.00005:  # flat or rising → skip
            continue

        # Activation time = time of the activation_offset-th down swing after nose
        act_idx = downs_after[activation_offset_short] if len(downs_after) > activation_offset_short else eighth_idx
        activation_time = df.index[act_idx]

        strike_t = None
        for k in range(eighth_idx + 1, len(df)):
            t_k     = df.index[k]
            hours_k = (t_k - a1_t).total_seconds() / 3600.0
            proj    = a1_p + slope * hours_k
            if df['high'].iloc[k] < proj:
                strike_t = t_k
                break

        end_t = strike_t if strike_t is not None else a2_t

        lines.append({
            'bias':           'short',
            'anchor1_time':   a1_t,
            'anchor1_price':  a1_p,
            'anchor2_time':   a2_t,
            'anchor2_price':  a2_p,
            'end_time':       end_t,
            'slope':          slope,
            'color':          'red',
            'style':          '--' if strike_t is None else '-',
            'alpha':          0.9 if strike_t else 0.6,
            'width':          2.0 if strike_t else 1.5,
            'strike_time':    strike_t,
            'activation_time': activation_time,   # ← new
        })

        if debug and strike_t:
            print(f"[Short] struck {strike_t} | activated after {activation_time}")

    # ─── Long bias harbors (support → potential fade short) ─────────────────
    long_noses = [
        i-1 for i in range(2, len(df))
        if df['low'].iloc[i-1] < df['low'].iloc[i-2] and
           df['low'].iloc[i-1] < df['low'].iloc[i]
    ]

    for nose_idx in long_noses:
        downs_after = [idx for idx in down_swings if idx > nose_idx]
        if len(downs_after) < MIN_CHAIN_LONG:
            continue

        man_idx    = downs_after[MAN_OFFSET_LONG]
        eighth_idx = downs_after[EIGHTH_OFFSET_LONG]

        if eighth_idx >= len(df):
            continue

        if df['close'].iloc[eighth_idx] <= df['open'].iloc[eighth_idx]:
            continue  # prefer green candle

        a1_t = df.index[man_idx]
        a1_p = float(df['low'].iloc[man_idx])
        a2_t = df.index[eighth_idx]
        a2_p = float(df['close'].iloc[eighth_idx])

        delta_h = (a2_t - a1_t).total_seconds() / 3600.0
        if delta_h <= 0:
            continue

        slope = (a2_p - a1_p) / delta_h
        if slope <= 0.00005:  # flat or falling → skip
            continue

        # Activation time
        act_idx = downs_after[activation_offset_long] if len(downs_after) > activation_offset_long else eighth_idx
        activation_time = df.index[act_idx]

        strike_t = None
        for k in range(eighth_idx + 1, len(df)):
            t_k     = df.index[k]
            hours_k = (t_k - a1_t).total_seconds() / 3600.0
            proj    = a1_p + slope * hours_k
            if df['low'].iloc[k] > proj:
                strike_t = t_k
                break

        end_t = strike_t if strike_t is not None else a2_t

        lines.append({
            'bias':           'long',
            'anchor1_time':   a1_t,
            'anchor1_price':  a1_p,
            'anchor2_time':   a2_t,
            'anchor2_price':  a2_p,
            'end_time':       end_t,
            'slope':          slope,
            'color':          'lime',
            'style':          '--' if strike_t is None else '-',
            'alpha':          0.9 if strike_t else 0.6,
            'width':          2.0 if strike_t else 1.5,
            'strike_time':    strike_t,
            'activation_time': activation_time,   # ← new
        })

        if debug and strike_t:
            print(f"[Long]  struck {strike_t} | activated after {activation_time}")

    return lines


def plot_chart(df, symbol, lines):
    addplots = []

    for line in lines:
        s = pd.Series(index=df.index, dtype=float)
        mask = (df.index >= line['anchor1_time']) & (df.index <= line['end_time'])
        if mask.sum() < 2:
            continue

        hours = (df.index[mask] - line['anchor1_time']).total_seconds() / 3600.0
        s[mask] = line['anchor1_price'] + line['slope'] * hours

        addplots.append(mpf.make_addplot(
            s,
            type='line',
            color=line['color'],
            linestyle=line['style'],
            alpha=line['alpha'],
            width=line['width'],
            label=f"{line['bias']} harbor ({line['slope']:.6f})"
        ))

    title = f"{symbol} 1h – Harbor Segments ({df.index[0]:%Y-%m-%d} to {df.index[-1]:%Y-%m-%d})"
    filename = f"{symbol}_1h_harbor_segments.png"

    mpf.plot(
        df,
        type='candle',
        style='charles',
        addplot=addplots or None,
        volume=True,
        title=title,
        figsize=(14, 9),
        savefig=filename,
        tight_layout=True
    )
    print(f"Chart saved → {filename}")


def get_reversal_signals(df, lines):
    sig_list = []
    for line in lines:
        if line.get('strike_time') is None:
            continue

        strike_t = line['strike_time']
        act_t    = line['activation_time']

        # Skip if strike happened before activation
        if strike_t < act_t:
            continue

        try:
            idx = df.index.get_loc(strike_t)
            price = float(df['close'].iloc[idx])

            # Reverse direction (fade the harbor)
            direction = -1 if line['bias'] == 'long' else 1

            sig_list.append({
                'time': strike_t,
                'dir': direction,
                'price': price,
                'harbor_bias': line['bias'],
                'activation_time': act_t,
            })
        except Exception:
            pass

    return sorted(sig_list, key=lambda x: x['time'])


def backtest_reversal_pnl_pct(df, signals):
    if not signals:
        return 0.0

    equity = 1.0
    pos_dir = 0
    entry = 0.0

    for sig in signals:
        price = sig['price']

        if pos_dir != 0:
            if pos_dir == sig['dir']:
                continue

            ret = (price - entry) / entry * pos_dir
            equity *= (1 + ret)

        pos_dir = sig['dir']
        entry = price

    if pos_dir != 0:
        last_price = float(df['close'].iloc[-1])
        ret = (last_price - entry) / entry * pos_dir
        equity *= (1 + ret)

    return (equity - 1) * 100


def main():
    parser = argparse.ArgumentParser(description="1h NYSE harbor lines + reversal (fade) backtest")
    parser.add_argument("--symbol", default="JPM")
    parser.add_argument("--days-back", type=int, default=75)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    print(f"Fetching {args.symbol} 1h (~last {args.days_back} days)...")

    ticker = yf.Ticker(args.symbol)
    df = ticker.history(
        period=f"{args.days_back}d",
        interval="1h",
        auto_adjust=True,
        prepost=False
    )

    if df.empty or len(df) < 150:
        print("Not enough data returned.")
        return

    df.columns = [col.lower() for col in df.columns]
    try:
        df = df[['open', 'high', 'low', 'close', 'volume']].copy()
    except KeyError as e:
        print("Missing expected columns:", e)
        print("Available columns:", list(df.columns))
        return

    df.index.name = 'Date'

    if args.debug:
        print(f"Bars: {len(df)} | Range: {df.index.min():%Y-%m-%d %H:%M} → {df.index.max():%Y-%m-%d %H:%M}")
        print(df.tail(3))

    lines = get_harbor_lines(df, debug=args.debug)
    print(f"Detected {len(lines)} harbor lines.")

    plot_chart(df, args.symbol, lines)

    signals = get_reversal_signals(df, lines)
    print(f"Reversal signals (after activation): {len(signals)}")

    pnl_pct = backtest_reversal_pnl_pct(df, signals)
    print(f"Simulated reversal/fade PNL %: {pnl_pct:+.2f}%")

if __name__ == "__main__":
    main()
