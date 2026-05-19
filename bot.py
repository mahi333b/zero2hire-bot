import discord
from discord.ext import commands, tasks
from notion_client import Client
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
import asyncio
import pytz
from typing import Optional, List, Dict
import logging

load_dotenv()

# ============================================================================
# LOGGING SETUP
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ============================================================================
# ENVIRONMENT VARIABLE VALIDATION
# ============================================================================

def validate_env_vars():
    """Validate all required environment variables exist before starting bot."""
    required_vars = {
        'DISCORD_TOKEN': 'Discord bot token',
        'NOTION_TOKEN': 'Notion API token',
        'BOOKINGS_DATABASE_ID': 'Notion database ID for bookings'
    }
    
    missing = []
    for var, description in required_vars.items():
        if not os.getenv(var):
            missing.append(f"{var} ({description})")
    
    if missing:
        error_msg = f"❌ Missing required environment variables:\n" + "\n".join(f"  • {v}" for v in missing)
        logger.error(error_msg)
        raise ValueError(error_msg)
    
    logger.info("✅ All environment variables validated")

validate_env_vars()

TOKEN = os.getenv('DISCORD_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
BOOKINGS_DATABASE_ID = os.getenv('BOOKINGS_DATABASE_ID')

BD_TZ = pytz.timezone('Asia/Dhaka')

# ============================================================================
# NOTION CLIENT INITIALIZATION
# ============================================================================

try:
    notion = Client(auth=NOTION_TOKEN)
    # Test connection
    notion.databases.retrieve(database_id=BOOKINGS_DATABASE_ID)
    logger.info("✅ Notion connected successfully")
except Exception as e:
    logger.error(f"❌ Failed to initialize Notion: {e}")
    raise RuntimeError(f"Cannot connect to Notion. Check your token and database ID: {e}")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix='/', intents=intents)

# ============================================================================
# GLOBAL STATE WITH LOCKS
# ============================================================================

DIALING_QUEUE_CHANNEL_ID = None
DASHBOARD_MESSAGE_ID = None
CURRENT_DASHBOARD_DAY = None
DASHBOARD_VIEW = None  # Initialized inside on_ready() after event loop starts

# Async locks for thread-safe state management
STATE_LOCK = asyncio.Lock()
CONFIRMATION_LOCK = asyncio.Lock()

TIME_SLOTS = ['7pm', '8pm', '9pm', '10pm', '11pm', '12am', '1am', '2am', '3am']
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']

PENDING_CONFIRMATIONS = {}  # {f"{day}_{slot}": (member_id, expiry_time)}

CACHE = {
    'bookings': None,
    'timestamp': None,
    'ttl': 10  # seconds
}
if __name__ == '__main__':
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"❌ Failed to start bot: {e}")
        raise
