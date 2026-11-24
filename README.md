# SN Registration Bot

Multi-user Telegram bot for Bittensor subnet registration. Each Telegram user has fully isolated wallets, orders, and in-memory unlock sessions.

## Features

- Per-user wallet isolation under `data/users/<telegram_user_id>/wallets/`
- Create/import/remove coldkeys, create hotkeys, unlock for signing
- Subnet registration orders with price monitoring and automatic `burned_register`
- Periodic price alerts while watching (rotating Telegram message, no spam)
- Centralized monitor loop — one RPC fetch per subnet per tick (scalable for ~50 users)
- SQLite persistence — orders resume after restart
- Docker support

## Quick start

### Local

```bash
cd ~/workspace/sn-reg-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# Edit .env and set TELEGRAM_BOT_TOKEN
python main.py
```

### Docker

```bash
cp .env.example .env
# Edit .env and set TELEGRAM_BOT_TOKEN
docker compose up -d --build
docker compose logs -f
```

## Commands

| Command | Description |
|---------|-------------|
| `/wallets` | List wallets and TAO balances |
| `/wallet <name>` | Hotkeys and registered subnets |
| `/createwallet` | Create new coldkey |
| `/importwallet` | Import from mnemonic or ZIP |
| `/removewallet` | Remove a wallet (requires confirmation) |
| `/createhotkey` | Add hotkey to coldkey |
| `/importhotkey` | Import hotkey (mnemonic or ZIP) |
| `/unlock <wallet>` | Unlock wallet for signing |
| `/addorder` | Create registration order |
| `/orders` | List orders |
| `/order <id>` | Order details |
| `/cancelorder <id>` | Cancel active order |
| `/price <netuid>` | Current registration price |
| `/status` | Bot status |

## Rate limiting & scalability

The bot uses a **centralized monitor** instead of per-order polling loops:

| Setting | Default | Purpose |
|---------|---------|---------|
| `MONITOR_TICK_SEC` | 5 | How often all orders are checked |
| `PRICE_CACHE_TTL_SEC` | 5 | One Bittensor RPC call per subnet per tick |
| `PRICE_NOTIFY_INTERVAL_SEC` | 60 | Price alert frequency per order |
| `TELEGRAM_CHAT_MIN_INTERVAL_SEC` | 1.2 | Min gap between messages to same chat |

With 50 users all watching SN15, Bittensor RPC usage is ~1 call/5s for that subnet (not 50×). Price alerts use rotating messages (send new → delete old) to avoid Telegram chat spam.

## Security notes

- Wallet passwords are kept in memory only for the current process lifetime
- Mnemonic/password messages are deleted from Telegram when possible
- `/removewallet` is blocked while active orders reference that coldkey
- Never share your bot host with untrusted parties — filesystem access to `data/users/` exposes encrypted wallet files

## Tests

```bash
pytest tests/ -v
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md).
