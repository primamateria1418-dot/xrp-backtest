"""
XRP Scalper v4.4 — 30-Day Backtest
Mirrors live bot logic exactly: signals, entry, trailing stop, conviction check,
RSI extreme exit, time stop, stop loss, deceleration tightening.
No AI scorer (no live API in backtest) — uses passthrough score of 75.
"""

import requests
import os
import math
import time
from collections import deque
from datetime import datetime, timezone

COINEX_ACCESS_ID = os.getenv("COINEX_ACCESS_ID", "")

# ─── CONFIG (mirrors live bot defaults) ───────────────────────────────────────
CAPITAL          = 27.00
LEVERAGE         = 3
FEE_RATE         = 0.0005
MIN_VOL_RATIO    = 0.60
STOP_LOSS        = 0.008
TAKE_PROFIT      = 0.015
TRAIL_OFFSET     = 0.004       # loose trail (below 0.15%)
TIME_STOP_SECS   = 180
TIME_STOP_MIN_PNL= 0.001
MIN_HOLD_SECS    = 180
KILL_MAX_LOSS    = 0.002
RSI_OVERSOLD     = 35
RSI_OVERBOUGHT   = 65
RSI_EXTREME_LOW  = 25
RSI_EXTREME_HIGH = 80
RSI_EXTREME_LOOPS= 3
BB_PERIOD        = 20
EMA_PERIOD       = 200
RSI_PERIOD       = 14
CONVICTION_LOOPS = 3
DECEL_LOOPS      = 3
DECEL_MIN_PNL    = 0.002
DECEL_TRAIL      = 0.0010
VOL_FADE_RATIO   = 0.50
SUSTAINED_LOOPS  = 3
SUSTAINED_HITS   = 2

# ─── FETCH HISTORICAL DATA ────────────────────────────────────────────────────
def fetch_klines_range(days=30):
    """Fetch ~30 days of 1m klines from CoinEx in batches of 1000."""
    print(f"Fetching {days} days of 1m klines from CoinEx...")
    all_klines = []
    # 30 days = 43200 candles, fetch in 1000-candle batches
    total_needed = days * 24 * 60
    end_ts = int(time.time() * 1000)  # CoinEx v2 uses milliseconds
    batch_ms = 1000 * 60 * 1000  # 1000 candles × 1 min each, in milliseconds

    fetched = 0
    while fetched < total_needed:
        start_ts = end_ts - batch_ms
        try:
            r = requests.get(
                "https://api.coinex.com/v2/futures/kline",
                params={
                    "market": "XRPUSDT",
                    "period": "1min",
                    "limit": 1000,
                    "start_time": start_ts,
                    "end_time": end_ts,
                },
                timeout=15
            )
            d = r.json()
            if d.get("code") != 0 or not d.get("data"):
                print(f"  API error: {d.get('message', 'unknown')}")
                break
            raw_batch = d["data"]
            if not raw_batch:
                break
            # CoinEx v2 returns dicts: {created_at, open, high, low, close, volume, ...}
            # Normalize to list format: [timestamp_ms, open, close, high, low, volume]
            batch = []
            for k in raw_batch:
                if isinstance(k, dict):
                    batch.append([
                        int(k.get("created_at", 0)),
                        float(k.get("open", 0)),
                        float(k.get("close", 0)),
                        float(k.get("high", 0)),
                        float(k.get("low", 0)),
                        float(k.get("volume", 0)),
                    ])
                else:
                    batch.append([float(x) for x in k])
            all_klines = batch + all_klines
            fetched += len(batch)
            end_ts = batch[0][0] - 60000  # step back 1 minute in milliseconds
            print(f"  Fetched {fetched} candles so far...")
            time.sleep(0.3)  # rate limit respect
            if fetched >= total_needed:
                break
        except Exception as e:
            print(f"  Fetch error: {e}")
            break

    print(f"Total candles fetched: {len(all_klines)}")
    return all_klines

# ─── INDICATORS ───────────────────────────────────────────────────────────────
def calc_signals(klines):
    """Calculate RSI, BB, EMA200, vol from a window of candles."""
    if len(klines) < EMA_PERIOD + 5:
        return None

    closes = [float(k[2]) for k in klines]
    vols   = [float(k[5]) for k in klines]

    # RSI
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    if len(gains) < RSI_PERIOD:
        return None
    avg_gain = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
    avg_loss = sum(losses[:RSI_PERIOD]) / RSI_PERIOD
    rsi_vals = []
    for i in range(RSI_PERIOD, len(gains)):
        avg_gain = (avg_gain * (RSI_PERIOD-1) + gains[i]) / RSI_PERIOD
        avg_loss = (avg_loss * (RSI_PERIOD-1) + losses[i]) / RSI_PERIOD
        rs = avg_gain / avg_loss if avg_loss != 0 else float('inf')
        rsi_vals.append(100 - (100 / (1 + rs)))
    if len(rsi_vals) < 2:
        return None

    # BB
    bb_w  = closes[-BB_PERIOD:]
    sma   = sum(bb_w) / BB_PERIOD
    std   = math.sqrt(sum((x - sma)**2 for x in bb_w) / BB_PERIOD)
    bb_upper = sma + 2 * std
    bb_lower = sma - 2 * std

    # EMA200
    k_ema = 2 / (EMA_PERIOD + 1)
    ema   = closes[0]
    for p in closes[1:]:
        ema = p * k_ema + ema * (1 - k_ema)

    # Vol
    vol_avg = sum(vols[-BB_PERIOD-1:-1]) / BB_PERIOD
    vol_cur = vols[-2]

    return {
        "price":    closes[-1],
        "rsi":      rsi_vals[-1],
        "rsi_prev": rsi_vals[-2],
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "ema200":   ema,
        "vol":      vol_cur,
        "vol_avg":  vol_avg,
    }

# ─── SIGNAL DIRECTION ─────────────────────────────────────────────────────────
def get_signal(sig, vol_history):
    price    = sig["price"]
    rsi      = sig["rsi"]
    rsi_prev = sig["rsi_prev"]
    bb_lower = sig["bb_lower"]
    bb_upper = sig["bb_upper"]
    ema200   = sig["ema200"]
    vol      = sig["vol"]
    vol_avg  = sig["vol_avg"]

    # Volume check
    vol_ratio = vol / vol_avg if vol_avg > 0 else 0
    if vol_ratio < MIN_VOL_RATIO:
        return None

    # Sustained volume filter
    hits = sum(1 for v in vol_history if v >= MIN_VOL_RATIO)
    if hits < SUSTAINED_HITS:
        return None

    long_score = 0
    if rsi_prev < RSI_OVERSOLD <= rsi:         long_score += 1
    if price <= bb_lower * 1.005 and rsi < 50: long_score += 1
    long_score += 1  # volume always passes here
    if price >= ema200 * 0.99:                 long_score += 1
    if rsi <= 50:                              long_score += 1

    short_score = 0
    if rsi_prev > RSI_OVERBOUGHT >= rsi:       short_score += 1
    if price >= bb_upper * 0.990:              short_score += 1
    short_score += 1  # volume always passes here
    if price <= ema200 * 1.03:                 short_score += 1
    if rsi >= 50:                              short_score += 1

    if long_score >= 3 and short_score >= 3:
        return "long" if long_score >= short_score else "short"
    elif long_score >= 3:
        return "long"
    elif short_score >= 3:
        return "short"
    return None

# ─── TRAIL OFFSET ─────────────────────────────────────────────────────────────
def get_trail_offset(pnl, decelerating=False):
    if decelerating and pnl >= DECEL_MIN_PNL:
        return DECEL_TRAIL
    if pnl >= TAKE_PROFIT: return 0.0020
    if pnl >= 0.0080:      return 0.0030
    if pnl >= 0.0050:      return 0.0025
    if pnl >= 0.0030:      return 0.0020
    if pnl >= 0.0015:      return 0.0010
    return TRAIL_OFFSET

# ─── CONVICTION CHECK ─────────────────────────────────────────────────────────
def check_conviction(sig, direction, rsi_at_entry, loops_since_entry):
    if loops_since_entry > CONVICTION_LOOPS:
        return False
    rsi_now = sig["rsi"]
    rsi_vel = sig["rsi"] - sig["rsi_prev"]
    if direction == "long" and rsi_at_entry < 40:
        if rsi_now < rsi_at_entry and rsi_vel < 0:
            return True
    if direction == "short" and rsi_at_entry > 60:
        if rsi_now > rsi_at_entry and rsi_vel > 0:
            return True
    return False

# ─── BACKTEST ENGINE ──────────────────────────────────────────────────────────
def run_backtest(klines):
    print(f"\nRunning backtest on {len(klines)} candles...")

    balance      = CAPITAL
    trades       = []
    in_trade     = False
    direction    = None
    entry        = 0.0
    peak         = 0.0
    position     = 0.0
    entry_loop   = 0
    rsi_at_entry = 50.0
    rsi_extreme_count = 0
    rsi_vel_history   = deque(maxlen=DECEL_LOOPS)
    vol_history       = deque(maxlen=SUSTAINED_LOOPS)

    WINDOW = EMA_PERIOD + 50  # candles needed before we can trade

    for i in range(WINDOW, len(klines)):
        window  = klines[i - WINDOW: i + 1]
        sig     = calc_signals(window)
        if not sig:
            continue

        price    = sig["price"]
        rsi      = sig["rsi"]
        vol_avg  = sig["vol_avg"]
        vol_ratio = sig["vol"] / vol_avg if vol_avg > 0 else 0
        vol_history.append(vol_ratio)

        if in_trade:
            loops_held = i - entry_loop
            secs_held  = loops_held * 60  # 1m candles = 60s each

            # RSI velocity
            rsi_vel = sig["rsi"] - sig["rsi_prev"]
            rsi_vel_history.append(rsi_vel)

            # Deceleration check
            decelerating = False
            if len(rsi_vel_history) >= DECEL_LOOPS:
                recent = list(rsi_vel_history)[-DECEL_LOOPS:]
                decelerating = all(recent[j] > recent[j+1] for j in range(len(recent)-1))

            # PnL
            if direction == "long":
                pnl = (price - entry) / entry
            else:
                pnl = (entry - price) / entry

            peak = max(peak, price) if direction == "long" else min(peak, price)

            exit_reason = None
            exit_type   = None

            # 1. Stop loss
            if pnl <= -STOP_LOSS:
                exit_reason = f"SL {pnl*100:.2f}%"
                exit_type   = "sl"

            # 2. Time stop
            elif secs_held >= TIME_STOP_SECS and pnl < TIME_STOP_MIN_PNL:
                if decelerating or pnl < 0:
                    exit_reason = f"Time stop {secs_held}s | {pnl*100:+.2f}%"
                    exit_type   = "time"

            # 3. RSI extreme
            elif (direction == "long"  and rsi <= RSI_EXTREME_LOW) or \
                 (direction == "short" and rsi >= RSI_EXTREME_HIGH):
                rsi_extreme_count += 1
                if rsi_extreme_count >= RSI_EXTREME_LOOPS:
                    exit_reason = f"RSI extreme {rsi:.1f} for {RSI_EXTREME_LOOPS} loops"
                    exit_type   = "rsi_extreme"
            else:
                rsi_extreme_count = 0

            # 4. Conviction check
            if not exit_reason and check_conviction(sig, direction, rsi_at_entry, loops_held):
                exit_reason = f"Conviction fail RSI {rsi:.1f}"
                exit_type   = "signal"

            # 5. Trailing stop
            if not exit_reason and pnl > -STOP_LOSS:
                trail_offset = get_trail_offset(pnl, decelerating)
                if direction == "long":
                    trail_stop = peak * (1 - trail_offset)
                    if price <= trail_stop and pnl > 0:
                        exit_reason = f"Trail {pnl*100:.2f}% (offset {trail_offset*100:.2f}%)"
                        exit_type   = "trail"
                else:
                    trail_stop = peak * (1 + trail_offset)
                    if price >= trail_stop and pnl > 0:
                        exit_reason = f"Trail {pnl*100:.2f}% (offset {trail_offset*100:.2f}%)"
                        exit_type   = "trail"

            # 6. Opposite signal exit (after min hold)
            if not exit_reason and secs_held >= MIN_HOLD_SECS:
                opp = get_signal(sig, vol_history)
                if opp and opp != direction and pnl >= -KILL_MAX_LOSS:
                    exit_reason = f"Opposite signal {pnl*100:+.2f}%"
                    exit_type   = "signal"

            # Close trade
            if exit_reason:
                if direction == "long":
                    gross   = position * price
                    pnl_usd = gross * (1 - FEE_RATE) - (position * entry)
                else:
                    gross   = position * entry
                    pnl_usd = pnl * gross

                margin  = (position * entry) / LEVERAGE
                balance += margin + pnl_usd

                ts = int(klines[i][0]) // 1000  # convert ms to seconds
                dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                trades.append({
                    "n":          len(trades) + 1,
                    "dt":         dt,
                    "direction":  direction,
                    "entry":      entry,
                    "exit":       price,
                    "pnl_pct":    pnl * 100,
                    "pnl_usd":    pnl_usd,
                    "balance":    balance,
                    "reason":     exit_reason,
                    "exit_type":  exit_type,
                    "held_min":   loops_held,
                })

                in_trade          = False
                direction         = None
                rsi_extreme_count = 0
                rsi_vel_history   = deque(maxlen=DECEL_LOOPS)

        else:
            # Look for entry
            direction = get_signal(sig, vol_history)
            if direction:
                spend      = balance * 0.30  # 30% of balance per trade
                position   = (spend * LEVERAGE * (1 - FEE_RATE)) / price
                entry      = price
                peak       = price
                balance   -= spend
                entry_loop = i
                rsi_at_entry      = rsi
                rsi_extreme_count = 0
                rsi_vel_history   = deque(maxlen=DECEL_LOOPS)
                in_trade   = True

    return trades, balance

# ─── RESULTS ──────────────────────────────────────────────────────────────────
def print_results(trades, final_balance):
    if not trades:
        print("No trades generated.")
        return

    n        = len(trades)
    wins     = [t for t in trades if t["pnl_pct"] > 0]
    losses   = [t for t in trades if t["pnl_pct"] <= 0]
    win_rate = len(wins) / n * 100
    total_pnl= sum(t["pnl_usd"] for t in trades)
    avg_win  = sum(t["pnl_pct"] for t in wins)  / len(wins)  if wins   else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    best     = max(t["pnl_pct"] for t in trades)
    worst    = min(t["pnl_pct"] for t in trades)

    # Exit type breakdown
    exit_types = {}
    for t in trades:
        et = t["exit_type"]
        exit_types[et] = exit_types.get(et, 0) + 1

    # Max drawdown
    peak_bal = CAPITAL
    max_dd   = 0
    for t in trades:
        peak_bal = max(peak_bal, t["balance"])
        dd = (peak_bal - t["balance"]) / peak_bal * 100
        max_dd = max(max_dd, dd)

    # Consecutive losses
    max_consec = 0
    cur_consec = 0
    for t in trades:
        if t["pnl_pct"] <= 0:
            cur_consec += 1
            max_consec = max(max_consec, cur_consec)
        else:
            cur_consec = 0

    print("\n" + "="*60)
    print("  XRP SCALPER v4.4 — 30-DAY BACKTEST RESULTS")
    print("="*60)
    print(f"  Starting balance : ${CAPITAL:.2f}")
    print(f"  Final balance    : ${final_balance:.4f}")
    print(f"  Total PnL        : ${total_pnl:+.4f}  ({(final_balance/CAPITAL-1)*100:+.2f}%)")
    print(f"  Total trades     : {n}")
    print(f"  Win rate         : {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Avg win          : {avg_win:+.3f}%")
    print(f"  Avg loss         : {avg_loss:+.3f}%")
    print(f"  Best trade       : {best:+.3f}%")
    print(f"  Worst trade      : {worst:+.3f}%")
    print(f"  Max drawdown     : {max_dd:.2f}%")
    print(f"  Max consec losses: {max_consec}")
    print(f"  Exit breakdown   : {exit_types}")
    print("="*60)

    # Last 20 trades
    print("\n  LAST 20 TRADES:")
    print(f"  {'#':<4} {'Date':<17} {'Dir':<6} {'Entry':>8} {'Exit':>8} {'PnL%':>7} {'Reason'}")
    print("  " + "-"*80)
    for t in trades[-20:]:
        icon = "▲" if t["direction"] == "long" else "▼"
        print(f"  {t['n']:<4} {t['dt']:<17} {icon}{t['direction']:<5} "
              f"{t['entry']:>8.5f} {t['exit']:>8.5f} "
              f"{t['pnl_pct']:>+7.3f}%  {t['reason']}")

    # Daily PnL summary
    print("\n  DAILY PnL SUMMARY:")
    daily = {}
    for t in trades:
        day = t["dt"][:10]
        daily[day] = daily.get(day, {"pnl": 0, "n": 0, "wins": 0})
        daily[day]["pnl"]  += t["pnl_usd"]
        daily[day]["n"]    += 1
        daily[day]["wins"] += 1 if t["pnl_pct"] > 0 else 0
    for day, d in sorted(daily.items()):
        wr = d["wins"] / d["n"] * 100 if d["n"] else 0
        bar = "█" * int(abs(d["pnl"]) * 20) if abs(d["pnl"]) > 0.01 else ""
        sign = "+" if d["pnl"] >= 0 else ""
        print(f"  {day}  {sign}${d['pnl']:.4f}  {d['n']} trades  {wr:.0f}% WR  {bar}")

    print("="*60)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("XRP Scalper v4.4 — Backtest Runner")
    print(f"CoinEx API: {'CONFIGURED' if COINEX_ACCESS_ID else 'NOT SET — using public endpoints'}")

    klines = fetch_klines_range(days=30)
    if len(klines) < EMA_PERIOD + 100:
        print("Not enough data to backtest.")
    else:
        trades, final_balance = run_backtest(klines)
        print_results(trades, final_balance)
