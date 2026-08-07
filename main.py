"""
SPY Intraday Options-Signal Monitoring Dashboard — FastAPI backend.

Run:
    pip install -r requirements.txt
    python main.py                 # or: uvicorn main:app --port 8765
    open http://127.0.0.1:8765

Endpoints:
    GET /               -> the single-page dashboard (static/index.html)
    GET /api/signals    -> JSON: header data + all 5 strategy evaluations
    GET /api/health     -> provider status

PAPER-TRADING TOOL ONLY — signals are informational, nothing is executed.
"""

# =============================================================================
# CONFIG — single tuning block: every entry / exit / leg filter lives here
# =============================================================================
CONFIG = {
    "symbol": "SPY",
    "refresh_seconds": 60,            # default poll hint (UI overrides: 5s–5min, default 1 min)
    # "auto"   = Schwab (if OAuth tokens) → Yahoo → stub
    # "schwab" | "yahoo" | "stub"
    "provider": "auto",

    # -- Session clock (ET) ----------------------------------------------------
    "market_open": (9, 30),
    "market_close": (16, 0),
    "open_drive_end": (11, 0),        # open_drive session ends
    "power_hour_start": (15, 0),      # power_hour session starts
    "flatten_time": (15, 45),         # EXIT — no new entries after this (flat by close)

    # -- Option leg filters (entry) --------------------------------------------
    "dte_range": (0, 2),              # allowed DTE (nearest weekly fallback if empty)
    "delta_range": (0.30, 0.50),      # |delta| band for suggested / scalp legs
    "target_delta": 0.40,             # prefer contracts closest to this |delta|
    "min_open_interest": 0,           # drop legs below this OI (0 = off)
    "max_option_spread": 0.10,        # warn / soft-filter when leg bid-ask wider than this
    "strike_pct_from_spot": 0.03,     # chain fetch keeps strikes within ±3% of spot

    # -- Universal exit geometry (ATR / range based) ---------------------------
    "exit_target_atr_mult": 2.0,      # target = entry ± N × recent_range
    "exit_stop_atr_mult": 1.0,        # stop   = entry ∓ N × recent_range (momentum)
    "min_atr_floor": 0.10,            # $ floor for recent_range used in exits

    # -- Momentum (entry) ------------------------------------------------------
    "momentum_pct_5min": 0.3,         # |5-min % move| to qualify
    "volume_spike_mult": 1.5,         # recent bar vol vs rolling avg
    "momentum_require_vwap_side": False,  # if True, price must be on momentum side of VWAP

    # -- Scalping (entry / exit) -----------------------------------------------
    "scalp_max_option_spread": 0.05,  # $ max bid-ask on the option leg
    "scalp_target_underlying_move": (0.05, 0.20),  # $ target move clamp
    "scalp_max_hold_minutes": 3,      # time-stop
    "scalp_range_pct": 0.0012,        # max 10-bar range / price for "tight" scalp
    "scalp_min_tick_pct": 0.01,       # min |1-min %| to lean direction
    "scalp_profit_cost_mult": 2.0,    # flag if est. profit < N × round_trip_cost
    "round_trip_cost": 1.30,          # $ commissions+fees per contract round trip

    # -- Breakout (entry / exit) -----------------------------------------------
    "breakout_volume_mult": 1.3,      # volume confirmation multiple
    "breakout_proximity_pct": 0.004,  # only signal if break is within this % of level
    "breakout_stop_buffer": 0.10,     # $ past broken level for stop
    "breakout_target_mult": 2.0,      # target = N × distance from level
    "breakout_min_move": 0.15,        # $ floor for break distance
    "retest_lookback_bars": 15,
    "retest_min_bars_after": 4,       # need this many bars after breach for retest
    "retest_touch_pct": 0.0006,       # "touched level" tolerance

    # -- VWAP reclaim / rejection (entry / exit) -------------------------------
    "vwap_min_volume_ratio": 0.7,     # suppress below this day-volume pace
    "vwap_require_above_avg_volume": True,
    "vwap_stop_buffer": 0.15,         # $ past VWAP for stop
    "vwap_target_atr_mult": 2.0,

    # -- Gap and Go (entry / exit) ---------------------------------------------
    "gap_pct_threshold": 0.5,         # |gap %| to qualify
    "gap_window_minutes": 60,         # only first N minutes after open
    "gap_require_volume": False,      # if True, skip when volume not confirming
    "gap_target_gap_mult": 1.0,       # target = N × gap height; stop = today's open

    # -- Risk / session shaping ------------------------------------------------
    "max_risk_pct": 1.5,              # max risk per trade, % of account
    "account_size": None,             # None => risk shown in % only
    "lunch_confidence_mult": 0.6,     # multiply confidence in lunch_chop
    "wide_spread_confidence_mult": 0.85,
    "paper_mode": True,               # visible badge; this tool never executes
}
# =============================================================================

import os
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from providers import MarketDataProvider, SchwabProvider, YahooProvider, StubProvider, asdict
from strategies import (
    STRATEGIES,
    apply_risk_layer,
    session_tag,
    filters_summary,
    rules_from_config,
)

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="SPY Intraday Options Monitor")

# Allow embedding / health-check from the EdgeOwl Vite UI (localhost:5173 / :4000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:4000",
        "http://localhost:4000",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Provider management with auto-fallback to stub
# ---------------------------------------------------------------------------

_provider_cache: dict[str, MarketDataProvider] = {}
_stub_provider = StubProvider(CONFIG["symbol"])


def _make_provider(name: str) -> MarketDataProvider | None:
    """Construct a named provider (cached). Returns None if unavailable."""
    if name in _provider_cache:
        return _provider_cache[name]
    try:
        if name == "schwab":
            if not SchwabProvider.available():
                return None
            p = SchwabProvider(CONFIG["symbol"])
        elif name == "yahoo":
            p = YahooProvider(CONFIG["symbol"])
        else:
            return None
        _provider_cache[name] = p
        return p
    except Exception:
        traceback.print_exc()
        return None


def provider_cascade() -> list[str]:
    """Ordered live providers to try for the configured mode."""
    mode = CONFIG.get("provider") or "auto"
    if mode == "stub":
        return []
    if mode == "schwab":
        return ["schwab"]
    if mode == "yahoo":
        return ["yahoo"]
    # auto: Schwab when OAuth is connected in EdgeOwl, else Yahoo
    return ["schwab", "yahoo"]


def get_live_provider() -> MarketDataProvider | None:
    """First constructible provider in the cascade (for /api/health)."""
    for name in provider_cascade():
        p = _make_provider(name)
        if p is not None:
            return p
    return None


# ---------------------------------------------------------------------------
# Snapshot: one fetch per refresh, all derived metrics computed here
# ---------------------------------------------------------------------------

def _regular_session_bars(bars: list[dict]) -> list[dict]:
    out = []
    for b in bars:
        t = datetime.fromisoformat(b["ts"])
        if (t.hour, t.minute) >= (9, 30) and (t.hour, t.minute) < (16, 0):
            out.append(b)
    return out


def _fetch_from(provider: MarketDataProvider) -> dict:
    quote = asdict(provider.get_quote())
    bars = [asdict(b) for b in provider.get_intraday_bars()]
    pm = asdict(provider.get_premarket_levels())
    options = [asdict(o) for o in provider.get_options_chain(
        CONFIG["dte_range"],
        strike_pct_from_spot=float(CONFIG.get("strike_pct_from_spot", 0.03)),
    )]
    return {"quote": quote, "bars": bars, "pm": pm, "options": options, "source": provider.name}


def build_snapshot() -> dict:
    """Fetch raw data — cascade Schwab → Yahoo → stub — and compute metrics."""
    raw = None
    fallback_reason = None
    errors = []
    for name in provider_cascade():
        live = _make_provider(name)
        if live is None:
            errors.append(f"{name}: unavailable")
            continue
        try:
            raw = _fetch_from(live)
            break
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            traceback.print_exc()
            # Drop a failed cached instance so the next refresh can retry
            _provider_cache.pop(name, None)
    if raw is None:
        raw = _fetch_from(_stub_provider)
        fallback_reason = "; ".join(errors) if errors else "live provider unavailable"
    elif errors:
        fallback_reason = "; ".join(errors)  # tried earlier sources first

    quote, bars, pm, options = raw["quote"], raw["bars"], raw["pm"], raw["options"]
    rth = _regular_session_bars(bars)

    # --- VWAP: cumulative(price*volume)/cumulative(volume) since 9:30 ET -----
    # (typical price = (H+L+C)/3 per 1-minute bar)
    pv = v = 0.0
    for b in rth:
        tp = (b["high"] + b["low"] + b["close"]) / 3
        pv += tp * b["volume"]
        v += b["volume"]
    vwap = round(pv / v, 2) if v > 0 else None

    # --- rolling / recent volume ------------------------------------------------
    vols = [b["volume"] for b in rth]
    rolling = vols[-25:-5] if len(vols) > 10 else vols
    rolling_vol_avg = sum(rolling) / len(rolling) if rolling else 0
    recent = vols[-5:]
    recent_vol_avg = sum(recent) / len(recent) if recent else 0

    # --- day volume vs time-adjusted average pace -------------------------------
    day_volume_ratio = None
    if quote["avg_daily_volume"] and rth:
        frac_of_day = min(len(rth) / 390.0, 1.0)
        expected = quote["avg_daily_volume"] * max(frac_of_day, 0.02)
        day_volume_ratio = round(quote["day_volume"] / expected, 2) if expected else None

    # --- price change windows ----------------------------------------------------
    closes = [b["close"] for b in rth]
    px = quote["price"]
    pct_5 = round((px - closes[-6]) / closes[-6] * 100, 3) if len(closes) >= 6 else None
    pct_1 = round((px - closes[-2]) / closes[-2] * 100, 3) if len(closes) >= 2 else None
    last10 = rth[-10:]
    range_10 = round(max(b["high"] for b in last10) - min(b["low"] for b in last10), 2) if len(last10) >= 5 else None
    last5 = rth[-5:]
    recent_range = round(max(b["high"] for b in last5) - min(b["low"] for b in last5), 2) if last5 else 0.15

    # --- intraday & opening-range levels ---------------------------------------------
    intraday_high = round(max(b["high"] for b in rth), 2) if rth else None
    intraday_low = round(min(b["low"] for b in rth), 2) if rth else None
    or_bars = rth[:30]  # opening range = first 30 minutes
    or_high = round(max(b["high"] for b in or_bars), 2) if len(or_bars) >= 5 else None
    or_low = round(min(b["low"] for b in or_bars), 2) if len(or_bars) >= 5 else None

    return {
        "price": px,
        "bid": quote["bid"],
        "ask": quote["ask"],
        "spread": round(quote["ask"] - quote["bid"], 3),
        "prev_close": pm["prev_close"] or quote["prev_close"],
        "today_open": pm["today_open"],
        "day_volume": quote["day_volume"],
        "avg_daily_volume": quote["avg_daily_volume"],
        "day_volume_ratio": day_volume_ratio,
        "vwap": vwap,
        "premarket_high": pm["premarket_high"],
        "premarket_low": pm["premarket_low"],
        "intraday_high": intraday_high,
        "intraday_low": intraday_low,
        "opening_range_high": or_high,
        "opening_range_low": or_low,
        "pct_change_5min": pct_5,
        "pct_change_1min": pct_1,
        "range_10bar": range_10,
        "recent_range": recent_range,
        "recent_vol_avg": recent_vol_avg,
        "rolling_vol_avg": rolling_vol_avg,
        "bars": rth,
        "options": options,
        "source": raw["source"],
        "fallback_reason": fallback_reason,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index(request: Request):
    """Dashboard — also Schwab OAuth callback (Callback URL = https://127.0.0.1:8765)."""
    code = request.query_params.get("code")
    err = request.query_params.get("error")
    if err:
        return HTMLResponse(
            f"<h2>Schwab OAuth error</h2><p>{err}</p>"
            f"<p>{request.query_params.get('error_description','')}</p>"
            "<p><a href='/'>Back to monitor</a></p>",
            status_code=400,
        )
    if code:
        try:
            # Full callback URL (schwabdev accepts URL or bare code)
            callback = str(request.url)
            schwab = SchwabProvider.from_db(CONFIG["symbol"])
            result = schwab.exchange_code(callback)
            # Drop cached providers so next /api/signals uses Schwab
            _provider_cache.clear()
            return HTMLResponse(
                "<h2>Schwab connected</h2>"
                "<p>OAuth via <b>schwabdev</b> — tokens saved locally and mirrored to EdgeOwl.</p>"
                "<p><a href='/'>Open SPY Day-Trade Monitor</a></p>"
                f"<pre>access expires ms={result.get('accessTokenExpiresAt')}</pre>"
            )
        except Exception as e:
            traceback.print_exc()
            return HTMLResponse(
                f"<h2>Schwab token exchange failed</h2><pre>{type(e).__name__}: {e}</pre>"
                "<p>Callback URL in Schwab portal must be exactly <code>https://127.0.0.1:8765</code> "
                "(no trailing slash — required by schwabdev).</p>"
                "<p><a href='/'>Back</a></p>",
                status_code=400,
            )
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/api/schwab/auth-url")
def schwab_auth_url():
    """Build Step-1 authorize URL using credentials stored in EdgeOwl DB."""
    try:
        schwab = SchwabProvider.from_db(CONFIG["symbol"])
        c = schwab._creds()
        if not c["client_id"] or not c["client_secret"]:
            return JSONResponse(
                {"error": "Save client_id + client_secret in EdgeOwl Integrations first"},
                status_code=400,
            )
        from urllib.parse import urlencode
        q = urlencode({
            "response_type": "code",
            "client_id": c["client_id"],
            "scope": "readonly",
            "redirect_uri": c["redirect_uri"],
        })
        return {
            "url": f"https://api.schwabapi.com/v1/oauth/authorize?{q}",
            "redirectUri": c["redirect_uri"],
            "client": "schwabdev",
            "note": "Callback URL in Schwab portal must be exactly https://127.0.0.1:8765 (no trailing slash)",
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/health")
def health():
    live = get_live_provider()
    schwab_ok = SchwabProvider.available()
    schwab = SchwabProvider.from_db(CONFIG["symbol"])
    creds = schwab._creds()
    return {
        "ok": True,
        "configured_provider": CONFIG["provider"],
        "cascade": provider_cascade(),
        "schwab_configured": bool(creds["client_id"] and creds["client_secret"]),
        "schwab_connected": schwab_ok,
        "schwab_redirect": creds["redirect_uri"],
        "schwab_client": "schwabdev",
        "schwab_tokens_db": schwab.tokens_db,
        "live_provider_ready": live is not None,
        "live_provider": live.name if live else None,
    }


def _clock_hm(key: str) -> tuple[int, int]:
    v = CONFIG[key]
    return (int(v[0]), int(v[1]))


@app.get("/api/config")
def api_config():
    """All entry/exit/leg filters currently in effect."""
    return filters_summary(CONFIG)


@app.get("/api/signals")
def signals():
    now = datetime.now(ET)
    try:
        snap = build_snapshot()
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {e}"})

    tag = session_tag(now, CONFIG)
    flatten = _clock_hm("flatten_time")
    close = _clock_hm("market_close")
    flatten_active = tag != "closed_avoid" and (now.hour, now.minute) >= flatten

    # --- evaluate all 5 strategies -------------------------------------------
    results = []
    for key, fn in STRATEGIES:
        try:
            sig, reason = fn(snap, CONFIG, now)
        except Exception as e:
            traceback.print_exc()
            sig, reason = None, f"strategy error: {type(e).__name__}: {e}"
        if sig is not None:
            sig = apply_risk_layer(sig, snap, CONFIG, now)
        if flatten_active and sig is None:
            reason = "EXIT — no new entries, flatten before close. " + (reason or "")
        results.append({
            "key": key,
            "strategy": sig["strategy"] if sig else _pretty_name(key),
            "active": sig is not None,
            "signal": sig,
            "no_signal_reason": reason,
        })

    # --- flatten countdown --------------------------------------------------------
    flatten_dt = now.replace(hour=flatten[0], minute=flatten[1], second=0, microsecond=0)
    close_dt = now.replace(hour=close[0], minute=close[1], second=0, microsecond=0)
    secs_to_flatten = int((flatten_dt - now).total_seconds())
    secs_to_close = int((close_dt - now).total_seconds())

    account = CONFIG["account_size"]
    risk_display = (f"${account * CONFIG['max_risk_pct'] / 100:,.0f} ({CONFIG['max_risk_pct']}%)"
                    if account else f"{CONFIG['max_risk_pct']}% of account")
    filt = filters_summary(CONFIG)

    return {
        "header": {
            "symbol": CONFIG["symbol"],
            "price": snap["price"],
            "bid": snap["bid"],
            "ask": snap["ask"],
            "spread": snap["spread"],
            "prev_close": snap["prev_close"],
            "change_pct": round((snap["price"] - snap["prev_close"]) / snap["prev_close"] * 100, 2) if snap["prev_close"] else None,
            "day_volume": snap["day_volume"],
            "avg_daily_volume": snap["avg_daily_volume"],
            "day_volume_ratio": snap["day_volume_ratio"],
            "vwap": snap["vwap"],
            "premarket_high": snap["premarket_high"],
            "premarket_low": snap["premarket_low"],
            "intraday_high": snap["intraday_high"],
            "intraday_low": snap["intraday_low"],
            "session_tag": tag,
            "market_clock_et": now.strftime("%a %b %d  %I:%M:%S %p ET"),
        },
        "signals": results,
        "risk": {
            "max_risk": risk_display,
            "paper_mode": CONFIG["paper_mode"],
            "flatten_active": flatten_active,
            "seconds_to_flatten": max(secs_to_flatten, 0) if tag != "closed_avoid" else None,
            "seconds_to_close": max(secs_to_close, 0) if tag != "closed_avoid" else None,
            "session_window": _session_window_text(tag),
            "rules": rules_from_config(CONFIG, risk_display),
        },
        "filters": filt,
        "meta": {
            "refresh_seconds": CONFIG["refresh_seconds"],
            "provider_configured": CONFIG["provider"],
            "provider_used": snap["source"],
            "fallback_reason": snap["fallback_reason"],
            "generated_at": now.isoformat(),
            "delta_range": list(CONFIG["delta_range"]),
            "dte_range": list(CONFIG["dte_range"]),
            "target_delta": CONFIG["target_delta"],
            "flatten_time": list(flatten),
        },
    }


def _pretty_name(key: str) -> str:
    return {"momentum": "Momentum", "scalping": "Scalping", "breakout": "Breakout",
            "vwap": "VWAP Reclaim/Rejection", "gap_and_go": "Gap and Go"}[key]


def _session_window_text(tag: str) -> str:
    return {
        "open_drive": "Open drive (9:30-11:00 ET) — high confidence window",
        "lunch_chop": "Lunch chop (11:00-15:00 ET) — low confidence, size down",
        "power_hour": "Power hour (15:00-16:00 ET) — high confidence window",
        "closed_avoid": "Market closed — avoid trading",
    }[tag]


if __name__ == "__main__":
    import uvicorn
    # Schwab Callback URL is https://127.0.0.1:8765 (no trailing slash — schwabdev).
    cert = os.path.join(HERE, "certs", "cert.pem")
    key = os.path.join(HERE, "certs", "key.pem")
    ssl_kwargs = {}
    if os.path.isfile(cert) and os.path.isfile(key):
        ssl_kwargs = {"ssl_certfile": cert, "ssl_keyfile": key}
        print("[SPY Day-Trade] HTTPS on https://127.0.0.1:8765 (schwabdev OAuth callback ready)")
    else:
        print("[SPY Day-Trade] HTTP on http://127.0.0.1:8765 — generate certs/ for Schwab HTTPS callback:")
        print("  openssl req -x509 -newkey rsa:2048 -keyout certs/key.pem -out certs/cert.pem -days 365 -nodes -subj /CN=127.0.0.1")
    uvicorn.run(app, host="127.0.0.1", port=8765, **ssl_kwargs)
