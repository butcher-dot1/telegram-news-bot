# 📰 Telegram Subscription Bot

A Telegram bot that lets an admin upload daily newspaper PDFs and automatically distributes them to paid subscribers on a daily schedule.

## Features

- **Admin Controls**: Upload PDFs, approve user subscriptions via Telegram
- **User Journey**: Browse plans, pay via UPI, check subscription status
- **Auto Delivery**: Daily 12:40 PM IST broadcast of papers to active subscribers
- **Auto Cleanup**: Daily 2:00 AM IST removal of expired subscriptions
- **Supabase Backend**: PostgreSQL database for users and papers

## Plans

| Plan | Newspaper | Price |
|------|-----------|-------|
| `hindu` | The Hindu | ₹69/month |
| `toi` | Times of India | ₹65/month |

## Setup

### 1. Prerequisites
- Python 3.10+
- Supabase project with `users` and `daily_papers` tables
- Telegram Bot Token from [@BotFather](https://t.me/BotFather)

### 2. Install Dependencies
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure Environment
Create a `.env` file:
```env
BOT_TOKEN=your_bot_token
ADMIN_ID=your_telegram_user_id
DATABASE_URL=postgresql://postgres.xxx:password@aws-0-ap-south-1.pooler.supabase.com:6543/postgres
```

### 4. Add Payment QR Code
Place your UPI QR code image at `assets/qr.png`.

### 5. Run
```bash
python main.py
```

## Docker Deployment

```bash
docker build -t telegram-sub-bot .
docker run --env-file .env telegram-sub-bot
```

## Bot Commands

| Command | Access | Description |
|---------|--------|-------------|
| `/start` | User | Welcome message with plans |
| `/buy_hindu` | User | Show Hindu plan + QR code |
| `/buy_toi` | User | Show TOI plan + QR code |
| `/paid <plan>` | User | Notify admin of payment |
| `/myplan` | User | Check subscription status |
| `/approve <id> <plan>` | Admin | Activate user subscription |
| Send PDF | Admin | Upload daily newspaper |

## Architecture

```
main.py       → Bot handlers, commands, scheduled jobs
db.py         → Async database module (asyncpg)
assets/       → QR code image
.env          → Environment variables (not committed)
```
