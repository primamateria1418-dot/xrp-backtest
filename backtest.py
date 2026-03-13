"""
XRP Scalper v4.2 — 30-Day Backtest
Mirrors v4.2 logic exactly:
  - Signal threshold: 3/5 (not 4/5)
  - Tiered trailing stop (0.10% → 0.20% → 0.25% → 0.30% → 0.20% at TP)
  - NO time stop
  - NO conviction check
  - NO RSI extreme exit
  - NO deceleration tightening
  - Opposite signal kill after MIN_HOLD_SECS (gated by KILL_MAX_LOSS)
  - Hard stop loss: 0.8%
No AI scorer — uses passthrough (all signals accepted).
"""

import requests
import os
import math
import time
from datetime import datetime, timezone

COINEX_ACCESS_ID = os.getenv("COINEX_ACCESS_ID", "")

CAPITAL        = 27.00
LEVERAGE       = 3
FEE_RATE       = 0.0005
MIN_VOL_RATIO  = 0.60
STOP_LOSS      = 0.008
TAKE_PROFIT    = 0.015
TRAIL_OFFSET   = 0.002  # reduced from 0.004 — cap early losses at 0.20%
MIN_HOLD_SECS  = 180
KILL_MAX_LOSS  = 0.002
RSI_OVERSOLD   = 35
RSI_OVERBOUGHT = 65
BB_PERIOD      = 20
EMA_PERIOD     = 200
RSI_PERIOD     = 14

def fetch_klines_range(days=30):
    print(f"Fetching {days} days of 1m klines from CoinEx...")
    all_klines   = []
    total_needed = days * 24 * 60
    end_ts       = int(time.time() * 1000)
    batch_ms     = 1000 * 60 * 1000
    fetched      = 0
    while fetched < total_needed:
        start_ts = end_ts - batch_ms
        try:
            r = requests.get(
                "https://api.coinex.com/v2/futures/kline",
                params={"market": "XRPUSDT", "period": "1min", "limit": 1000,
                        "start_time": start_ts, "end_time": end_ts},
                timeout=15,
            )
            d = r.json()
            if d.get("code") != 0 or not d.get("data"):
                print(f"  API error: {d.get('message','unknown')}")
                break
            raw = d["data"]
            if not raw:
                break
            batch = []
            for k in raw:
                if isinstance(k, dict):
                    batch.append([int(k.get("created_at",0)), float(k.get("open",0)),
                                  float(k.get("close",0)), float(k.get("high",0)),
                                  float(k.get("low",0)),   float(k.get("volume",0))])
                else:
                    batch.append([float(x) for x in k])
            all_klines = batch + all_klines
            fetched   += len(batch)
            end_ts     = batch[0][0] - 60000
            print(f"  Fetched {fetched} candles so far...")
            time.sleep(0.3)
            if fetched >= total_needed:
                break
        except Exception as e:
            print(f"  Fetch error: {e}")
            break
    print(f"Total candles fetched: {len(all_klines)}")
    return all_klines

def calc_signals(klines):
    if len(klines) < EMA_PERIOD + 5:
        return None
    closes = [float(k[2]) for k in klines]
    vols   = [float(k[5]) for k in klines]
    deltas = [closes[i]-closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d,0) for d in deltas]
    losses = [max(-d,0) for d in deltas]
    if len(gains) < RSI_PERIOD:
        return None
    avg_gain = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
    avg_loss = sum(losses[:RSI_PERIOD]) / RSI_PERIOD
    rsi_vals = []
    for i in range(RSI_PERIOD, len(gains)):
        avg_gain = (avg_gain*(RSI_PERIOD-1)+gains[i]) / RSI_PERIOD
        avg_loss = (avg_loss*(RSI_PERIOD-1)+losses[i]) / RSI_PERIOD
        rs = avg_gain/avg_loss if avg_loss != 0 else float('inf')
        rsi_vals.append(100-(100/(1+rs)))
    if len(rsi_vals) < 2:
        return None
    bb_w     = closes[-BB_PERIOD:]
    sma      = sum(bb_w)/BB_PERIOD
    std      = math.sqrt(sum((x-sma)**2 for x in bb_w)/BB_PERIOD)
    bb_upper = sma + 2*std
    bb_lower = sma - 2*std
    k_ema    = 2/(EMA_PERIOD+1)
    ema      = closes[0]
    for p in closes[1:]:
        ema = p*k_ema + ema*(1-k_ema)
    vol_avg = sum(vols[-BB_PERIOD-1:-1])/BB_PERIOD
    vol_cur = vols[-2]
    return {"price": closes[-1], "rsi": rsi_vals[-1], "rsi_prev": rsi_vals[-2],
            "bb_upper": bb_upper, "bb_lower": bb_lower, "ema200": ema,
            "vol": vol_cur, "vol_avg": vol_avg}

def get_signal(sig):
    price    = sig["price"]
    rsi      = sig["rsi"]
    rsi_prev = sig["rsi_prev"]
    vol_ratio = sig["vol"]/sig["vol_avg"] if sig["vol_avg"] > 0 else 0
    if vol_ratio < MIN_VOL_RATIO:
        return None
    long_score = 0
    if rsi_prev < RSI_OVERSOLD <= rsi:              long_score += 1
    if price <= sig["bb_lower"]*1.005 and rsi < 50: long_score += 1
    long_score += 1
    if price >= sig["ema200"]*0.99:                 long_score += 1
    if rsi <= 50:                                   long_score += 1
    short_score = 0
    if rsi_prev > RSI_OVERBOUGHT >= rsi:            short_score += 1
    if price >= sig["bb_upper"]*0.990:              short_score += 1
    short_score += 1
    if price <= sig["ema200"]*1.03:                 short_score += 1
    if rsi >= 50:                                   short_score += 1
    if long_score >= 3 and short_score >= 3:
        return "long" if long_score >= short_score else "short"
    elif long_score >= 3:  return "long"
    elif short_score >= 3: return "short"
    return None

def get_trail_offset(pnl):
    if pnl >= TAKE_PROFIT: return 0.0020
    if pnl >= 0.0080:      return 0.0030
    if pnl >= 0.0050:      return 0.0025
    if pnl >= 0.0030:      return 0.0020
    if pnl >= 0.0015:      return 0.0010
    return TRAIL_OFFSET

def run_backtest(klines):
    print(f"\nRunning backtest on {len(klines)} candles...")
    balance    = CAPITAL
    trades     = []
    in_trade   = False
    direction  = None
    entry      = peak = position = 0.0
    entry_loop = 0
    WINDOW     = EMA_PERIOD + 50

    for i in range(WINDOW, len(klines)):
        window = klines[i-WINDOW:i+1]
        sig    = calc_signals(window)
        if not sig:
            continue
        price = sig["price"]

        if in_trade:
            loops_held = i - entry_loop
            secs_held  = loops_held * 60
            if direction == "long":
                pnl  = (price - entry) / entry
                peak = max(peak, price)
            else:
                pnl  = (entry - price) / entry
                peak = min(peak, price)

            exit_reason = exit_type = None

            # 1. Hard SL
            if pnl <= -STOP_LOSS:
                exit_reason = f"SL {pnl*100:.2f}%"
                exit_type   = "sl"

            # 2. Tiered trail (fires from entry, no trigger threshold — v4.2 style)
            if not exit_reason:
                to = get_trail_offset(pnl)
                if direction == "long":
                    if price <= peak*(1-to) and pnl > -STOP_LOSS:
                        exit_reason = f"Trail {pnl*100:.2f}% (offset {to*100:.2f}%)"
                        exit_type   = "trail"
                else:
                    if price >= peak*(1+to) and pnl > -STOP_LOSS:
                        exit_reason = f"Trail {pnl*100:.2f}% (offset {to*100:.2f}%)"
                        exit_type   = "trail"

            # 3. Opposite signal kill
            if not exit_reason and secs_held >= MIN_HOLD_SECS and pnl >= -KILL_MAX_LOSS:
                opp = get_signal(sig)
                if opp and opp != direction:
                    exit_reason = f"Opposite signal {pnl*100:+.2f}%"
                    exit_type   = "signal"

            if exit_reason:
                if direction == "long":
                    pnl_usd = position*price*(1-FEE_RATE) - position*entry
                else:
                    pnl_usd = pnl * position * entry
                margin   = (position*entry)/LEVERAGE
                balance += margin + pnl_usd
                ts = int(klines[i][0])//1000
                dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                trades.append({"n": len(trades)+1, "dt": dt, "direction": direction,
                                "entry": entry, "exit": price, "pnl_pct": pnl*100,
                                "pnl_usd": pnl_usd, "balance": balance,
                                "reason": exit_reason, "exit_type": exit_type,
                                "held_min": loops_held})
                in_trade = False
                direction = None
        else:
            direction = get_signal(sig)
            if direction:
                spend    = balance * 0.30
                position = (spend*LEVERAGE*(1-FEE_RATE))/price
                entry    = price
                peak     = price
                balance -= spend
                entry_loop = i
                in_trade = True

    return trades, balance

def print_results(trades, final_balance):
    if not trades:
        print("No trades generated.")
        return
    n         = len(trades)
    wins      = [t for t in trades if t["pnl_pct"] > 0]
    losses    = [t for t in trades if t["pnl_pct"] <= 0]
    win_rate  = len(wins)/n*100
    total_pnl = sum(t["pnl_usd"] for t in trades)
    avg_win   = sum(t["pnl_pct"] for t in wins)/len(wins)   if wins   else 0
    avg_loss  = sum(t["pnl_pct"] for t in losses)/len(losses) if losses else 0
    best      = max(t["pnl_pct"] for t in trades)
    worst     = min(t["pnl_pct"] for t in trades)
    exit_types= {}
    for t in trades:
        et = t.get("exit_type","?")
        exit_types[et] = exit_types.get(et,0)+1
    peak_bal = CAPITAL; max_dd = 0
    for t in trades:
        peak_bal = max(peak_bal, t["balance"])
        dd = (peak_bal-t["balance"])/peak_bal*100
        max_dd = max(max_dd, dd)
    max_consec = cur_consec = 0
    for t in trades:
        if t["pnl_pct"] <= 0: cur_consec += 1; max_consec = max(max_consec, cur_consec)
        else: cur_consec = 0
    avg_hold = sum(t["held_min"] for t in trades)/n

    print("\n" + "="*60)
    print("  XRP SCALPER v4.2 — 30-DAY BACKTEST RESULTS (TRAIL_OFFSET=0.20%)")
    print("="*60)
    print(f"  Starting balance : ${CAPITAL:.2f}")
    print(f"  Final balance    : ${final_balance:.4f}")
    print(f"  Total PnL        : ${total_pnl:+.4f}  ({(final_balance/CAPITAL-1)*100:+.2f}%)")
    print(f"  Total trades     : {n}  (~{n//30}/day)")
    print(f"  Win rate         : {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Avg win          : {avg_win:+.3f}%")
    print(f"  Avg loss         : {avg_loss:+.3f}%")
    print(f"  Best trade       : {best:+.3f}%")
    print(f"  Worst trade      : {worst:+.3f}%")
    print(f"  Max drawdown     : {max_dd:.2f}%")
    print(f"  Max consec losses: {max_consec}")
    print(f"  Avg hold time    : {avg_hold:.0f} min")
    print(f"  Exit breakdown   : {exit_types}")
    print("="*60)

    print("\n  LAST 20 TRADES:")
    print(f"  {'#':<4} {'Date':<17} {'Dir':<6} {'Entry':>8} {'Exit':>8} {'PnL%':>7} {'Hold':>5} {'Reason'}")
    print("  "+"-"*90)
    for t in trades[-20:]:
        icon = "▲" if t["direction"]=="long" else "▼"
        print(f"  {t['n']:<4} {t['dt']:<17} {icon}{t['direction']:<5} "
              f"{t['entry']:>8.5f} {t['exit']:>8.5f} "
              f"{t['pnl_pct']:>+7.3f}%  {t['held_min']:>4}m  {t['reason']}")

    print("\n  DAILY PnL SUMMARY:")
    daily = {}
    for t in trades:
        day = t["dt"][:10]
        daily[day] = daily.get(day, {"pnl":0,"n":0,"wins":0})
        daily[day]["pnl"]  += t["pnl_usd"]
        daily[day]["n"]    += 1
        daily[day]["wins"] += 1 if t["pnl_pct"]>0 else 0
    for day, d in sorted(daily.items()):
        wr  = d["wins"]/d["n"]*100 if d["n"] else 0
        bar = "█"*int(abs(d["pnl"])*20) if abs(d["pnl"])>0.01 else ""
        sign = "+" if d["pnl"]>=0 else ""
        print(f"  {day}  {sign}${d['pnl']:.4f}  {d['n']} trades  {wr:.0f}% WR  {bar}")
    print("="*60)

if __name__ == "__main__":
    print("XRP Scalper v4.2 — Backtest Runner")
    print(f"CoinEx API: {'CONFIGURED' if COINEX_ACCESS_ID else 'NOT SET'}")
    klines = fetch_klines_range(days=30)
    if len(klines) < EMA_PERIOD + 100:
        print("Not enough data to backtest.")
    else:
        trades, final_balance = run_backtest(klines)
        print_results(trades, final_balance)
