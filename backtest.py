"""
XRP Scalper v4.5 — 5m vs 1m Backtest Comparison
Fetches 30 days of data, runs v4.5 signal logic on both timeframes, compares results.
No AI scorer — uses passthrough score of 75.
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
FEE_RATE         = 0.001       # maker+taker for paper
STOP_LOSS        = 0.008
TAKE_PROFIT      = 0.015
TRAIL_OFFSET     = 0.004       # flat trail below 0.15%
TRAIL_FROM_ENTRY = True
RSI_OVERSOLD     = 35
RSI_OVERBOUGHT   = 65
BB_PERIOD        = 20
EMA_PERIOD       = 200
RSI_PERIOD       = 14
VOL_SPIKE        = 1.5         # v4.5 required spike
MIN_VOL_RATIO    = 0.60
SUSTAINED_LOOPS  = 3
SUSTAINED_HITS   = 2
SAME_DIR_COOLDOWN= 10 * 60     # seconds
COOLDOWN_LOSS_THRESHOLD = 0.001
RSI_EXTREME_LOOPS= 3

DECEL_LOOPS      = 3
DECEL_MIN_PNL    = 0.002
DECEL_TRAIL      = 0.001

# ─── FETCH ────────────────────────────────────────────────────────────────────
def fetch_klines(interval="1min", days=30):
    print(f"Fetching {days}d of {interval} klines from CoinEx...")
    all_klines = []
    candle_secs = 300 if "5" in interval else 60
    total_needed = days * 24 * 3600 // candle_secs
    end_ts = int(time.time())
    batch = 1000

    while len(all_klines) < total_needed:
        try:
            r = requests.get(
                "https://api.coinex.com/v2/futures/kline",
                params={"market": "XRPUSDT", "period": interval, "limit": batch},
                timeout=15
            )
            data = r.json()
            if data.get("code") != 0 or not data.get("data"):
                print("  API error:", data.get("message")); break
            batch_data = data["data"]
            all_klines = batch_data + all_klines
            end_ts = int(batch_data[0][0]) - 1
            if len(batch_data) < batch:
                break
            time.sleep(0.3)
        except Exception as e:
            print(f"  Fetch error: {e}"); break

    all_klines = all_klines[-total_needed:]
    print(f"  Got {len(all_klines)} candles")
    return all_klines

# ─── INDICATORS ───────────────────────────────────────────────────────────────
def calc_signals(klines):
    min_needed = max(BB_PERIOD + 10, EMA_PERIOD + 5)
    if len(klines) < min_needed:
        return None

    closes = [float(k[2]) for k in klines]
    vols   = [float(k[5]) for k in klines]

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
    sma = sum(bb_w) / BB_PERIOD
    std = math.sqrt(sum((x-sma)**2 for x in bb_w) / BB_PERIOD)
    bb_upper = sma + 2.0 * std
    bb_lower = sma - 2.0 * std

    # EMA200
    k_ema = 2 / (EMA_PERIOD + 1)
    ema = closes[0]
    for p in closes[1:]:
        ema = p * k_ema + ema * (1 - k_ema)

    closed_vols = vols[:-1]
    vol_avg = sum(closed_vols[-BB_PERIOD:]) / min(BB_PERIOD, len(closed_vols)) if closed_vols else 0
    vol_current = vols[-2] if len(vols) >= 2 else vols[-1]

    return {
        "price": closes[-1],
        "rsi": rsi_vals[-1],
        "rsi_prev": rsi_vals[-2],
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "ema200": ema,
        "vol": vol_current,
        "vol_avg": vol_avg,
    }

# ─── SIGNAL (v4.5 logic) ──────────────────────────────────────────────────────
def get_signal(sig, sig_hi=None):
    """sig = primary timeframe, sig_hi = higher timeframe RSI filter"""
    price    = sig["price"]
    rsi      = sig["rsi"]
    rsi_prev = sig["rsi_prev"]
    vol      = sig["vol"]
    vol_avg  = sig["vol_avg"]
    vol_ratio = vol / vol_avg if vol_avg > 0 else 0

    # LONG: RSI cross >35 mandatory + 2 of 3
    if rsi_prev < RSI_OVERSOLD <= rsi:
        score = 1
        if price <= sig["bb_lower"] * 1.002: score += 1
        if vol_ratio >= VOL_SPIKE:            score += 1
        if sig_hi and sig_hi["rsi"] < 45:    score += 1
        if score >= 3:
            return "long", vol_ratio

    # SHORT: RSI cross <65 mandatory + 2 of 3
    if rsi_prev > RSI_OVERBOUGHT >= rsi:
        score = 1
        if price >= sig["bb_upper"] * 0.998: score += 1
        if vol_ratio >= VOL_SPIKE:            score += 1
        if sig_hi and sig_hi["rsi"] > 55:    score += 1
        if score >= 3:
            return "short", vol_ratio

    return None, vol_ratio

# ─── TRAIL OFFSET ─────────────────────────────────────────────────────────────
def trail_offset(pnl, decelerating=False):
    if decelerating and pnl >= DECEL_MIN_PNL: return DECEL_TRAIL
    if pnl >= TAKE_PROFIT:  return 0.0020
    if pnl >= 0.0080:       return 0.0030
    if pnl >= 0.0050:       return 0.0025
    if pnl >= 0.0030:       return 0.0020
    if pnl >= 0.0015:       return 0.0010
    return TRAIL_OFFSET

# ─── BACKTEST CORE ────────────────────────────────────────────────────────────
def run_backtest(klines_primary, klines_higher=None, label="1m"):
    """
    klines_primary: list of OHLCV candles for signal + entry/exit
    klines_higher:  list of OHLCV candles for RSI filter (optional)
    """
    print(f"\n{'='*60}")
    print(f"  Running v4.5 backtest on {label} candles ({len(klines_primary)} bars)")
    print(f"{'='*60}")

    balance = CAPITAL
    trades  = []
    position = None  # {"dir", "entry", "peak", "entry_i", "spend"}

    vol_history     = deque(maxlen=SUSTAINED_LOOPS)
    last_loss_dir   = None
    last_loss_time  = 0
    rsi_extreme_count = 0

    # Build higher-TF index by timestamp for fast lookup
    hi_by_ts = {}
    if klines_higher:
        for k in klines_higher:
            hi_by_ts[int(k[0])] = k

    warmup = max(BB_PERIOD + 10, EMA_PERIOD + 5)

    for i in range(warmup, len(klines_primary)):
        window = klines_primary[max(0, i-EMA_PERIOD-10):i+1]
        sig = calc_signals(window)
        if not sig:
            continue

        ts    = int(klines_primary[i][0])
        price = sig["price"]
        vol_ratio = sig["vol"] / sig["vol_avg"] if sig["vol_avg"] > 0 else 0
        vol_history.append(vol_ratio)

        # ── IN POSITION ──────────────────────────────────────────────────────
        if position:
            d   = position["dir"]
            ent = position["entry"]
            pk  = position["peak"]
            ei  = position["entry_i"]
            bars_held = i - ei

            if d == "long":
                pnl = (price - ent) / ent
                position["peak"] = max(pk, price)
            else:
                pnl = (ent - price) / ent
                position["peak"] = min(pk, price)

            new_peak = position["peak"]
            offset   = trail_offset(pnl)
            reason   = None

            # Stop loss
            if pnl <= -STOP_LOSS:
                reason = "sl"

            # RSI extreme
            elif (d == "short" and sig["rsi"] > 72) or (d == "long" and sig["rsi"] < 28):
                rsi_extreme_count += 1
                if rsi_extreme_count >= RSI_EXTREME_LOOPS:
                    reason = "rsi_extreme"
            else:
                rsi_extreme_count = 0

            # Trailing stop (from entry)
            if not reason and TRAIL_FROM_ENTRY:
                if d == "long":
                    trail_stop = new_peak * (1 - offset)
                    if price <= trail_stop and pnl > -STOP_LOSS:
                        reason = "trail"
                else:
                    trail_stop = new_peak * (1 + offset)
                    if price >= trail_stop and pnl > -STOP_LOSS:
                        reason = "trail"

            if reason:
                # Close
                if d == "long":
                    pnl_pct = (price - ent) / ent
                    pnl_usd = position["spend"] * LEVERAGE * pnl_pct - position["spend"] * LEVERAGE * FEE_RATE
                else:
                    pnl_pct = (ent - price) / ent
                    pnl_usd = position["spend"] * LEVERAGE * pnl_pct - position["spend"] * LEVERAGE * FEE_RATE

                balance += position["spend"] + pnl_usd
                is_real_loss = pnl_pct < -COOLDOWN_LOSS_THRESHOLD

                if is_real_loss and reason != "rsi_extreme":
                    last_loss_dir  = d
                    last_loss_time = ts

                trades.append({
                    "dir": d, "entry": ent, "exit": price,
                    "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                    "bars": bars_held, "reason": reason,
                    "balance": balance,
                })
                position = None
                rsi_extreme_count = 0

        # ── NO POSITION — look for entry ─────────────────────────────────────
        else:
            # Sustained vol filter
            if len(vol_history) >= SUSTAINED_LOOPS:
                hits = sum(1 for v in vol_history if v >= MIN_VOL_RATIO)
                if hits < SUSTAINED_HITS:
                    continue

            # Same-dir cooldown
            if last_loss_dir:
                if ts - last_loss_time < SAME_DIR_COOLDOWN:
                    continue

            # Higher TF signal for RSI filter
            sig_hi = None
            if klines_higher:
                # find closest 5m candle at or before this 1m candle
                candle_secs_hi = 300
                hi_ts = (ts // candle_secs_hi) * candle_secs_hi
                if hi_ts in hi_by_ts:
                    window_hi = [k for k in klines_higher if int(k[0]) <= hi_ts][-60:]
                    sig_hi = calc_signals(window_hi) if len(window_hi) >= warmup else None

            direction, vr = get_signal(sig, sig_hi)
            if not direction:
                continue

            spend = balance * 0.10  # 10% of balance per trade
            balance -= spend
            position = {
                "dir": direction,
                "entry": price,
                "peak": price,
                "entry_i": i,
                "spend": spend,
            }
            rsi_extreme_count = 0

    # ── RESULTS ──────────────────────────────────────────────────────────────
    if not trades:
        print("  No trades taken.")
        return {}

    pnls    = [t["pnl_pct"] for t in trades]
    pnl_usd = [t["pnl_usd"] for t in trades]
    wins    = [p for p in pnls if p > 0]
    losses  = [p for p in pnls if p <= 0]
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    total_pnl = sum(pnl_usd)
    win_rate  = len(wins) / len(trades) * 100

    avg_bars  = sum(t["bars"] for t in trades) / len(trades)
    candle_min = 5 if "5" in label else 1
    avg_mins  = avg_bars * candle_min

    print(f"  Trades     : {len(trades)}  ({len(wins)}W / {len(losses)}L)")
    print(f"  Win Rate   : {win_rate:.1f}%")
    print(f"  Total PnL  : ${total_pnl:+.4f}")
    print(f"  Avg PnL    : {sum(pnls)/len(pnls)*100:+.3f}%")
    print(f"  Best/Worst : +{max(pnls)*100:.2f}% / {min(pnls)*100:.2f}%")
    print(f"  Avg win    : +{sum(wins)/len(wins)*100:.3f}%" if wins else "  No wins")
    print(f"  Avg loss   : {sum(losses)/len(losses)*100:.3f}%" if losses else "  No losses")
    print(f"  Avg hold   : {avg_mins:.1f} mins ({avg_bars:.1f} bars)")
    print(f"  Exit types : {reasons}")
    print(f"  Final bal  : ${balance:.4f}  (started ${CAPITAL:.2f})")
    print(f"  Return     : {(balance-CAPITAL)/CAPITAL*100:+.2f}%")

    return {
        "label": label,
        "trades": len(trades),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "balance": balance,
        "return_pct": (balance-CAPITAL)/CAPITAL*100,
        "avg_pnl_pct": sum(pnls)/len(pnls)*100,
        "exit_types": reasons,
    }

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    DAYS = 30

    # Fetch 1m data
    klines_1m = fetch_klines("1min", DAYS)
    # Fetch 5m data
    klines_5m = fetch_klines("5min", DAYS)

    if not klines_1m or not klines_5m:
        print("Failed to fetch data. Check network / API.")
        exit(1)

    # Run 1m backtest (1m primary, 5m as higher TF filter)
    result_1m = run_backtest(klines_1m, klines_5m, label="1m+5mfilter")

    # Run 5m backtest (5m primary, no higher TF)
    result_5m = run_backtest(klines_5m, None, label="5m")

    # Summary comparison
    print(f"\n{'='*60}")
    print("  COMPARISON SUMMARY")
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
