# heyemily

`heyemily` is a Hyperliquid trading workspace with:

- a FastAPI control plane (`src/hft_platform`) for auth, sessions, encrypted custodial keys, worker bootstrap, and telemetry streaming
- a React dashboard (`platform/web`) for onboarding and bot operations
- a low-latency lite worker (`src/litebot`) for sub-second signal execution
- a legacy grid bot path (`src/run_bot.py`) plus learning scripts

> [!WARNING]
> This project is for research and education. Crypto trading is risky. Use testnet first and only trade with funds you can afford to lose.

## What You Get

- Wallet-signature login (browser wallet or WalletConnect)
- Session-based bot lifecycle: create, fund, start, stop, delete
- Encrypted custodial key management (server-side, Fernet)
- Live telemetry stream + event history
- Wallet management UX with always-visible balance summary
- Safety controls:
  - spot/perp collateral transfers
  - close all orders + flatten positions
  - worker stop controls

## Repository Layout

```text
.
├── src/
│   ├── hft_platform/      # FastAPI app (auth, bots, internal telemetry)
│   ├── litebot/           # Lite HFT engine, strategy, risk, telemetry
│   ├── run_lite_worker.py # Worker entrypoint (YAML or API bootstrap)
│   └── run_bot.py         # Legacy grid bot entrypoint
├── platform/web/          # React + Vite dashboard
├── bots/                  # YAML presets
└── learning_examples/     # API usage examples
```

## Prerequisites

- Python `>=3.13`
- [uv](https://github.com/astral-sh/uv)
- Node.js + npm
- Hyperliquid testnet account + test funds

## Quick Start (Dashboard + API + Worker)

### 1) Install dependencies

```bash
git clone <your-repo-url>
cd hyperliquid-trading-bot
uv sync
```

For the web app:

```bash
cd platform/web
npm install
cd ../..
```

### 2) Configure environment

Copy root env template:

```bash
cp .env.example .env
```

Recommended to set at least:

- `JWT_SECRET`
- `MASTER_ENCRYPTION_KEY`
- `CORS_ORIGINS` (for your dashboard URL)
- optional: `SPAWN_LOCAL_LITE_WORKER=true` for local auto-spawn on Start

Copy web env template:

```bash
cp platform/web/.env.example platform/web/.env.local
```

Set:

- `VITE_WALLETCONNECT_PROJECT_ID` (if using WalletConnect)
- optional: `VITE_HFT_API_BASE` if your API is not `http://127.0.0.1:8000`

### 3) Start API

```bash
uv run uvicorn hft_platform.main:app --reload --host 0.0.0.0 --port 8000
```

Health check:

```bash
curl http://127.0.0.1:8000/api/health
```

### 4) Start web dashboard

```bash
cd platform/web
npm run dev -- --host
```

Open the shown URL (typically `http://localhost:5173`).

### 5) Run a session

1. Sign in with wallet
2. Create session
3. Fund trading address
4. Start session (worker token issued; optional local auto-spawn)
5. Monitor telemetry and balances

## Running Worker Directly

### A) Platform bootstrap mode

After pressing Start in the UI, run:

```bash
HFT_API_BASE=http://127.0.0.1:8000 \
HFT_SESSION_ID=<session-uuid> \
HFT_WORKER_TOKEN=<worker-jwt> \
uv run python src/run_lite_worker.py
```

### B) Local YAML mode

```bash
LITEBOT_PRIVATE_KEY=0x... \
uv run python src/run_lite_worker.py --config bots/lite_hft_micro_arb.yaml
```

## Bot Presets

- `bots/lite_hft_micro_arb.yaml` - micro-arbitrage style lite strategy
- `bots/lite_1_buy_per_sec.yaml` - interval-style lite preset
- `bots/btc_conservative.yaml` - legacy conservative grid preset

## Stop and Flatten Safely

Use the dashboard in this order:

1. `Stop` (session control)
2. `Close all orders` (Wallet management)
3. `Refresh wallet balance`

Confirm:

- `Open positions = 0`
- `Notional positions = 0`

## Troubleshooting

### Repeated `close` with `success:false`

- Ensure latest worker code is running (restart API + worker)
- Use `Close all orders` in Wallet management
- Check telemetry for `error` events around close attempts

### `429 Too Many Requests` from Hyperliquid `/info`

- This indicates rate limiting on testnet/public endpoints
- Recent updates include internal caching/fallback to reduce request pressure
- If still frequent, stop/restart session and reduce aggressive polling patterns

### `Amount X exceeds perp withdrawable (Y)`

- The available withdrawable changed between reads and submit
- UI now refreshes and can auto-adjust to the latest limit when possible

## Legacy and Learning Paths

Legacy grid bot:

```bash
uv run src/run_bot.py --validate
uv run src/run_bot.py bots/btc_conservative.yaml
```

Learning scripts:

```bash
uv run learning_examples/03_account_info/get_open_orders.py
uv run learning_examples/04_trading/place_limit_order.py
```

