"""
Market data providers for the SPY intraday options-signal dashboard.

Exposes a clean provider interface (MarketDataProvider) with three shipped
implementations:

  * SchwabProvider — Charles Schwab Market Data via the schwabdev Client
    (https://github.com/tylerebowers/Schwabdev). App credentials come from
    EdgeOwl `provider_token_schwab`; OAuth tokens live in
    `data/schwabdev-tokens.db` (repo root) and are mirrored to EdgeOwl
    `schwab_oauth` so the Node provider stays in sync.

  * YahooProvider — free Yahoo Finance data via the `yfinance` package.
    No API key required. Options greeks (delta) are NOT supplied by Yahoo,
    so delta is approximated locally with Black-Scholes using Yahoo's IV.

  * StubProvider  — fully simulated but plausible data (random-walk price,
    realistic session volume curve, synthetic options chain). Lets the
    dashboard run with ZERO keys and no internet.

------------------------------------------------------------------------------
PLUG-IN POINT FOR PAID / BROKER FEEDS (Tradier, Polygon, thinkorswim, IBKR):
Subclass MarketDataProvider and implement the four abstract methods, then set
CONFIG["provider"] in main.py (see get_live_provider cascade).
------------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import math
import os
import random
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

RISK_FREE_RATE = 0.04  # used only for the local Black-Scholes delta approximation


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class Quote:
    price: float
    prev_close: float
    day_volume: int            # cumulative shares traded today
    avg_daily_volume: int      # ~3 month average daily volume (liquidity filter)
    bid: float
    ask: float
    ts: str                    # ISO timestamp (ET)


@dataclass
class Bar:
    ts: str        # ISO timestamp (ET) of bar start
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class PremarketLevels:
    premarket_high: float | None
    premarket_low: float | None
    prev_close: float | None
    today_open: float | None


@dataclass
class OptionContract:
    symbol: str
    type: str          # 'call' | 'put'
    strike: float
    expiry: str        # YYYY-MM-DD
    dte: int
    bid: float
    ask: float
    mid: float
    spread: float
    iv: float          # implied volatility (decimal, e.g. 0.18)
    open_interest: int
    delta: float       # calls > 0, puts < 0


# ---------------------------------------------------------------------------
# Black-Scholes delta helper (Yahoo does not ship greeks)
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(spot: float, strike: float, dte_days: float, iv: float, is_call: bool) -> float:
    """Approximate Black-Scholes delta. Good enough for 0.30-0.50 filtering.

    Yahoo's free feed often reports IV as 0 (deep ITM) or implausibly tiny
    values on 0DTE contracts, and a literal t=0 makes delta binary. So we
    floor time-to-expiry at half a trading day and IV at 8% — this keeps the
    delta curve smooth enough to rank/filter contracts, which is all we need.
    (Vendor feeds like Tradier/Polygon ship real greeks — prefer those.)"""
    if spot <= 0 or strike <= 0:
        return 0.0
    t = max(dte_days, 0.5) / 365.0
    vol = max(iv, 0.08)
    d1 = (math.log(spot / strike) + (RISK_FREE_RATE + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    return _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

class MarketDataProvider(ABC):
    """Abstract market-data interface. Implement these four methods to plug in
    any feed (Tradier / Polygon / thinkorswim / IBKR — see module docstring)."""

    name: str = "abstract"

    @abstractmethod
    def get_quote(self) -> Quote:
        """Latest SPY quote: last price, day volume, avg daily volume, bid/ask."""

    @abstractmethod
    def get_intraday_bars(self) -> list[Bar]:
        """Today's 1-minute bars (including pre-market where available), ET."""

    @abstractmethod
    def get_premarket_levels(self) -> PremarketLevels:
        """Pre-market high/low, previous close, and today's regular-session open."""

    @abstractmethod
    def get_options_chain(
        self,
        dte_range: tuple[int, int],
        strike_pct_from_spot: float = 0.03,
    ) -> list[OptionContract]:
        """Snapshot of near-dated contracts (DTE band + nearest weekly).

        strike_pct_from_spot: keep strikes within ± this fraction of spot.
        """


# ---------------------------------------------------------------------------
# SchwabProvider — schwabdev Client + EdgeOwl credential/token bridge
# ---------------------------------------------------------------------------

REFRESH_TOKEN_LIFETIME_MS = 7 * 24 * 60 * 60 * 1000
# schwabdev rejects callback URLs that end with "/".
DEFAULT_SCHWAB_REDIRECT = "https://127.0.0.1:8765"


def _default_edgeowl_db() -> str:
    """Optional EdgeOwl scanner.db for shared Schwab app keys / token mirror.

    Resolution order (first existing file wins when callers check isfile):
      EDGEDOWL_DB env → sibling ../options-scanner/server/scanner.db
      → local data/settings.db (standalone settings store)
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.environ.get("EDGEDOWL_DB"),
        os.path.normpath(os.path.join(here, "..", "options-scanner", "server", "scanner.db")),
        os.path.join(here, "data", "settings.db"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    # Default write target for standalone (created on first _write_setting if needed)
    return os.path.join(here, "data", "settings.db")


def _default_schwabdev_tokens_db() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(here, "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "schwabdev-tokens.db")


def _normalize_redirect(uri: str | None) -> str:
    """Schwabdev requires https and forbids a trailing slash."""
    u = (uri or DEFAULT_SCHWAB_REDIRECT).strip()
    while u.endswith("/"):
        u = u[:-1]
    return u or DEFAULT_SCHWAB_REDIRECT


class SchwabProvider(MarketDataProvider):
    """Charles Schwab Market Data via schwabdev.

    Credentials:
      EdgeOwl `provider_token_schwab` → { clientId, token=<client_secret>, redirectUri }
      Env fallbacks: SCHWAB_CLIENT_ID, SCHWAB_CLIENT_SECRET, SCHWAB_REDIRECT_URI

    Tokens:
      Primary: data/schwabdev-tokens.db (schwabdev format, repo root)
      Mirror:  EdgeOwl `schwab_oauth` (so Node Integrations / universe stay in sync)

    Callback URL in the Schwab developer portal must be exactly
    https://127.0.0.1:8765  (no trailing slash — schwabdev requirement).
    """

    name = "schwab"

    def __init__(self, symbol: str = "SPY", db_path: str | None = None, tokens_db: str | None = None):
        self.symbol = symbol.upper()
        self.db_path = db_path or os.environ.get("EDGEDOWL_DB") or _default_edgeowl_db()
        self.tokens_db = tokens_db or os.environ.get("SCHWABDEV_TOKENS_DB") or _default_schwabdev_tokens_db()
        self._cache: dict[str, tuple[float, object]] = {}
        self._client = None
        if not self.is_connected():
            raise RuntimeError(
                "Schwab not connected — open https://127.0.0.1:8765/api/schwab/auth-url "
                "and complete OAuth (Callback URL must be https://127.0.0.1:8765)"
            )
        self._client = self._build_client()

    # -- settings helpers ----------------------------------------------------
    def _read_setting(self, key: str):
        if not os.path.isfile(self.db_path):
            return None
        try:
            con = sqlite3.connect(self.db_path, timeout=5)
            try:
                row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            finally:
                con.close()
            return json.loads(row[0]) if row and row[0] else None
        except Exception:
            return None

    def _write_setting(self, key: str, obj: dict) -> None:
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        con = sqlite3.connect(self.db_path, timeout=5)
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updatedAt TEXT
                )
                """
            )
            con.execute(
                "INSERT OR REPLACE INTO settings (key, value, updatedAt) VALUES (?, ?, ?)",
                (key, json.dumps(obj), datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
            )
            con.commit()
        finally:
            con.close()

    def _creds(self) -> dict:
        row = self._read_setting("provider_token_schwab") or {}
        return {
            "client_id": row.get("clientId") or os.environ.get("SCHWAB_CLIENT_ID") or "",
            "client_secret": row.get("token") or os.environ.get("SCHWAB_CLIENT_SECRET") or "",
            "redirect_uri": _normalize_redirect(
                row.get("redirectUri")
                or os.environ.get("SCHWAB_REDIRECT_URI")
                or DEFAULT_SCHWAB_REDIRECT
            ),
        }

    def _edgeowl_tokens(self) -> dict | None:
        t = self._read_setting("schwab_oauth")
        if t and t.get("refreshToken"):
            return t
        at = os.environ.get("SCHWAB_ACCESS_TOKEN")
        rt = os.environ.get("SCHWAB_REFRESH_TOKEN")
        if at and rt:
            now = int(time.time() * 1000)
            return {
                "accessToken": at,
                "refreshToken": rt,
                "accessTokenExpiresAt": now + 25 * 60 * 1000,
                "refreshTokenExpiresAt": now + REFRESH_TOKEN_LIFETIME_MS,
                "obtainedAt": now,
            }
        return None

    def _schwabdev_has_tokens(self) -> bool:
        if not os.path.isfile(self.tokens_db):
            return False
        try:
            con = sqlite3.connect(self.tokens_db, timeout=5)
            try:
                row = con.execute(
                    "SELECT refresh_token FROM schwabdev LIMIT 1"
                ).fetchone()
            finally:
                con.close()
            return bool(row and row[0])
        except Exception:
            return False

    def _seed_schwabdev_from_edgeowl(self) -> bool:
        """Copy EdgeOwl schwab_oauth into schwabdev's tokens.db if needed."""
        if self._schwabdev_has_tokens():
            return True
        eo = self._edgeowl_tokens()
        if not eo or not eo.get("refreshToken") or not eo.get("accessToken"):
            return False

        now = datetime.now(timezone.utc)
        at_exp_ms = int(eo.get("accessTokenExpiresAt") or 0)
        rt_exp_ms = int(eo.get("refreshTokenExpiresAt") or 0)
        expires_in = 1800
        if at_exp_ms > 0:
            at_issued = datetime.fromtimestamp(at_exp_ms / 1000 - expires_in, tz=timezone.utc)
        else:
            at_issued = now
        if rt_exp_ms > 0:
            rt_issued = datetime.fromtimestamp(
                rt_exp_ms / 1000 - (REFRESH_TOKEN_LIFETIME_MS / 1000), tz=timezone.utc
            )
        else:
            rt_issued = now

        os.makedirs(os.path.dirname(self.tokens_db) or ".", exist_ok=True)
        con = sqlite3.connect(self.tokens_db, timeout=5)
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS schwabdev (
                    access_token_issued TEXT NOT NULL,
                    refresh_token_issued TEXT NOT NULL,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    id_token TEXT NOT NULL,
                    expires_in INTEGER,
                    token_type TEXT,
                    scope TEXT
                )
                """
            )
            con.execute("DELETE FROM schwabdev")
            con.execute(
                """
                INSERT INTO schwabdev (
                    access_token_issued, refresh_token_issued,
                    access_token, refresh_token, id_token,
                    expires_in, token_type, scope
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    at_issued.isoformat(),
                    rt_issued.isoformat(),
                    eo["accessToken"],
                    eo["refreshToken"],
                    eo.get("idToken") or "",
                    expires_in,
                    eo.get("tokenType") or "Bearer",
                    eo.get("scope") or "api",
                ),
            )
            con.commit()
            return True
        finally:
            con.close()

    def _sync_edgeowl_from_client(self) -> None:
        """Mirror schwabdev in-memory tokens into EdgeOwl so Node stays connected."""
        if not self._client:
            return
        tokens_obj = self._client.tokens
        access = getattr(tokens_obj, "access_token", None)
        refresh = getattr(tokens_obj, "refresh_token", None)
        if not access or not refresh:
            return
        at_issued = getattr(tokens_obj, "_access_token_issued", None) or datetime.now(timezone.utc)
        rt_issued = getattr(tokens_obj, "_refresh_token_issued", None) or at_issued
        at_timeout = int(getattr(tokens_obj, "_access_token_timeout", 1800) or 1800)
        if at_issued.tzinfo is None:
            at_issued = at_issued.replace(tzinfo=timezone.utc)
        if rt_issued.tzinfo is None:
            rt_issued = rt_issued.replace(tzinfo=timezone.utc)
        at_ms = int(at_issued.timestamp() * 1000)
        rt_ms = int(rt_issued.timestamp() * 1000)
        payload = {
            "accessToken": access,
            "refreshToken": refresh,
            "accessTokenExpiresAt": at_ms + at_timeout * 1000,
            "refreshTokenExpiresAt": rt_ms + REFRESH_TOKEN_LIFETIME_MS,
            "tokenType": "Bearer",
            "scope": "api",
            "obtainedAt": at_ms,
            "source": "schwabdev",
        }
        if os.path.isfile(self.db_path):
            self._write_setting("schwab_oauth", payload)

    def _auth_blocker(self, auth_url: str):
        """Never block the FastAPI process on interactive OAuth."""
        raise RuntimeError(
            "Schwab OAuth required. GET /api/schwab/auth-url then complete login. "
            f"Authorize URL: {auth_url}"
        )

    def _build_client(self, call_on_auth=None, seed_from_edgeowl: bool = True):
        import schwabdev

        c = self._creds()
        if not c["client_id"] or not c["client_secret"]:
            raise RuntimeError("Schwab client_id/secret missing in EdgeOwl DB")
        if seed_from_edgeowl:
            self._seed_schwabdev_from_edgeowl()
        return schwabdev.Client(
            app_key=c["client_id"],
            app_secret=c["client_secret"],
            callback_url=c["redirect_uri"],
            tokens_db=self.tokens_db,
            timeout=25,
            open_browser_for_auth=False,
            call_on_auth=call_on_auth or self._auth_blocker,
        )

    def _json(self, resp) -> dict:
        if resp is None:
            raise RuntimeError("Schwabdev returned empty response")
        status = getattr(resp, "status_code", 200)
        try:
            data = resp.json() if hasattr(resp, "json") else resp
        except Exception as e:
            text = getattr(resp, "text", "")[:300]
            raise RuntimeError(f"Schwab JSON parse error ({status}): {text}") from e
        if status >= 400:
            raise RuntimeError(f"Schwab {status}: {str(data)[:300]}")
        if not isinstance(data, dict):
            raise RuntimeError(f"Schwab unexpected payload type: {type(data).__name__}")
        self._sync_edgeowl_from_client()
        return data

    @classmethod
    def from_db(
        cls,
        symbol: str = "SPY",
        db_path: str | None = None,
        tokens_db: str | None = None,
    ) -> "SchwabProvider":
        """Construct without requiring an existing OAuth session (for code exchange)."""
        obj = cls.__new__(cls)
        obj.symbol = symbol.upper()
        obj.db_path = db_path or os.environ.get("EDGEDOWL_DB") or _default_edgeowl_db()
        obj.tokens_db = tokens_db or os.environ.get("SCHWABDEV_TOKENS_DB") or _default_schwabdev_tokens_db()
        obj._cache = {}
        obj._client = None
        return obj

    def exchange_code(self, code_or_url: str) -> dict:
        """Step 2 of OAuth via schwabdev: exchange code, persist tokens, sync EdgeOwl."""
        raw = (code_or_url or "").strip()
        if not raw:
            raise RuntimeError("Empty authorization code")

        c = self._creds()
        if not c["client_id"] or not c["client_secret"]:
            raise RuntimeError("Schwab client_id/secret missing in EdgeOwl DB")

        # Clear stale schwabdev row so Client.__init__ runs the auth path.
        if os.path.isfile(self.tokens_db):
            try:
                con = sqlite3.connect(self.tokens_db, timeout=5)
                try:
                    con.execute("DELETE FROM schwabdev")
                    con.commit()
                finally:
                    con.close()
            except Exception:
                try:
                    os.remove(self.tokens_db)
                except OSError:
                    pass

        def supply_code(_auth_url: str) -> str:
            return raw

        # Do not re-seed from EdgeOwl — that would skip the code-exchange path.
        self._client = self._build_client(call_on_auth=supply_code, seed_from_edgeowl=False)
        if not self._client.tokens.refresh_token:
            raise RuntimeError("schwabdev token exchange produced no refresh_token")
        self._sync_edgeowl_from_client()
        t = self._edgeowl_tokens() or {}
        return {
            "ok": True,
            "connected": True,
            "accessTokenExpiresAt": t.get("accessTokenExpiresAt"),
            "refreshTokenExpiresAt": t.get("refreshTokenExpiresAt"),
            "via": "schwabdev",
        }

    def is_connected(self) -> bool:
        c = self._creds()
        if not (c["client_id"] and c["client_secret"]):
            return False
        if self._schwabdev_has_tokens():
            return True
        t = self._edgeowl_tokens()
        if not (t and t.get("refreshToken")):
            return False
        exp = t.get("refreshTokenExpiresAt") or 0
        if exp and int(exp) <= int(time.time() * 1000):
            return False
        return True

    @classmethod
    def available(cls, db_path: str | None = None, tokens_db: str | None = None) -> bool:
        """True when app creds + usable OAuth tokens exist (no Client init)."""
        try:
            probe = cls.from_db("SPY", db_path=db_path, tokens_db=tokens_db)
            return probe.is_connected()
        except Exception:
            return False

    def _cached(self, key: str, ttl: float, fn):
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        val = fn()
        self._cache[key] = (now, val)
        return val

    def _ensure_client(self):
        if self._client is None:
            self._client = self._build_client()
        else:
            try:
                self._client.update_tokens()
            except Exception:
                pass
        return self._client

    # -- MarketDataProvider --------------------------------------------------
    def get_quote(self) -> Quote:
        def fetch():
            client = self._ensure_client()
            data = self._json(client.quotes(self.symbol, fields="quote"))
            entry = data.get(self.symbol) or {}
            q = entry.get("quote") or {}
            price = float(q.get("lastPrice") or q.get("mark") or q.get("closePrice") or 0)
            if price <= 0:
                raise RuntimeError(f"Schwab returned no price for {self.symbol}")
            prev = float(q.get("closePrice") or price)
            bid = float(q.get("bidPrice") or price - 0.01)
            ask = float(q.get("askPrice") or price + 0.01)
            day_vol = int(q.get("totalVolume") or 0)
            avg_vol = int(os.environ.get("SPY_AVG_DAILY_VOLUME") or 70_000_000)
            return Quote(
                price=round(price, 2),
                prev_close=round(prev, 2),
                day_volume=day_vol,
                avg_daily_volume=avg_vol,
                bid=round(bid, 2),
                ask=round(ask, 2),
                ts=datetime.now(ET).isoformat(),
            )
        return self._cached("quote", 5, fetch)

    def get_intraday_bars(self) -> list[Bar]:
        def fetch():
            client = self._ensure_client()
            end = datetime.now(timezone.utc)
            start = end - timedelta(hours=18)
            data = self._json(client.price_history(
                self.symbol,
                periodType="day",
                frequencyType="minute",
                frequency=1,
                startDate=start,
                endDate=end,
                needExtendedHoursData=True,
            ))
            candles = data.get("candles") or []
            if not candles:
                raise RuntimeError("Schwab returned no intraday candles")
            bars: list[Bar] = []
            for c in candles:
                ms = c.get("datetime")
                if ms is None:
                    continue
                ts_et = datetime.fromtimestamp(ms / 1000, tz=ET)
                bars.append(Bar(
                    ts=ts_et.isoformat(),
                    open=round(float(c["open"]), 4),
                    high=round(float(c["high"]), 4),
                    low=round(float(c["low"]), 4),
                    close=round(float(c["close"]), 4),
                    volume=int(c.get("volume") or 0),
                ))
            return bars
        return self._cached("bars", 10, fetch)

    def get_premarket_levels(self) -> PremarketLevels:
        bars = self.get_intraday_bars()
        q = self.get_quote()
        prev_close = q.prev_close
        pm_hi = pm_lo = None
        today_open = None
        for b in bars:
            t = datetime.fromisoformat(b.ts)
            hm = (t.hour, t.minute)
            if hm < (9, 30):
                pm_hi = b.high if pm_hi is None else max(pm_hi, b.high)
                pm_lo = b.low if pm_lo is None else min(pm_lo, b.low)
            elif today_open is None:
                today_open = b.open
        return PremarketLevels(
            premarket_high=round(pm_hi, 2) if pm_hi else None,
            premarket_low=round(pm_lo, 2) if pm_lo else None,
            prev_close=round(prev_close, 2) if prev_close else None,
            today_open=round(today_open, 2) if today_open else None,
        )

    def get_options_chain(
        self,
        dte_range: tuple[int, int],
        strike_pct_from_spot: float = 0.03,
    ) -> list[OptionContract]:
        def fetch():
            client = self._ensure_client()
            today = datetime.now(ET).date()
            from_d = today.isoformat()
            to_d = (today + timedelta(days=max(dte_range[1] + 5, 7))).isoformat()
            data = self._json(client.option_chains(
                self.symbol,
                contractType="ALL",
                strategy="SINGLE",
                fromDate=from_d,
                toDate=to_d,
                includeUnderlyingQuote=True,
            ))
            spot = float(data.get("underlyingPrice") or self.get_quote().price)
            band = float(strike_pct_from_spot)
            contracts: list[OptionContract] = []

            def walk(exp_map, otype: str):
                for exp_key, strike_map in (exp_map or {}).items():
                    expiry = str(exp_key).split(":")[0]
                    try:
                        dte = (date.fromisoformat(expiry) - today).days
                    except ValueError:
                        continue
                    in_window = dte_range[0] <= dte <= dte_range[1]
                    for clist in (strike_map or {}).values():
                        for c in (clist if isinstance(clist, list) else [clist]):
                            strike = float(c.get("strikePrice") or 0)
                            if strike <= 0 or abs(strike - spot) > spot * band:
                                continue
                            bid = float(c.get("bid") or 0)
                            ask = float(c.get("ask") or 0)
                            if bid <= 0 and ask <= 0:
                                continue
                            mid = float(c.get("mark") or 0) or ((bid + ask) / 2 if ask > 0 else bid)
                            vol = c.get("volatility")
                            iv = float(vol or 0)
                            if iv > 3:
                                iv /= 100.0
                            delta = c.get("delta")
                            if delta is None or float(delta) <= -999:
                                delta = bs_delta(spot, strike, dte, iv, otype == "call")
                            else:
                                delta = float(delta)
                            if not in_window and dte > dte_range[1]:
                                continue
                            contracts.append(OptionContract(
                                symbol=str(c.get("symbol") or f"SPY {expiry} {strike} {otype[0].upper()}"),
                                type=otype,
                                strike=strike,
                                expiry=expiry,
                                dte=int(c.get("daysToExpiration") if c.get("daysToExpiration") is not None else dte),
                                bid=round(bid, 2),
                                ask=round(ask, 2),
                                mid=round(mid, 3),
                                spread=round(ask - bid, 3),
                                iv=round(iv, 4),
                                open_interest=int(c.get("openInterest") or 0),
                                delta=round(delta, 3),
                            ))

            walk(data.get("callExpDateMap"), "call")
            walk(data.get("putExpDateMap"), "put")

            in_band = [c for c in contracts if dte_range[0] <= c.dte <= dte_range[1]]
            if in_band:
                return in_band
            if not contracts:
                raise RuntimeError("Schwab options chain had no usable contracts")
            min_dte = min(c.dte for c in contracts)
            return [c for c in contracts if c.dte == min_dte]

        return self._cached("chain", 30, fetch)


# ---------------------------------------------------------------------------
# YahooProvider — free, keyless, best-effort
# ---------------------------------------------------------------------------

class YahooProvider(MarketDataProvider):
    """Free Yahoo Finance data via yfinance.

    Caveats (all handled gracefully):
      * Yahoo does not provide option greeks -> delta approximated locally.
      * Bid/ask for the SPY underlying is unreliable on the free feed ->
        approximated as last +/- $0.01 (SPY is penny-wide essentially always).
      * Data is delayed ~15 min on the free feed.
    Any exception raised here makes main.py auto-fall back to StubProvider.
    """

    name = "yahoo"

    def __init__(self, symbol: str = "SPY"):
        import yfinance as yf  # imported lazily so StubProvider works without it
        self._yf = yf
        self.symbol = symbol
        self._ticker = yf.Ticker(symbol)
        # tiny TTL caches so a 5s refresh doesn't hammer Yahoo
        self._cache: dict[str, tuple[float, object]] = {}

    def _cached(self, key: str, ttl: float, fn):
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        val = fn()
        self._cache[key] = (now, val)
        return val

    # -- quote ---------------------------------------------------------------
    def get_quote(self) -> Quote:
        def fetch():
            fi = self._ticker.fast_info
            price = float(fi["lastPrice"])
            prev_close = float(fi["previousClose"])
            try:
                avg_vol = int(fi["threeMonthAverageVolume"])
            except Exception:
                avg_vol = 70_000_000  # sane SPY fallback
            bars = self.get_intraday_bars()
            day_volume = sum(b.volume for b in bars) or int(fi.get("lastVolume") or 0)
            # SPY underlying is essentially always penny-wide; free Yahoo bid/ask
            # is stale, so we approximate. Option-leg spreads come from the chain.
            return Quote(
                price=round(price, 2),
                prev_close=round(prev_close, 2),
                day_volume=day_volume,
                avg_daily_volume=avg_vol,
                bid=round(price - 0.01, 2),
                ask=round(price + 0.01, 2),
                ts=datetime.now(ET).isoformat(),
            )
        return self._cached("quote", 5, fetch)

    # -- bars ------------------------------------------------------------------
    def get_intraday_bars(self) -> list[Bar]:
        def fetch():
            df = self._ticker.history(period="1d", interval="1m", prepost=True)
            if df is None or df.empty:
                raise RuntimeError("Yahoo returned no intraday bars")
            bars: list[Bar] = []
            for ts, row in df.iterrows():
                ts_et = ts.tz_convert(ET) if ts.tzinfo else ts.tz_localize("UTC").tz_convert(ET)
                bars.append(Bar(
                    ts=ts_et.isoformat(),
                    open=round(float(row["Open"]), 4),
                    high=round(float(row["High"]), 4),
                    low=round(float(row["Low"]), 4),
                    close=round(float(row["Close"]), 4),
                    volume=int(row["Volume"]),
                ))
            return bars
        return self._cached("bars", 10, fetch)

    # -- pre-market levels -----------------------------------------------------
    def get_premarket_levels(self) -> PremarketLevels:
        bars = self.get_intraday_bars()
        fi = self._ticker.fast_info
        prev_close = float(fi["previousClose"])
        pm_hi = pm_lo = None
        today_open = None
        for b in bars:
            t = datetime.fromisoformat(b.ts)
            hm = (t.hour, t.minute)
            if hm < (9, 30):
                pm_hi = b.high if pm_hi is None else max(pm_hi, b.high)
                pm_lo = b.low if pm_lo is None else min(pm_lo, b.low)
            elif today_open is None:
                today_open = b.open
        return PremarketLevels(
            premarket_high=round(pm_hi, 2) if pm_hi else None,
            premarket_low=round(pm_lo, 2) if pm_lo else None,
            prev_close=round(prev_close, 2),
            today_open=round(today_open, 2) if today_open else None,
        )

    # -- options chain ----------------------------------------------------------
    def get_options_chain(
        self,
        dte_range: tuple[int, int],
        strike_pct_from_spot: float = 0.03,
    ) -> list[OptionContract]:
        def fetch():
            spot = float(self._ticker.fast_info["lastPrice"])
            today = datetime.now(ET).date()
            expiries = list(self._ticker.options or [])
            if not expiries:
                raise RuntimeError("Yahoo returned no option expiries")
            picked: list[tuple[str, int]] = []
            for e in expiries:
                d = date.fromisoformat(e)
                dte = (d - today).days
                if dte_range[0] <= dte <= dte_range[1]:
                    picked.append((e, dte))
            # always include the nearest weekly even if outside dte_range
            if not picked:
                e = expiries[0]
                picked = [(e, (date.fromisoformat(e) - today).days)]
            band = float(strike_pct_from_spot)
            contracts: list[OptionContract] = []
            for expiry, dte in picked[:3]:
                chain = self._ticker.option_chain(expiry)
                for df, otype in ((chain.calls, "call"), (chain.puts, "put")):
                    for _, r in df.iterrows():
                        strike = float(r["strike"])
                        if abs(strike - spot) > spot * band:
                            continue
                        bid = float(r.get("bid") or 0.0)
                        ask = float(r.get("ask") or 0.0)
                        if bid <= 0 and ask <= 0:
                            continue
                        iv = float(r.get("impliedVolatility") or 0.0)
                        delta = bs_delta(spot, strike, dte, iv, otype == "call")
                        contracts.append(OptionContract(
                            symbol=str(r.get("contractSymbol") or f"SPY {expiry} {strike} {otype[0].upper()}"),
                            type=otype,
                            strike=strike,
                            expiry=expiry,
                            dte=dte,
                            bid=round(bid, 2),
                            ask=round(ask, 2),
                            mid=round((bid + ask) / 2, 3),
                            spread=round(ask - bid, 3),
                            iv=round(iv, 4),
                            open_interest=int(r.get("openInterest") or 0),
                            delta=round(delta, 3),
                        ))
            if not contracts:
                raise RuntimeError("Yahoo options chain had no usable contracts")
            return contracts
        return self._cached("chain", 60, fetch)


# ---------------------------------------------------------------------------
# StubProvider — plausible simulated data, zero keys, zero internet
# ---------------------------------------------------------------------------

class StubProvider(MarketDataProvider):
    """Simulated SPY session.

    Generates a random-walk price path anchored at a realistic level, a
    U-shaped intraday volume curve, occasional momentum bursts (so strategies
    actually fire once in a while), pre-market levels, and a synthetic options
    chain with Black-Scholes deltas. State persists across refreshes so the
    dashboard evolves naturally. If the market is closed the most recent
    weekday session is simulated in full so there is always data to look at.
    """

    name = "stub"

    def __init__(self, symbol: str = "SPY", seed: int | None = None):
        self.symbol = symbol
        self.rng = random.Random(seed if seed is not None else int(time.time()) // 86400)
        self.prev_close = round(self.rng.uniform(560, 640), 2)
        gap_pct = self.rng.gauss(0, 0.45)                      # some days gap, some don't
        self.today_open = round(self.prev_close * (1 + gap_pct / 100), 2)
        self.pm_high = round(max(self.prev_close, self.today_open) * (1 + abs(self.rng.gauss(0, 0.08)) / 100), 2)
        self.pm_low = round(min(self.prev_close, self.today_open) * (1 - abs(self.rng.gauss(0, 0.08)) / 100), 2)
        self.avg_daily_volume = 72_000_000
        self._bars: list[Bar] = []
        self._last_price = self.today_open
        self._regime = 0          # -1 down burst, 0 chop, +1 up burst
        self._regime_left = 0

    # -- internal path generation -------------------------------------------
    def _session_times(self) -> list[datetime]:
        """Minute timestamps from 09:30 ET up to now (or full session if closed)."""
        now = datetime.now(ET)
        d = now.date()
        # weekend / after hours -> replay the most recent full weekday session
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        start = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
        end = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET)
        if d == now.date() and start <= now < end:
            end = now
        elif d == now.date() and now < start:
            d -= timedelta(days=1)
            while d.weekday() >= 5:
                d -= timedelta(days=1)
            start = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
            end = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET)
        times = []
        t = start
        while t < end:
            times.append(t)
            t += timedelta(minutes=1)
        return times or [start]

    def _vol_curve(self, minute_of_day: int) -> float:
        """U-shaped volume multiplier: heavy at open/close, light at lunch."""
        x = minute_of_day / 390.0
        return 0.55 + 1.8 * ((x - 0.5) ** 2) * 4 * 0.55 + (0.9 if x < 0.08 or x > 0.92 else 0)

    def _extend_bars(self):
        times = self._session_times()
        while len(self._bars) < len(times):
            i = len(self._bars)
            t = times[i]
            # momentum regime machine: mostly chop, occasional directional bursts
            if self._regime_left <= 0:
                r = self.rng.random()
                if r < 0.06:
                    self._regime, self._regime_left = (1 if self.rng.random() < 0.5 else -1), self.rng.randint(4, 9)
                else:
                    self._regime, self._regime_left = 0, self.rng.randint(3, 8)
            self._regime_left -= 1
            drift = self._regime * self._last_price * 0.0007
            noise = self.rng.gauss(0, self._last_price * 0.00035)
            o = self._last_price
            c = round(o + drift + noise, 2)
            hi = round(max(o, c) + abs(self.rng.gauss(0, 0.05)), 2)
            lo = round(min(o, c) - abs(self.rng.gauss(0, 0.05)), 2)
            base_vol = self.avg_daily_volume / 390
            mult = self._vol_curve(i) * (2.2 if self._regime != 0 else 1.0)
            vol = int(base_vol * mult * self.rng.uniform(0.7, 1.3))
            self._bars.append(Bar(ts=t.isoformat(), open=o, high=hi, low=lo, close=c, volume=vol))
            self._last_price = c

    # -- interface -------------------------------------------------------------
    def get_quote(self) -> Quote:
        self._extend_bars()
        p = self._last_price
        return Quote(
            price=round(p, 2),
            prev_close=self.prev_close,
            day_volume=sum(b.volume for b in self._bars),
            avg_daily_volume=self.avg_daily_volume,
            bid=round(p - 0.01, 2),
            ask=round(p + 0.01, 2),
            ts=datetime.now(ET).isoformat(),
        )

    def get_intraday_bars(self) -> list[Bar]:
        self._extend_bars()
        return list(self._bars)

    def get_premarket_levels(self) -> PremarketLevels:
        return PremarketLevels(
            premarket_high=self.pm_high,
            premarket_low=self.pm_low,
            prev_close=self.prev_close,
            today_open=self.today_open,
        )

    def get_options_chain(
        self,
        dte_range: tuple[int, int],
        strike_pct_from_spot: float = 0.03,
    ) -> list[OptionContract]:
        self._extend_bars()
        spot = self._last_price
        today = datetime.now(ET).date()
        band = float(strike_pct_from_spot)
        contracts: list[OptionContract] = []
        for dte in range(dte_range[0], dte_range[1] + 1):
            expiry = (today + timedelta(days=dte)).isoformat()
            for k in range(-8, 9):
                strike = round(spot) + k
                if abs(strike - spot) > spot * band:
                    continue
                for otype in ("call", "put"):
                    iv = 0.13 + 0.015 * abs(k) / 4 + self.rng.uniform(0, 0.02) + (0.04 if dte == 0 else 0)
                    delta = bs_delta(spot, strike, dte, iv, otype == "call")
                    # near-the-money SPY weeklies are famously tight
                    mid = max(0.05, abs(spot - strike) * 0.4 + spot * iv * math.sqrt(max(dte, 0.3) / 365) * 0.55)
                    spread = 0.01 + (0.01 if abs(k) < 4 else 0.04) + self.rng.uniform(0, 0.03)
                    bid = round(mid - spread / 2, 2)
                    ask = round(mid + spread / 2, 2)
                    contracts.append(OptionContract(
                        symbol=f"SPY{expiry.replace('-', '')[2:]}{'C' if otype == 'call' else 'P'}{int(strike * 1000):08d}",
                        type=otype,
                        strike=float(strike),
                        expiry=expiry,
                        dte=dte,
                        bid=max(bid, 0.01),
                        ask=ask,
                        mid=round((bid + ask) / 2, 3),
                        spread=round(ask - bid, 3),
                        iv=round(iv, 4),
                        open_interest=int(self.rng.uniform(500, 40000) / (1 + abs(k))),
                        delta=round(delta, 3),
                    ))
        return contracts
