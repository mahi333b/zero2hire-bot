import discord
from discord.ext import commands, tasks
from notion_client import Client
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
import asyncio
import pytz
from typing import Optional, List, Dict

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
BOOKINGS_DATABASE_ID = os.getenv('BOOKINGS_DATABASE_ID')

BD_TZ = pytz.timezone('Asia/Dhaka')

try:
    notion = Client(auth=NOTION_TOKEN)
except Exception as e:
    print(f"Error initializing Notion: {e}")
    notion = None

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix='/', intents=intents)

# ============================================================================
# GLOBAL STATE
# ============================================================================

DIALING_QUEUE_CHANNEL_ID = None
DASHBOARD_MESSAGE_ID = None
CURRENT_DASHBOARD_DAY = None

TIME_SLOTS = ['7pm', '8pm', '9pm', '10pm', '11pm', '12am', '1am', '2am', '3am']
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']

PENDING_CONFIRMATIONS = {}  # {f"{day}_{slot}": member_id}

CACHE = {
    'bookings': None,
    'timestamp': None,
    'ttl': 10  # seconds
}

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_bd_time():
    return datetime.now(BD_TZ)

def get_calendar_day():
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    return days[get_bd_time().weekday()]

def get_current_calling_day():
    """
    7pm-11:59pm -> same calendar day
    12am-3:59am -> previous calendar day (overnight shift)
    Outside window -> None
    """
    now = get_bd_time()
    hour = now.hour

    if hour < 4:
        calling_day = now - timedelta(days=1)
    elif hour >= 19:
        calling_day = now
    else:
        return None

    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    return days[calling_day.weekday()]

def get_current_time_slot():
    hour = get_bd_time().hour
    slot_map = {
        19: '7pm', 20: '8pm', 21: '9pm', 22: '10pm', 23: '11pm',
        0: '12am', 1: '1am', 2: '2am', 3: '3am'
    }
    return slot_map.get(hour)

def get_next_time_slot():
    hour = get_bd_time().hour
    next_hour = (hour + 1) % 24
    slot_map = {
        19: '7pm', 20: '8pm', 21: '9pm', 22: '10pm', 23: '11pm',
        0: '12am', 1: '1am', 2: '2am', 3: '3am'
    }
    return slot_map.get(next_hour)

def invalidate_cache():
    global CACHE
    CACHE['bookings'] = None
    CACHE['timestamp'] = None

def query_notion_database():
    if not notion:
        return []
    try:
        results = []
        has_more = True
        start_cursor = None

        while has_more:
            kwargs = {"page_size": 100}
            if start_cursor:
                kwargs["start_cursor"] = start_cursor

            response = notion.databases.query(
                database_id=BOOKINGS_DATABASE_ID,
                **kwargs
            )
            results.extend(response.get('results', []))
            has_more = response.get('has_more', False)
            start_cursor = response.get('next_cursor')

        return results
    except Exception as e:
        print(f"Error querying Notion database: {e}")
        return []

def get_all_bookings():
    global CACHE

    now = datetime.now()

    if CACHE['bookings'] is not None and CACHE['timestamp'] is not None:
        elapsed = (now - CACHE['timestamp']).total_seconds()
        if elapsed < CACHE['ttl']:
            return CACHE['bookings']

    results = query_notion_database()
    bookings = []

    for page in results:
        try:
            props = page['properties']
            booking = {
                'id': page['id'],
                'day': props['day']['select']['name'] if props['day']['select'] else None,
                'Time_Slot': props['Time_Slot']['select']['name'] if props['Time_Slot']['select'] else None,
                'Member_ID': props['Member_ID']['rich_text'][0]['text']['content'] if props['Member_ID']['rich_text'] else None,
                'Member_Name': props['Member_Name']['title'][0]['text']['content'] if props['Member_Name']['title'] else None,
                'Status': props['Status']['select']['name'] if props['Status']['select'] else None,
                'Booked_At': props['Booked_At']['date']['start'] if props['Booked_At']['date'] else None,
                'Called_At': props['Called_At']['date']['start'] if props['Called_At']['date'] else None,
                'Duration_Minutes': props['Duration_Minutes']['number'] if props['Duration_Minutes']['number'] else None,
                'Cancelled_At': props['Cancelled_At']['date']['start'] if props['Cancelled_At']['date'] else None,
            }
            bookings.append(booking)
        except Exception as e:
            print(f"Error parsing booking page {page.get('id', 'unknown')}: {e}")
            continue

    CACHE['bookings'] = bookings
    CACHE['timestamp'] = now

    return bookings

def get_slot_booking(day, time_slot):
    bookings = get_all_bookings()
    for booking in bookings:
        if (booking['day'] == day and
            booking['Time_Slot'] == time_slot and
            booking['Status'] in ['booked', 'called']):
            return booking
    return None

def member_has_slot_on_day(member_id, day):
    bookings = get_all_bookings()
    for booking in bookings:
        if (booking['day'] == day and
            booking['Member_ID'] == str(member_id) and
            booking['Status'] in ['booked', 'called']):
            return True
    return False

def create_booking(member_id, member_name, day, time_slot):
    if not notion:
        return False

    if get_slot_booking(day, time_slot):
        return False

    try:
        notion.pages.create(
            parent={"database_id": BOOKINGS_DATABASE_ID},
            properties={
                "day": {"select": {"name": day}},
                "Time_Slot": {"select": {"name": time_slot}},
                "Member_ID": {"rich_text": [{"text": {"content": str(member_id)}}]},
                "Member_Name": {"title": [{"text": {"content": str(member_name)}}]},
                "Status": {"select": {"name": "booked"}},
                "Booked_At": {"date": {"start": get_bd_time().isoformat()}},
            }
        )
        invalidate_cache()
        return True
    except Exception as e:
        print(f"Error creating booking for {member_name} ({member_id}): {e}")
        return False

def update_booking_status(page_id, new_status, field_to_update=None, value=None):
    if not notion:
        return False

    try:
        update_dict = {
            "Status": {"select": {"name": new_status}}
        }

        if field_to_update and value:
            if field_to_update == "Called_At":
                update_dict["Called_At"] = {"date": {"start": value}}
            elif field_to_update == "Cancelled_At":
                update_dict["Cancelled_At"] = {"date": {"start": value}}
            elif field_to_update == "Duration_Minutes":
                update_dict["Duration_Minutes"] = {"number": value}

        notion.pages.update(page_id=page_id, properties=update_dict)
        invalidate_cache()
        return True
    except Exception as e:
        print(f"Error updating booking {page_id} to {new_status}: {e}")
        return False

def mark_as_called(member_id, day, time_slot):
    bookings = get_all_bookings()
    for booking in bookings:
        if (booking['day'] == day and
            booking['Time_Slot'] == time_slot and
            booking['Member_ID'] == str(member_id)):
            return update_booking_status(
                booking['id'],
                'called',
                'Called_At',
                get_bd_time().isoformat()
            )
    return False

def mark_as_no_show(day, time_slot):
    bookings = get_all_bookings()
    for booking in bookings:
        if (booking['day'] == day and
            booking['Time_Slot'] == time_slot and
            booking['Status'] == 'booked'):
            return update_booking_status(booking['id'], 'no-show')
    return False

def mark_slot_complete(member_id, day, time_slot):
    bookings = get_all_bookings()
    for booking in bookings:
        if (booking['day'] == day and
            booking['Time_Slot'] == time_slot and
            booking['Member_ID'] == str(member_id) and
            booking['Status'] == 'called'):
            return update_booking_status(booking['id'], 'called', 'Duration_Minutes', 60)
    return False

def get_member_slots(member_id):
    """
    FIX: Removed broken datetime math that was silently dropping valid bookings.
    Status is the source of truth:
      - 'booked'  = confirmed, not yet started
      - 'called'  = currently in session
    Completed/no-show/cancelled slots won't appear because their status has moved on.
    """
    bookings = get_all_bookings()
    slots = []

    for booking in bookings:
        if booking['Member_ID'] != str(member_id):
            continue
        if booking['Status'] not in ['booked', 'called']:
            continue
        if not booking['day'] or not booking['Time_Slot']:
            continue
        slots.append(booking)

    return slots

def get_available_slots(day):
    available = []
    for slot in TIME_SLOTS:
        if not get_slot_booking(day, slot):
            available.append(slot)
    return available

def get_future_days():
    today = get_bd_time()
    result = []
    days_list = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

    for i in range(7):
        future_date = today + timedelta(days=i)
        day_name = days_list[future_date.weekday()]

        if day_name in DAYS:
            result.append({
                'name': day_name,
                'date': future_date.date(),
                'is_today': i == 0
            })

    return result

def build_dashboard_embed(day):
    bookings = get_all_bookings()

    embed = discord.Embed(
        title="🎙️ ZERO2HIRE CALLING SCHEDULER",
        color=discord.Color.gold()
    )

    # Live Now
    live_member = None
    current_slot = get_current_time_slot()
    calling_day = get_current_calling_day()

    if current_slot and calling_day:
        for booking in bookings:
            if (booking['day'] == calling_day and
                booking['Time_Slot'] == current_slot and
                booking['Status'] == 'called'):
                live_member = booking['Member_Name']
                break

    embed.add_field(
        name="🔴 LIVE NOW",
        value=f"**{live_member}** is calling ({current_slot})" if live_member else "(No one calling right now)",
        inline=False
    )

    # Next Up
    next_slot = get_next_time_slot()
    next_member = None

    if next_slot and calling_day:
        for booking in bookings:
            if (booking['day'] == calling_day and
                booking['Time_Slot'] == next_slot and
                booking['Status'] in ['booked', 'called']):
                next_member = booking['Member_Name']
                break

    embed.add_field(
        name="⏭️ NEXT UP",
        value=f"**{next_member}** ({next_slot})" if next_member else "(Check back soon)",
        inline=False
    )

    # Queue for selected day
    queue_text = ""
    for slot in TIME_SLOTS:
        booked_member = None
        for booking in bookings:
            if (booking['day'] == day and
                booking['Time_Slot'] == slot and
                booking['Status'] in ['booked', 'called']):
                booked_member = booking['Member_Name']
                break

        if booked_member:
            queue_text += f"✅ {slot} -> **{booked_member}**\n"
        else:
            queue_text += f"⏳ {slot} -> (Open)\n"

    embed.add_field(
        name=f"👥 {day.upper()}'S QUEUE",
        value=queue_text or "All slots open",
        inline=False
    )

    embed.set_footer(text="📋 /book to book a slot  •  Updated: " + get_bd_time().strftime("%I:%M %p BDT"))

    return embed

# ============================================================================
# PERSISTENT DASHBOARD VIEW
# ============================================================================

class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        for day_obj in get_future_days():
            day_name = day_obj['name']
            label = day_name + (" (Today)" if day_obj['is_today'] else "")
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                custom_id=f"dash_day_{day_name}"
            )
            btn.callback = self._make_day_callback(day_name)
            self.add_item(btn)

    def _make_day_callback(self, day_name: str):
        async def callback(interaction: discord.Interaction):
            global CURRENT_DASHBOARD_DAY
            CURRENT_DASHBOARD_DAY = day_name

            # FIX: Defer immediately to beat Discord's 3-second interaction timeout.
            # Then build the embed (slow Notion call) and edit the message after.
            await interaction.response.defer()
            embed = build_dashboard_embed(day_name)
            await interaction.message.edit(embed=embed, view=self)

        return callback

    def refresh_buttons(self):
        """Call daily to keep the Mon-Fri rolling window current."""
        self._build_buttons()


# Single persistent instance — registered with bot on startup
DASHBOARD_VIEW = DashboardView()

# ============================================================================
# DISCORD BOT EVENTS
# ============================================================================

@bot.event
async def on_ready():
    global DIALING_QUEUE_CHANNEL_ID, CURRENT_DASHBOARD_DAY

    print(f'{bot.user} has connected to Discord!')

    # Reconnects persistent view buttons after restarts
    bot.add_view(DASHBOARD_VIEW)

    for guild in bot.guilds:
        for channel in guild.channels:
            if channel.name == 'dialing-queue':
                DIALING_QUEUE_CHANNEL_ID = channel.id
                break

    print(f"✅ Dialing queue channel ID: {DIALING_QUEUE_CHANNEL_ID}")

    CURRENT_DASHBOARD_DAY = get_calendar_day()

    try:
        synced = await bot.tree.sync()
        print(f'✅ Synced {len(synced)} command(s)')
    except Exception as e:
        print(f"❌ Error syncing commands: {e}")

    if not update_dashboard.is_running():
        update_dashboard.start()
        print("✅ Dashboard task started")

    if not send_reminders.is_running():
        send_reminders.start()
        print("✅ Reminder task started")

    if not check_confirmations.is_running():
        check_confirmations.start()
        print("✅ Confirmation checker started")

    if not auto_complete_slots.is_running():
        auto_complete_slots.start()
        print("✅ Auto-complete task started")

    print("🚀 Bot ready!")


@bot.event
async def on_reaction_add(reaction, user):
    if user == bot.user:
        return

    if reaction.emoji != '👍':
        return

    if not isinstance(reaction.message.channel, discord.DMChannel):
        return

    for key, member_id in list(PENDING_CONFIRMATIONS.items()):
        if member_id == user.id:
            day, time_slot = key.split('_', 1)
            if mark_as_called(user.id, day, time_slot):
                await user.send(f"✅ Confirmed! You're now live for **{day} {time_slot}**. You have 60 minutes — go!")
                del PENDING_CONFIRMATIONS[key]
            else:
                await user.send("⚠️ Could not confirm your slot. Please message an admin.")
            return

# ============================================================================
# SLASH COMMANDS
# ============================================================================

@bot.tree.command(name="book", description="Book a calling slot")
async def book_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    member = interaction.user
    member_id = member.id
    future_days = get_future_days()

    class DayView(discord.ui.View):
        pass

    day_view = DayView()

    for day_obj in future_days:
        day_name = day_obj['name']
        label = day_name + (" (Today)" if day_obj['is_today'] else "")

        async def day_callback(day_interaction: discord.Interaction, d=day_name):
            await day_interaction.response.defer()

            if member_has_slot_on_day(member_id, d):
                await day_interaction.followup.send(
                    f"❌ You already have a slot on **{d}**. One slot per day only.",
                    ephemeral=True
                )
                return

            available = get_available_slots(d)

            if not available:
                await day_interaction.followup.send(
                    f"❌ All slots are full for **{d}**. Try another day.",
                    ephemeral=True
                )
                return

            class TimeView(discord.ui.View):
                pass

            time_view = TimeView()

            for time_slot in available:
                async def time_callback(time_interaction: discord.Interaction, ts=time_slot, dn=d):
                    if create_booking(member_id, str(member.display_name), dn, ts):
                        embed = discord.Embed(
                            title="✅ SLOT BOOKED!",
                            description=f"You're confirmed for **{dn} {ts}**",
                            color=discord.Color.green()
                        )
                        embed.add_field(name="⏰ Time", value=f"{ts} Bangladesh Time", inline=False)
                        embed.add_field(
                            name="📍 What happens next",
                            value="• **30 min before:** Reminder DM\n• **At slot time:** DM with 👍 to confirm you're calling\n• **60 minutes:** Session auto-completes",
                            inline=False
                        )
                        await time_interaction.response.send_message(embed=embed, ephemeral=True)
                    else:
                        await time_interaction.response.send_message(
                            "❌ That slot was just taken! Pick another.",
                            ephemeral=True
                        )

                button = discord.ui.Button(label=time_slot, style=discord.ButtonStyle.success)
                button.callback = time_callback
                time_view.add_item(button)

            embed = discord.Embed(
                title=f"📅 Select a time — {d}",
                description="Pick your 1-hour calling slot:",
                color=discord.Color.blue()
            )
            await day_interaction.followup.send(embed=embed, view=time_view, ephemeral=True)

        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
        button.callback = day_callback
        day_view.add_item(button)

    embed = discord.Embed(
        title="📅 Book a Calling Slot",
        description="Select a day (Mon–Fri):",
        color=discord.Color.blue()
    )
    await interaction.followup.send(embed=embed, view=day_view, ephemeral=True)


@bot.tree.command(name="myslot", description="See your upcoming booked slots")
async def myslot_command(interaction: discord.Interaction):
    member = interaction.user
    slots = get_member_slots(member.id)

    if not slots:
        embed = discord.Embed(
            title="📅 Your Slots",
            description="You have no active bookings.\nUse **/book** to grab a slot.",
            color=discord.Color.greyple()
        )
    else:
        embed = discord.Embed(
            title="📅 Your Active Slots",
            color=discord.Color.blue()
        )
        for slot in slots:
            emoji = "🔴" if slot['Status'] == 'called' else "✅"
            embed.add_field(
                name=f"{emoji} {slot['day']} — {slot['Time_Slot']}",
                value=f"Status: **{slot['Status'].capitalize()}**",
                inline=False
            )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="help", description="How to use the calling scheduler")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📚 Zero2Hire Calling Scheduler — Help",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="/book",
        value="Book a 1-hour calling slot (Mon–Fri, 7pm–3am BDT)\nOne slot per day per person.",
        inline=False
    )
    embed.add_field(
        name="/myslot",
        value="View your active booked slots.",
        inline=False
    )
    embed.add_field(
        name="📍 How it works",
        value=(
            "1. Book via **/book**\n"
            "2. **30 min before:** Get a reminder DM\n"
            "3. **At your time:** DM arrives — react 👍 to confirm you're calling\n"
            "4. **No reaction in 10 min:** Marked as no-show\n"
            "5. **After 60 min:** Session auto-completes and is logged"
        ),
        inline=False
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================================
# BACKGROUND TASKS
# ============================================================================

@tasks.loop(seconds=30)
async def update_dashboard():
    global DASHBOARD_MESSAGE_ID, CURRENT_DASHBOARD_DAY

    if not DIALING_QUEUE_CHANNEL_ID:
        return

    channel = bot.get_channel(DIALING_QUEUE_CHANNEL_ID)
    if not channel:
        return

    try:
        if not CURRENT_DASHBOARD_DAY:
            CURRENT_DASHBOARD_DAY = get_calendar_day()

        embed = build_dashboard_embed(CURRENT_DASHBOARD_DAY)

        if DASHBOARD_MESSAGE_ID:
            try:
                msg = await channel.fetch_message(DASHBOARD_MESSAGE_ID)
                await msg.edit(embed=embed, view=DASHBOARD_VIEW)
                return
            except discord.NotFound:
                print("Dashboard message not found — will recreate.")
                DASHBOARD_MESSAGE_ID = None
            except Exception as e:
                print(f"Error editing dashboard message: {e}")
                DASHBOARD_MESSAGE_ID = None

        async for msg in channel.history(limit=20):
            if msg.author == bot.user and msg.embeds:
                if "ZERO2HIRE CALLING SCHEDULER" in msg.embeds[0].title:
                    DASHBOARD_MESSAGE_ID = msg.id
                    await msg.edit(embed=embed, view=DASHBOARD_VIEW)
                    return

        msg = await channel.send(embed=embed, view=DASHBOARD_VIEW)
        await msg.pin()
        DASHBOARD_MESSAGE_ID = msg.id
        print(f"✅ Dashboard created: {DASHBOARD_MESSAGE_ID}")

    except Exception as e:
        print(f"Dashboard update error: {e}")


@tasks.loop(minutes=1)
async def send_reminders():
    now = get_bd_time()
    hour = now.hour
    minute = now.minute

    slot_hours = {
        19: '7pm', 20: '8pm', 21: '9pm', 22: '10pm', 23: '11pm',
        0: '12am', 1: '1am', 2: '2am', 3: '3am'
    }

    # 30-minute warning
    if minute == 30:
        next_hour = (hour + 1) % 24
        next_slot = slot_hours.get(next_hour)

        if next_slot:
            calling_day = get_current_calling_day()
            if calling_day:
                booking = get_slot_booking(calling_day, next_slot)
                if booking:
                    try:
                        user = await bot.fetch_user(int(booking['Member_ID']))
                        embed = discord.Embed(
                            title=f"⏰ Heads Up — {next_slot} is in 30 minutes",
                            description=f"**{calling_day} {next_slot}**\n\nGet ready to start calling!",
                            color=discord.Color.blue()
                        )
                        await user.send(embed=embed)
                    except Exception as e:
                        print(f"Error sending 30-min reminder to {booking['Member_ID']}: {e}")

    # At slot start — send confirmation DM
    elif minute == 0:
        current_slot = slot_hours.get(hour)

        if current_slot:
            calling_day = get_current_calling_day()
            if calling_day:
                booking = get_slot_booking(calling_day, current_slot)
                if booking and booking['Status'] == 'booked':
                    try:
                        user = await bot.fetch_user(int(booking['Member_ID']))
                        embed = discord.Embed(
                            title="🎙️ IT'S YOUR TURN!",
                            description=(
                                f"**{calling_day} {current_slot}**\n\n"
                                "React 👍 below to confirm you're calling.\n\n"
                                "⚠️ **You have 10 minutes to confirm or you'll be marked no-show.**"
                            ),
                            color=discord.Color.green()
                        )
                        msg = await user.send(embed=embed)
                        await msg.add_reaction('👍')
                        PENDING_CONFIRMATIONS[f"{calling_day}_{current_slot}"] = int(booking['Member_ID'])
                    except Exception as e:
                        print(f"Error sending slot-start DM to {booking['Member_ID']}: {e}")


@tasks.loop(minutes=1)
async def check_confirmations():
    now = get_bd_time()
    hour = now.hour
    minute = now.minute

    if minute != 10:
        return

    slot_hours = {
        19: '7pm', 20: '8pm', 21: '9pm', 22: '10pm', 23: '11pm',
        0: '12am', 1: '1am', 2: '2am', 3: '3am'
    }
    current_slot = slot_hours.get(hour)

    if not current_slot:
        return

    calling_day = get_current_calling_day()
    if not calling_day:
        return

    key = f"{calling_day}_{current_slot}"
    if key not in PENDING_CONFIRMATIONS:
        return

    booking = get_slot_booking(calling_day, current_slot)
    if booking and booking['Status'] == 'booked':
        mark_as_no_show(calling_day, current_slot)

        try:
            user = await bot.fetch_user(int(booking['Member_ID']))
            embed = discord.Embed(
                title="⚠️ No-Show",
                description=(
                    f"You didn't confirm for **{calling_day} {current_slot}**.\n\n"
                    "Marked as no-show. Use **/book** to reschedule."
                ),
                color=discord.Color.orange()
            )
            await user.send(embed=embed)
        except Exception as e:
            print(f"Error sending no-show DM to {booking['Member_ID']}: {e}")

    del PENDING_CONFIRMATIONS[key]


@tasks.loop(minutes=1)
async def auto_complete_slots():
    now = get_bd_time()
    hour = now.hour
    minute = now.minute

    if minute != 0:
        return

    prev_hour = (hour - 1) % 24

    slot_hours = {
        19: '7pm', 20: '8pm', 21: '9pm', 22: '10pm', 23: '11pm',
        0: '12am', 1: '1am', 2: '2am', 3: '3am'
    }
    completed_slot = slot_hours.get(prev_hour)

    if not completed_slot:
        return

    calling_day = get_current_calling_day()
    if not calling_day:
        calling_day = get_calendar_day()

    booking = get_slot_booking(calling_day, completed_slot)
    if booking and booking['Status'] == 'called':
        mark_slot_complete(booking['Member_ID'], calling_day, completed_slot)

        try:
            user = await bot.fetch_user(int(booking['Member_ID']))
            embed = discord.Embed(
                title="✅ Session Complete!",
                description=f"Great work on **{calling_day} {completed_slot}**!\n\n60 minutes logged. Keep the momentum going.",
                color=discord.Color.green()
            )
            await user.send(embed=embed)
        except Exception as e:
            print(f"Error sending completion DM to {booking['Member_ID']}: {e}")

# ============================================================================
# RUN BOT
# ============================================================================

if __name__ == '__main__':
    bot.run(TOKEN)
