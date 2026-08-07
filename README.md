# SPY Day-Trade Monitor

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

Intraday SPY options signal dashboard. Evaluates **five day-trading strategies** every refresh and shows CALL/PUT ideas with entry reason, exit plan, key risk, confidence, and a suggested near-dated option leg.

**Paper mode by default — nothing is executed. Not financial advice.**

## Features

- Live cascade: **Charles Schwab** ([schwabdev](https://github.com/tylerebowers/Schwabdev)) → Yahoo Finance → offline stub
- Strategies: Momentum, Scalping, Breakout, VWAP reclaim/rejection, Gap and Go
- All entry/exit filters in one `CONFIG` block (`main.py`) — DTE, |delta|, spreads, ATR exits, flatten time
- Flat-by-close rule (default no new entries after 15:45 ET)
- Optional shared credentials with a local EdgeOwl `options-scanner` install

## Quick start

```batch
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
start.bat
```

Open **http://127.0.0.1:8765** (or **https://127.0.0.1:8765** if `certs/cert.pem` + `certs/key.pem` exist).

### TLS for Schwab OAuth

Schwab callback URL must be exactly:

```text
https://127.0.0.1:8765
```

(no trailing slash — required by schwabdev)

```batch
openssl req -x509 -newkey rsa:2048 -keyout certs/key.pem -out certs/cert.pem -days 365 -nodes -subj /CN=127.0.0.1
```

Then: `GET /api/schwab/auth-url` → approve → tokens land in `data/schwabdev-tokens.db`.

## Layout

```text
spy-day-trade-monitor/
├── main.py              # FastAPI app + CONFIG
├── providers.py         # Schwab / Yahoo / stub
├── strategies.py        # 5 strategies + risk layer
├── static/index.html    # dashboard UI
├── requirements.txt
├── start.bat
├── .env.example
├── certs/               # local TLS (gitignored)
└── data/                # tokens / settings (gitignored)
```

## Configuration

Edit `CONFIG` at the top of `main.py` for every filter (delta, DTE, spreads, exits, session windows).  
`GET /api/config` and the sidebar **Entry / Exit Filters** panel mirror live values.

## API

| Endpoint | Description |
|----------|-------------|
| `GET /api/signals` | Header + five strategy cards + filters |
| `GET /api/config` | Full filter snapshot |
| `GET /api/health` | Provider status |
| `GET /api/schwab/auth-url` | OAuth authorize URL |

## EdgeOwl (optional)

If you also run [options-scanner](https://github.com/shravansangireddy/options-scanner) as a sibling folder, this app can reuse `server/scanner.db` Schwab keys/tokens. Override with:

```env
EDGEDOWL_DB=C:\path\to\options-scanner\server\scanner.db
```

## License

[MIT](LICENSE)
