"""
XRP Scalper v4.5 — 5m vs 1m Backtest Comparison
Fetches 30 days of data, runs v4.5 signal logic on both timeframes.
No AI scorer — passthrough score of 75.
Run on Railway or any machine with internet access.
"""

import requests
import math
import time
from collections import deque
from datetime import datetime, timezone

# ─── CONFIG ───────────────────────────────────────────────────────────────────
CAPITAL          = 27.00
LEVERAGE         = 3
FEE_RATE         = 0.001       # 0.05% each side round trip
STOP_LOSS        = 0.008
TAKE_PROFIT      = 0.015
TRAIL_OFFSET     = 0.004
TRAIL_FROM_ENTRY = True
RSI_OVERSOLD     = 35
RSI_OVERBOUGHT   = 65
BB_PERIOD        = 20
EMA_PERIOD       = 200
RSI_PERIOD       = 14
VOL_SPIKE        = 1.5
MIN_VOL_RATIO    = 0.60
SUSTAINED_LOOPS  = 3
SUSTAINED_HITS   = 2
SAME_DIR_COOLDOWN   = 10 * 60
COOLDOWN_LOSS_THRESHOLD = 0.001
RSI_EXTREME_LOOPS   = 3
DECEL_MIN_PNL    = 0.002
DECEL_TRAIL      = 0.001

# ─── FETCH ────────────────────────────────────────────────────────────────────
def fetch_klines(interval="1min", days=30):
    """
    CoinEx v2 returns klines as list of dicts:
    {"created_at": ts_ms, "open": "1.23", "close": "1.24",
     "high": "1.25", "low": "1.22", "volume": "1000", "value": "1234"}
    Sorted oldest->newest.
    """
    candle_secs = 300 if "5" in interval else 60
    limit = min(days * 24 * 3600 // candle_secs, 1000)
    print(f"Fetching {limit} {interval} candles from CoinEx...")

    try:
        r = requests.get(
            "https://api.coinex.com/v2/futures/kline",
            params={"market": "XRPUSDT", "period": interval, "limit": limit},
            timeout=30
        )
        data = r.json()
        if data.get("code") != 0:
            print(f"  API error: {data.get('message')}")
            return []
        candles = data.get("data", [])
        print(f"  Got {len(candles)} candles")
        return candles
    except Exception as e:
        print(f"  Fetch error: {e}")
        return []

def parse_candle(k):
    """Extract (ts_sec, open, close, high, low, volume) from dict or list candle."""
    if isinstance(k, dict):
        ts  = int(k.get("created_at", k.get("timestamp", 0))) // 1000
        c   = float(k.get("close", 0))
        o   = float(k.get("open", 0))
        h   = float(k.get("high", 0))
        l   = float(k.get("low", 0))
        vol = float(k.get("volume", k.get("vol", 0)))
    else:
        # legacy array format: [ts_ms, open, close, high, low, vol, value]
        ts  = int(k[0]) // 1000
        o, c, h, l, vol = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
    return ts, o, c, h, l, vol

# ─── INDICATORS ───────────────────────────────────────────────────────────────
def calc_signals(klines):
    min_needed = max(BB_PERIOD + 10, EMA_PERIOD + 5)
    if len(klines) < min_needed:
        return None

    closes = [parse_candle(k)[2] for k in klines]
    vols   = [parse_candle(k)[5] for k in klines]

    # RSI
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    ag = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
    al = sum(losses[:RSI_PERIOD]) / RSI_PERIOD
    rsi_vals = []
    for i in range(RSI_PERIOD, len(gains)):
        ag = (ag * (RSI_PERIOD-1) + gains[i]) / RSI_PERIOD
        al = (al * (RSI_PERIOD-1) + losses[i]) / RSI_PERIOD
        rs = ag / al if al != 0 else float('inf')
        rsi_vals.append(100 - (100 / (1 + rs)))
    if len(rsi_vals) < 2:
        return None

    # BB
    bb_w = closes[-BB_PERIOD:]
    sma  = sum(bb_w) / BB_PERIOD
    std  = math.sqrt(sum((x-sma)**2 for x in bb_w) / BB_PERIOD)
    bb_upper = sma + 2.0 * std
    bb_lower = sma - 2.0 * std

    # EMA200
    k_ema = 2 / (EMA_PERIOD + 1)
    ema   = closes[0]
    for p in closes[1:]:
        ema = p * k_ema + ema * (1 - k_ema)

    closed_vols = vols[:-1]
    vol_avg     = sum(closed_vols[-BB_PERIOD:]) / min(BB_PERIOD, len(closed_vols)) if closed_vols else 0
    vol_current = vols[-2] if len(vols) >= 2 else vols[-1]

    return {
        "price":    closes[-1],
        "rsi":      rsi_vals[-1],
        "rsi_prev": rsi_vals[-2],
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "ema200":   ema,
        "vol":      vol_current,
        "vol_avg":  vol_avg,
    }

# ─── SIGNAL (v4.5) ────────────────────────────────────────────────────────────
def get_signal(sig, sig_hi=None):
    price     = sig["price"]
    rsi       = sig["rsi"]
    rsi_prev  = sig["rsi_prev"]
    vol_ratio = sig["vol"] / sig["vol_avg"] if sig["vol_avg"] > 0 else 0

    # LONG: RSI cross >35 mandatory + 2 of remaining 3
    if rsi_prev < RSI_OVERSOLD <= rsi:
        score = 1
        if price <= sig["bb_lower"] * 1.002:    score += 1
        if vol_ratio >= VOL_SPIKE:               score += 1
        if sig_hi and sig_hi["rsi"] < 45:        score += 1
        if score >= 3:
            return "long", vol_ratio

    # SHORT: RSI cross <65 mandatory + 2 of remaining 3
    if rsi_prev > RSI_OVERBOUGHT >= rsi:
        score = 1
        if price >= sig["bb_upper"] * 0.998:     score += 1
        if vol_ratio >= VOL_SPIKE:               score += 1
        if sig_hi and sig_hi["rsi"] > 55:        score += 1
        if score >= 3:
            return "short", vol_ratio

    return None, vol_ratio

# ─── TRAIL OFFSET ─────────────────────────────────────────────────────────────
def get_trail_offset(pnl, decelerating=False):
    if decelerating and pnl >= DECEL_MIN_PNL: return DECEL_TRAIL
    if pnl >= TAKE_PROFIT:  return 0.0020
    if pnl >= 0.0080:       return 0.0030
    if pnl >= 0.0050:       return 0.0025
    if pnl >= 0.0030:       return 0.0020
    if pnl >= 0.0015:       return 0.0010
    return TRAIL_OFFSET

# ─── BACKTEST ─────────────────────────────────────────────────────────────────
def run_backtest(klines_primary, klines_higher=None, label="1m"):
    print(f"\n{'='*60}")
    print(f"  Backtest: {label} | {len(klines_primary)} bars")
    print(f"{'='*60}")

    balance  = CAPITAL
    trades   = []
    position = None

    vol_history       = deque(maxlen=SUSTAINED_LOOPS)
    last_loss_dir     = None
    last_loss_time    = 0
    rsi_extreme_count = 0

    # Build higher-TF lookup list sorted by ts
    hi_ts_list = []
    if klines_higher:
        hi_ts_list = [parse_candle(k)[0] for k in klines_higher]

    warmup = max(BB_PERIOD + 10, EMA_PERIOD + 5)

    for i in range(warmup, len(klines_primary)):
        window = klines_primary[max(0, i - EMA_PERIOD - 10): i + 1]
        sig = calc_signals(window)
        if not sig:
            continue

        ts    = parse_candle(klines_primary[i])[0]
        price = sig["price"]
        vol_ratio = sig["vol"] / sig["vol_avg"] if sig["vol_avg"] > 0 else 0
        vol_history.append(vol_ratio)

        # ── IN POSITION ──────────────────────────────────────────────────────
        if position:
            d   = position["dir"]
            ent = position["entry"]
            ei  = position["entry_i"]
            bars_held = i - ei

            if d == "long":
                pnl = (price - ent) / ent
                position["peak"] = max(position["peak"], price)
            else:
                pnl = (ent - price) / ent
                position["peak"] = min(position["peak"], price)

            pk     = position["peak"]
            offset = get_trail_offset(pnl)
            reason = None

            if pnl <= -STOP_LOSS:
                reason = "sl"
            elif (d == "short" and sig["rsi"] > 72) or (d == "long" and sig["rsi"] < 28):
                rsi_extreme_count += 1
                if rsi_extreme_count >= RSI_EXTREME_LOOPS:
                    reason = "rsi_extreme"
            else:
                rsi_extreme_count = 0

            if not reason:
                if d == "long":
                    if price <= pk * (1 - offset):
                        reason = "trail"
                else:
                    if price >= pk * (1 + offset):
                        reason = "trail"

            if reason:
                pnl_pct = (price - ent) / ent if d == "long" else (ent - price) / ent
                spend   = position["spend"]
                pnl_usd = spend * LEVERAGE * pnl_pct - spend * LEVERAGE * FEE_RATE
                balance += spend + pnl_usd

                if pnl_pct < -COOLDOWN_LOSS_THRESHOLD and reason != "rsi_extreme":
                    last_loss_dir  = d
                    last_loss_time = ts

                trades.append({
                    "dir": d, "entry": ent, "exit": price,
                    "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                    "bars": bars_held, "reason": reason,
                    "balance": balance,
                })
                position         = None
                rsi_extreme_count = 0
            continue

        # ── NO POSITION ──────────────────────────────────────────────────────
        if len(vol_history) >= SUSTAINED_LOOPS:
            hits = sum(1 for v in vol_history if v >= MIN_VOL_RATIO)
            if hits < SUSTAINED_HITS:
                continue

        if last_loss_dir and (ts - last_loss_time) < SAME_DIR_COOLDOWN:
            continue

        # Higher TF RSI — find nearest candle at or before ts
        sig_hi = None
        if klines_higher and hi_ts_list:
            # Binary search for closest ts
            lo, hi = 0, len(hi_ts_list) - 1
            best = -1
            while lo <= hi:
                mid = (lo + hi) // 2
                if hi_ts_list[mid] <= ts:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            if best >= 0:
                hi_window = klines_higher[max(0, best - EMA_PERIOD - 10): best + 1]
                if len(hi_window) >= warmup:
                    sig_hi = calc_signals(hi_window)

        direction, vr = get_signal(sig, sig_hi)
        if not direction:
            continue

        spend    = balance * 0.10
        balance -= spend
        position = {
            "dir":     direction,
            "entry":   price,
            "peak":    price,
            "entry_i": i,
            "spend":   spend,
        }
        rsi_extreme_count = 0

    # Close any open position at end
    if position and len(klines_primary) > warmup:
        sig = calc_signals(klines_primary[max(0, len(klines_primary)-EMA_PERIOD-10):])
        if sig:
            price   = sig["price"]
            d       = position["dir"]
            pnl_pct = (price - position["entry"]) / position["entry"] if d == "long" \
                      else (position["entry"] - price) / position["entry"]
            spend   = position["spend"]
            pnl_usd = spend * LEVERAGE * pnl_pct - spend * LEVERAGE * FEE_RATE
            balance += spend + pnl_usd
            trades.append({"dir": d, "entry": position["entry"], "exit": price,
                           "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                           "bars": len(klines_primary) - position["entry_i"],
                           "reason": "end", "balance": balance})

    # ── RESULTS ──────────────────────────────────────────────────────────────
    if not trades:
        print("  No trades taken.")
        return {}

    pnls    = [t["pnl_pct"] for t in trades]
    usd     = [t["pnl_usd"] for t in trades]
    wins    = [p for p in pnls if p > 0]
    losses  = [p for p in pnls if p <= 0]
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    total_pnl  = sum(usd)
    win_rate   = len(wins) / len(trades) * 100
    candle_min = 5 if "5" in label else 1
    avg_mins   = (sum(t["bars"] for t in trades) / len(trades)) * candle_min

    print(f"  Trades     : {len(trades)}  ({len(wins)}W / {len(losses)}L)")
    print(f"  Win Rate   : {win_rate:.1f}%")
    print(f"  Total PnL  : ${total_pnl:+.4f}")
    print(f"  Avg PnL    : {sum(pnls)/len(pnls)*100:+.3f}%")
    print(f"  Best/Worst : +{max(pnls)*100:.2f}% / {min(pnls)*100:.2f}%")
    if wins:   print(f"  Avg win    : +{sum(wins)/len(wins)*100:.3f}%")
    if losses: print(f"  Avg loss   : {sum(losses)/len(losses)*100:.3f}%")
    print(f"  Avg hold   : {avg_mins:.1f} mins")
    print(f"  Exit types : {reasons}")
    print(f"  Final bal  : ${balance:.4f}  (started ${CAPITAL:.2f})")
    print(f"  Return     : {(balance-CAPITAL)/CAPITAL*100:+.2f}%")

    return {
        "label": label, "trades": len(trades), "win_rate": win_rate,
        "total_pnl": total_pnl, "balance": balance,
        "return_pct": (balance-CAPITAL)/CAPITAL*100,
        "avg_pnl_pct": sum(pnls)/len(pnls)*100,
        "exit_types": reasons,
    }

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    DAYS = 30

    klines_1m = fetch_klines("1min", DAYS)
    klines_5m = fetch_klines("5min", DAYS)

    if not klines_1m or not klines_5m:
        print("Failed to fetch data."); exit(1)

    print(f"\nSample 1m candle: {klines_1m[0]}")
    print(f"Sample 5m candle: {klines_5m[0]}\n")

    result_1m = run_backtest(klines_1m, klines_5m, label="1m+5mfilter")
    result_5m = run_backtest(klines_5m, None,       label="5m-only")

    print(f"\n{'='*60}")
    print("  COMPARISON")
    print(f"{'='*60}")
    for r in [result_1m, result_5m]:
        if not r: continue
        print(f"  [{r['label']:15s}] "
              f"Trades:{r['trades']:4d} | "
              f"WR:{r['win_rate']:.0f}% | "
              f"AvgPnL:{r['avg_pnl_pct']:+.3f}% | "
              f"Total:${r['total_pnl']:+.2f} | "
              f"Return:{r['return_pct']:+.1f}%")
    print(f"{'='*60}")
