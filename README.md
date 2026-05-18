# Zero2Hire Calling Scheduler Bot

A Discord bot for scheduling 1-hour calling slots (7pm–3am Bangladesh Time).

## Features

- 📅 Book calling slots 7 days in advance
- 🔔 Automatic DM reminders 30 minutes before
- 📊 Live dashboard showing current caller + queue
- ✅ Reaction-based call confirmation
- ⚠️ Auto-detect no-shows and reopen slots
- 📝 Google Sheets tracking (audit trail)

## Setup Instructions

### 1. Discord Bot Token
- Go to https://discord.com/developers/applications
- Create a new application
- Add a Bot
- Copy the Token and save it

### 2. Google Sheets Setup
- Create a Google Sheet named `Zero2Hire_Schedule`
- Create 2 tabs: `Calling_Schedule` and `Summary`
- Create a Google Service Account (see Phase 2 in main spec)
- Download the JSON credentials file
- Share the sheet with the service account email

### 3. Environment Variables
Edit `.env` and fill in:
```
DISCORD_TOKEN=your_bot_token_here
GOOGLE_SHEETS_CREDENTIALS={"paste":"entire","json":"credentials","here"}
```

To convert JSON credentials to single line:
1. Open the JSON file
2. Go to https://jsoncrush.com/
3. Paste JSON → copy output → paste into .env

### 4. Deploy to Railway

1. Go to https://railway.app
2. Connect GitHub
3. Create new project from your GitHub repo
4. Add environment variables (DISCORD_TOKEN and GOOGLE_SHEETS_CREDENTIALS)
5. Deploy

Bot will run 24/7.

## Commands

- `/book` - Book a calling slot
- `/cancel [day] [time]` - Cancel a booking
- `/myslot` - See your booked slots
- `/help` - Show help

## File Structure

```
zero2hire-bot/
├── bot.py              # Main bot code
├── requirements.txt    # Python dependencies
├── .env               # Environment variables (not uploaded to GitHub)
├── .gitignore         # Ignore sensitive files
└── README.md          # This file
```

## Running Locally (Optional)

```bash
pip install -r requirements.txt
python bot.py
```

## Support

Check #dialing-queue in Discord for all scheduling updates.
