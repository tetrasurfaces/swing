# PRIVATE AND CONFIDENTIAL
# Copyright (c) 2025 Tetrasurfaces. All Rights Reserved.
#
# This software and all associated files, models, patterns, and intellectual property
# (including but not limited to harbor line logic, sailfish pattern recognition,
# nested swing detection, and trading algorithms) are strictly confidential.
#
# Unauthorized copying, distribution, disclosure, reverse engineering, or use is
# strictly prohibited. This code is provided under a limited, revocable license
# for internal use only.
#
# Any violation will be pursued to the fullest extent of the law.
# Mutual Non-Disclosure Agreement (NDA) applies to all collaborators.
#
# For licensing, access, or collaboration inquiries contact: tetrasurfaces (via GitHub issues only).
# Last updated: 2026-05-02
# PRIVATE AND CONFIDENTIAL
# Copyright (c) 2025 Tetrasurfaces. All Rights Reserved.
# Unauthorized distribution or use is prohibited.
# Mutual Non-Disclosure Agreement applies.
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tetris + Topiary v15.9.5 - 10× + 0.01 BTC + REVERSALS + ACT+1
- Full 72 live limit orders (8 per TF: pico/nano/micro/macro × long/short)
- Leverage default 10×, target order size 0.01 BTC
- Nearest-order hit → full reversal; further same-side hits → add size
- Activation offset +1 swing (all chains) — affordance pushed one swing right
- ACCOUNT_ADDRESS / BALANCE_OVERRIDE for agent vs master balance
- Kill-switch on nose-stop (−2 %) + session DD; soft per-chain cap 3
- Glide + Topiary + roster + Pine export
"""

import argparse
import os
import time
import json
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np
import ccxt
from collections import defaultdict
import logging
import sys
from dotenv import load_dotenv
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account
from tqdm import tqdm

load_dotenv()

# ==================== CONSTANTS ====================
EXTRA_HISTORY_DAYS = 7
MAX_RISK_PER_TF_PCT = 10.0
LEAD_FACTOR = 0.20
DEFAULT_FEES = 0.045
DEFAULT_MIN_DELTA_PCT = 0.20
STOP_BUFFER = 0.0002
MAX_POSITION_USD = 25_000
MAX_TF_EXPOSURE_USD = 6000
MAX_MARTINGALE_ROUNDS = 10
CYCLE_PCT = 0.30
LEVERAGE = int(os.getenv("LEVERAGE", 10))          # v15.9.5: default 10×
TARGET_ORDER_SIZE_BTC = float(os.getenv("ORDER_SIZE_BTC", "0.01"))  # target 0.01 BTC
LOWER_TFS = ['1m', '3m', '5m']
HIGHER_TFS = ['15m', '1h', '4h', '1d', '1w', '1M']
ALL_TFS = LOWER_TFS + HIGHER_TFS
STATE_EXPORT_INTERVAL = 300
TF_RESET_INTERVAL = {
    '1m': 60, '3m': 180, '5m': 300, '15m': 900,
    '1h': 3600, '4h': 14400, '1d': 86400
}

# ==================== v15.9.5 RISK CONTROLS ====================
NOSE_STOP_PCT = 0.02                    # −2 % nose stop
KILL_SWITCH_COOLDOWN_SEC = 900          # 15 min pause after any nose-stop
MAX_OPEN_PER_CHAIN = 3                  # soft global cap per chain type
MIN_HOTSPOT_MICRO_MACRO = 1.0           # micro/macro need hotspot ≥ 1
SESSION_MAX_DD_PCT = 8.0                # hard session DD circuit (extra safety)

# ====================== PICO (Short-term, reactive) — ACT +1 ======================
MAN_OFFSET_PICO      = 2
EIGHTH_OFFSET_PICO   = 3
MIN_CHAIN_PICO       = 3
ACTIVATION_OFFSET_PICO = 9              # was 8 → +1 swing right

# ====================== NANO (Ultra-short) — ACT +1 ======================
MAN_OFFSET_NANO      = 8
EIGHTH_OFFSET_NANO   = 11
MIN_CHAIN_NANO       = 16
ACTIVATION_OFFSET_NANO = 6              # was 5 → +1

# ====================== MICRO — ACT +1 ======================
MAN_OFFSET_MICRO     = 9
EIGHTH_OFFSET_MICRO  = 17
MIN_CHAIN_MICRO      = 22
ACTIVATION_OFFSET_MICRO = 5             # was 4 → +1

# ====================== MACRO — ACT +1 ======================
MAN_OFFSET_MACRO     = 21
EIGHTH_OFFSET_MACRO  = 35
MIN_CHAIN_MACRO      = 44
ACTIVATION_OFFSET_MACRO = 10            # was 9 → +1

GLIDE_MIN_STEP_USD = 6.0
# ==================== v15.6 GLIDE EXECUTION (Candle-Driven) ====================
GLIDE_STEP_PER_MIN = {
    '1m': 1.8, '3m': 2.8, '5m': 3.8,
    '15m': 5.5, '1h': 9.0, '4h': 14.0,
}
GLIDE_ACTIVATION_OFFSET_PCT = 0.0045
GLIDE_MIN_STEP_USD = 6.0          # only move if glide has moved at least this much

class GlideExecutionManager:
    def __init__(self, active_limits_ref, wallet, testnet=False):
        self.active_glides = {}
        self.last_candle_close = {}
        self.active_limits = active_limits_ref
        self.wallet = wallet
        self.testnet = testnet

    def register_order(self, oid, tf, bias, origin_price, glide_line, side):
        self.active_glides[oid] = {
            'tf': tf, 'bias': bias, 'origin_price': origin_price,
            'glide_line': glide_line, 'last_candle_close': datetime.now(timezone.utc), 'side': side
        }

    def on_candle_close(self, tf, new_candle_close_time):
        self.last_candle_close[tf] = new_candle_close_time
        to_move = []
        for oid, data in list(self.active_glides.items()):
            if data['tf'] != tf:
                continue
            minutes_elapsed = (new_candle_close_time - data['last_candle_close']).total_seconds() / 60
            step = GLIDE_STEP_PER_MIN.get(tf, 3.0)
            drift = step * minutes_elapsed
            direction = 1 if data['bias'] == 'long' else -1
            new_glide_line = data['glide_line'] + (drift * direction)

            if abs(new_glide_line - data['glide_line']) >= GLIDE_MIN_STEP_USD:
                to_move.append((oid, new_glide_line, data))

        for oid, new_target, data in to_move:
            self._move_order(oid, new_target, data)

    def _move_order(self, old_oid, new_target_price, data):
        try:
            coin = "BTC"
            symbol = f"{coin}/USDC"
            side = "buy" if data['side'] == 'long' else "sell"

            exchange = get_hyperliquid_exchange(self.wallet, self.testnet)
            exchange.cancel(oid=old_oid, name=coin)
            time.sleep(0.35)

            result = place_glide_limit_with_perturbation(
                symbol=symbol, side=side, size=0.01,
                calculated_glide_price=new_target_price, coin=coin,
                is_buy=(side == "buy"), wallet=self.wallet, testnet=self.testnet
            )

            if result:
                new_oid = None
                try:
                    statuses = result.get('response', {}).get('data', {}).get('statuses', [])
                    if statuses and isinstance(statuses[0], dict):
                        resting = statuses[0].get('resting')
                        if resting:
                            new_oid = resting.get('oid')
                except:
                    pass

                if new_oid:
                    self.active_glides[new_oid] = data.copy()
                    self.active_glides[new_oid]['glide_line'] = new_target_price
                    self.active_glides[new_oid]['last_candle_close'] = datetime.now(timezone.utc)
                    if old_oid in self.active_glides:
                        del self.active_glides[old_oid]

                    for k, v in list(self.active_limits.items()):
                        if v.get('oid') == old_oid:
                            v['oid'] = new_oid
                            v['price'] = round(new_target_price, 2)
                            v['glide_origin_price'] = new_target_price
                            break

                    print(f"[GLIDE EXEC] {data['tf']} {data['bias'].upper()} moved → {new_target_price:.2f}")
        except Exception as e:
            print(f"[GLIDE EXEC ERROR] {e}")

# ==================== AFFORDANCE ROSTER (72 slots - 8 per TF) ====================
class AffordanceRoster:
    """
    Automatic slot allocation for up to 72 orders (8 per TF).
    Each TF gets its own pool of 8 slots.
    Slots are allocated automatically when we place a new harbor order.
    Released on cleanup, cancel, or when position is closed.
    """

    def __init__(self, max_slots_per_tf=8):
        self.max_slots_per_tf = max_slots_per_tf
        self.slots = {}          # tf -> list of allocated dicts
        self.order_to_slot = {}  # order_key -> (tf, slot_index)

        # Initialize empty pools for all TFs we care about
        for tf in ['1m', '3m', '5m', '15m', '1h', '4h', '1d', '1w', '1M']:
            self.slots[tf] = []

    def get_free_slots(self, tf):
        return self.max_slots_per_tf - len(self.slots.get(tf, []))

    def can_allocate(self, tf):
        return self.get_free_slots(tf) > 0

    def allocate(self, tf, bias, chain_type, size_usd, order_key):
        """Automatically allocate the next available slot for this TF."""
        if not self.can_allocate(tf):
            return False

        slot = {
            'bias': bias,
            'chain_type': chain_type,
            'allocated_usd': round(size_usd, 2),
            'order_key': order_key,
            'allocated_ts': time.time()
        }
        self.slots[tf].append(slot)
        self.order_to_slot[order_key] = (tf, len(self.slots[tf]) - 1)
        return True

    def release(self, order_key):
        """Release a slot by order_key."""
        if order_key not in self.order_to_slot:
            return False
        tf, idx = self.order_to_slot[order_key]
        if tf in self.slots and idx < len(self.slots[tf]):
            del self.slots[tf][idx]
            del self.order_to_slot[order_key]
            self._rebuild_indices(tf)
            return True
        return False

    def _rebuild_indices(self, tf):
        self.order_to_slot = {k: v for k, v in self.order_to_slot.items() if v[0] != tf}
        for i, slot in enumerate(self.slots.get(tf, [])):
            self.order_to_slot[slot['order_key']] = (tf, i)

    def get_total_allocated(self):
        return sum(len(slots) for slots in self.slots.values())

    def get_roster_state(self):
        """Export for dashboard / live_state.json"""
        state = {
            'total_allocated': self.get_total_allocated(),
            'max_slots': self.max_slots_per_tf * len(self.slots),
            'by_tf': {}
        }
        for tf, slot_list in self.slots.items():
            state['by_tf'][tf] = {
                'allocated': len(slot_list),
                'free': self.get_free_slots(tf),
                'slots': slot_list
            }
        return state

    def get_recommended_size(self, capital, tf, base_risk_pct=0.10):
        """Simple dynamic sizing: smaller when many slots used, larger when capital grows."""
        free = self.get_free_slots(tf)
        if free <= 0:
            return 0.0
        slot_factor = max(0.6, free / self.max_slots_per_tf)
        size = (capital * base_risk_pct * slot_factor) / 9.0
        return max(0.005, round(size, 4))

# ==================== HYPERLIQUID SDK ====================
def get_account_address(wallet):
    """
    Master account that holds funds.
    - If PRIVATE_KEY is an API/agent wallet, set ACCOUNT_ADDRESS (or MASTER_ADDRESS)
      in .env to the funded main wallet address.
    - Otherwise defaults to the signing wallet address.
    """
    master = (os.getenv('ACCOUNT_ADDRESS') or os.getenv('MASTER_ADDRESS') or '').strip()
    if master:
        return master
    return wallet.address


def get_hyperliquid_exchange(wallet, testnet=False, account_address=None):
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
    if account_address is None:
        account_address = get_account_address(wallet)

    # When agent ≠ master, pass account_address so orders/margin hit the funded account
    if account_address.lower() != wallet.address.lower():
        exchange = Exchange(wallet, url, account_address=account_address)
        print(f"🔗 Connected to {'TESTNET' if testnet else 'MAINNET'} | agent {wallet.address[:10]}… → account {account_address[:10]}…")
    else:
        exchange = Exchange(wallet, url)
        print(f"🔗 Connected to {'TESTNET' if testnet else 'MAINNET'} | account {wallet.address[:10]}…")

    exchange.info = Info(url, skip_ws=True)
    exchange._account_address = account_address   # stash for callers
    return exchange


def setup_isolated_margin(coin: str, testnet=False, wallet=None, account_address=None):
    try:
        if wallet is None:
            wallet = Account.from_key(os.getenv('PRIVATE_KEY'))
        if account_address is None:
            account_address = get_account_address(wallet)
        exchange = get_hyperliquid_exchange(wallet, testnet=testnet, account_address=account_address)
        exchange.update_leverage(name=coin, leverage=LEVERAGE, is_cross=False)
        print(f"[ISOLATED] {coin} set to ISOLATED margin @ {LEVERAGE}x")
    except Exception as e:
        print(f"[ISOLATED WARNING] {e} (this is usually harmless on testnet)")

def place_glide_limit_with_perturbation(symbol, side, size, calculated_glide_price, coin, is_buy=True, max_attempts=6, wallet=None, testnet=False, account_address=None):
    if wallet is None:
        wallet = Account.from_key(os.getenv('PRIVATE_KEY'))
    if account_address is None:
        account_address = get_account_address(wallet)

    exchange = get_hyperliquid_exchange(wallet, testnet=testnet, account_address=account_address)
    is_buy = side.lower() == 'buy'

    if coin.upper() == "BTC":
        price = round(calculated_glide_price / 0.5) * 0.5
    else:
        price = round(calculated_glide_price, 1)

    print(f"[LIMIT] {side.upper()} {size} {coin} @ {price:.2f} (raw: {calculated_glide_price:.2f})")

    for attempt in range(max_attempts):
        try:
            order = exchange.order(
                name=coin,
                is_buy=is_buy,
                sz=float(f"{float(size):.5f}"),
                limit_px=price,
                order_type={"limit": {"tif": "Gtc"}},
                reduce_only=False
            )

            if isinstance(order, dict) and order.get('status') == 'ok':
                print("✅ [SUCCESS] Order resting")
                return order
        except Exception as e:
            err = str(e).lower()
            print(f"   Attempt {attempt+1}: {err[:100]}")
            if any(x in err for x in ["precision", "float_to_wire"]):
                if coin.upper() == "BTC":
                    price += 0.5 if is_buy else -0.5
                else:
                    price += 0.01 if is_buy else -0.01
                time.sleep(0.4)
                continue
            else:
                break

    print(f"❌ [FAILED] Could not place {side}")
    return None

def check_balance():
    print("\n🔍 Checking Hyperliquid Balance...")
    try:
        from eth_account import Account
        private_key = os.getenv('PRIVATE_KEY')
        if not private_key:
            print("❌ PRIVATE_KEY not found in .env")
            return

        wallet = Account.from_key(private_key)
        print(f"✅ Wallet Address: {wallet.address}")

        exchange = get_hyperliquid_exchange(wallet, testnet=args.testnet if hasattr(args, 'testnet') else False)
        user_state = exchange.info.user_state(address=wallet.address)
        total_balance = float(user_state.get('marginSummary', {}).get('accountValue', 0))

        print(f"✅ SDK Balance: ${total_balance:,.2f} USDC")
        print(f"✅ Withdrawable: ${float(user_state.get('withdrawable', 0)):,.2f} USDC")

    except Exception as e:
        print(f"❌ Error checking balance: {e}")

def update_or_create_limit(active_limits, key, limit_price, size, current_time, sig):
    """
    Creates or refreshes an active limit entry.
    - Keeps the original_price from the first time the order was placed.
    - Updates current price, size, and timestamps.
    """
    existing = active_limits.get(key, {})
    
    # Only set original_price the FIRST time this key is created
    original_price = existing.get('original_price', limit_price)
    
    active_limits[key] = {
        'price': limit_price,
        'size': size,
        'time': current_time,
        'original_price': original_price,
        'anchor1': sig.get('anchor1_price', 0),
        'anchor2': sig.get('anchor2_price', limit_price)
    }
    
    return active_limits[key]

def sync_filled_positions(tf_data, symbol, wallet, testnet=False, account_address=None):
    try:
        if account_address is None:
            account_address = get_account_address(wallet)
        exchange = get_hyperliquid_exchange(wallet, testnet=testnet, account_address=account_address)
        user_state = exchange.info.user_state(address=account_address)
        coin = symbol.split('/')[0]
        
        real_pos = None
        for p in user_state.get('assetPositions', []):
            pos = p.get('position', {})
            if pos.get('coin') == coin:
                szi = float(pos.get('szi', 0))
                if abs(szi) > 0.00005:
                    direction = 'long' if szi > 0 else 'short'
                    entry = float(pos.get('entryPx', 0))
                    size = abs(szi)
                    real_pos = (direction, entry, size, entry)
                    print(f"[SYNC] ✅ Real {direction.upper()} on {coin}: {size:.4f} @ {entry:.2f}")
                    break

        for tf, d in tf_data.items():
            d['position'] = real_pos
    except Exception as e:
        print(f"[SYNC ERROR] {e}")


def fetch_account_equity(exchange, account_address, balance_override=None):
    """
    Best-effort equity from perps + spot. Returns (equity, detail_dict).
    """
    detail = {'perps_av': 0.0, 'perps_wd': 0.0, 'spot_usdc': 0.0}
    try:
        us = exchange.info.user_state(address=account_address)
        detail['perps_av'] = float(us.get('marginSummary', {}).get('accountValue', 0) or 0)
        detail['perps_wd'] = float(us.get('withdrawable', 0) or 0)
    except Exception as e:
        detail['perps_err'] = str(e)
    try:
        spot = exchange.info.spot_user_state(address=account_address)
        for b in spot.get('balances', []) or []:
            coin = (b.get('coin') or b.get('token') or '').upper()
            if coin in ('USDC', 'USDC.E', 'USDT'):
                detail['spot_usdc'] += float(b.get('total', b.get('available', 0)) or 0)
    except Exception as e:
        detail['spot_err'] = str(e)

    equity = max(detail['perps_av'], detail['perps_wd'], detail['spot_usdc'])
    if equity < 1.0 and balance_override:
        equity = float(balance_override)
        detail['override'] = float(balance_override)
    return equity, detail


def process_limit_fills_and_reversals(active_limits, tf_data, current_price, symbol, wallet,
                                     testnet=False, account_address=None, order_size=None):
    """
    v15.9.5 reversal / add-on rules:
    - When the nearest resting limit is crossed → full reversal of current position.
    - Further limits in the same direction as the new bias (price continues through them)
      → allowed to increase (add) position size.
    Detects fills by price cross vs resting limit prices; removes filled keys from active_limits.
    """
    if order_size is None:
        order_size = TARGET_ORDER_SIZE_BTC
    if account_address is None:
        account_address = get_account_address(wallet)

    # Gather crossed limits this tick
    crossed = []  # (key, lim, dist)
    for key, lim in list(active_limits.items()):
        if not isinstance(lim, dict):
            continue
        px = float(lim.get('price', 0) or 0)
        if px <= 0:
            continue
        bias = lim.get('bias', 'long')
        # long limit fills when market trades at/below limit; short at/above
        if bias == 'long' and current_price <= px * 1.0005:
            crossed.append((key, lim, abs(current_price - px)))
        elif bias == 'short' and current_price >= px * 0.9995:
            crossed.append((key, lim, abs(current_price - px)))

    if not crossed:
        return

    # Nearest crossed order drives the decision
    crossed.sort(key=lambda x: x[2])
    nearest_key, nearest_lim, _ = crossed[0]
    new_bias = nearest_lim.get('bias', 'long')
    tf = nearest_lim.get('tf', nearest_key.split('_')[0])
    fill_size = float(nearest_lim.get('size') or order_size)

    # Current book position (shared across TFs from sync)
    pos = None
    for d in tf_data.values():
        if d.get('position'):
            pos = d['position']
            break

    old_side = pos[0] if pos and len(pos) >= 3 else None
    old_size = float(pos[2]) if pos and len(pos) >= 3 else 0.0
    old_entry = float(pos[1]) if pos and len(pos) >= 3 else 0.0

    # --- REVERSE: nearest fill is opposite to current position ---
    if old_side and old_side != new_bias:
        close_side = 'sell' if old_side == 'long' else 'buy'
        print(f"[REVERSE] Nearest fill {tf} {new_bias.upper()} @ {nearest_lim.get('price'):.2f} "
              f"→ close {old_side.upper()} {old_size:.4f} then open {new_bias.upper()} {fill_size:.4f}")
        place_market_order(symbol, close_side, old_size, wallet=wallet, testnet=testnet,
                           reduce_only=True, account_address=account_address)
        open_side = 'buy' if new_bias == 'long' else 'sell'
        place_market_order(symbol, open_side, fill_size, wallet=wallet, testnet=testnet,
                           reduce_only=False, account_address=account_address)
        new_pos = (new_bias, current_price, fill_size)
        for d in tf_data.values():
            d['position'] = new_pos
            d['stop_loss_level'] = (current_price * (1 - NOSE_STOP_PCT) if new_bias == 'long'
                                    else current_price * (1 + NOSE_STOP_PCT))
            d['last_flip_time'] = time.time()

    # --- ADD: same direction (or flat) and further same-side fills ---
    elif not old_side or old_side == new_bias:
        # Count how many same-bias limits were crossed (adds)
        same_side = [(k, lim) for k, lim, _ in crossed if lim.get('bias') == new_bias]
        add_lots = len(same_side)
        add_size = fill_size * max(1, add_lots) if not old_side else fill_size * max(0, add_lots)
        # If flat, open with first lot; if already same side, only add the extra crosses
        if not old_side:
            open_side = 'buy' if new_bias == 'long' else 'sell'
            print(f"[OPEN] Nearest fill {tf} {new_bias.upper()} @ {nearest_lim.get('price'):.2f} "
                  f"→ open {fill_size:.4f} (+{max(0, add_lots-1)} add-on lots)")
            place_market_order(symbol, open_side, fill_size * add_lots, wallet=wallet, testnet=testnet,
                               reduce_only=False, account_address=account_address)
            new_pos = (new_bias, current_price, fill_size * add_lots)
            for d in tf_data.values():
                d['position'] = new_pos
                d['stop_loss_level'] = (current_price * (1 - NOSE_STOP_PCT) if new_bias == 'long'
                                        else current_price * (1 + NOSE_STOP_PCT))
                d['last_flip_time'] = time.time()
        else:
            # already same side — only increase on further hits beyond nearest
            further = [x for x in same_side if x[0] != nearest_key]
            if further:
                extra = fill_size * len(further)
                open_side = 'buy' if new_bias == 'long' else 'sell'
                print(f"[ADD] Price continued through {len(further)} further {new_bias.upper()} limit(s) "
                      f"→ add {extra:.4f}")
                place_market_order(symbol, open_side, extra, wallet=wallet, testnet=testnet,
                                   reduce_only=False, account_address=account_address)
                new_size = old_size + extra
                # volume-weighted entry approx
                new_entry = ((old_entry * old_size) + (current_price * extra)) / max(new_size, 1e-9)
                new_pos = (new_bias, new_entry, new_size)
                for d in tf_data.values():
                    d['position'] = new_pos
                    d['last_flip_time'] = time.time()

    # Remove all crossed limits from active book + cancel residual oids if any
    for key, lim, _ in crossed:
        try:
            if lim.get('oid'):
                ex = get_hyperliquid_exchange(wallet, testnet=testnet, account_address=account_address)
                ex.cancel(name=symbol.split('/')[0], oid=lim['oid'])
        except Exception:
            pass
        if key in active_limits:
            del active_limits[key]
        # roster release best-effort
        try:
            pass  # roster release handled by caller if needed
        except Exception:
            pass


def place_market_order(symbol: str, side: str, size: float, wallet=None, testnet=False, reduce_only=False, account_address=None):
    """Aggressive market order with None guard"""
    try:
        if wallet is None:
            wallet = Account.from_key(os.getenv('PRIVATE_KEY'))
        if account_address is None:
            account_address = get_account_address(wallet)
        if size is None or size <= 0:
            print(f"[MARKET SKIP] Invalid size: {size}")
            return None

        exchange = get_hyperliquid_exchange(wallet, testnet=testnet, account_address=account_address)
        coin = symbol.split('/')[0]
        is_buy = side.lower() == 'buy'

        print(f"[MARKET] {side.upper()} {size:.4f} {coin} @ MARKET")

        order = exchange.order(
            name=coin,
            is_buy=is_buy,
            sz=round(size, 4),
            limit_px=None,
            order_type={"limit": {"tif": "Ioc"}},
            reduce_only=reduce_only
        )
        print(f"[MARKET SUCCESS] {side.upper()} {size:.4f} filled")
        return order
    except Exception as e:
        print(f"[MARKET ERROR] {e}")
        return None

        # v15.5 Glide metadata tag
        lim['glide_origin_price'] = price
        lim['glide_origin_ts'] = time.time()
        lim['tf'] = tf
        lim['bias'] = bias
        
# ==================== TOPIARY HEDGE v15.3 (True Martingale + Capital Request) ====================

class TopiaryHedge:
    def __init__(self, direction, entry_price, size, tf):
        self.direction = direction
        self.entry_price = entry_price
        self.size = size
        self.rounds = 1
        self.high = entry_price
        self.low = entry_price
        self.active = True
        self.tf = tf
        self.tapered_value = 0.0
        self.cumulative_loss = 0.0   # for future creative recovery sequences

    def double_down(self, current_price, is_top):
        if self.rounds >= MAX_MARTINGALE_ROUNDS:
            return False
        self.rounds += 1
        self.size *= 1.85                    # True aggressive martingale on hedge size
        if is_top:
            self.high = max(self.high, current_price)
        else:
            self.low = min(self.low, current_price)
        return True

    def taper(self, current_price, reason="MANUAL"):
        if not self.active:
            return 0.0
        notional = min(self.size * current_price * min(LEVERAGE, 40), 12000)
        returned = notional * 0.28
        self.tapered_value = returned
        self.active = False
        print(f"[TOPIARY TAPER] {self.tf} {self.direction.upper()} R{self.rounds} → ${returned:,.0f} ({reason}) @ {current_price:.2f}")
        return returned


def update_topiary_hedges_v155(tf_data, current_price, args, current_signals=None, symbol=None, wallet=None):
    """v15.4 - Harbor-gated NEW hedges + detect_left_turn counting + 36 global cap 
       + market fallback for larger hedges + clearer prints + per-TF opening conditions"""
    total_tapered = 0.0
    current_ts = time.time()
    is_live = wallet is not None and symbol is not None

    # === GLOBAL 36 HEDGE CAP ===
    total_active_hedges = sum(len(d.get('hedges', [])) for d in tf_data.values())
    hedge_cap_reached = total_active_hedges >= 36
    if hedge_cap_reached and not any(d.get('hedge_cap_logged') for d in tf_data.values()):
        print("[HEDGE CAP] 36 hedge orders active across all TFs — new hedge creation paused")
        for d in tf_data.values():
            d['hedge_cap_logged'] = True

    for tf, d in list(tf_data.items()):
        pos = d.get('position')
        if not pos:
            continue

        if len(pos) == 4:
            side, entry, size, nose = pos
        elif len(pos) == 3:
            side, entry, size = pos
            nose = d.get('last_nose') or entry
        else:
            continue

        if 'hedges' not in d:
            d['hedges'] = []

        if len(d['hedges']) >= 8:  # per-TF safety
            if not d.get('hedge_cap_logged', False):
                print(f"[HEDGE CAP] Max 8 rounds on {tf} — further doubles locked")
                d['hedge_cap_logged'] = True
            continue

        direction = side
        tf_capital = d.get('capital', 800)

        # === DOUBLE DOWN (always allowed for defense, even at cap) ===
        doubled = False
        for hedge in d['hedges']:
            if hedge.direction == direction and hedge.active:
                is_top = (direction == 'short')
                if hedge.double_down(current_price, is_top):
                    print(f"[NAKED HEDGE] {tf} {direction.upper()} doubled → R{hedge.rounds} @ {current_price:.2f}")
                    # Count toward detect_left_turn (harden the link)
                    try:
                        detect_left_turn(None, current_price, direction)  # lower_tf_df optional in your version
                    except Exception:
                        pass

                    if is_live:
                        hedge_side = "buy" if direction == "long" else "sell"
                        h_size = round(hedge.size * 0.98, 4)

                        if h_size > 0.005:
                            place_market_order(symbol, hedge_side, h_size, wallet)
                        else:
                            h_px = current_price * (1.008 if direction == "long" else 0.992)
                            place_live_limit_order(symbol, hedge_side, h_size, h_px, wallet=wallet)
                    doubled = True
                break

        # === NEW HEDGE — ONLY ON HARBOR STRIKE + OPENING CONDITIONS ===
        if not doubled:
            if not any(h.direction == direction and h.active for h in d['hedges']):
                # Harbor gate (must be struck) + respect same opening conditions as main entries
                harbor_struck = False
                if current_signals:
                    for sig in current_signals:
                        if sig.get('timeframe') == tf:
                            h_price = sig.get('harbor_price') or sig.get('eighth_price')
                            if h_price:
                                if abs(current_price - h_price) / current_price < 0.007:
                                    harbor_struck = True
                                    break

                # Past activation offset / other opening conditions (if you track them per TF)
                past_activation_ok = d.get('past_activation_offset_ok', True)

                if not (harbor_struck and past_activation_ok):
                    continue  # do not create new hedge

                if hedge_cap_reached:
                    continue

                hedge_risk_usd = tf_capital * 0.10
                hedge_size = max(round(hedge_risk_usd / current_price / max(LEVERAGE, 10), 4), 0.001)

                hedge = TopiaryHedge(direction, current_price, hedge_size, tf)
                d['hedges'].append(hedge)
                d['last_hedge_time'] = current_ts
                print(f"[TOPIARY] New hedge on {tf} {direction.upper()} @ {current_price:.2f} (size {hedge_size:.4f}) [HARBOR GATED]")

                # Count this new hedge toward detect_left_turn
                try:
                    detect_left_turn(None, current_price, direction)
                except Exception:
                    pass

                if is_live:
                    h_side = "buy" if direction == "long" else "sell"
                    h_px = current_price * (1.006 if direction == "long" else 0.994)
                    place_live_limit_order(symbol, h_side, hedge_size, h_px, wallet=wallet)

        # === TAPER CONDITIONS ===
        for hedge in list(d['hedges']):
            if not hedge.active:
                continue

            move_pct = 0.004 if tf in LOWER_TFS else 0.002
            price_taper = (hedge.direction == 'short' and current_price < hedge.low * (1 - move_pct)) or \
                          (hedge.direction == 'long' and current_price > hedge.high * (1 + move_pct))

            structural_taper = False
            if current_signals:
                for sig in current_signals:
                    if (sig.get('timeframe') == tf and
                        sig.get('eighth_price') and
                        abs(sig.get('eighth_price') - current_price) / current_price < 0.005):
                        structural_taper = True
                        break

            round_taper = hedge.rounds >= 6

            if price_taper or structural_taper or round_taper:
                reason = "ROUND" if round_taper else "EIGHTH-MAN" if structural_taper else "PRICE"
                returned = hedge.taper(current_price, reason)
                if returned > 0:
                    d['tapered_profit'] = d.get('tapered_profit', 0.0) + returned
                    total_tapered += returned

    if total_tapered > 10:
        print(f"[TOPIARY] ✅ Session tapered: ${total_tapered:,.0f}")

def evaluate_glide_v155(active_limits, current_price, current_ts):
    """v15.5 — Evaluation only. 
       Shows vertical distance from market to diagonal glide line.
       Prints ALL tagged orders (FORMING or ACTIVE). Shorts ↑, Longs ↓."""
    if not active_limits:
        return

    print("\n[GLIDE EVAL v15.5] Diagonal line check — vertical distance to glide path")
    evaluated = 0
    forming = 0

    for key, lim in active_limits.items():          # <-- FIXED: iterate over .items()
        if not isinstance(lim, dict):
            continue
        if not lim.get('glide_origin_price'):
            continue

        tf = lim.get('tf', '1m')
        bias = lim.get('bias', 'long')
        origin_px = lim.get('glide_origin_price')
        origin_ts = lim.get('glide_origin_ts', current_ts)
        resting_px = lim.get('price', origin_px)

        minutes_elapsed = max((current_ts - origin_ts) / 60.0, 0.05)
        step = GLIDE_STEP_PER_MIN.get(tf, 2.5)

        if bias == 'short':
            expected_glide_px = origin_px + (step * minutes_elapsed)
        else:
            expected_glide_px = origin_px - (step * minutes_elapsed)

        vertical_dist = current_price - expected_glide_px
        dist_pct = (vertical_dist / current_price) * 100.0
        offset_pct = abs(current_price - resting_px) / resting_px

        arrow = "↑" if bias == 'short' else "↓"

        if offset_pct >= GLIDE_ACTIVATION_OFFSET_PCT:
            status = "ACTIVE GLIDE"
            evaluated += 1
        else:
            status = "FORMING"
            forming += 1

        print(f"  [{tf} {bias.upper()}] resting@{resting_px:.0f} | origin@{origin_px:.0f} | "
              f"{minutes_elapsed:.1f}m | glide line: {expected_glide_px:.0f} | "
              f"market {current_price:.0f} → vert.dist {vertical_dist:+.0f} ({dist_pct:+.2f}%) {arrow} [{status}]")

    if evaluated == 0 and forming == 0:
        print("  (no glide-tagged orders yet)")
    else:
        print(f"  → {evaluated} active glide | {forming} still forming (below 0.15% offset)")
    print("[GLIDE EVAL] done\n")

def create_topiary_hedge(tf, direction, price, size, tf_data):
    if tf not in tf_data:
        tf_data[tf] = {
            'hedges': [], 
            'hedge_capital': 0.0, 
            'capital': 1000.0, 
            'initial_capital': 1000.0,
            'last_hedge_time': 0,
            'tapered_profit': 0.0,
            'hedge_cap_logged': False
        }
    hedge = TopiaryHedge(direction, price, size, tf)
    tf_data[tf]['hedges'].append(hedge)
    logging.info(f"[TOPIARY] Hedge created on {tf} @ {price:.2f}")
    return hedge

def request_capital(tf_data, needy_tf, amount_needed, min_transfer=25):
    """Needy TF can request capital from tapered profits or light emergency harvest"""
    if needy_tf not in tf_data:
        return 0.0

    pulled = 0.0
    # 1. Pull from other TFs' tapered_profit first
    for tf in list(tf_data.keys()):
        if tf == needy_tf:
            continue
        avail = tf_data[tf].get('tapered_profit', 0.0)
        if avail > min_transfer:
            take = min(avail * 0.6, amount_needed - pulled)
            if take > 0:
                tf_data[tf]['tapered_profit'] -= take
                pulled += take
                print(f"[CAPITAL REQUEST] {needy_tf} pulled ${take:,.0f} from {tf} tapered_profit")

    # 2. Light emergency harvest (only profitable portion) if still short
    if pulled < amount_needed * 0.6:
        for tf in list(tf_data.keys()):
            if tf == needy_tf or not tf_data[tf].get('position'):
                continue
            # Only harvest if the TF has meaningful profit buffer (simplified check)
            profit_buffer = tf_data[tf].get('capital', 0) - tf_data[tf].get('initial_capital', 800)
            if profit_buffer > 40:
                take = min(profit_buffer * 0.3, amount_needed - pulled)
                if take > 10:
                    tf_data[tf]['capital'] -= take
                    pulled += take
                    print(f"[EMERGENCY HARVEST] {needy_tf} received ${take:,.0f} from {tf} profit buffer")

    if pulled > 0:
        tf_data[needy_tf]['capital'] += pulled
        print(f"[CAPITAL FOCUSED] {needy_tf} received total ${pulled:,.0f}")
    return pulled


def cycle_profits(tf_data, higher_tf, args):
    """Enhanced cycle + answers capital requests"""
    if higher_tf not in tf_data:
        return

    lower_tfs = [t for t in LOWER_TFS if t != higher_tf]
    total_profit = 0.0

    for low_tf in lower_tfs:
        if low_tf not in tf_data:
            continue
        profit = tf_data[low_tf].get('tapered_profit', 0.0)
        if profit > 30:
            total_profit += profit
            tf_data[low_tf]['tapered_profit'] = 0.0
            tf_data[low_tf]['capital'] = tf_data[low_tf].get('initial_capital', tf_data[low_tf]['capital'])

    if total_profit > 0:
        added = total_profit * 0.70
        tf_data[higher_tf]['capital'] += added
        print(f"[CYCLE PROFIT] → {higher_tf} received ${added:,.0f} from lower TFs")

def detect_left_turn(df, price, direction):
    if df is None or len(df) < 8: return False
    recent = df['close'].tail(8).values
    recent_high = float(recent.max())
    recent_low = float(recent.min())
    if direction == "short":
        return price >= recent_high * 0.997   # strong breakout up
    else:
        return price <= recent_low * 1.003    # strong breakout down

def print_hedge_summary(tf_data, args):
    logging.info("\n" + "="*80)
    logging.info("HEDGE SUMMARY (15.1)")
    logging.info("="*80)
    total_hedges = 0
    total_tapered = 0.0
    total_rounds = 0
    largest_hedge = 0.0
    for tf in HIGHER_TFS:
        if tf not in tf_data or 'hedges' not in tf_data[tf]:
            continue
        hedges = tf_data[tf]['hedges']
        total_hedges += len(hedges)
        for h in hedges:
            if hasattr(h, 'tapered_value'):
                total_tapered += h.tapered_value
            total_rounds += h.rounds
            largest_hedge = max(largest_hedge, h.size * h.entry_price * LEVERAGE)
    logging.info(f"Total Hedges Created : {total_hedges}")
    logging.info(f"Total Value Tapered : ${total_tapered:,.2f}")
    logging.info(f"Average Rounds : {total_rounds / total_hedges:.1f}" if total_hedges > 0 else "Average Rounds : 0")
    logging.info(f"Largest Hedge Value : ${largest_hedge:,.0f}")
    logging.info("="*80 + "\n")

# ==================== Exports ====================
import json
import time
import os
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # ~/bots

def export_harbor_lines_json(active_limits, current_price, symbol="BTC/USDC"):
    """Clean export for Pine Script"""
    lines = []
    for key, order in active_limits.items():
        lines.append({
            "key": key,
            "tf": order.get("tf", key.split("_")[0]),
            "bias": order.get("bias", "long" if "long" in key else "short"),
            "chain_type": order.get("chain_type", "pico"),
            "price": float(order.get("price", 0)),
            "size": float(order.get("size", 0.02)),
            "hotspot_score": int(order.get("hotspot_score", 0)),
            "timestamp": int(order.get("time", time.time())),
            "hotspot": order.get("hotspot_score", 0) > 0
        })

    data = {
        "timestamp": int(time.time()),
        "current_price": float(current_price),
        "symbol": symbol,
        "total_lines": len(lines),
        "lines": lines
    }

    try:
        filepath = os.path.join(BASE_DIR, "harbor_lines.json")
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        print(f"[HARBOUR] Exported {len(lines)} lines → {filepath}")
    except Exception as e:
        print(f"[HARBOUR EXPORT ERROR] {e}")


def export_state_json(active_limits, current_price, free_capital=0, harbor_notional=0,
                      hedged_reserve=0, reinvestable_margin=0, roster=None, symbol="BTC/USDC"):
    """Rich state for Dashboard + Affordance Roster"""
    state = {
        "timestamp": int(time.time()),
        "current_price": float(current_price),
        "symbol": symbol,
        "active_count": len(active_limits),
        "free_capital": float(free_capital),
        "harbor_notional": float(harbor_notional),
        "hedged_reserve": float(hedged_reserve),
        "reinvestable_margin": float(reinvestable_margin),
        "active_limits": {}
    }

    # Include roster data if available
    if roster:
        state["roster"] = roster.get_roster_state()

    for key, order in active_limits.items():
        state["active_limits"][key] = {
            "tf": order.get("tf", key.split("_")[0]),
            "bias": order.get("bias", "long" if "long" in key.lower() else "short"),
            "chain_type": order.get("chain_type", "pico"),
            "price": float(order.get("price", 0)),
            "size": float(order.get("size", 0.02)),
            "time": int(order.get("time", time.time())),
            "hotspot_score": int(order.get("hotspot_score", 0)),
            "oid": order.get("oid"),
            "original_price": float(order.get("original_price", order.get("price", 0)))
        }

    try:
        os.makedirs("public", exist_ok=True)
        with open("public/live_state.json", "w") as f:
            json.dump(state, f, indent=2)
        print(f"[STATE] Exported rich dashboard state ({len(active_limits)} limits) → public/live_state.json")
    except Exception as e:
        print(f"[STATE EXPORT ERROR] {e}")

def export_pine_ready(active_limits, current_price, symbol="BTC/USDC"):
    """Generate clean, beautiful Pine Script with up to 36 rays"""
    calls = ""
    count = 0
    
    for key, o in active_limits.items():
        tf = o.get("tf", key.split("_")[0] if "_" in key else "1m")
        bias = o.get("bias", "long")
        chain = o.get("chain_type", "pico")
        
        a1 = float(o.get("anchor1_price", o.get("price", 0)))
        a2 = float(o.get("anchor2_price", o.get("price", 0)))
        hotspot = o.get("hotspot_score", 0) > 0
        
        # Smart offset calculation
        try:
            anchor1_time = pd.to_datetime(o.get("anchor1_time"))
            anchor2_time = pd.to_datetime(o.get("anchor2_time"))
            
            if anchor1_time.tz is None:
                anchor1_time = anchor1_time.tz_localize('UTC')
            if anchor2_time.tz is None:
                anchor2_time = anchor2_time.tz_localize('UTC')
            
            tf_minutes = {'1m':1, '3m':3, '5m':5, '15m':15, '1h':60, '4h':240, '1d':1440, '1w':10080, '1M':43200}.get(tf, 1)
            offset1 = int((datetime.now(timezone.utc) - anchor1_time).total_seconds() / 60 / tf_minutes) + 45
            offset2 = int((datetime.now(timezone.utc) - anchor2_time).total_seconds() / 60 / tf_minutes) + 10
            
            offset1 = max(80, min(offset1, 450))
            offset2 = max(10, min(offset2, 120))
        except:
            offset1 = 180
            offset2 = 40
        
        calls += f'    draw_ray("{tf}", "{bias}", "{chain}", {offset1}, {a1:.2f}, {offset2}, {a2:.2f}, {str(hotspot).lower()})\n'
        count += 1

    full_script = f"""//@version=6
indicator("TETRA • Harbor Rays LIVE [Auto from Python - 36 Rays]", overlay=true, max_lines_count=500, max_labels_count=500)

// ── SETTINGS ──
highlight_hot = input.bool(true, "Highlight Hotspots", group="Global")
show_debug    = input.bool(false, "Show Debug Labels", group="Global")

left_magnet   = input.string("High/Low", "Left Anchor (Wick)", 
     options=["High/Low", "High", "Low", "Closest OHLC", "Exact"], group="Magnet")

right_magnet  = input.string("Open/Close", "Right Anchor (Body)", 
     options=["Open/Close", "Open", "Close", "Closest OHLC", "Exact"], group="Magnet")

// Colors - Light Pico / Dark Nano
c_pico_long   = color.new(#00F5FF, 0)   // cyan light
c_pico_short  = color.new(#FF44AA, 0)   // pink light
c_nano_long   = color.new(#0088FF, 0)   // blue dark
c_nano_short  = color.new(#FF0088, 0)   // red dark

var line[]   rays   = array.new<line>()
var label[]  labels = array.new<label>()

// ==================== SAFE MAGNET ====================
magnet_price(src_price, bars_back, side) =>
    safe_bars = math.min(bars_back, 4999)
    mode = side == "left" ? left_magnet : right_magnet
    switch mode
        "Exact" => src_price
        "High"  => high[safe_bars]
        "Low"   => low[safe_bars]
        "Open"  => open[safe_bars]
        "Close" => close[safe_bars]
        "High/Low" =>
            h = high[safe_bars]
            l = low[safe_bars]
            math.abs(src_price - h) <= math.abs(src_price - l) ? h : l
        "Open/Close" =>
            o = open[safe_bars]
            c = close[safe_bars]
            math.abs(src_price - o) <= math.abs(src_price - c) ? o : c
        => 
            o = open[safe_bars]
            h = high[safe_bars]
            l = low[safe_bars]
            c = close[safe_bars]
            d_o = math.abs(src_price - o)
            min_d = math.min(d_o, math.abs(src_price - h), math.abs(src_price - l), math.abs(src_price - c))
            min_d == d_o ? o : min_d == math.abs(src_price - h) ? h : min_d == math.abs(src_price - l) ? l : c

// ==================== DRAW FUNCTION ====================
draw_ray(tf, bias, chain, offset1, p1, offset2, p2, hotspot) =>
    is_pico = chain == "pico"
    col = if is_pico
        bias == "long" ? c_pico_long : c_pico_short
    else
        bias == "long" ? c_nano_long : c_nano_short
    
    width = hotspot and highlight_hot ? 3 : 2
    
    start_idx = math.max(bar_index - offset1, 0)
    end_idx   = math.max(bar_index - offset2, 0)
    
    a1 = magnet_price(p1, offset1, "left")
    a2 = magnet_price(p2, offset2, "right")
    
    if math.abs(a1 - a2) < syminfo.mintick * 3
        a2 := a1 + (bias == "long" ? -syminfo.mintick * 8 : syminfo.mintick * 8)
    
    ray = line.new(start_idx, a1, end_idx, a2, color=col, width=width, style=line.style_solid, extend=extend.right)
    array.push(rays, ray)
    
    label.new(start_idx + 4, a1, "●", color=col, textcolor=color.white, size=size.small)
    label.new(end_idx + 4, a2, "◆", color=col, textcolor=color.white, size=size.small)
    
    label_text = tf + (is_pico ? " P" : " N") + (bias == "long" ? "▲" : "▼")
    label.new(end_idx + 15, a2, label_text, 
              color=color.new(col, 70), textcolor=color.white, 
              style=label.style_label_left, size=size.tiny)

// ==================== SAFE CLEAR + DRAW ====================
if barstate.islast
    if array.size(rays) > 0
        for i = 0 to array.size(rays) - 1
            line.delete(array.get(rays, i))
        array.clear(rays)
    if array.size(labels) > 0
        for i = 0 to array.size(labels) - 1
            label.delete(array.get(labels, i))
        array.clear(labels)
    
    // === AUTO-GENERATED RAYS FROM TETRIS_TOPIARY.PY ===
{calls}

    // Add more draw_ray() calls here if needed (up to 36)

// ==================== INFO PANEL ====================
var table info = table.new(position.top_right, 1, 5, bgcolor=color.new(#0f1a1f, 85))
if barstate.islast
    table.cell(info, 0, 0, "TETRA HARBOR RAYS", text_color=#50D2C1, text_size=size.normal)
    table.cell(info, 0, 1, "P = Pico (Light) | N = Nano (Dark)", text_color=color.gray)
    table.cell(info, 0, 2, "▲ Long | ▼ Short", text_color=color.gray)
    table.cell(info, 0, 3, "Increase Max Left Extension if needed", text_color=color.gray)
    table.cell(info, 0, 4, "🦆 Ducks fly together", text_color=#50D2C1)
"""

    try:
        filepath = os.path.join(BASE_DIR, "harbor_pine_ready.txt")
        with open(filepath, "w") as f:
            f.write(full_script)
        print(f"[PINE] ✅ Exported {count} rays → {filepath}")
        print("     → Copy ALL content into TradingView")
    except Exception as e:
        print(f"[PINE EXPORT ERROR] {e}")

# ==================== CORE SIGNAL FUNCTIONS ====================
def triplet_swings(df):
    """Atomic triplet swings - used by all layers"""
    highs = df["high"].values
    lows = df["low"].values
    up, down = [], []
    for i in range(2, len(df)):
        if highs[i-1] > highs[i-2] and highs[i-1] > highs[i]:
            up.append(i-1)
        if lows[i-1] < lows[i-2] and lows[i-1] < lows[i]:
            down.append(i-1)
    return up, down

def count_triplets(df, max_lookback=400):
    """Count current streak of long (lower lows) and short (higher highs) structures"""
    if len(df) < 30:
        return {'long_streak': 0, 'short_streak': 0, 'last_swing': None}

    up_swings, down_swings = triplet_swings(df.tail(max_lookback))  # up = local highs, down = local lows

    # Merge all swings with type
    all_swings = []
    for idx in up_swings:
        all_swings.append((idx, 'short'))   # higher high → short bias structure
    for idx in down_swings:
        all_swings.append((idx, 'long'))    # lower low → long bias structure

    all_swings.sort(key=lambda x: x[0])  # sort by candle index

    long_streak = 0
    short_streak = 0
    last_type = None

    for _, swing_type in all_swings[-20:]:   # only look at recent swings for streak
        if swing_type == 'long':
            if last_type == 'long':
                long_streak += 1
            else:
                long_streak = 1
            short_streak = 0
        else:  # 'short'
            if last_type == 'short':
                short_streak += 1
            else:
                short_streak = 1
            long_streak = 0
        last_type = swing_type

    return {
        'long_streak': long_streak,
        'short_streak': short_streak,
        'last_swing': last_type,
        'total_swings': len(all_swings)
    }

def detect_sailfish(df_leg, bias):
    if len(df_leg) < 12:
        return None
    df_leg = df_leg.tail(500).copy()
    if len(df_leg) < 12:
        return None
    _, down_swings = triplet_swings(df_leg)
    if len(down_swings) < 4:
        return None
    best = None
    best_score = -1
    for i in range(2, len(down_swings)-2):
        sail_low_idx = down_swings[i]
        man_idx = down_swings[i+1]
        sail5_idx = down_swings[i+2]
        sail_low = float(df_leg['low'].iloc[sail_low_idx])
        man_price = float(df_leg['high'].iloc[man_idx] if bias == 'long' else df_leg['low'].iloc[man_idx])
        sail5 = float(df_leg['high'].iloc[sail5_idx] if bias == 'long' else df_leg['low'].iloc[sail5_idx])
        man_candle = df_leg.iloc[man_idx]
        body = abs(man_candle['close'] - man_candle['open'])
        rng = man_candle['high'] - man_candle['low'] + 1e-8
        if body / rng < 0.40:
            man_price = float(man_candle['close'])
        if bias == 'long':
            if sail_low < man_price < sail5 * 1.002:
                depth = (man_price - sail_low) / (sail5 - sail_low + 1e-8)
                recency = (man_idx / len(df_leg)) ** 2.0
                score = depth + recency * 1.6
                if score > best_score:
                    best_score = score
                    best = {'man_idx': man_idx, 'man_price': man_price, 'low_price': sail_low}
        else:
            if sail_low > man_price > sail5 * 0.998:
                depth = (sail_low - man_price) / (sail_low - sail5 + 1e-8)
                recency = (man_idx / len(df_leg)) ** 2.0
                score = depth + recency * 1.6
                if score > best_score:
                    best_score = score
                    best = {'man_idx': man_idx, 'man_price': man_price, 'low_price': sail_low}
    return best

def get_zipper_state(tf_data, tf):
    structures = tf_data[tf].get('recent_structures', [])
    if len(structures) < 4:
        return {'zipper_score': 0.0, 'alternating': False, 'streak': 1, 'momentum': 0.0}

    # Weight recent alternations more heavily
    recent = structures[-8:]
    alternations = sum(1 for i in range(1, len(recent)) if recent[i] != recent[i-1])
    alt_ratio = alternations / max(1, len(recent) - 1)

    streak = 1
    for i in range(len(structures)-2, -1, -1):
        if structures[i] == structures[-1]:
            streak += 1
        else:
            break

    zipper_score = min(3.0, alt_ratio * 3.2)
    if alternations >= 4 and streak <= 2:
        zipper_score += 0.8

    momentum = min(1.0, alternations / 6.0)   # how fast it's flipping

    return {
        'zipper_score': round(zipper_score, 2),
        'alternating': alternations >= 3,
        'streak': streak,
        'momentum': round(momentum, 2),
        'recent': structures[-6:]
    }

def is_too_close_to_existing(new_price: float, active_limits: dict, tolerance: float = 8.0) -> bool:
    """v15.9.3: FULLY DISABLED — every chain/bias gets its own roster slot (72 total)."""
    return False

def detect_sailfish_strength(sig, df=None):
    """Boost for clean sailfish patterns"""
    if not sig or 'chain_type' not in sig:
        return 1.0
    
    chain = sig['chain_type']
    slope_abs = abs(sig.get('slope', 0))
    
    if chain == 'macro' and slope_abs > 60:
        return 2.2
    elif chain == 'micro' and slope_abs > 100:
        return 1.8
    elif chain in ['pico', 'nano'] and slope_abs > 160:
        return 1.5
    return 1.0

def should_replace_existing_harbor(existing_order, new_sig, current_price, dev=False):
    """
    Replacement rules (v15.9.2):
    - Same TF + same bias
    - New signal has a more recent nose, OR
    - New signal came from an opposite-bias retracement (fresh nose)
    → Replace the old harbor
    """
    if not existing_order or not new_sig:
        return False

    if existing_order.get('tf') != new_sig.get('timeframe'):
        return False
    if existing_order.get('bias') != new_sig.get('bias'):
        return False

    # High priority if this new nose came from a retracement on the opposite bias
    if new_sig.get('opposite_bias_retracement', False):
        if dev:
            print(f"[REPLACED] Opposite-bias retracement created fresh nose on "
                  f"{new_sig.get('timeframe')} {new_sig.get('bias').upper()}")
        return True

    # Normal recency check
    try:
        existing_nose_time = pd.to_datetime(existing_order.get('nose_time') or existing_order.get('born_on'))
        new_nose_time = pd.to_datetime(new_sig.get('nose_time') or new_sig.get('born_on'))

        if existing_nose_time.tz is None:
            existing_nose_time = existing_nose_time.tz_localize('UTC')
        if new_nose_time.tz is None:
            new_nose_time = new_nose_time.tz_localize('UTC')

        if new_nose_time > existing_nose_time:
            return True

    except Exception:
        return True

    return False

def get_most_recent_unstruck_per_tf(signals, tf):
    tf_signals = [s for s in signals if s.get('timeframe') == tf and not s.get('struck', False)]
    if not tf_signals:
        return []
    by_bias = {}
    for sig in tf_signals:
        bias = sig.get('bias')
        if bias not in by_bias or sig.get('born_on', 0) > by_bias[bias].get('born_on', 0):
            by_bias[bias] = sig
    return list(by_bias.values())

def should_place_reversal(sig):
    if not sig.get('eighth_idx'):
        return False
    born = sig.get('born_on')
    if born and (datetime.now() - born).total_seconds() > 300:
        return True
    return False

def passes_activation_gate(eighth_idx, swings_after, chain_type):
    """
    Activation gate — v15.9.5: +1 swing on every chain (push affordance right).
    - pico: was 0 → now 1
    - nano: was 1 → now 2
    - micro: was 2 → now 3
    - macro: was 3 → now 4
    """
    if not swings_after:
        return False

    extra_swings = sum(1 for idx in swings_after if idx > eighth_idx)

    min_required = {
        'pico': 1,      # +1 from 0
        'nano': 2,      # +1 from 1
        'micro': 3,     # +1 from 2
        'macro': 4,     # +1 from 3
    }.get(chain_type, 2)

    return extra_swings >= min_required
    
def get_recent_harbor_signals(df_input, timeframe_or_dict=None, args=None, dev: bool = False):
    """
    v15.9.2 - Clean fractal version (no layover logic inside)
    - 1m uses raw triplets (pico/nano) with activation gate
    - All other TFs use centre-of-5 noses
    - Swapped bias + safe indexing
    - Uses your exact MICRO/MACRO constants
    """
    if isinstance(df_input, dict):
        df_dict = df_input
        dev_flag = getattr(args, 'dev', dev)
    else:
        df_dict = {timeframe_or_dict: df_input}
        dev_flag = getattr(args, 'dev', dev) if args else dev

    all_signals = []
    seen = {}

    for tf, df in df_dict.items():
        if df is None or len(df) < 100:
            continue

        if tf in ['1d', '1w', '1M']:
            df = df.tail(400).copy()
        else:
            df = df.tail(900).copy()

        if tf in ['15m', '1h', '4h', '1d', '1w', '1M']:
            recency_cutoff = df.index[-1] - pd.Timedelta(hours=48 if tf in ['15m','1h'] else 96)
            df = df[df.index >= recency_cutoff].copy()

        up_swings, down_swings = triplet_swings(df)
        signals = []

        # === Mode Selection ===
        if tf == '1m':
            use_raw_triplets = True
            if dev_flag:
                print(f"[{tf}] Using RAW TRIPLETS on 1m (for pico/nano)")
        else:
            use_raw_triplets = False
            if dev_flag:
                print(f"[{tf}] Using Centre-of-5 noses")

        if use_raw_triplets:
            # RAW TRIPLET MODE (1m)
            for i in range(3, len(up_swings)):
                if i + 2 >= len(up_swings): continue
                nose_idx = up_swings[i]
                man_idx = up_swings[i + 1]
                eighth_idx = up_swings[i + 2]
                ups_after = [idx for idx in up_swings if idx > nose_idx]

                if not passes_activation_gate(eighth_idx, ups_after, 'pico' if (i % 2 == 0) else 'nano'):
                    continue

                nose_price = float(df['high'].iloc[nose_idx])
                a1_p = float(df['low'].iloc[man_idx])
                a2_p = float(df['close'].iloc[eighth_idx])
                if a2_p <= a1_p * 1.001:
                    a2_p = a1_p * 1.007

                delta_t = max((df.index[eighth_idx] - df.index[man_idx]).total_seconds() / 3600, 0.25)
                slope = min((a2_p - a1_p) / delta_t, 220)
                leading = round(a1_p + slope * 1.15, 2)
                leading = min(leading, float(df['close'].iloc[-1]) * 1.085)

                sig = {
                    'timeframe': tf,
                    'bias': 'long',
                    'chain_type': 'pico' if (i % 2 == 0) else 'nano',
                    'nose_idx': nose_idx,
                    'nose_price': nose_price,
                    'nose_time': str(df.index[nose_idx]),
                    'man_idx': man_idx,
                    'man_time': str(df.index[man_idx]),
                    'eighth_idx': eighth_idx,
                    'eighth_time': str(df.index[eighth_idx]),
                    'anchor1_price': a1_p,
                    'anchor1_time': str(df.index[man_idx]),
                    'anchor2_price': a2_p,
                    'anchor2_time': str(df.index[eighth_idx]),
                    'leading_limit': leading,
                    'slope': slope,
                    'born_on': df.index[nose_idx],
                    'activation_offset': 1,
                    'hotspot_score': 0,
                    'sailfish_strength': 1.0,
                    'opposite_bias_retracement': False
                }
                signals.append(sig)

            # SHORTS block (exactly the same as you have)
            for i in range(3, len(down_swings)):
                if i + 2 >= len(down_swings): continue
                nose_idx = down_swings[i]
                man_idx = down_swings[i + 1]
                eighth_idx = down_swings[i + 2]
                downs_after = [idx for idx in down_swings if idx > nose_idx]

                if not passes_activation_gate(eighth_idx, downs_after, 'pico' if (i % 2 == 0) else 'nano'):
                    continue

                nose_price = float(df['low'].iloc[nose_idx])
                a1_p = float(df['high'].iloc[man_idx])
                a2_p = float(df['close'].iloc[eighth_idx])
                if a2_p >= a1_p * 0.999:
                    a2_p = a1_p * 0.993

                delta_t = max((df.index[eighth_idx] - df.index[man_idx]).total_seconds() / 3600, 0.25)
                slope = max((a2_p - a1_p) / delta_t, -220)
                leading = round(a1_p + slope * 1.15, 2)
                leading = max(leading, float(df['close'].iloc[-1]) * 0.915)

                sig = {
                    'timeframe': tf,
                    'bias': 'short',
                    'chain_type': 'pico' if (i % 2 == 0) else 'nano',
                    'nose_idx': nose_idx,
                    'nose_price': nose_price,
                    'nose_time': str(df.index[nose_idx]),
                    'man_idx': man_idx,
                    'man_time': str(df.index[man_idx]),
                    'eighth_idx': eighth_idx,
                    'eighth_time': str(df.index[eighth_idx]),
                    'anchor1_price': a1_p,
                    'anchor1_time': str(df.index[man_idx]),
                    'anchor2_price': a2_p,
                    'anchor2_time': str(df.index[eighth_idx]),
                    'leading_limit': leading,
                    'slope': slope,
                    'born_on': df.index[nose_idx],
                    'activation_offset': 1,
                    'hotspot_score': 0,
                    'sailfish_strength': 1.0,
                    'opposite_bias_retracement': False
                }
                signals.append(sig)

        else:
            # ==================== CENTRE-OF-5 NOSES (3m+) - SAFE + FRACTAL ====================
            long_noses = []
            short_noses = []

            # LONGS: centre of 5 higher highs
            for i in range(2, len(up_swings) - 2):
                window = up_swings[i-2:i+3]
                prices = [float(df['high'].iloc[j]) for j in window]
                if prices[2] == max(prices):
                    long_noses.append((window[2], prices[2]))

            # SHORTS: centre of 5 lower lows
            for i in range(2, len(down_swings) - 2):
                window = down_swings[i-2:i+3]
                prices = [float(df['low'].iloc[j]) for j in window]
                if prices[2] == min(prices):
                    short_noses.append((window[2], prices[2]))

            layers = [
                ('pico',  MIN_CHAIN_PICO,  MAN_OFFSET_PICO,  EIGHTH_OFFSET_PICO,  ACTIVATION_OFFSET_PICO, 180, 1.4),
                ('nano',  MIN_CHAIN_NANO,  MAN_OFFSET_NANO,  EIGHTH_OFFSET_NANO,  ACTIVATION_OFFSET_NANO, 220, 1.1),
                ('micro', MIN_CHAIN_MICRO, MAN_OFFSET_MICRO, EIGHTH_OFFSET_MICRO, ACTIVATION_OFFSET_MICRO, 120, 3.2),
                ('macro', MIN_CHAIN_MACRO, MAN_OFFSET_MACRO, EIGHTH_OFFSET_MACRO, ACTIVATION_OFFSET_MACRO, 80,  7.0),
            ]

            for chain_type, min_chain, man_off, eighth_off, act_off, slope_cap, hours_ahead in layers:
                # LONGS
                for nose_idx, nose_price in long_noses[-10:]:
                    ups_after = [idx for idx in up_swings if idx > nose_idx]
                    if len(ups_after) <= eighth_off:
                        continue
                    man_idx = ups_after[man_off]
                    eighth_idx = ups_after[eighth_off]

                    if not passes_activation_gate(eighth_idx, ups_after, chain_type):
                        continue

                    a1_p = float(df['low'].iloc[man_idx])
                    a2_p = float(df['close'].iloc[eighth_idx])
                    if a2_p <= a1_p * 1.001:
                        a2_p = a1_p * 1.007

                    delta_t = max((df.index[eighth_idx] - df.index[man_idx]).total_seconds() / 3600, 0.6)
                    slope = min((a2_p - a1_p) / delta_t, slope_cap)

                    leading = round(a1_p + slope * hours_ahead, 2)
                    leading = min(leading, float(df['close'].iloc[-1]) * (1 + 0.085))

                    sig = {
                        'timeframe': tf,
                        'bias': 'long',
                        'chain_type': chain_type,
                        'nose_idx': nose_idx,
                        'nose_price': float(nose_price),
                        'nose_time': str(df.index[nose_idx]),
                        'man_idx': man_idx,
                        'man_time': str(df.index[man_idx]),
                        'eighth_idx': eighth_idx,
                        'eighth_time': str(df.index[eighth_idx]),
                        'anchor1_price': a1_p,
                        'anchor1_time': str(df.index[man_idx]),
                        'anchor2_price': a2_p,
                        'anchor2_time': str(df.index[eighth_idx]),
                        'leading_limit': leading,
                        'slope': slope,
                        'born_on': df.index[nose_idx],
                        'activation_offset': act_off,
                        'hotspot_score': 1 if chain_type in ['micro', 'macro'] else 0,
                        'sailfish_strength': 1.0 if chain_type in ['pico', 'nano'] else 0.6
                    }
                    signals.append(sig)

                # SHORTS
                for nose_idx, nose_price in short_noses[-10:]:
                    downs_after = [idx for idx in down_swings if idx > nose_idx]
                    if len(downs_after) <= eighth_off:
                        continue
                    man_idx = downs_after[man_off]
                    eighth_idx = downs_after[eighth_off]

                    if not passes_activation_gate(eighth_idx, downs_after, chain_type):
                        continue

                    a1_p = float(df['high'].iloc[man_idx])
                    a2_p = float(df['close'].iloc[eighth_idx])
                    if a2_p >= a1_p * 0.999:
                        a2_p = a1_p * 0.993

                    delta_t = max((df.index[eighth_idx] - df.index[man_idx]).total_seconds() / 3600, 0.6)
                    slope = max((a2_p - a1_p) / delta_t, -slope_cap)

                    leading = round(a1_p + slope * hours_ahead, 2)
                    leading = max(leading, float(df['close'].iloc[-1]) * (1 - 0.085))

                    sig = {
                        'timeframe': tf,
                        'bias': 'short',
                        'chain_type': chain_type,
                        'nose_idx': nose_idx,
                        'nose_price': float(nose_price),
                        'nose_time': str(df.index[nose_idx]),
                        'man_idx': man_idx,
                        'man_time': str(df.index[man_idx]),
                        'eighth_idx': eighth_idx,
                        'eighth_time': str(df.index[eighth_idx]),
                        'anchor1_price': a1_p,
                        'anchor1_time': str(df.index[man_idx]),
                        'anchor2_price': a2_p,
                        'anchor2_time': str(df.index[eighth_idx]),
                        'leading_limit': leading,
                        'slope': slope,
                        'born_on': df.index[nose_idx],
                        'activation_offset': act_off,
                        'hotspot_score': 1 if chain_type in ['micro', 'macro'] else 0,
                        'sailfish_strength': 1.0 if chain_type in ['pico', 'nano'] else 0.6
                    }
                    signals.append(sig)

        # DEDUPLICATION (unchanged)
        for sig in signals:
            base_price = sig['leading_limit']
            price_key = f"{tf}_{sig['bias']}_{round(base_price, 1)}"
            score = sig.get('hotspot_score', 0)
            if price_key not in seen or score > seen[price_key].get('score', -1):
                sig['score'] = score
                seen[price_key] = sig

    final_signals = list(seen.values())
    final_signals.sort(key=lambda x: x.get('score', 0), reverse=True)

    print(f"[HARBOUR] Generated {len(final_signals)} unique signals after dedup "
          f"(from {len(seen)} candidates)")

    return final_signals

def flip_to_phase(tf_data, tf, new_direction, price, size, line, trades, current_time, i, stop_buffer, min_delta_pct, fees):
    d = tf_data[tf]
    
    if d.get('position'):
        old_dir, old_entry, old_size = d['position']
        if old_dir == new_direction:
            return False
        delta = abs(price - old_entry) / old_entry * 100
        if delta < min_delta_pct + fees:
            return False
        profit = (price - old_entry) * old_size * LEVERAGE if old_dir == "long" else (old_entry - price) * old_size * LEVERAGE
        fee_cost = abs(profit) * fees / 100
        d['capital'] += profit - fee_cost
        d['total_fees'] = d.get('total_fees', 0) + fee_cost
        d['wins' if profit > 0 else 'losses'] += 1
        d['total_trades'] += 1
        trades.append(f"{current_time} {tf} REVERSE {old_dir.upper()} → {new_direction.upper()} P/L {profit:.2f}")
    else:
        trades.append(f"{current_time} {tf} OPEN {new_direction.upper()} @ {price:.2f} ({line.get('chain_type','pico')})")

    # === NEW SAFE STOP LOGIC ===
    # Use A2 price as base + small buffer, or fallback to current price
    a2_price = line.get('anchor2_price', price)
    if new_direction == "long":
        sl_level = a2_price * (1 - stop_buffer * 3)      # wider buffer for long
    else:
        sl_level = a2_price * (1 + stop_buffer * 3)

    d['position'] = (new_direction, price, size)
    d['stop_loss_level'] = sl_level
    d['last_flip_candle_idx'] = i
    line['disabled'] = True
    return True

import json
import pandas as pd
from datetime import datetime
import os

LAYOVER_FILE = "harbor_layover.json"   # ← change path if you want

def create_harbor_layover_from_backtest(df_dict, end_time=None, dev: bool = False):
    """
    Run full backtest-style signal generation on historical data up to `end_time`
    and save the MOST RECENT active Harbor signals as the layover file.
    """
    if end_time is None:
        # If no cutoff given, use the very last timestamp in the data
        end_time = max(df.index[-1] for df in df_dict.values() if df is not None)

    # Create a "frozen" copy of the data up to the cutoff (simulates backtest end)
    backtest_df_dict = {}
    for tf, df in df_dict.items():
        if df is not None:
            backtest_df_dict[tf] = df[df.index <= end_time].copy()

    if dev:
        print(f"[LAYOVER] Creating layover from backtest data ending at {end_time}")

    # === FIX: Generate most-recent Harbor signals from final backtest state ===
    final_signals = get_recent_harbor_signals(backtest_df_dict, dev=dev)

    # Add metadata so we know when the layover was created
    layover_payload = {
        "created_at": datetime.now().isoformat(),
        "backtest_end_time": str(end_time),
        "total_signals": len(final_signals),
        "signals": final_signals
    }

    # Save
    with open(LAYOVER_FILE, "w") as f:
        json.dump(layover_payload, f, indent=2, default=str)

    print(f"[LAYOVER] ✅ Saved {len(final_signals)} most-recent Harbor signals "
          f"to {LAYOVER_FILE} (backtest ended {end_time})")

    return final_signals


def load_harbor_layover_for_live(dev: bool = False):
    """
    Load the layover file and return the signals so live mode can seed its engine.
    Returns [] if no layover exists (first-time run).
    """
    if not os.path.exists(LAYOVER_FILE):
        if dev:
            print("[LAYOVER] No layover file found — starting live with clean slate")
        return []

    with open(LAYOVER_FILE, "r") as f:
        payload = json.load(f)

    signals = payload.get("signals", [])
    created = payload.get("created_at", "unknown")

    if dev:
        print(f"[LAYOVER] ✅ Loaded {len(signals)} Harbor signals from backtest "
              f"(created {created})")

    return signals

def fetch_ohlcv(symbol, timeframe, days_back):
    exchange = ccxt.binance({'enableRateLimit': True})
    since = exchange.milliseconds() - int((days_back + EXTRA_HISTORY_DAYS) * 24 * 60 * 60 * 1000)
    ohlcv = []
    limit = 1000
    attempt = 0
    while since < exchange.milliseconds() and attempt < 8:
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
            if not batch: break
            ohlcv += batch
            since = batch[-1][0] + 1
            time.sleep(exchange.rateLimit / 1000.0)
            if len(batch) < limit: break
        except Exception:
            attempt += 1
            time.sleep(1.5)
            since += 60 * 1000
            continue
    if not ohlcv: return None
    df = pd.DataFrame(ohlcv, columns=['time','open','high','low','close','volume'])
    df['time'] = pd.to_datetime(df['time'], unit='ms')
    df.set_index('time', inplace=True)
    return df[['open','high','low','close','volume']].astype(float)

def setup_logging(symbol, tf, dev_mode=False, verbose=False):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = f"backtest_full_log_{symbol.replace('/', '')}_{tf}_{ts}.txt"
    logger = logging.getLogger()
    logger.setLevel(logging.INFO if not verbose else logging.DEBUG)
    logger.handlers = []
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter('%(asctime)s | %(message)s'))
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(console_handler)
    return log_file

# ==================== BACKTEST ====================
def run_backtest_for_symbol(symbol, args):
    days = args.test_days if getattr(args, 'test_days', 0) > 0 and args.dev else args.days_back
    log_file = setup_logging(symbol, args.timeframe, dev_mode=args.dev)
    logging.info(f"\n🚀 Starting 15.1 Backtest | {symbol} | TF: {args.timeframe} | Days: {days} | 4-Layer System")

    global LEVERAGE
    LEVERAGE = 25 if getattr(args, 'hunt', False) else 20
    fees = getattr(args, 'fees', DEFAULT_FEES)
    
    hit_stats = {
        'total_signals': 0,
        'hits': 0,
        'hotspot_hits': 0,
        'glide_hits': 0,
        'by_chain': defaultdict(lambda: {'total':0, 'hits':0})
    }

    df = fetch_ohlcv(symbol, args.timeframe, days)
    if df is None or len(df) < 300:
        logging.error(f"Not enough data for {symbol}")
        return None, 0, 0

    tfs = ['1m','3m','5m','15m','1h','4h','1d']
    extra_frames = {tf: fetch_ohlcv(symbol, tf, days + EXTRA_HISTORY_DAYS) for tf in tfs}

    tf_lines = {'short': defaultdict(list), 'long': defaultdict(list)}

    for tf, fdf in extra_frames.items():
        if fdf is None or fdf.empty:
            continue

        signals = get_recent_harbor_signals(fdf, tf, args)
        for sig in signals:
            line = {'tf': tf, **sig}
            tf_lines[sig['bias']][tf].append(line)

    active_tfs = list(set(tf for bias in tf_lines for tf in tf_lines[bias]))

    # === PER CHAIN TYPE STATS ===
    chain_stats = {layer: {'trades': 0, 'wins': 0, 'pnl': 0.0}
                   for layer in ['pico', 'nano', 'micro', 'macro']}

    tf_data = {tf: {
        'capital': 1000.0 / max(1, len(active_tfs)),
        'initial_capital': 1000.0 / max(1, len(active_tfs)),
        'position': None,
        'stop_loss_level': None,
        'total_trades': 0, 'wins': 0, 'losses': 0,
        'total_fees': 0.0,
        'last_flip_candle_idx': -999,
        'hedges': [],
        'hedge_capital': 0.0,
        'hedge_cap_logged': False,
        'last_hedge_time': 0,
    } for tf in active_tfs}

    tf_stats = {tf: {'trades': 0, 'wins': 0} for tf in active_tfs}
    peak_cap = 1000.0
    max_dd_pct = 0.0
    trades = []
    candle_counter = 0

    pbar = tqdm(total=len(df)-200, desc=f"{symbol} {args.timeframe}", unit=" candles", disable=not args.dev)

    for i in range(200, len(df)):
        current_time = df.index[i]
        current_price = float(df['close'].iloc[i])
        last_low = float(df['low'].iloc[i])
        last_high = float(df['high'].iloc[i])

        # Capital tracking
        realized = sum(d['capital'] for d in tf_data.values())
        unrealized = sum(
            (current_price - entry) * sz * LEVERAGE if dir_ == "long" else (entry - current_price) * sz * LEVERAGE
            for d in tf_data.values() if (pos := d.get('position')) is not None
            for dir_, entry, sz in [pos]
        )

        total_cap = realized + unrealized
        dd_pct = (peak_cap - total_cap) / peak_cap * 100 if peak_cap > 0 else 0
        max_dd_pct = max(max_dd_pct, dd_pct)
        peak_cap = max(peak_cap, total_cap)

        # === Stop hits ===
        for tf, d in list(tf_data.items()):
            if not d.get('position'): continue
            dir_, entry, sz = d['position']
            sl = d.get('stop_loss_level')
            if sl is None: continue
            hit = (dir_ == "long" and current_price <= sl) or (dir_ == "short" and current_price >= sl)
            if hit:
                profit = (sl - entry) * sz * LEVERAGE if dir_ == "long" else (entry - sl) * sz * LEVERAGE
                fee_cost = abs(profit) * fees / 100
                d['capital'] += profit - fee_cost
                d['total_fees'] += fee_cost
                d['wins' if profit > 0 else 'losses'] += 1
                d['total_trades'] += 1
                tf_stats[tf]['trades'] += 1
                tf_stats[tf]['wins'] += 1 if profit > 0 else 0
                trades.append(f"{current_time} {tf} NOSE STOP {dir_.upper()} P/L {profit:.2f}")
                d['position'] = None
                d['stop_loss_level'] = None

        # === Signal hits ===
        for tf in active_tfs:
            if i - tf_data[tf]['last_flip_candle_idx'] < (20 if tf in ['3m','5m'] else 6):
                continue

            risk_based_size = (tf_data[tf]['capital'] * (MAX_RISK_PER_TF_PCT / 100)) / current_price / LEVERAGE
            hard_cap_size = MAX_POSITION_USD / current_price / LEVERAGE
            size = min(risk_based_size, hard_cap_size)

            for bias in ['short', 'long']:
                for line in tf_lines[bias][tf]:
                    if line.get('disabled', False): 
                        continue
                    if current_time < line.get('activation_time', current_time): 
                        continue

                    hours = min((current_time - pd.to_datetime(line['anchor1_time'])).total_seconds() / 3600, 48)
                    base_trend = line['anchor1_price'] + line.get('slope', 0) * hours
                    lead_offset = LEAD_FACTOR * abs(line.get('slope', 0)) * hours
                    leading_limit = base_trend - lead_offset if bias == "short" else base_trend + lead_offset

                    hit = (bias == "short" and last_high >= leading_limit) or \
                          (bias == "long" and last_low <= leading_limit)

                    # === ALWAYS COUNT SIGNALS (moved outside the hit block) ===
                    hit_stats['total_signals'] += 1
                    chain = line.get('chain_type', 'pico')
                    hit_stats['by_chain'][chain]['total'] += 1

                    if hit:
                        hit_stats['hits'] += 1
                        hit_stats['by_chain'][chain]['hits'] += 1
                        
                        # Glide / Hotspot classification
                        base = line.get('leading_limit', line.get('anchor2_price', 0))
                        if base > 0 and abs(current_price - base) / base < 0.0015:
                            hit_stats['hotspot_hits'] += 1
                        elif leading_limit > 0 and abs(current_price - leading_limit) / leading_limit < 0.002:
                            hit_stats['glide_hits'] += 1

                        line['disabled'] = True
                        chain_type = line.get('chain_type', 'pico')

                        if flip_to_phase(tf_data, tf, bias, current_price, size, line, trades, 
                                       current_time, i,
                                       getattr(args, 'stop_buffer', STOP_BUFFER),
                                       getattr(args, 'min_delta_pct', DEFAULT_MIN_DELTA_PCT), 
                                       fees):
                            if args.dev:
                                logging.info(f"[{tf}] {bias.upper()} {chain_type.upper()} PHASE @ {current_price:.2f}")

                            chain_stats[chain_type]['trades'] += 1

                            if tf in HIGHER_TFS:
                                cycle_profits(tf_data, tf, args)
                            if tf != '1h':   # temporary safety
                                create_topiary_hedge(tf, bias, current_price, size, tf_data)

                        break  # only take first hit per direction per TF

        # v15.2 rich hedge update (safe in backtest — no real orders placed)
        update_topiary_hedges_v155(tf_data, current_price, args, 
                                   current_signals=None, symbol=None, wallet=None)
        candle_counter += 1
        pbar.update(1)

        if candle_counter % 500 == 0 and args.dev:
            logging.info(f"Processed {candle_counter:,} candles | Capital ≈ ${total_cap:,.0f} | DD {dd_pct:.1f}%")

    pbar.close()

    # === BACKTEST END → CREATE LAYOVER ===
    print("\n[BACKTEST] Finished. Creating layover for live mode...")
    create_harbor_layover_from_backtest(
        df_dict=extra_frames,          # ← correct df_dict
        end_time=df.index[-1],         # ← last candle
        dev=args.dev
    )

    # Final settlement
    final_price = float(df['close'].iloc[-1])
    for tf, d in tf_data.items():
        if d.get('position'):
            dir_, entry, sz = d['position']
            profit = (final_price - entry) * sz * LEVERAGE if dir_ == "long" else (entry - final_price) * sz * LEVERAGE
            fee_cost = abs(profit) * fees / 100
            d['capital'] += profit - fee_cost
            d['capital'] = min(d['capital'], d['initial_capital'] * getattr(args, 'max_capital_mult', 5.0))
            d['total_fees'] += fee_cost
            d['total_trades'] += 1
            d['wins' if profit > 0 else 'losses'] += 1

    final_cap = sum(d['capital'] for d in tf_data.values())
    total_trades = sum(d['total_trades'] for d in tf_data.values())
    wins = sum(d['wins'] for d in tf_data.values())
    total_fees = sum(d.get('total_fees', 0) for d in tf_data.values())

    # Better per-chain PnL approximation
    for layer in chain_stats:
        layer_trades = chain_stats[layer]['trades']
        if layer_trades > 0 and total_trades > 0:
            chain_stats[layer]['pnl'] = (final_cap - 1000.0) * (layer_trades / total_trades)
        else:
            chain_stats[layer]['pnl'] = 0.0

    print("\n" + "="*60)
    print("HOTSPOT / GLIDE HIT ANALYSIS")
    print("="*60)
    print(f"Total Signals Generated : {hit_stats['total_signals']}")
    print(f"Total Hits              : {hit_stats['hits']} ({hit_stats['hits']/hit_stats['total_signals']*100:.1f}%)")
    print(f"True Hotspot Hits       : {hit_stats['hotspot_hits']}")
    print(f"Glide-assisted Hits     : {hit_stats['glide_hits']}")
    print("\nBy Chain Type:")
    for chain in ['pico','nano','micro','macro']:
        if chain in hit_stats['by_chain']:
            stats = hit_stats['by_chain'][chain]
            print(f"  {chain:>6}: {stats['hits']:3d}/{stats['total']:3d} hits ({stats['hits']/stats['total']*100:.1f}%)")

    # ====================== FINAL REPORT ======================
    logging.info(f"\n{'='*80}")
    logging.info("FINAL BACKTEST RESULTS - 4 LAYER BREAKDOWN")
    logging.info(f"{'='*80}")
    logging.info(f"Final Capital : ${final_cap:,.2f} | Max DD: {max_dd_pct:.2f}%")
    logging.info(f"Total Trades  : {total_trades} | Win Rate: {wins/total_trades*100 if total_trades else 0:.1f}%")
    logging.info(f"Total Fees    : ${total_fees:,.2f}\n")

    logging.info("PER CHAIN TYPE:")
        # Better per-chain PnL approximation
    for layer in chain_stats:
        layer_trades = chain_stats[layer]['trades']
        if layer_trades > 0:
            chain_stats[layer]['pnl'] = (final_cap - 1000.0) * (layer_trades / total_trades)
        else:
            chain_stats[layer]['pnl'] = 0.0

    logging.info("\nPer-TF:")
    all_tfs_order = ['1m', '3m', '5m', '15m', '1h', '4h', '1d', '1w', '1M']
    for tf in all_tfs_order:
        s = tf_stats.get(tf, {'trades': 0, 'wins': 0})
        wr = (s['wins'] / s['trades'] * 100) if s['trades'] > 0 else 0.0
        pnl = tf_data.get(tf, {}).get('capital', 0) - tf_data.get(tf, {}).get('initial_capital', 0)
        logging.info(f"  {tf:>4} : {s['trades']:3d} trades | {wr:5.1f}% win | P/L ${pnl:,.2f}")

    print_hedge_summary(tf_data, args)

    if args.dev:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"backtest_readout_{symbol.replace('/', '')}_{args.timeframe}_{ts}.txt"
        with open(fname, "w", encoding='utf-8') as f:
            f.write(f"Backtest Readout 15.1 - {symbol} {args.timeframe} {datetime.now()}\n")
            f.write(f"Final capital: ${final_cap:,.2f} | Max DD: {max_dd_pct:.2f}% | Trades: {total_trades}\n")
        logging.info(f"✅ Readout saved → {fname}")

    return final_cap, max_dd_pct, total_fees

def get_filled_positions(wallet, symbol):
    """Returns actual filled positions from Hyperliquid (not resting limits)"""
    try:
        exchange = get_hyperliquid_exchange(wallet, testnet=args.testnet if hasattr(args, 'testnet') else False)
        user_state = exchange.info.user_state(address=wallet.address)
        positions = user_state.get('positions', [])
        coin = symbol.split('/')[0]

        filled = {'long': 0.0, 'short': 0.0}
        for pos in positions:
            if pos.get('coin') == coin:
                szi = float(pos.get('szi', 0))
                if szi > 0:
                    filled['long'] = abs(szi)
                elif szi < 0:
                    filled['short'] = abs(szi)
        return filled
    except Exception as e:
        print(f"[FILLED POS ERROR] {e}")
        return {'long': 0.0, 'short': 0.0}

def detect_candle_close(tf, last_known_time, symbol):
    """Detect new candle close for glide manager"""
    try:
        df = fetch_ohlcv(symbol, tf, days_back=1)
        if df is not None and len(df) > 0:
            latest = df.index[-1]
            if latest.tz is None:
                latest = latest.tz_localize('UTC')
            if last_known_time is None or latest > last_known_time:
                return latest
    except Exception as e:
        print(f"[CANDLE DETECT ERROR] {e}")
    return None

def run_live_mode(args):
    from datetime import datetime, timezone
    import time
    import json
    import os
    import pandas as pd

    print("🚀 Starting LIVE MODE v15.9.5 — 10× · 0.01 BTC · Reversals · ACT+1")

    symbol = args.symbols.split(',')[0].strip()
    verbose = getattr(args, 'verbose', False)
    dev = getattr(args, 'dev', False)

    private_key = os.getenv('PRIVATE_KEY')
    if not private_key:
        raise ValueError("PRIVATE_KEY environment variable not set!")

    wallet = Account.from_key(private_key)
    account_address = get_account_address(wallet)
    is_testnet = bool(getattr(args, 'testnet', False))

    # Optional explicit override (e.g. BALANCE_OVERRIDE=435.23)
    balance_override = os.getenv('BALANCE_OVERRIDE')
    balance_override = float(balance_override) if balance_override else None

    # One-time isolated margin setup (on the funded account)
    setup_isolated_margin("BTC", testnet=is_testnet, wallet=wallet, account_address=account_address)
    print(f"✅ Signer (agent/key): {wallet.address}")
    print(f"✅ Account (funds)   : {account_address}")
    if wallet.address.lower() != account_address.lower():
        print("   → Agent mode: signing with API wallet, balances/orders on master account")

    LOWER_TFS = ['1m', '3m', '5m', '15m']
    HIGHER_TFS = ['1h', '4h', '1d', '1w']

    try:
        exchange = get_hyperliquid_exchange(wallet, testnet=is_testnet, account_address=account_address)
        total_balance, bal_detail = fetch_account_equity(exchange, account_address, balance_override)
        if total_balance < 1.0:
            total_balance = balance_override or 435.23
            print(f"[BALANCE] API near $0 on {account_address[:12]}… — using ${total_balance:,.2f}")
            print(f"   Detail: perps_av={bal_detail.get('perps_av')} wd={bal_detail.get('perps_wd')} spot={bal_detail.get('spot_usdc')}")
            print("   Tip: set ACCOUNT_ADDRESS=<funded master> and/or BALANCE_OVERRIDE=435.23 in .env")
        else:
            print(f"✅ Balance: ${total_balance:,.2f} USDC "
                  f"(perps ${bal_detail.get('perps_av',0):,.2f} | wd ${bal_detail.get('perps_wd',0):,.2f} | "
                  f"spot ${bal_detail.get('spot_usdc',0):,.2f})")
    except Exception as e:
        total_balance = balance_override or 435.23
        print(f"[BALANCE] Using fallback ${total_balance:,.2f} | Error: {e}")

    # === INITIALISE CORE OBJECTS FIRST ===
    active_limits = {}
    roster = AffordanceRoster(max_slots_per_tf=8)
    tf_data = {}
    for t in LOWER_TFS + HIGHER_TFS:
        tf_cap = total_balance / 9.0
        tf_data[t] = {
            'capital': tf_cap,
            'initial_capital': tf_cap,
            'position': None,
            'hedges': [],
            'last_nose': None,
            'last_flip_time': 0.0,
            'recent_structures': [],
            'tapered_profit': 0.0,
            'total_fees': 0.0,
            'stop_loss_level': None,
        }

    # v15.9.4 session risk state
    session_peak_equity = total_balance
    session_fees = 0.0
    kill_switch_until = 0.0          # unix ts — no new harbors until this time
    nose_stops_this_session = 0
    equity_valid_cycles = 0          # don't arm DD kill-switch until we have real readings

    # === LIVE STARTUP — LOAD LAYOVER (smart filter) ===
    print("[LIVE] Checking for backtest layover...")
    layover_signals = load_harbor_layover_for_live(dev=dev)

    # Fetch a real mid ASAP so we can filter stale layover lines
    try:
        mids0 = exchange.info.all_mids()
        current_market_price = float(mids0.get(symbol.split('/')[0], 64000.0))
    except Exception:
        current_market_price = 64000.0

    LAYOVER_MAX_DIST_PCT = 0.06      # only keep signals within ±6% of market
    LAYOVER_MAX_SEED = 18            # hard cap on seeded lines
    if layover_signals:
        seeded = 0
        for sig in sorted(layover_signals,
                          key=lambda s: abs(float(s.get('leading_limit', 0)) - current_market_price)):
            if seeded >= LAYOVER_MAX_SEED:
                break
            lead = float(sig.get('leading_limit', 0) or 0)
            if lead <= 0:
                continue
            dist = abs(lead - current_market_price) / current_market_price
            if dist > LAYOVER_MAX_DIST_PCT:
                continue
            tf = sig['timeframe']
            bias = sig['bias']
            chain_type = sig.get('chain_type', 'pico')
            key = f"{tf}_{bias}_{chain_type}"
            if key in active_limits:
                continue
            active_limits[key] = {
                **sig,
                'price': lead,
                'size': 0.001,          # tiny placeholder — real size on live place
                'oid': None,            # not yet on exchange
                'time': time.time(),
                'tf': tf,
                'bias': bias,
                'chain_type': chain_type,
                'glide_origin_price': lead,
                'glide_origin_ts': time.time(),
                'activated': False,
                'from_layover': True,
            }
            roster.allocate(tf, bias, chain_type, 0.001 * current_market_price, key)
            seeded += 1
        print(f"[LIVE] ✅ Seeded {seeded}/{len(layover_signals)} nearby layover lines "
              f"(≤{LAYOVER_MAX_DIST_PCT*100:.0f}% of ${current_market_price:,.0f})")
        if seeded == 0:
            print("[LIVE] All layover signals too far from market — starting clean")
    else:
        print("[LIVE] No layover — starting clean")

    last_cleanup = time.time()
    last_hedge_update = time.time()
    glide_manager = GlideExecutionManager(
        active_limits_ref=active_limits, 
        wallet=wallet,
        testnet=is_testnet
    )

    sync_filled_positions(tf_data, symbol, wallet, testnet=is_testnet, account_address=account_address)

    while True:
        try:
            current_time = datetime.now(timezone.utc)
            current_ts = time.time()
            mids = exchange.info.all_mids()
            current_market_price = float(mids.get(symbol.split('/')[0], 80000.0))

            sync_filled_positions(tf_data, symbol, wallet, testnet=is_testnet, account_address=account_address)

            # ─── REVERSALS / ADD-ONS on limit fills (v15.9.5) ───
            try:
                process_limit_fills_and_reversals(
                    active_limits, tf_data, current_market_price, symbol, wallet,
                    testnet=is_testnet, account_address=account_address,
                    order_size=TARGET_ORDER_SIZE_BTC
                )
            except Exception as e:
                print(f"[REVERSE/ADD ERROR] {e}")

            # ─── LIVE EQUITY + KILL-SWITCH / NOSE-STOP MONITOR ───
            try:
                live_equity, _bd = fetch_account_equity(exchange, account_address, balance_override)
                if live_equity < 1.0:
                    live_equity = total_balance
                else:
                    total_balance = live_equity
                    equity_valid_cycles += 1
            except Exception:
                live_equity = total_balance

            session_peak_equity = max(session_peak_equity, live_equity)
            session_dd_pct = (session_peak_equity - live_equity) / session_peak_equity * 100 if session_peak_equity > 0 else 0.0

            # Hard session DD circuit — only after ≥3 valid readings AND equity is meaningful
            if (equity_valid_cycles >= 3 and live_equity >= 50.0
                    and session_dd_pct >= SESSION_MAX_DD_PCT
                    and current_ts > kill_switch_until):
                kill_switch_until = current_ts + KILL_SWITCH_COOLDOWN_SEC
                print(f"[KILL-SWITCH] Session DD {session_dd_pct:.1f}% ≥ {SESSION_MAX_DD_PCT}% → pause new harbors for {KILL_SWITCH_COOLDOWN_SEC//60} min")

            # Per-position nose-stop (−2 %)
            for tf, d in list(tf_data.items()):
                pos = d.get('position')
                if not pos:
                    continue
                if len(pos) >= 3:
                    side, entry, size = pos[0], pos[1], pos[2]
                else:
                    continue
                # Ensure a stop level exists (set on first sight of the position)
                if d.get('stop_loss_level') is None:
                    if side == 'long':
                        d['stop_loss_level'] = entry * (1.0 - NOSE_STOP_PCT)
                    else:
                        d['stop_loss_level'] = entry * (1.0 + NOSE_STOP_PCT)
                sl = d['stop_loss_level']
                hit = (side == 'long' and current_market_price <= sl) or \
                      (side == 'short' and current_market_price >= sl)
                if hit:
                    # Close via market (reduce-only style)
                    close_side = 'sell' if side == 'long' else 'buy'
                    print(f"[NOSE STOP] {tf} {side.upper()} hit −{NOSE_STOP_PCT*100:.0f}% @ {current_market_price:.2f} (entry {entry:.2f})")
                    place_market_order(symbol, close_side, size, wallet, testnet=is_testnet, reduce_only=True, account_address=account_address)
                    # Realised loss estimate
                    pnl = (current_market_price - entry) * size * LEVERAGE if side == 'long' else (entry - current_market_price) * size * LEVERAGE
                    fee_est = abs(pnl) * DEFAULT_FEES / 100
                    d['capital'] += pnl - fee_est
                    d['total_fees'] = d.get('total_fees', 0.0) + fee_est
                    session_fees += fee_est
                    nose_stops_this_session += 1
                    # Clear position + related topiary hedges for this TF
                    d['position'] = None
                    d['stop_loss_level'] = None
                    if d.get('hedges'):
                        for h in d['hedges']:
                            if h.active:
                                h.active = False
                        print(f"[NOSE STOP] Cleared {len(d['hedges'])} Topiary hedges on {tf}")
                        d['hedges'] = []
                    # Arm kill-switch
                    kill_switch_until = max(kill_switch_until, current_ts + KILL_SWITCH_COOLDOWN_SEC)
                    print(f"[KILL-SWITCH] Nose-stop → new harbor entries paused until {datetime.fromtimestamp(kill_switch_until, tz=timezone.utc).strftime('%H:%M:%S')} UTC")

            # === SIGNAL GATHERING ===
            all_signals = []
            for t in LOWER_TFS + HIGHER_TFS:
                days = 3
                if t in ['1d', '1w', '1M']:
                    days = 365
                elif t in ['4h', '1h']:
                    days = 120
                elif t == '15m':
                    days = 60
                tf_df = fetch_ohlcv(symbol, t, days_back=days)
                if tf_df is not None and len(tf_df) >= 100:
                    tf_signals = get_recent_harbor_signals(tf_df, t, args)
                    all_signals.extend(tf_signals)

            # === HOTSPOT SCORING + FILTERING (v15.9.4 tighter for micro/macro) ===
            for sig in all_signals:
                slope = sig.get('slope', 0)
                if (sig['bias'] == 'long' and slope > 0) or (sig['bias'] == 'short' and slope < 0):
                    sig['hotspot_score'] = 1.0
                else:
                    sig['hotspot_score'] = 0.0

            # keep best signal PER (tf, bias, chain_type) → full 72-slot roster
            filtered = []
            for tf in LOWER_TFS + HIGHER_TFS:
                for bias in ['long', 'short']:
                    for chain in ['pico', 'nano', 'micro', 'macro']:
                        min_hot = MIN_HOTSPOT_MICRO_MACRO if chain in ('micro', 'macro') else 0.0
                        candidates = [s for s in all_signals
                                      if s.get('timeframe') == tf
                                      and s.get('bias') == bias
                                      and s.get('chain_type', 'pico') == chain
                                      and s.get('hotspot_score', 0) >= min_hot]
                        if not candidates:
                            continue
                        if tf in ['1m', '3m', '5m']:
                            candidates.sort(key=lambda x: pd.to_datetime(x.get('nose_time') or x.get('born_on')), reverse=True)
                        else:
                            candidates.sort(key=lambda x: x.get('hotspot_score', 0), reverse=True)
                        filtered.append(candidates[0])

            if dev or verbose:
                progress = f"\n[UTC {current_time.strftime('%H:%M:%S')}] Price ${current_market_price:,.0f} | "
                for t in ['1w','1d','4h','1h','15m','5m','3m','1m']:
                    count = len([s for s in filtered if s.get('timeframe') == t])
                    progress += f"{t}:{count} "
                progress += f"| Active: {len(active_limits)}"
                print(progress)

            # === ORDER PLACEMENT LOOP ===
            # Global chain counts (for soft exposure cap)
            chain_open = defaultdict(int)
            for k, lim in active_limits.items():
                chain_open[lim.get('chain_type', 'pico')] += 1

            placed_this_cycle = set()
            for sig in filtered:
                tf = sig['timeframe']
                bias = sig['bias']
                chain_type = sig.get('chain_type', 'pico')
                key = f"{tf}_{bias}_{chain_type}"   # unique per chain → 8 slots/TF

                # ── Kill-switch gate ──
                if current_ts < kill_switch_until:
                    continue

                # ── Soft per-chain exposure cap ──
                if chain_open.get(chain_type, 0) >= MAX_OPEN_PER_CHAIN and key not in active_limits:
                    if dev or verbose:
                        print(f" → Skip {tf} {bias} {chain_type}: already {chain_open[chain_type]} open (cap {MAX_OPEN_PER_CHAIN})")
                    continue

                # Basic age filter — reactive for "every wick"
                try:
                    born = pd.to_datetime(sig.get('born_on'))
                    if born.tz is None:
                        born = born.tz_localize('UTC')
                    age_hours = (current_time - born).total_seconds() / 3600
                except:
                    age_hours = 999

                min_age = 0.3 if chain_type in ['pico', 'nano'] else 1.0
                if age_hours < min_age:
                    continue

                cooldown = 3 * 60 if tf in LOWER_TFS else 2 * 60
                if current_ts - tf_data[tf]['last_flip_time'] < cooldown:
                    continue

                nose_price = sig.get('nose_price', sig.get('anchor1_price'))
                tf_data[tf]['last_nose'] = nose_price

                zipper = get_zipper_state(tf_data, tf)
                sailfish_mult = detect_sailfish_strength(sig)

                print(f" [ZIPPER] {tf} {bias.upper()} | Score:{zipper.get('zipper_score',0):.2f} | "
                      f"Alt:{zipper.get('alternating')} | Streak:{zipper.get('streak')}")

                # === REPLACEMENT with cooldown ===
                existing_order = active_limits.get(key)
                if existing_order:
                    last_replace = existing_order.get('last_replace_ts', 0)
                    if current_ts - last_replace < 75:  # 75 second cooldown
                        continue
                    
                    if should_replace_existing_harbor(existing_order, sig, current_market_price, dev=dev):
                        try:
                            if existing_order.get('oid'):
                                exchange.cancel(name=symbol.split('/')[0], oid=existing_order['oid'])
                        except:
                            pass

                        roster.release(key)
                        print(f"[REPLACED] Old harbor missed new nose on {tf} {bias.upper()}")
                        del active_limits[key]
                        # Mark new one
                        sig['last_replace_ts'] = current_ts
                    else:
                        continue

                # === SIZING v15.9.5 — target 0.01 BTC @ 10× ===
                capital = tf_data[tf]['capital']
                real_balance = live_equity if live_equity >= 1.0 else total_balance

                zipper_score = zipper.get('zipper_score', 0)
                # Prefer fixed target size; scale down only if margin can't support it
                needed_margin = TARGET_ORDER_SIZE_BTC * current_market_price / max(LEVERAGE, 1)
                if real_balance >= needed_margin * 1.15:
                    size = TARGET_ORDER_SIZE_BTC
                else:
                    # scale to what 25% of free equity allows at current leverage
                    affordable = (real_balance * 0.25) * LEVERAGE / current_market_price
                    size = max(round(min(TARGET_ORDER_SIZE_BTC, affordable), 5), 0.001)
                # light zipper / sailfish nudge (never below target when funded)
                size = min(size * (1.0 + zipper_score * 0.05) * max(sailfish_mult, 1.0), TARGET_ORDER_SIZE_BTC * 1.2)
                size = round(min(size, 0.015), 5)

                # Glide price
                base_price = sig.get('leading_limit', sig.get('anchor2_price', current_market_price))
                try:
                    a1_time = pd.to_datetime(sig['anchor1_time'])
                    if a1_time.tz is None:
                        a1_time = a1_time.tz_localize('UTC')
                    hours_elapsed = (current_time - a1_time).total_seconds() / 3600
                    glide_mult = 1.35 if tf in LOWER_TFS else 0.85
                    glided = sig.get('anchor1_price', base_price) + sig.get('slope', 0) * hours_elapsed * glide_mult
                except:
                    glided = base_price

                tf_offset = (hash(tf) % 180 - 90) * 0.06
                if bias == 'long':
                    price = min(glided, base_price, current_market_price * 0.982)
                else:
                    price = max(glided, base_price, current_market_price * 1.018)
                price += tf_offset

                # Dynamic closeness tolerance — lower TFs can stack on the same wick
                # Dynamic closeness tolerance — now much looser
                tf_tolerance = {
                    '1m': 6.0, '3m': 10.0, '5m': 12.0,
                    '15m': 20.0, '1h': 35.0, '4h': 50.0,
                }.get(tf, 25.0)

                if is_too_close_to_existing(price, active_limits, tolerance=tf_tolerance):
                    continue

                distance_pct = abs(price - current_market_price) / current_market_price
                side_str = "buy" if bias == "long" else "sell"
                coin = symbol.split('/')[0]

                # === RICH DIAGNOSTIC PRINT (UTC) ===
                try:
                    nose_dt = pd.to_datetime(sig.get('nose_time')).tz_localize('UTC')
                    man_dt = pd.to_datetime(sig.get('man_time')).tz_localize('UTC')
                    eighth_dt = pd.to_datetime(sig.get('eighth_time')).tz_localize('UTC')

                    nose_str = nose_dt.strftime('%H:%M %m/%d/%y')
                    man_str = man_dt.strftime('%H:%M %m/%d/%y')
                    eighth_str = eighth_dt.strftime('%H:%M %m/%d/%y')
                except:
                    nose_str = man_str = eighth_str = "??:?? ??/??/??"

                print(f"\n[HARBOUR] {tf} {bias.upper()} {chain_type} | "
                      f"Nose@{sig.get('nose_price',0):.0f} @ {nose_str} | "
                      f"Man@{sig.get('anchor1_price',0):.0f} @ {man_str} | "
                      f"Eighth@{sig.get('anchor2_price',0):.0f} @ {eighth_str} | "
                      f"Lead@{price:.2f} | Activated:False")

                # === MARGIN GUARD — use master account equity ===
                try:
                    us_m = exchange.info.user_state(address=account_address)
                    account_value = float(us_m.get('marginSummary', {}).get('accountValue', 0) or 0)
                    withdrawable = float(us_m.get('withdrawable', 0) or 0)
                    free_margin = max(account_value, withdrawable, live_equity, total_balance * 0.5)
                    print(f" → Margin check: Account ${account_value:,.2f} | Withdrawable ${withdrawable:,.2f} → Using ${free_margin:,.2f}")
                    # Need ~ (size * price / leverage) free; skip if clearly short
                    needed = size * current_market_price / max(LEVERAGE, 1)
                    if free_margin < needed * 0.9 and not dev:
                        print(f" → Low margin (${free_margin:.2f} < need ~${needed:.2f}) — skipping order")
                        continue
                except Exception as e:
                    print(f" → Margin check failed ({e}) — proceeding with size {size}")
                    free_margin = total_balance

                # Place order
                if distance_pct < 0.0015:
                    order_result = place_market_order(symbol, side_str, size, wallet,
                                                     testnet=is_testnet, account_address=account_address)
                else:
                    order_result = place_glide_limit_with_perturbation(
                        symbol=symbol, side=side_str, size=size,
                        calculated_glide_price=price, coin=coin,
                        is_buy=(side_str.lower() == "buy"),
                        wallet=wallet, testnet=is_testnet, account_address=account_address
                    )

                if order_result:
                    oid = None
                    try:
                        statuses = order_result.get('response', {}).get('data', {}).get('statuses', [])
                        if statuses and isinstance(statuses[0], dict):
                            resting = statuses[0].get('resting')
                            if resting:
                                oid = resting.get('oid')
                    except:
                        pass

                    active_limits[key] = {
                        **sig,
                        'price': price,
                        'size': size,
                        'oid': oid,
                        'time': current_ts,
                        'nose_price': nose_price,
                        'tf': tf,
                        'bias': bias,
                        'chain_type': chain_type,
                        'glide_origin_price': price,
                        'glide_origin_ts': current_ts,
                        'activated': False
                    }

                    roster.allocate(tf, bias, chain_type, size * current_market_price, key)
                    chain_open[chain_type] = chain_open.get(chain_type, 0) + 1
                    if dev or verbose:
                        print(f"[ROSTER ALLOCATED] {tf} {bias.upper()} {chain_type} | Slots used: {roster.get_total_allocated()}/72")

                    print(f"[{tf} {bias.upper()}] {chain_type.upper()} @ {price:.2f} [SUCCESS]\n")

                    tf_data[tf]['last_flip_time'] = current_ts + 20
                    placed_this_cycle.add(key)
                else:
                    print(f"[{tf} {bias.upper()}] ❌ Order failed\n")

            # Glide + Hedge + Cleanup + Export (exactly as you have)
            for tf in ['1m', '3m', '5m', '15m', '1h', '4h']:
                new_close_time = detect_candle_close(tf, glide_manager.last_candle_close.get(tf), symbol)
                if new_close_time:
                    glide_manager.on_candle_close(tf, new_close_time)

            if current_ts - last_hedge_update > 45:
                evaluate_glide_v155(active_limits, current_market_price, current_ts)
                update_topiary_hedges_v155(tf_data, current_market_price, args,
                                           current_signals=all_signals, symbol=symbol, wallet=wallet)
                last_hedge_update = current_ts

            if current_ts - last_cleanup > 180:
                to_remove = [k for k, v in active_limits.items() if current_ts - v['time'] > 900]
                for k in to_remove:
                    try:
                        if active_limits[k].get('oid'):
                            exchange.cancel(name=symbol.split('/')[0], oid=active_limits[k]['oid'])
                    except:
                        pass
                    roster.release(k)
                    if k in active_limits:
                        del active_limits[k]
                last_cleanup = current_ts

            ks_status = f"KILL {int(kill_switch_until - current_ts)}s" if current_ts < kill_switch_until else "OK"
            chain_str = " ".join(f"{c}:{chain_open.get(c,0)}" for c in ['pico','nano','micro','macro'])
            print(f"\n📊 ACTIVE: {len(active_limits)}/72 | Roster: {roster.get_total_allocated()} | "
                  f"Eq ${live_equity:,.0f} | DD {session_dd_pct:.1f}% | Fees ${session_fees:.2f} | "
                  f"NoseStops {nose_stops_this_session} | {ks_status} | Chains [{chain_str}] | "
                  f"Price ${current_market_price:,.0f}")

            export_state_json(active_limits, current_market_price,
                              free_capital=live_equity, roster=roster, symbol=symbol)
            export_harbor_lines_json(active_limits, current_market_price, symbol=symbol)
            export_pine_ready(active_limits, current_market_price, symbol=symbol)

        except KeyboardInterrupt:
            print("\n🛑 Stopped by user.")
            break
        except Exception as e:
            print(f"[CRITICAL LOOP ERROR] {e}")
            import traceback
            traceback.print_exc()
            time.sleep(25)

# ==================== MAIN ====================
def parse_args():
    parser = argparse.ArgumentParser(description="Tetris + Topiary v15.9.5 - 10x · 0.01 BTC · Reversals · ACT+1")
    parser.add_argument("--symbols", default="BTC/USDC")
    parser.add_argument("--timeframe", default="1m")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--testnet", action="store_true")
    parser.add_argument("--dev", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--cycle", action="store_true")
    parser.add_argument("--topiary", action="store_true", help="Enable richer 15.1 Topiary hedge logic")
    parser.add_argument("--days-back", type=int, default=7)
    parser.add_argument("--stop", type=float, default=0.02)
    parser.add_argument("--check-balance", action="store_true")
    parser.add_argument("--backtest-then-live", action="store_true", 
                        help="Run backtest then automatically start live mode")
    return parser.parse_args()

# ==================== MAIN (unchanged) ====================
def main():
    args = parse_args()
    if args.check_balance:
        check_balance()
        return
    if args.live:
        run_live_mode(args)
    if args.backtest_then_live:
        print("🔄 Running Backtest first...")
        final_cap, max_dd, fees = run_backtest_for_symbol(args.symbols.split(',')[0].strip(), args)
        print(f"\n✅ Backtest finished → Starting LIVE mode automatically...\n")
        args.live = True
        run_live_mode(args)
    elif args.live:
        run_live_mode(args)
    else:
        # Standalone backtest
        print("🔄 Running standalone backtest...")
        final_cap, max_dd, fees = run_backtest_for_symbol(args.symbols.split(',')[0].strip(), args)

if __name__ == "__main__":
    main()

# 2. Live only
#python tetris_topiary.py --symbols BTC/USDC --timeframe 1m --live --testnet --dev

# 3. Backtest only
#python tetris_topiary.py --symbols BTC/USDC --timeframe 1m --dev --days-back 7

# screen -S tetra_bot ./run_tetra.sh
# screen -r tetra_bot
# screen -ls
# screen -S tetra_bot -X quit
# screen -S tetra_bot python tetris_topiary.py --symbols BTC/USDC --timeframe 1m --live --testnet --dev --verbose
