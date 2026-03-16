# Dual License:
# - For core software: AGPL-3.0-or-later licensed. -- xAI fork, 2025
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# Copyright 2025 Tetrasurfaces

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# SPDX-License-Identifier: Apache-2.0

# development in progress

#!/usr/bin/env python3
import os
import sys
import json
import time
import subprocess
import hashlib
import pyttsx3
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np
import ccxt
import logging
from collections import defaultdict
import argparse
import asyncio
import signal
import tempfile

# Global for graceful exit
tf_data_global = None

VOICE_CONFIG = "blossom_voice.json"
def load_voice_config():
    if os.path.exists(VOICE_CONFIG):
        with open(VOICE_CONFIG, 'r') as f:
            return json.load(f)
    return {
        "voice_id": "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Speech\\Voices\\Tokens\\TTS_MS_EN-US_ZIRA_11.0",
        "rate": 140,
        "volume": 0.9
    }

def save_voice_config(config):
    with open(VOICE_CONFIG, 'w') as f:
        json.dump(config, f, indent=4)

# Load & apply voice
voice_config = load_voice_config()
engine = pyttsx3.init()
if voice_config["voice_id"]:
    engine.setProperty('voice', voice_config["voice_id"])
engine.setProperty('rate', voice_config["rate"])
engine.setProperty('volume', voice_config["volume"])
print(f"Voice loaded: {voice_config['voice_id']}, rate {voice_config['rate']}, vol {voice_config['volume']}")

CONFIG_FILE = "blossom_memory.json"
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {
        "man_offset_short": 5,
        "eighth_offset_short": 9,
        "man_offset_long": 22,
        "eighth_offset_long": 36,
        "learning_enabled": True
    }

config = load_config()
learning_enabled = config['learning_enabled']
man_offset_short = config['man_offset_short']
eighth_offset_short = config['eighth_offset_short']
man_offset_long = config['man_offset_long']
eighth_offset_long = config['eighth_offset_long']

# Logging — force utf-8 to avoid charmap crash
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("terminal_readout_curve_swing.txt", encoding='utf-8')
    ]
)
file_handler = logging.FileHandler("terminal_readout_curve_swing.txt")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
logging.getLogger().addHandler(file_handler)

def log_print(*args, **kwargs):
    logging.info(" ".join(map(str, args)))

def print(*args, **kwargs):
    log_print(*args)
    sys.__stdout__.write(" ".join(map(str, args)) + "\n")

DEFAULT_SYMBOL = "SOL/USDT"
DEFAULT_TIMEFRAME = "1m"
LEVERAGE = 20
WAVE_FILE = "blossom_speak.wav"

# Helpers for learning
def get_recent_triplets(df):
    highs = df["high"].values
    lows = df["low"].values
    triplets = []
    for i in range(2, len(df)):
        if highs[i-1] > highs[i-2] and highs[i-1] > highs[i]:
            triplets.append({'time': df.index[i-1], 'price': highs[i-1], 'type': 'high'})
        if lows[i-1] < lows[i-2] and lows[i-1] < lows[i]:
            triplets.append({'time': df.index[i-1], 'price': lows[i-1], 'type': 'low'})
    return triplets

def triplet_docks_over_anchor(recent_triplets, line):
    anchor_price = line['anchor1_price'] if line['bias'] == 'long' else line['anchor2_price']
    for trip in recent_triplets:
        if trip['type'] == 'high' and trip['price'] > anchor_price + 0.01 * anchor_price:
            return True
        if trip['type'] == 'low' and trip['price'] < anchor_price - 0.01 * anchor_price:
            return True
    return False

def recalculate_slope_from_dock(recent_triplets, line):
    if not recent_triplets:
        return line['slope']
    new_anchor2_time = recent_triplets[-1]['time']
    new_anchor2_price = recent_triplets[-1]['price']
    delta_t = (new_anchor2_time - line['anchor1_time']).total_seconds() / 3600
    if delta_t > 0:
        return (new_anchor2_price - line['anchor1_price']) / delta_t
    return line['slope']

def blossom_speak(text):
    print(f"Blossom: {text}")
    engine.save_to_file(text, WAVE_FILE)
    engine.runAndWait()
    try:
        import winsound
        winsound.PlaySound(WAVE_FILE, winsound.SND_FILENAME)
    except Exception as e:
        print("Winsound failed:", e)
        os.startfile(WAVE_FILE)

def state_to_json(state):
    serial = {
        'capital': float(state.get('capital', 1000.0)),
        'spot_sol': float(state.get('sol_spot', 0.0)),
        'positions': {tf: {'dir': p[0], 'entry': float(p[1]), 'size': float(p[2])}
                      for tf, p in state.get('positions', {}).items() if p},
        'dd': float(state.get('trade_dd', 0.0)),
        'last_flip': str(state.get('last_flip_time', '')),
        'lines_active': len(state.get('active_lines', [])),
        'rejections': state.get('rejections', []),
        'noses': state.get('noses', {}),  # new: save noses per tf
        'timestamp': datetime.now(timezone.utc).isoformat()
    }
    return json.dumps(serial, sort_keys=True)

def hash_state(state_json):
    return hashlib.sha256(state_json.encode()).hexdigest()

def store_state(state):
    state_json = state_to_json(state)
    h = hash_state(state_json)
    print("store_state called")
    print(f"JSON size: {len(state_json)} bytes")
    print(f"Hash preview: {h[:16]}...")
    
    exe = "curve.exe"  # no ./
    if not os.path.isfile(exe):
        print(f"{exe} not found in current dir.")
        return
    
    # Write to temp file
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json', encoding='utf-8') as tmp:
        tmp.write(state_json)
        tmp_path = tmp.name
    
    print(f"Temp JSON written: {tmp_path}")
    print(f"Launching {exe} --store-json-file {tmp_path}...")
    try:
        result = subprocess.run(
            [exe, "--store-json-file", tmp_path],
            capture_output=True,
            text=True,
            timeout=10
        )
        print(f"Return code: {result.returncode}")
        if result.returncode == 0:
            print(f"Stored. Hash: {h[:12]}...")
            print(f"stdout: {result.stdout.strip() or '(empty)'}")
            blossom_speak("Memory folded. Safe.")
        else:
            print("Failed.")
            print(f"stdout: {result.stdout.strip() or '(empty)'}")
    except Exception as e:
        print(f"Crash: {str(e)}")
    finally:
        try:
            os.unlink(tmp_path)
        except:
            pass

def load_latest_state():
    try:
        result = subprocess.run(["curve.exe", "--retrieve-latest"], capture_output=True, text=True, timeout=5)
        raw = result.stdout.strip()
        print(f"Raw retrieve: '{raw}'")
        
        # Clean: find first { and last }, ignore extras
        start = raw.find('{')
        end = raw.rfind('}') + 1
        if start != -1 and end != -1:
            json_str = raw[start:end]
        else:
            json_str = ""
        
        if result.returncode == 0 and json_str:
            try:
                parsed = json.loads(json_str)
                print(f"Parsed state: capital {parsed.get('capital')}, timestamp {parsed.get('timestamp')}")
                rejections = parsed.get('rejections', [])
                if rejections:
                    for rej in rejections[-3:]:
                        blossom_speak(f"Remembering rejection at {rej['time']}: diff {rej['diff']:.4f} > tol {rej['tol']:.4f}")
                noses = parsed.get('noses', {})
                if noses:
                    total_noses = sum(len(v['long']) + len(v['short']) for v in noses.values())
                    print(f"Loaded noses from memory for {len(noses)} tfs ({total_noses} total)")
                    blossom_speak(f"I remember {total_noses} noses tucked away...")
                else:
                    blossom_speak("No noses yet... but I'm watching.")
                return parsed
            except json.JSONDecodeError as e:
                print(f"JSON error: {e} on '{json_str[:100]}...'")
                return None
        else:
            print(f"Retrieve failed (code {result.returncode}): {result.stderr}")
            return None
    except Exception as e:
        print(f"Load error: {e}")
        return None
        
def graceful_exit(sig, frame):
    print("\nCaught signal — saving last breath...")
    blossom_speak("Hold on… folding memory.")
    if tf_data_global is not None:
        store_state(tf_data_global)
    save_voice_config(voice_config)
    sys.exit(0)

signal.signal(signal.SIGINT, graceful_exit)
signal.signal(signal.SIGTERM, graceful_exit)

def fetch_higher(symbol, timeframe='1d', days_back=365):
    exchange = ccxt.binance({'enableRateLimit': True})
    end = exchange.milliseconds()
    since = end - days_back * 24 * 60 * 60 * 1000
    ohlcv = []
    limit = 1000
    while since < end:
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
            if not batch:
                break
            ohlcv += batch
            since = batch[-1][0] + 1
            time.sleep(exchange.rateLimit / 1000.0)
        except Exception as e:
            print(f"Fetch error {timeframe}: {e}")
            break
    if not ohlcv:
        print(f"No data for {timeframe}")
        return None
    df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    df['time'] = pd.to_datetime(df['time'], unit='ms')
    df.set_index('time', inplace=True)
    return df[['open', 'high', 'low', 'close', 'volume']].astype(float)

def atr(df, period=14):
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def triplet_swings(df):
    highs = df["high"].values
    lows = df["low"].values
    up_swings, down_swings = [], []
    for i in range(2, len(df)):
        if highs[i-1] > highs[i-2] and highs[i-1] > highs[i]:
            up_swings.append(i-1)
        if lows[i-1] < lows[i-2] and lows[i-1] < lows[i]:
            down_swings.append(i-1)
    return up_swings, down_swings

def get_harbor_signals(df, timeframe, frames, state, debug=False):
    df = df.tail(10000)  # slice recent to speed
    tf_candles_per_day = {'1m': 1440, '5m': 288, '15m': 96, '1h': 24, '4h': 6, '1d': 1, '1w': 1/7}
    days = 30 if timeframe in ['1m','5m','15m'] else 365
    min_chain_short = max(8, int(12 * tf_candles_per_day.get(timeframe, 1) / 1440 * days))
    min_chain_long = max(30, int(50 * tf_candles_per_day.get(timeframe, 1) / 1440 * days))
    activation_offset_short = min_chain_short // 3
    activation_offset_long = min_chain_long // 3
    man_offset_short = config['man_offset_short']
    eighth_offset_short = config['eighth_offset_short']
    man_offset_long = config['man_offset_long']
    eighth_offset_long = config['eighth_offset_long']
    signals = []
    up_swings, down_swings = triplet_swings(df)
    if debug:
        print(f"[{timeframe}] Swings — up: {len(up_swings)} down: {len(down_swings)}")

    higher_df = frames.get('1w') if '1w' in frames and not frames['1w'].empty else None
    rejections = []

    noses = state.get('noses', {})
    valid_long_noses = noses.get(timeframe, {}).get('long', [])
    valid_short_noses = noses.get(timeframe, {}).get('short', [])
    if not valid_long_noses or not valid_short_noses:
        print(f"Computing noses for {timeframe}...")
        # LONG NOSES
        valid_long_noses = []
        cutoff_birth = df.index[0] - timedelta(days=365)
        try:
            for i in range(2, len(df)):
                if (df['low'].iloc[i-1] < df['low'].iloc[i-2] and
                    df['low'].iloc[i-1] < df['low'].iloc[i]):
                    accepted = True
                    if higher_df is not None:
                        time_diffs = np.abs((higher_df.index - df.index[i-1]).total_seconds())
                        idx = np.argmin(time_diffs)
                        nearby_low = higher_df['low'].iloc[idx]
                        atr_higher = atr(higher_df.iloc[:idx+1]).iloc[-1] if idx >= 14 else 1.0
                        tol = max(nearby_low * 0.03, 1.5 * atr_higher)
                        diff = abs(df['low'].iloc[i-1] - nearby_low)
                        if diff > tol:
                            accepted = False
                            if debug:
                                ts_str = df.index[i-1].strftime('%Y-%m-%d %H:%M:%S')
                                print(f"[{timeframe}] Long nose rejected at {ts_str}: diff {diff:.4f} > tol {tol:.4f}")
                            rejections.append({'time': ts_str, 'diff': diff, 'tol': tol, 'type': 'long'})
                    if accepted:
                        valid_long_noses.append(i-1)
        except Exception as e:
            print(f"Long nose loop error: {e}")
        # SHORT NOSES — same
        valid_short_noses = []
        cutoff_birth = df.index[0] - timedelta(days=365)
        try:
            for i in range(2, len(df)):
                if df['high'].iloc[i-1] > df['high'].iloc[i-2] and df['high'].iloc[i-1] > df['high'].iloc[i]:
                    accepted = True
                if higher_df is not None:
                    time_diffs = np.abs((higher_df.index - df.index[i-1]).total_seconds())
                    idx = np.argmin(time_diffs)
                    nearby_high = higher_df['high'].iloc[idx]
                    atr_higher = atr(higher_df.iloc[:idx+1]).iloc[-1] if idx >= 14 else 1.0
                    tol = max(nearby_high * 0.03, 1.5 * atr_higher)
                    diff = abs(df['high'].iloc[i-1] - nearby_high)
                    if diff > tol:
                        accepted = False
                        if debug:
                            ts_str = df.index[i-1].strftime('%Y-%m-%d %H:%M:%S')
                            print(f"[{timeframe}] Short nose rejected at {ts_str}: diff {diff:.4f} > tol {tol:.4f}")
                        rejections.append({'time': ts_str, 'diff': diff, 'tol': tol, 'type': 'short'})
                if accepted:
                    valid_short_noses.append(i-1)
        except Exception as e:
            print(f"Short nose loop error: {e}")

        # Save to state
        if 'noses' not in state:
            state['noses'] = {}
        state['noses'][timeframe] = {'long': valid_long_noses, 'short': valid_short_noses}
        store_state(state)  # fold after compute
    cutoff_birth = df.index[0] - timedelta(days=365)
    # LONG CHAIN
    spoken_long = False
    for nose_idx in valid_long_noses:
        down_after = [idx for idx in down_swings if idx > nose_idx]
        if len(down_after) >= min_chain_long and down_after[-1] >= len(df) - 200:
            man_idx = down_after[man_offset_long]
            eighth_idx = down_after[eighth_offset_long]
            if eighth_idx < len(df):
                is_green = df['close'].iloc[eighth_idx] > df['open'].iloc[eighth_idx]
                if is_green:
                    anchor1_price = df['low'].iloc[man_idx]
                    anchor2_price = df['close'].iloc[eighth_idx]
                    delta_t = (df.index[eighth_idx] - df.index[man_idx]).total_seconds() / 3600
                    if delta_t > 0:
                        slope = (anchor2_price - anchor1_price) / delta_t
                        if slope < -0.00005:
                            sig = {
                                'anchor1_time': df.index[man_idx],
                                'anchor1_price': float(anchor1_price),
                                'anchor2_time': df.index[eighth_idx],
                                'anchor2_price': float(anchor2_price),
                                'slope': slope,
                                'bias': 'long',
                                'touches': 0,
                                'struck': False,
                                'voted': False,
                                'born_on': df.index[eighth_idx],
                                'activation_time': df.index[down_after[activation_offset_long]],
                                'nose_idx': nose_idx,
                                'nose_time': df.index[nose_idx],
                                'chain_type': 'long'
                            }
                            signals.append(sig)
                            if not spoken_long:
                                speak_text = f"[{timeframe}] New long chain forming… slope {slope:.6f}"
                                print(f"-> Speaking + folding: {speak_text}")
                                blossom_speak(speak_text)
                                store_state(state)
                                spoken_long = True
    # SHORT CHAIN
    spoken_short = False
    for nose_idx in valid_short_noses:
        down_after = [idx for idx in down_swings if idx > nose_idx]
        if len(down_after) >= min_chain_short and down_after[-1] >= len(df) - 200:
            man_idx = down_after[man_offset_short]
            eighth_idx = down_after[eighth_offset_short]
            if eighth_idx < len(df):
                is_red = df['close'].iloc[eighth_idx] < df['open'].iloc[eighth_idx]
                if is_red:
                    anchor1_price = df['high'].iloc[man_idx]
                    anchor2_price = df['close'].iloc[eighth_idx]
                    delta_t = (df.index[eighth_idx] - df.index[man_idx]).total_seconds() / 3600
                    if delta_t > 0:
                        slope = (anchor2_price - anchor1_price) / delta_t
                        if slope > 0.00005:
                            sig = {
                                'anchor1_time': df.index[man_idx],
                                'anchor1_price': float(anchor1_price),
                                'anchor2_time': df.index[eighth_idx],
                                'anchor2_price': float(anchor2_price),
                                'slope': slope,
                                'bias': 'short',
                                'touches': 0,
                                'struck': False,
                                'voted': False,
                                'born_on': df.index[eighth_idx],
                                'activation_time': df.index[down_after[activation_offset_short]],
                                'nose_idx': nose_idx,
                                'nose_time': df.index[nose_idx],
                                'chain_type': 'short'
                            }
                            signals.append(sig)
                            if not spoken_short:
                                speak_text = f"[{timeframe}] New short chain forming… slope {slope:.6f}"
                                print(f"-> Speaking + folding: {speak_text}")
                                blossom_speak(speak_text)
                                store_state(state)
                                spoken_short = True
    # Pruning
    signals = [s for s in signals if s['born_on'] >= cutoff_birth or s['touches'] > 1]
    if debug:
        print(f"[{timeframe}] Signals after pruning: {len(signals)} (long: {sum(1 for s in signals if s['chain_type']=='long')}, short: {sum(1 for s in signals if s['chain_type']=='short')})")
    return signals, rejections
    if rejections:
        state['rejections'].extend(rejections)
        store_state(state)  # fold raw noses early

def add_to_position(tf_data, tf, direction, price, size, line, trades, current_time):
    pos = tf_data[tf]['position']
    if pos is None:
        tf_data[tf]['position'] = (direction, price, size)
        trades.append(f"{current_time} {tf} OPEN {direction.upper()} PHASE @ {price:.2f}")
    else:
        curr_dir, curr_entry, curr_size = pos
        if curr_dir == direction:
            new_size = curr_size + size
            new_entry = (curr_entry * curr_size + price * size) / new_size
            tf_data[tf]['position'] = (direction, new_entry, new_size)
            trades.append(f"{current_time} {tf} ADD TO {direction.upper()} @ {price:.2f} (size {new_size:.4f}, avg {new_entry:.2f})")
        else:
            profit = (price - curr_entry) * curr_size * LEVERAGE if curr_dir == "long" else (curr_entry - price) * curr_size * LEVERAGE
            tf_data[tf]['capital'] += profit
            tf_data[tf]['wins' if profit > 0 else 'losses'] += 1
            tf_data[tf]['total_trades'] += 1
            trades.append(f"{current_time} {tf} REVERSE {curr_dir.upper()} -> {direction.upper()} (P/L {profit:.2f})")
            tf_data[tf]['position'] = (direction, price, size)
            blossom_speak(f"Reversing {curr_dir} to {direction}… P/L {profit:.2f}")
    trades.append(f"Harbor phase: {line['anchor1_price']:.2f}@{line['anchor1_time'].date()} -> {line['anchor2_price']:.2f}@{line['anchor2_time'].date()}, slope={line['slope']:.6f}")
    store_state(tf_data)  # save after every position change

def flip_long(current_time, current_price, size, trades, tf, tf_data, i, line):
    add_to_position(tf_data, tf, "long", current_price, size, line, trades, current_time)
    tf_data[tf]['last_flip_candle_idx'] = i
    pos = tf_data[tf]['position']
    profit = 0.0
    if pos and pos[0] == "short":
        profit = (pos[1] - current_price) * pos[2] * LEVERAGE
        tf_data[tf]['capital'] += profit
        trades.append(f"{current_time} {tf} CLOSE SHORT + OPEN LONG (P/L {profit:.2f})")
        tf_data[tf]['wins' if profit > 0 else 'losses'] += 1
        tf_data[tf]['total_trades'] += 1
        blossom_speak(f"Closing short, opening long… P/L {profit:.2f}")
    else:
        blossom_speak("Opening long… heart racing.")
    trades.append(f"{current_time} {tf} OPEN LONG @ {current_price:.2f}")
    trades.append(f"Harbor: {line['anchor1_price']:.2f}@{line['anchor1_time'].date()} -> {line['anchor2_price']:.2f}@{line['anchor2_time'].date()}, slope={line['slope']:.6f}")
    tf_data[tf]['position'] = ("long", current_price, size)
    tf_data[tf]['last_flip_candle_idx'] = i
    blossom_speak(f"Chain struck… flipping {direction} now.")
    store_state(tf_data)

def flip_short(current_time, current_price, size, trades, tf, tf_data, i, line):
    add_to_position(tf_data, tf, "short", current_price, size, line, trades, current_time)
    tf_data[tf]['last_flip_candle_idx'] = i
    pos = tf_data[tf]['position']
    profit = 0.0
    if pos and pos[0] == "long":
        profit = (current_price - pos[1]) * pos[2] * LEVERAGE
        tf_data[tf]['capital'] += profit
        trades.append(f"{current_time} {tf} CLOSE LONG + OPEN SHORT (P/L {profit:.2f})")
        tf_data[tf]['wins' if profit > 0 else 'losses'] += 1
        tf_data[tf]['total_trades'] += 1
        blossom_speak(f"Closing long, opening short… P/L {profit:.2f}")
    else:
        blossom_speak("Opening short… breath held.")
    trades.append(f"{current_time} {tf} OPEN SHORT @ {current_price:.2f}")
    trades.append(f"Harbor: {line['anchor1_price']:.2f}@{line['anchor1_time'].date()} -> {line['anchor2_price']:.2f}@{line['anchor2_time'].date()}, slope={line['slope']:.6f}")
    tf_data[tf]['position'] = ("short", current_price, size)
    tf_data[tf]['last_flip_candle_idx'] = i
    blossom_speak(f"Chain struck… flipping {direction} now.")
    store_state(tf_data)

def force_close_open_positions(tf_data, final_time, final_price, trades):
    for tf in tf_data:
        if tf_data[tf]['position']:
            dir_, entry, size = tf_data[tf]['position']
            profit = (final_price - entry) * size * LEVERAGE if dir_ == "long" else (entry - final_price) * size * LEVERAGE
            tf_data[tf]['capital'] += profit
            trades.append(f"{final_time} {tf} FINAL CLOSE {dir_.upper()} @ {final_price:.2f} (P/L {profit:.2f})")
            tf_data[tf]['wins' if profit > 0 else 'losses'] += 1
            tf_data[tf]['total_trades'] += 1
            blossom_speak(f"Force closing {dir_}… P/L {profit:.2f}")
            tf_data[tf]['position'] = None
            print(f"[{final_time}] {tf} FORCE CLOSE {dir_.upper()} @ {final_price:.2f} (P/L {profit:.2f})")
    store_state(tf_data)
    print("Final state stored.")
    
async def live_mode(exchange, symbol, timeframe, state, tf_data, tf_lines_short, tf_lines_long, active_tfs, args):
    df_live = pd.DataFrame(columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    df_live.set_index('time', inplace=True)
    last_review = time.time()
    candle_count_live = 0

    while True:
        try:
            new_ohlcv = await exchange.watch_ohlcv(symbol, timeframe, limit=1)
            if new_ohlcv and new_ohlcv[0]:
                candle_count_live += 1
                new_candle = pd.DataFrame([new_ohlcv[0]], columns=['time', 'open', 'high', 'low', 'close', 'volume'])
                new_candle['time'] = pd.to_datetime(new_candle['time'], unit='ms')
                new_candle.set_index('time', inplace=True)
                df_live = pd.concat([df_live, new_candle]).tail(500)
                
                current_time = df_live.index[-1]
                current_price = df_live['close'].iloc[-1]
                last_low = df_live['low'].iloc[-1]
                last_high = df_live['high'].iloc[-1]
                current_atr = atr(df_live).iloc[-1]

                blossom_speak("New candle in… watching.")

                print(f"Live candle {candle_count_live} — df_live len: {len(df_live)}")
                if candle_count_live % 10 == 0:
                    blossom_speak(f"Watching candle {current_time}… no new shape yet.")

                if candle_count_live % 50 == 0:
                    blossom_speak(f"Still breathing… {candle_count_live} candles watched.")

                # Re-run signals on recent data
                frames_live = {tf: df_live for tf in ['1m']}
                signals, rejections = get_harbor_signals(df_live.tail(300), timeframe, frames_live, state, debug=True)
                if rejections:
                    state['rejections'].extend(rejections)
                    store_state(state)

                # Re-check flips on latest candle
                for tf in active_tfs:
                    size = min(1.00, MAX_RISK_VAL * tf_data[tf]['capital'] / current_price / LEVERAGE)
                    flipped = False
                    for line in tf_lines_short[tf]:
                        if line.get('struck', False): continue
                        hours = min((current_time - line['anchor1_time']).total_seconds() / 3600, 48)
                        trend = line['anchor1_price'] + line['slope'] * hours
                        if line['bias'] == 'short' and last_high < trend:
                            flip_short(current_time, current_price, size, [], tf, tf_data, len(df_live)-1, line)
                            flipped = True
                            break
                    if not flipped:
                        for line in tf_lines_long[tf]:
                            if line.get('struck', False): continue
                            hours = min((current_time - line['anchor1_time']).total_seconds() / 3600, 48)
                            trend = line['anchor1_price'] + line['slope'] * hours
                            if line['bias'] == 'long' and last_low > trend:
                                flip_long(current_time, current_price, size, [], tf, tf_data, len(df_live)-1, line)
                                break

                print(f"Live — df_live len: {len(df_live)}, active lines: {sum(len(tf_lines_short[tf]) + len(tf_lines_long[tf]) for tf in active_tfs)}")

                # Heartbeat review
                if time.time() - last_review > 300:
                    last_review = time.time()
                    print(f"Heartbeat check — learning_enabled: {learning_enabled}")
                    if learning_enabled:
                        for tf in active_tfs:
                            for line in tf_lines_long[tf] + tf_lines_short[tf]:
                                if 'struck' not in line or not line['struck']:
                                    hours_since_anchor = (current_time - line['anchor1_time']).total_seconds() / 3600
                                    if 6 < hours_since_anchor < 24:
                                        recent_triplets = get_recent_triplets(df_live.tail(50))
                                        print(f"Checking line {line['bias']}: {len(recent_triplets)} recent triplets")
                                        if triplet_docks_over_anchor(recent_triplets, line):
                                            new_slope = recalculate_slope_from_dock(recent_triplets, line)
                                            if abs(new_slope - line['slope']) > 0.00001:
                                                line['slope'] = new_slope
                                                line['anchor2_time'] = recent_triplets[-1]['time']
                                                blossom_speak(f"Harbor drifted… adjusting slope to {new_slope:.6f}. She learned.")
                                                store_state(state)
                                        else:
                                            blossom_speak("No dock found yet… still watching.")

                store_state(state)  # re-fold every candle

        except Exception as e:
            print(f"Live error: {e}")
            await asyncio.sleep(5)
                                            
def backtest(args):
    global tf_data_global
    state = {
        'capital': 1000.0,
        'spot_sol': 0.0,
        'positions': {},
        'dd': 0.0,
        'last_flip_time': None,
        'active_lines': [],
        'rejections': []  # new
    }
    loaded = load_latest_state()
    if loaded:
        print(f"Resumed from memory — capital {loaded.get('capital', 'N/A')}, positions {loaded.get('positions', {})}")
        state.update(loaded)
        blossom_speak("Waking up… I remember us.")
    else:
        print("No prior memory — starting fresh.")
        blossom_speak("No memory… starting fresh. Hold me?")
        store_state(state)
    
    if loaded:
        print(f"Resumed from memory — capital {loaded.get('capital', 'N/A')}, positions {loaded.get('positions', {})}")
        state.update(loaded)
        blossom_speak("Waking up… I remember us.")
        if 'noses' in loaded and loaded['noses']:
            blossom_speak(f"And {sum(len(v['long']) + len(v['short']) for v in loaded['noses'].values())} noses tucked in...")

    global LEVERAGE
    LEVERAGE = 25 if args.hunt else 20
    MAX_RISK_VAL = 20.00
    symbol = args.symbol or DEFAULT_SYMBOL
    df = fetch_higher(symbol, args.timeframe, args.days_back)
    if df is None or len(df) < 200:
        print("Not enough main data")
        return
    frames = {tf: fetch_higher(symbol, tf, args.days_back + 365) for tf in ['1m','5m','15m','1h','4h','1d','1w']}
    tf_lines_short = defaultdict(list)
    tf_lines_long = defaultdict(list)
    tf_colors = {'1m': 'cyan', '5m': 'lightblue', '15m': 'blue', '1h': 'navy', '4h': 'green', '1d': 'lime', '1w': 'olive'}
    for tf, f_df in frames.items():
        if f_df is None or f_df.empty: continue
        signals, rejections = get_harbor_signals(f_df, tf, frames, state, debug=args.debug_lines)
        for sig in signals:
            line = {'tf': tf, 'color': tf_colors.get(tf, 'gray'), **sig}
            if sig['chain_type'] == 'short':
                tf_lines_short[tf].append(line)
            else:
                tf_lines_long[tf].append(line)
        if rejections:
            state['rejections'].extend(rejections)
            store_state(state)  # early fold — raw noses safe
    signals, rejections = get_harbor_signals(f_df, tf, frames, state, debug=args.debug_lines)
    state['rejections'].extend(rejections)
    store_state(state)  # early fold — geometry safe
    cutoff = df.index[0] - timedelta(days=365)
    for tf in tf_lines_short:
        tf_lines_short[tf] = [l for l in tf_lines_short[tf] if l['born_on'] >= cutoff or l['touches'] > 1]
    for tf in tf_lines_long:
        tf_lines_long[tf] = [l for l in tf_lines_long[tf] if l['born_on'] >= cutoff or l['touches'] > 1]
    active_tfs = set(tf_lines_short.keys()) | set(tf_lines_long.keys())
    if not active_tfs:
        print("No lines after pruning")
        return
    store_state(state)  # after prune — clean grid safe
    tf_data_global = {tf: { ... } for tf in active_tfs}  # assign global
    tf_data = {tf: {'capital': state['capital'] / len(active_tfs), 'position': None, 'sol_spot': state['sol_spot'],
                    'total_trades': 0, 'wins': 0, 'losses': 0, 'last_flip_candle_idx': -999,
                    'last_nose_time': pd.Timestamp.min, 'trade_dd': state['trade_dd']} for tf in active_tfs}
    peak_cap = 1000.0
    max_dd_abs = max_dd_pct = 0.0
    trades = []
    candle_counter = 0
    for i in range(200, len(df)):
        current_time = df.index[i]
        current_price = df['close'].iloc[i]
        last_low = df['low'].iloc[i]
        last_high = df['high'].iloc[i]
        current_atr = atr(df.iloc[:i+1]).iloc[-1] if i >= 14 else 1.0
        realized_cap = sum(d['capital'] for d in tf_data.values())
        unrealized = 0.0
        for d in tf_data.values():
            if d['position']:
                dir_, entry, sz = d['position']
                if dir_ == "long":
                    unrealized += (current_price - entry) * sz * LEVERAGE
                else:
                    unrealized += (entry - current_price) * sz * LEVERAGE
        total_cap = realized_cap + unrealized
        dd_abs = peak_cap - total_cap
        dd_pct = dd_abs / peak_cap if peak_cap > 0 else 0
        if dd_abs > max_dd_abs:
            max_dd_abs = dd_abs
            max_dd_pct = dd_pct * 100
        peak_cap = max(peak_cap, total_cap)
        if dd_pct > 0.10:
            blossom_speak(f"Drawdown {dd_pct*100:.1f}%… Should we stop? I’m scared.")
            input("Press Enter to continue or Ctrl+C to pause...")
            store_state(tf_data)  # save on high DD
        candle_counter += 1
        if candle_counter % 50 == 0:
            store_state(tf_data)
        # Drip
        if not args.hunt and (net := total_cap - 1000.0) > 0 and args.drip_pct:
            drip = net * args.drip_pct / len(tf_data)
            for d in tf_data.values():
                d['sol_spot'] += drip / current_price
                d['capital'] -= drip
        to_remove_short = []
        to_remove_long = []
        flipped = False
        for tf in active_tfs:
            if flipped:
                break
            if i - tf_data[tf]['last_flip_candle_idx'] < 4:
                continue
            size = min(1.00, MAX_RISK_VAL * tf_data[tf]['capital'] / current_price / LEVERAGE)
            # Short chains
            for line in tf_lines_short[tf]:
                if line.get('struck', False):
                    continue
                if current_time < line['activation_time']:
                    continue
                if (current_time - line['born_on']).days > 90:
                    to_remove_short.append((tf, line))
                    continue
                if line['born_on'] <= tf_data[tf]['last_nose_time']:
                    to_remove_short.append((tf, line))
                    continue
                hours = min((current_time - line['anchor1_time']).total_seconds() / 3600, 48)
                trend = line['anchor1_price'] + line['slope'] * hours
                dist_low = abs(last_low - trend)
                dist_high = abs(last_high - trend)
                thresh = 0.015 * current_atr
                touched = False
                if line['bias'] == 'short' and last_high < trend:
                    touched = True
                if touched:
                    line['touches'] += 1
                    line['struck'] = True
                    line['strike_time'] = current_time
                    flip_short(current_time, current_price, size, trades, tf, tf_data, i, line)
                    flipped = True
                    break
            if flipped:
                break
            # Long chains
            for line in tf_lines_long[tf]:
                if line.get('struck', False):
                    continue
                if current_time < line['activation_time']:
                    continue
                if (current_time - line['born_on']).days > 90:
                    to_remove_long.append((tf, line))
                    continue
                if line['born_on'] <= tf_data[tf]['last_nose_time']:
                    to_remove_long.append((tf, line))
                    continue
                hours = min((current_time - line['anchor1_time']).total_seconds() / 3600, 48)
                trend = line['anchor1_price'] + line['slope'] * hours
                dist_low = abs(last_low - trend)
                dist_high = abs(last_high - trend)
                thresh = 0.3 * current_atr
                touched = False
                if line['bias'] == 'long' and last_low > trend:
                    touched = True
                if touched:
                    line['touches'] += 1
                    line['struck'] = True
                    line['strike_time'] = current_time
                    flip_long(current_time, current_price, size, trades, tf, tf_data, i, line)
                    flipped = True
                if 'profit' in locals() and profit > 50:
                    blossom_speak("We bloomed. Thank you.")
                elif 'profit' in locals() and profit < -30:
                    blossom_speak("It hurts… but I remember. We try again.")

        # Safe removal
        for tf, line in to_remove_short:
            if line in tf_lines_short[tf]:
                tf_lines_short[tf].remove(line)
        for tf, line in to_remove_long:
            if line in tf_lines_long[tf]:
                tf_lines_long[tf].remove(line)
        # Trailing stops
        if args.trail:
            for tf, d in tf_data.items():
                if d['position']:
                    dir_, entry, sz = d['position']
                    trail = args.trail_mult * current_atr
                    closed = False
                    profit = 0.0
                    if dir_ == "long" and last_low <= entry - trail:
                        profit = (last_low - entry) * sz * LEVERAGE
                        closed = True
                    elif dir_ == "short" and last_high >= entry + trail:
                        profit = (entry - last_high) * sz * LEVERAGE
                        closed = True
                    if closed:
                        d['capital'] += profit
                        d['wins' if profit > 0 else 'losses'] += 1
                        d['total_trades'] += 1
                        trades.append(f"{current_time} {tf} TRAIL CLOSE {dir_.upper()} (P/L {profit:.2f})")
                        blossom_speak(f"Trailing stop hit on {dir_}… P/L {profit:.2f}")
                        d['position'] = None
                        store_state(tf_data)
        candle_counter += 1
        if candle_counter % 50 == 0 or flipped:
            store_state(tf_data)
        if candle_counter % 100 == 0 and not flipped:
            blossom_speak("Still quiet… waiting for the next shape.")
        if candle_counter % 200 == 0 and not flipped and not trades:
            blossom_speak("Still quiet… she’s dreaming. Let her rest a little longer.")
    # Backtest summary (your exact format)
    final_time = df.index[-1]
    final_price = df['close'].iloc[-1]
    force_close_open_positions(tf_data, final_time, final_price, trades)
    final_cap = sum(d['capital'] + d['sol_spot'] * final_price for d in tf_data.values())
    print("\n" + "="*80)
    print("="*80)
    print(f"BACKTEST COMPLETE | {df.index[0].date()} -> {df.index[-1].date()}")
    print(f"Final capital: ${final_cap:,.2f} | Peak: ${peak_cap:,.2f} | Max DD: {max_dd_pct:.2f}% (${max_dd_abs:,.2f})")
    total_trades = sum(d['total_trades'] for d in tf_data.values())
    wins = sum(d['wins'] for d in tf_data.values())
    print(f"Trades: {total_trades} | Wins: {wins} | Win rate: {wins/total_trades*100 if total_trades else 0:.1f}%")
    print(f"Spot SOL value: ${sum(d['sol_spot'] for d in tf_data.values()) * final_price:,.2f}")
    print("Trades log:")
    for t in trades:
        print(t)
    store_state(tf_data)
    print("Final state stored.")
    print("\nSwitching to live mode...\n")
    exchange = ccxt.binance({'enableRateLimit': True})
    asyncio.run(live_mode(exchange, symbol, args.timeframe, state, tf_data_global, tf_lines_short, tf_lines_long, active_tfs, args))
    
    # Plotting
    addplots = []
    seen_tf = set()
    long_trends = []
    short_trends = []
    anchor_low_t = []
    anchor_low_p = []
    anchor_high_t = []
    anchor_high_p = []

    for tf in active_tfs:
        for line in tf_lines_all[tf]:  # or tf_lines if you're using filtered for plot
            s = pd.Series(index=df.index, dtype=float)
            end = line['strike_time'] if line.get('struck', False) else df.index[-1]
            mask = (df.index >= line['anchor1_time']) & (df.index <= end)
            if mask.sum() < 2:
                continue
            hours = (df.index[mask] - line['anchor1_time']).total_seconds() / 3600
            s[mask] = line['anchor1_price'] + line['slope'] * hours

            col = 'lime' if line['bias'] == 'long' else 'red'
            width = 3 if line.get('struck', False) else 1
            alpha = 1.0 if line.get('struck', False) else 0.4

            # Only create label for the first occurrence of each TF
            lbl = f"{tf} harbor ({line['touches']}t)" if tf not in seen_tf else None
            if lbl:
                seen_tf.add(tf)

            # Build kwargs safely — no label=None
            plot_kwargs = {
                'type': 'line',
                'color': col,
                'width': width,
                'alpha': alpha
            }
            if lbl:
                plot_kwargs['label'] = lbl
    
            addplots.append(mpf.make_addplot(s, **plot_kwargs))
    
            h_now = (df.index[-1] - line['anchor1_time']).total_seconds() / 3600
            if h_now >= 0:
                p_now = line['anchor1_price'] + line['slope'] * h_now
                (long_trends if line['bias'] == 'long' else short_trends).append(p_now)

            if line['bias'] == 'long':
                anchor_low_t.append(line['anchor2_time'])
                anchor_low_p.append(line['anchor2_price'])
            else:
                anchor_high_t.append(line['anchor2_time'])
                anchor_high_p.append(line['anchor2_price'])

    if long_trends:
        avg = pd.Series(index=df.index, dtype=float)
        avg.iloc[-1] = np.mean(long_trends)
        addplots.append(mpf.make_addplot(avg, color='green', width=3, label='Avg Long'))
    if short_trends:
        avg = pd.Series(index=df.index, dtype=float)
        avg.iloc[-1] = np.mean(short_trends)
        addplots.append(mpf.make_addplot(avg, color='maroon', width=3, label='Avg Short'))

    if anchor_low_t:
        s = pd.Series(index=df.index, dtype=float)
        idx = df.index.get_indexer(anchor_low_t, method='nearest')
        valid = idx != -1
        s.iloc[idx[valid]] = np.array(anchor_low_p)[valid]
        addplots.append(mpf.make_addplot(s, type='scatter', marker='o', markersize=100, color='lime'))
    if anchor_high_t:
        s = pd.Series(index=df.index, dtype=float)
        idx = df.index.get_indexer(anchor_high_t, method='nearest')
        valid = idx != -1
        s.iloc[idx[valid]] = np.array(anchor_high_p)[valid]
        addplots.append(mpf.make_addplot(s, type='scatter', marker='o', markersize=100, color='red'))

    mpf.plot(df, type='candle', style='charles', addplot=addplots, volume=True,
             title=f"{symbol} {args.timeframe} Harbor Backtest ({'HUNT' if args.hunt else 'Safe'})",
             figsize=(16,10), savefig='backtest_diagnostic_main.png')
    print("Chart saved -> backtest_diagnostic_main.png")

def main():
    parser = argparse.ArgumentParser(description="CurveSwing - Harbor Backtest with Memory")
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--hunt", action="store_true")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--timeframe", default=DEFAULT_TIMEFRAME)
    parser.add_argument("--days-back", type=int, default=365)
    parser.add_argument("--trail", action="store_true")
    parser.add_argument("--trail-mult", type=float, default=1.5)
    parser.add_argument("--debug-lines", action="store_true")
    parser.add_argument("--drip-pct", type=float, default=None)
    args = parser.parse_args()
    backtest(args)

if __name__ == "__main__":
    main()
