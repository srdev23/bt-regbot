# Bittensor Subnet Registration Bot

Telegram bot that monitors Bittensor subnet registration (burn) prices and automatically registers when your target price is reached.

## Features

- Guided `/addorder` flow with step-by-step UI
- Reads wallets from `~/.bittensor/wallets`
- Persists orders as JSON (survives restarts)
- Monitors registration price every 3/5/10 seconds
- Auto-registers when price ≤ target with **0.5% slippage** protection
- Ephemeral price updates (auto-deleted after a few seconds)
- Smart order history with limits and cleanup

## Quick start (local)

```bash
cd workspace/bt-reg-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Set TELEGRAM_BOT_TOKEN in .env
python main.py
```

## Docker

```bash
cp .env.example .env
# Set TELEGRAM_BOT_TOKEN

# Optional: set wallet host path in .env
# BITTENSOR_WALLET_HOST_PATH=/home/you/.bittensor/wallets

docker compose up -d --build
docker compose logs -f
```

The container mounts:
- `~/.bittensor/wallets` → `/wallets` (your coldkeys/hotkeys)
- `./data` → order persistence

> **Note:** Wallets are mounted read-only in docker-compose. If registration fails due to wallet access, change `:ro` to `:rw` in `docker-compose.yml`.

## Configuration

`.env` requires only:

```
TELEGRAM_BOT_TOKEN=your_bot_token
```

| Variable | Default | Description |
|----------|---------|-------------|
| `BITTENSOR_NETWORK` | `finney` | Bittensor network |
| `BITTENSOR_WALLET_PATH` | `~/.bittensor/wallets` | Wallet directory |
| `ORDERS_DIR` | `./data/orders` | Order JSON storage |
| `SLIPPAGE_TOLERANCE` | `0.005` | 0.5% slippage on registration |
| `PRICE_NOTIFY_INTERVAL_SEC` | `60` | How often to rotate the price message (send new, delete old) |
| `TELEGRAM_CHAT_MIN_INTERVAL_SEC` | `1.2` | Min seconds between Telegram API calls per chat (avoids 429) |
| `ORDER_LIST_LIMIT` | `10` | Max active orders shown in `/orders` |
| `ORDER_HISTORY_LIMIT` | `15` | Max finished orders kept on disk and shown in history |
| `ORDER_HISTORY_DAYS` | `30` | Purge finished orders older than N days |
| `ORDER_AUTO_PURGE` | `true` | Auto-purge stale orders on startup |

## Commands

| Command | Description |
|---------|-------------|
| `/addorder` | Create a new registration order |
| `/orders` | Active orders (default) |
| `/orders history` | Recent finished orders |
| `/orders all` | Active + recent history |
| `/order <id>` | Order details |
| `/status` | Monitoring summary |
| `/cancelorder <id>` | Cancel an active order |
| `/clearorders` | Remove finished orders beyond the history limit |
| `/clearorders all` | Remove all finished orders |
| `/price <netuid>` | Current registration price |
| `/wallets` | List local wallets and hotkeys |
| `/unlock <wallet>` | Unlock wallet password for the session |

## Wallet passwords

Registration requires an unlocked coldkey. During `/addorder`, the bot asks for your wallet password once. Passwords are kept **in memory only**.

After a bot restart, use `/unlock <wallet_name>` before pending orders can auto-register.

## Order fields

- **netuid** — subnet to register on
- **wallet / hotkey** — from local `.bittensor/wallets`
- **regAt** — max registration price in TAO
- **duration** — monitoring window (`6h`, `24h`, `7d`)
- **poll interval** — 3, 5, or 10 seconds

## Notes

- Each hotkey registers once per subnet. Use different hotkeys for multiple registrations.
- Price monitoring sends one message per order and rotates it every notify interval (new message, old deleted).
- Finished order JSON files are auto-purged on startup and after each order completes, keeping only active orders plus the latest history limit (default 15).
