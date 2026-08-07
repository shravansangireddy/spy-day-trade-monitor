"""
Five intraday SPY options day-trading strategies + the universal risk layer.

Each strategy is a pure function: (snapshot, config, now_et) -> (signal | None, reason)
  * signal is a dict matching the Signal spec when ALL entry conditions are met
  * reason is a human string explaining why there is no signal (shown on the card)

All numeric thresholds come from CONFIG in main.py — do not hardcode filters here.

Signal object fields (per spec):
  strategy, direction ('CALL'|'PUT'), entry_price, entry_reason, exit_target,
  exit_stop, exit_reason, key_risk, session_tag, confidence (0-1)
plus extras added here / by the risk layer:
  conditions (list of confirming conditions), contract (suggested option leg),
  risk_note, flags, deemphasize, action.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Defaults used only when cfg omits a key (tests / partial configs)
_DEFAULTS = {
    "market_open": (9, 30),
    "market_close": (16, 0),
    "open_drive_end": (11, 0),
    "power_hour_start": (15, 0),
    "flatten_time": (15, 45),
}


def _hm(cfg: dict, key: str) -> tuple[int, int]:
    v = cfg.get(key) or _DEFAULTS[key]
    return (int(v[0]), int(v[1]))


# Module-level aliases kept for main.py countdown imports (overridden via cfg at runtime)
MARKET_OPEN = _DEFAULTS["market_open"]
MARKET_CLOSE = _DEFAULTS["market_close"]
FLATTEN_TIME = _DEFAULTS["flatten_time"]


# ---------------------------------------------------------------------------
# Session tagging
# ---------------------------------------------------------------------------

def session_tag(now: datetime, cfg: dict | None = None) -> str:
    """Map ET clock time to a session tag using CONFIG session windows."""
    cfg = cfg or {}
    open_hm = _hm(cfg, "market_open")
    close_hm = _hm(cfg, "market_close")
    od_end = _hm(cfg, "open_drive_end")
    ph_start = _hm(cfg, "power_hour_start")
    hm = (now.hour, now.minute)
    if now.weekday() >= 5 or hm < open_hm or hm >= close_hm:
        return "closed_avoid"
    if hm < od_end:
        return "open_drive"
    if hm >= ph_start:
        return "power_hour"
    return "lunch_chop"


def _base_signal(strategy: str, direction: str, snap: dict, conditions: list[str]) -> dict:
    return {
        "strategy": strategy,
        "direction": direction,
        "entry_price": snap["price"],
        "conditions": conditions,
        "flags": [],
    }


def _atr(snap: dict, cfg: dict) -> float:
    return max(float(snap.get("recent_range") or 0), float(cfg.get("min_atr_floor", 0.10)))


def _in_delta_dte(o: dict, cfg: dict) -> bool:
    lo, hi = cfg["delta_range"]
    d0, d1 = cfg["dte_range"]
    min_oi = int(cfg.get("min_open_interest") or 0)
    if not (d0 <= o["dte"] <= d1 and lo <= abs(o["delta"]) <= hi):
        return False
    if min_oi > 0 and int(o.get("open_interest") or 0) < min_oi:
        return False
    return True


# ---------------------------------------------------------------------------
# 1. Momentum
# ---------------------------------------------------------------------------

def momentum_strategy(snap: dict, cfg: dict, now: datetime):
    move = snap["pct_change_5min"]
    thresh = cfg["momentum_pct_5min"]
    vol_spike = snap["rolling_vol_avg"] > 0 and snap["recent_vol_avg"] >= cfg["volume_spike_mult"] * snap["rolling_vol_avg"]

    if move is None:
        return None, "not enough intraday bars yet (need 5 min of data)"
    if abs(move) < thresh:
        return None, f"5-min move {move:+.2f}% below ±{thresh}% threshold"
    if not vol_spike:
        return None, (f"momentum {move:+.2f}% but volume {snap['recent_vol_avg']:,.0f}/bar "
                      f"< {cfg['volume_spike_mult']}x rolling avg ({snap['rolling_vol_avg']:,.0f}) — no confirmation")

    direction = "CALL" if move > 0 else "PUT"
    conditions = [f"5-min move {move:+.2f}% > ±{thresh}%",
                  f"volume spike {snap['recent_vol_avg'] / max(snap['rolling_vol_avg'], 1):.1f}x rolling avg"]
    vwap_side_ok = (direction == "CALL" and snap["price"] > snap["vwap"]) or \
                   (direction == "PUT" and snap["price"] < snap["vwap"])
    if vwap_side_ok:
        conditions.append("price on momentum side of VWAP")
    elif cfg.get("momentum_require_vwap_side"):
        return None, "momentum direction is against VWAP side (momentum_require_vwap_side=true)"

    sig = _base_signal("Momentum", direction, snap, conditions)
    px = snap["price"]
    atr = _atr(snap, cfg)
    sign = 1 if direction == "CALL" else -1
    t_mult = float(cfg.get("exit_target_atr_mult", 2.0))
    s_mult = float(cfg.get("exit_stop_atr_mult", 1.0))
    sig.update({
        "entry_reason": "volume spike + directional momentum confirmed",
        "exit_target": round(px + sign * t_mult * atr, 2),
        "exit_stop": round(px - sign * s_mult * atr, 2),
        "exit_reason": (
            f"target {t_mult:g}×ATR / stop {s_mult:g}×ATR; also exit on momentum stall "
            "(bar volume back below rolling avg) or reverse through entry-side VWAP"
        ),
        "key_risk": "fast reversals — enforce tight stop, no chasing.",
        "confidence": round(len(conditions) / 3, 2),
    })
    return sig, None


# ---------------------------------------------------------------------------
# 2. Scalping
# ---------------------------------------------------------------------------

def scalping_strategy(snap: dict, cfg: dict, now: datetime):
    best = _tightest_atm_spread(snap["options"], cfg)
    if best is None:
        lo, hi = cfg["delta_range"]
        d0, d1 = cfg["dte_range"]
        return None, f"no near-the-money contract in |Δ| {lo}-{hi} / DTE {d0}-{d1}"
    if best["spread"] > cfg["scalp_max_option_spread"]:
        return None, (f"tightest ATM option spread ${best['spread']:.2f} > "
                      f"${cfg['scalp_max_option_spread']:.2f} limit — edge eaten by spread")

    rng = snap["range_10bar"]
    if rng is None:
        return None, "not enough bars for range detection"
    range_pct = float(cfg.get("scalp_range_pct", 0.0012))
    max_rng = snap["price"] * range_pct
    if rng > max_rng:
        return None, f"10-min range ${rng:.2f} too wide for scalp (want ≤ ${max_rng:.2f})"

    min_tick = float(cfg.get("scalp_min_tick_pct", 0.01))
    last_tick = snap["pct_change_1min"] or 0.0
    if abs(last_tick) < min_tick:
        return None, f"no micro-oscillation (need |1-min| ≥ {min_tick}%)"
    direction = "CALL" if last_tick > 0 else "PUT"

    lo_t, hi_t = cfg["scalp_target_underlying_move"]
    target_move = min(max(rng * 0.5, lo_t), hi_t)
    delta = abs(best["delta"]) or float(cfg.get("target_delta", 0.40))
    est_option_profit = target_move * delta * 100
    conditions = [f"option spread ${best['spread']:.2f} < ${cfg['scalp_max_option_spread']:.2f}",
                  f"tight 10-min range ${rng:.2f}",
                  f"micro-momentum {last_tick:+.3f}% last bar"]
    sig = _base_signal("Scalping", direction, snap, conditions)
    px = snap["price"]
    sign = 1 if direction == "CALL" else -1
    hold = cfg["scalp_max_hold_minutes"]
    sig.update({
        "entry_reason": f"tight-range micro oscillation; targeting ${target_move:.2f} underlying move "
                        f"(~${est_option_profit:.0f}/contract delta-adjusted)",
        "exit_target": round(px + sign * target_move, 2),
        "exit_stop": round(px - sign * target_move, 2),
        "exit_reason": f"target tick move hit or {hold} min max hold elapsed — whichever first",
        "key_risk": "commissions/fees erode edge",
        "confidence": round(len(conditions) / 3, 2),
    })
    rt = float(cfg.get("round_trip_cost", 1.30))
    cost_mult = float(cfg.get("scalp_profit_cost_mult", 2.0))
    if est_option_profit < cost_mult * rt:
        sig["flags"].append(
            f"projected profit ${est_option_profit:.2f} < {cost_mult:g}× round-trip "
            f"(${cost_mult * rt:.2f}) — SKIP-quality edge"
        )
        sig["confidence"] = round(sig["confidence"] * 0.5, 2)
    return sig, None


def _tightest_atm_spread(options: list[dict], cfg: dict):
    cands = [o for o in options if _in_delta_dte(o, cfg)]
    return min(cands, key=lambda o: o["spread"]) if cands else None


# ---------------------------------------------------------------------------
# 3. Breakout
# ---------------------------------------------------------------------------

def breakout_strategy(snap: dict, cfg: dict, now: datetime):
    px = snap["price"]
    prox = float(cfg.get("breakout_proximity_pct", 0.004))
    levels = []
    if snap.get("premarket_high"):
        levels.append(("pre-market high", snap["premarket_high"], "CALL"))
    if snap.get("premarket_low"):
        levels.append(("pre-market low", snap["premarket_low"], "PUT"))
    if snap.get("opening_range_high"):
        levels.append(("opening-range high", snap["opening_range_high"], "CALL"))
    if snap.get("opening_range_low"):
        levels.append(("opening-range low", snap["opening_range_low"], "PUT"))
    if not levels:
        return None, "no reference levels yet (pre-market / opening range unavailable)"

    broken = None
    for name, lvl, direction in levels:
        if direction == "CALL" and px > lvl and (px - lvl) <= px * prox:
            broken = (name, lvl, "CALL")
        elif direction == "PUT" and px < lvl and (lvl - px) <= px * prox:
            broken = (name, lvl, "PUT")
    if not broken:
        return None, "price not breaking any tracked resistance/support level right now"

    name, lvl, direction = broken
    vol_ok = snap["rolling_vol_avg"] > 0 and snap["recent_vol_avg"] >= cfg["breakout_volume_mult"] * snap["rolling_vol_avg"]
    if not vol_ok:
        return None, (f"{name} ${lvl:.2f} breached but volume only "
                      f"{snap['recent_vol_avg'] / max(snap['rolling_vol_avg'], 1):.1f}x avg "
                      f"(need {cfg['breakout_volume_mult']}x) — false-breakout risk, standing aside")

    held = _retest_held(snap["bars"], lvl, direction, cfg)
    conditions = [f"broke {name} ${lvl:.2f}",
                  f"volume {snap['recent_vol_avg'] / max(snap['rolling_vol_avg'], 1):.1f}x ≥ {cfg['breakout_volume_mult']}x avg"]
    if held:
        conditions.append("retest held — breakout confirmed")

    sig = _base_signal("Breakout", direction, snap, conditions)
    sign = 1 if direction == "CALL" else -1
    min_move = float(cfg.get("breakout_min_move", 0.15))
    move = max(abs(px - lvl), min_move)
    t_mult = float(cfg.get("breakout_target_mult", 2.0))
    stop_buf = float(cfg.get("breakout_stop_buffer", 0.10))
    sig.update({
        "entry_reason": f"break of {name} ${lvl:.2f} with volume confirmation"
                        + (" after retest held" if held else ""),
        "exit_target": round(px + sign * t_mult * move, 2),
        "exit_stop": round(lvl - sign * stop_buf, 2),
        "exit_reason": (
            f"price closes back {'below' if direction == 'CALL' else 'above'} ${lvl:.2f} "
            f"(failed breakout) or {t_mult:g}× break-distance target reached"
        ),
        "key_risk": "false breakout without volume confirmation.",
        "confidence": round(len(conditions) / 3, 2),
    })
    if not held:
        sig["flags"].append("unconfirmed breakout — high false-breakout risk (no retest yet)")
    return sig, None


def _retest_held(bars: list[dict], level: float, direction: str, cfg: dict) -> bool:
    lookback = int(cfg.get("retest_lookback_bars", 15))
    min_after = int(cfg.get("retest_min_bars_after", 4))
    touch_pct = float(cfg.get("retest_touch_pct", 0.0006))
    recent = bars[-lookback:]
    if len(recent) < 5:
        return False
    breach_idx = None
    for i, b in enumerate(recent):
        if (direction == "CALL" and b["close"] > level) or (direction == "PUT" and b["close"] < level):
            breach_idx = i
            break
    if breach_idx is None or breach_idx > len(recent) - min_after:
        return False
    after = recent[breach_idx + 1:]
    touched = any(
        abs((b["low"] if direction == "CALL" else b["high"]) - level) <= level * touch_pct
        for b in after
    )
    held = all((b["close"] > level) if direction == "CALL" else (b["close"] < level) for b in after)
    return touched and held


# ---------------------------------------------------------------------------
# 4. VWAP Reclaim / Rejection
# ---------------------------------------------------------------------------

def vwap_strategy(snap: dict, cfg: dict, now: datetime):
    if snap["vwap"] is None:
        return None, "VWAP unavailable (no regular-session bars yet)"
    if snap["day_volume_ratio"] is not None and snap["day_volume_ratio"] < cfg["vwap_min_volume_ratio"]:
        return None, (f"day volume {snap['day_volume_ratio'] * 100:.0f}% of normal pace "
                      f"(<{cfg['vwap_min_volume_ratio'] * 100:.0f}%) — choppy/low-volume day, VWAP signals suppressed")

    bars = snap["bars"]
    if len(bars) < 3:
        return None, "not enough bars to detect a VWAP cross"
    vwap = snap["vwap"]
    prev_close, cur_close = bars[-2]["close"], bars[-1]["close"]
    vol_ok = snap["recent_vol_avg"] >= snap["rolling_vol_avg"]

    if prev_close < vwap <= cur_close:
        direction, event = "CALL", f"reclaimed VWAP ${vwap:.2f} from below"
    elif prev_close > vwap >= cur_close:
        direction, event = "PUT", f"rejected at VWAP ${vwap:.2f} from above"
    else:
        side = "above" if cur_close > vwap else "below"
        return None, f"no VWAP cross this bar (price ${cur_close:.2f} holding {side} VWAP ${vwap:.2f})"

    if cfg.get("vwap_require_above_avg_volume", True) and not vol_ok:
        return None, f"{event} but on below-average volume — no conviction, skipping"

    conditions = [event, "cross on above-average volume" if vol_ok else "volume pace soft",
                  f"day volume pace {snap['day_volume_ratio'] * 100:.0f}% of normal" if snap["day_volume_ratio"] else "volume pace ok"]
    sig = _base_signal("VWAP Reclaim/Rejection", direction, snap, conditions)
    px = snap["price"]
    sign = 1 if direction == "CALL" else -1
    atr = _atr(snap, cfg)
    t_mult = float(cfg.get("vwap_target_atr_mult", cfg.get("exit_target_atr_mult", 2.0)))
    stop_buf = float(cfg.get("vwap_stop_buffer", 0.15))
    sig.update({
        "entry_reason": f"{event} with volume",
        "exit_target": round(px + sign * t_mult * atr, 2),
        "exit_stop": round(vwap - sign * stop_buf, 2),
        "exit_reason": (
            f"price closes back on the wrong side of VWAP (${vwap:.2f}) or "
            f"{t_mult:g}×ATR target reached"
        ),
        "key_risk": "whipsaw on choppy days.",
        "confidence": round(len(conditions) / 3, 2),
    })
    return sig, None


# ---------------------------------------------------------------------------
# 5. Gap and Go
# ---------------------------------------------------------------------------

def gap_and_go_strategy(snap: dict, cfg: dict, now: datetime):
    open_hm = _hm(cfg, "market_open")
    open_dt = now.replace(hour=open_hm[0], minute=open_hm[1], second=0, microsecond=0)
    mins_since_open = (now - open_dt).total_seconds() / 60
    if now.weekday() >= 5 or mins_since_open < 0:
        return None, "market not open — gap strategy inactive"
    if mins_since_open > cfg["gap_window_minutes"]:
        return None, f"only active first {cfg['gap_window_minutes']} min after open (window closed)"
    if not snap.get("prev_close") or not snap.get("today_open"):
        return None, "missing previous close / today's open — cannot measure gap"

    gap_pct = (snap["today_open"] - snap["prev_close"]) / snap["prev_close"] * 100
    if abs(gap_pct) < cfg["gap_pct_threshold"]:
        return None, f"gap {gap_pct:+.2f}% below ±{cfg['gap_pct_threshold']}% threshold"

    px = snap["price"]
    direction = "CALL" if gap_pct > 0 else "PUT"
    ref = snap.get("premarket_high") if direction == "CALL" else snap.get("premarket_low")
    holding = (ref is not None) and ((px >= ref) if direction == "CALL" else (px <= ref))
    if ref is not None and not holding:
        return None, (f"gapped {gap_pct:+.2f}% but price ${px:.2f} not holding "
                      f"{'above pre-market high' if direction == 'CALL' else 'below pre-market low'} ${ref:.2f}")
    gap_zone_reentry = (direction == "CALL" and px < snap["today_open"]) or \
                       (direction == "PUT" and px > snap["today_open"])
    if gap_zone_reentry:
        return None, f"price re-entered the gap zone (through open ${snap['today_open']:.2f}) — gap-fill in progress, invalidated"

    vol_ok = snap["recent_vol_avg"] >= snap["rolling_vol_avg"]
    if cfg.get("gap_require_volume") and not vol_ok:
        return None, "gap holding but volume not confirming (gap_require_volume=true)"

    conditions = [f"gap {gap_pct:+.2f}% ≥ ±{cfg['gap_pct_threshold']}%",
                  "holding beyond pre-market level" if ref is not None else "pre-market level unavailable"]
    if vol_ok:
        conditions.append("volume confirming")
    sig = _base_signal("Gap and Go", direction, snap, conditions)
    sign = 1 if direction == "CALL" else -1
    gap_h = abs(snap["today_open"] - snap["prev_close"])
    t_mult = float(cfg.get("gap_target_gap_mult", 1.0))
    sig.update({
        "entry_reason": f"gap {gap_pct:+.2f}% holding {'above pre-market high' if direction == 'CALL' else 'below pre-market low'} with volume",
        "exit_target": round(px + sign * t_mult * gap_h, 2),
        "exit_stop": round(snap["today_open"], 2),
        "exit_reason": (
            f"target = {t_mult:g}× gap-height extension; invalidate on re-entry into gap zone "
            f"(stop at open ${snap['today_open']:.2f})"
        ),
        "key_risk": "gap fill — invalidate if price re-enters gap zone.",
        "confidence": round(len(conditions) / 3, 2),
    })
    if not vol_ok:
        sig["flags"].append("volume not yet confirming — reduced size advised")
    return sig, None


STRATEGIES = [
    ("momentum", momentum_strategy),
    ("scalping", scalping_strategy),
    ("breakout", breakout_strategy),
    ("vwap", vwap_strategy),
    ("gap_and_go", gap_and_go_strategy),
]


# ---------------------------------------------------------------------------
# Universal risk layer — applied to EVERY signal before display
# ---------------------------------------------------------------------------

def pick_contract(options: list[dict], direction: str, cfg: dict):
    """Suggest an option leg using CONFIG delta / DTE / OI / target_delta."""
    want = "call" if direction == "CALL" else "put"
    target = float(cfg.get("target_delta", 0.40))
    cands = [o for o in options if o["type"] == want and _in_delta_dte(o, cfg)]
    if not cands:
        return None
    return min(cands, key=lambda o: (abs(abs(o["delta"]) - target), -o["open_interest"]))


def apply_risk_layer(sig: dict, snap: dict, cfg: dict, now: datetime) -> dict:
    """Session tagging, liquidity check, risk display, lunch de-emphasis, flatten override."""
    tag = session_tag(now, cfg)
    sig["session_tag"] = tag
    sig["deemphasize"] = False
    sig["action"] = "ENTER"

    contract = pick_contract(snap["options"], sig["direction"], cfg)
    sig["contract"] = contract
    max_spread = float(cfg.get("max_option_spread", 0.10))
    if contract is None:
        lo, hi = cfg["delta_range"]
        d0, d1 = cfg["dte_range"]
        sig["flags"].append(f"no contract in |Δ| {lo}-{hi} / DTE {d0}-{d1} — check chain data")
    elif contract["spread"] > max_spread:
        wide_mult = float(cfg.get("wide_spread_confidence_mult", 0.85))
        sig["flags"].append(
            f"option leg spread ${contract['spread']:.2f} > ${max_spread:.2f} — liquidity warning, use limit orders"
        )
        sig["confidence"] = round(sig["confidence"] * wide_mult, 2)

    if cfg.get("account_size"):
        dollars = cfg["account_size"] * cfg["max_risk_pct"] / 100
        sig["risk_note"] = f"Max risk this trade: ${dollars:,.0f} ({cfg['max_risk_pct']}% of ${cfg['account_size']:,.0f})"
    else:
        sig["risk_note"] = f"Max risk this trade: {cfg['max_risk_pct']}% of account (set account_size in CONFIG for $ figure)"

    if tag == "lunch_chop":
        lunch_mult = float(cfg.get("lunch_confidence_mult", 0.6))
        sig["confidence"] = round(sig["confidence"] * lunch_mult, 2)
        sig["deemphasize"] = True
        sig["flags"].append("lunch chop, low confidence")
    elif tag == "closed_avoid":
        sig["confidence"] = round(min(sig["confidence"], 0.2), 2)
        sig["deemphasize"] = True
        sig["flags"].append("market closed — signal for reference only, do not trade")

    flatten = _hm(cfg, "flatten_time")
    if tag != "closed_avoid" and (now.hour, now.minute) >= flatten:
        sig["action"] = "EXIT"
        sig["entry_reason"] = "EXIT — no new entries, flatten before close."
        sig["exit_reason"] = "EXIT — no new entries, flatten before close."
        sig["flags"].append(f"flat-by-close rule active (after {flatten[0]:02d}:{flatten[1]:02d} ET)")

    sig["confidence"] = max(0.0, min(1.0, sig["confidence"]))
    return sig


def filters_summary(cfg: dict) -> dict:
    """Structured view of every entry/exit filter for the UI / API."""
    d0, d1 = cfg["dte_range"]
    lo, hi = cfg["delta_range"]
    fo, fc = _hm(cfg, "flatten_time"), _hm(cfg, "market_close")
    return {
        "options": {
            "dte": f"{d0}–{d1}",
            "delta": f"|Δ| {lo:.2f}–{hi:.2f}",
            "target_delta": cfg.get("target_delta"),
            "min_open_interest": cfg.get("min_open_interest", 0),
            "max_option_spread": cfg.get("max_option_spread"),
            "scalp_max_option_spread": cfg.get("scalp_max_option_spread"),
        },
        "exits": {
            "target_atr_mult": cfg.get("exit_target_atr_mult"),
            "stop_atr_mult": cfg.get("exit_stop_atr_mult"),
            "min_atr_floor": cfg.get("min_atr_floor"),
            "flatten_et": f"{fo[0]:02d}:{fo[1]:02d}",
            "market_close_et": f"{fc[0]:02d}:{fc[1]:02d}",
            "scalp_max_hold_minutes": cfg.get("scalp_max_hold_minutes"),
        },
        "entry": {
            "momentum_pct_5min": cfg.get("momentum_pct_5min"),
            "volume_spike_mult": cfg.get("volume_spike_mult"),
            "breakout_volume_mult": cfg.get("breakout_volume_mult"),
            "vwap_min_volume_ratio": cfg.get("vwap_min_volume_ratio"),
            "gap_pct_threshold": cfg.get("gap_pct_threshold"),
            "gap_window_minutes": cfg.get("gap_window_minutes"),
            "max_risk_pct": cfg.get("max_risk_pct"),
        },
        "raw": {k: list(v) if isinstance(v, tuple) else v for k, v in cfg.items()},
    }


def rules_from_config(cfg: dict, risk_display: str) -> list[str]:
    d0, d1 = cfg["dte_range"]
    lo, hi = cfg["delta_range"]
    flatten = _hm(cfg, "flatten_time")
    close = _hm(cfg, "market_close")
    return [
        f"Max risk per trade: {risk_display}",
        f"Flat by {close[0]:02d}:{close[1]:02d} ET — no new entries after {flatten[0]:02d}:{flatten[1]:02d}",
        f"{cfg.get('symbol', 'SPY')} only, DTE {d0}–{d1}, |delta| {lo:.2f}–{hi:.2f} (target {cfg.get('target_delta', 0.4):.2f})",
        f"Warn when option leg spread > ${float(cfg.get('max_option_spread', 0.10)):.2f}",
        f"Scalp requires option spread ≤ ${float(cfg.get('scalp_max_option_spread', 0.05)):.2f}, hold ≤ {cfg.get('scalp_max_hold_minutes', 3)} min",
        f"Exits: target {cfg.get('exit_target_atr_mult', 2)}×ATR / stop {cfg.get('exit_stop_atr_mult', 1)}×ATR (strategy overrides apply)",
    ]
