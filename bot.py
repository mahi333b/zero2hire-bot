import discord
from discord.ext import commands, tasks
import gspread
from google.oauth2.service_account import Credentials
import json
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import asyncio
import pytz

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
CREDENTIALS_JSON = os.getenv('GOOGLE_SHEETS_CREDENTIALS')

# Bangladesh timezone
BD_TZ = pytz.timezone('Asia/Dhaka')

# Parse credentials
try:
    creds_dict = json.loads(CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    )
    gc = gspread.authorize(creds)
except Exception as e:
    print(f"Error loading credentials: {e}")
    gc = None

# Discord bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix='/', intents=intents)

# Global variables
SHEET_NAME = 'Zero2Hire_Schedule'
CALLING_SHEET_TAB = 'Calling_Schedule'
SUMMARY_SHEET_TAB = 'Summary'
DIALING_QUEUE_CHANNEL_ID = None
DASHBOARD_MESSAGE_ID = None
CURRENT_DASHBOARD_DAY = None  # Track which day is currently displayed

TIME_SLOTS = ['7pm', '8pm', '9pm', '10pm', '11pm', '12am', '1am', '2am', '3am']
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']

# Track turn announcements and reactions
TURN_ANNOUNCEMENTS = {}  # {day_time: message_id}
MEMBERS_REACTED = {}  # {day_time: member_id}
NO_SHOWS_ANNOUNCED = {}  # {day_time: True}

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_sheet():
    """Get the Google Sheet"""
    try:
        sh = gc.open(SHEET_NAME)
        return sh
    except Exception as e:
        print(f"Error accessing sheet: {e}")
        return None

def get_calling_schedule_data():
    """Fetch all data from Calling_Schedule tab"""
    sheet = get_sheet()
    if not sheet:
        return []
    try:
        worksheet = sheet.worksheet(CALLING_SHEET_TAB)
        return worksheet.get_all_records()
    except Exception as e:
        print(f"Error fetching schedule: {e}")
        return []

def get_bd_time():
    """Get current Bangladesh time"""
    return datetime.now(BD_TZ)

def get_current_calling_day():
    """
    Get which day's calling window is currently active.
    If hour is 0-3 (12am-3:59am), we're in yesterday's calling window.
    If hour is 19-23 (7pm-11:59pm), we're in today's calling window.
    Otherwise, no calling window.
    """
    now = get_bd_time()
    hour = now.hour
    
    if hour < 4:  # 12am-3:59am = previous day's calling window
        calling_day = now - timedelta(days=1)
    elif hour >= 19:  # 7pm-11:59pm = current day's calling window
        calling_day = now
    else:
        return None
    
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    return days[calling_day.weekday()]

def get_calendar_day():
    """Get current calendar day (Monday, Tuesday, etc.)"""
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    return days[get_bd_time().weekday()]

def get_current_time_slot():
    """Get current time slot (7pm, 8pm, etc.)"""
    hour = get_bd_time().hour
    
    slot_map = {
        19: '7pm', 20: '8pm', 21: '9pm', 22: '10pm', 23: '11pm',
        0: '12am', 1: '1am', 2: '2am', 3: '3am'
    }
    return slot_map.get(hour)

def get_next_time_slot():
    """Get next time slot"""
    current = get_bd_time()
    hour = current.hour
    
    slot_map = {
        19: '7pm', 20: '8pm', 21: '9pm', 22: '10pm', 23: '11pm',
        0: '12am', 1: '1am', 2: '2am', 3: '3am'
    }
    
    next_hour = (hour + 1) % 24
    return slot_map.get(next_hour)

def is_slot_booked(day, time_slot):
    """Check if a slot is already booked"""
    data = get_calling_schedule_data()
    for row in data:
        if row.get('Date') == day and row.get('Time_Slot') == time_slot:
            status = row.get('Status', '')
            if status in ['called', 'booked']:
                return True
    return False

def member_has_slot_on_day(member_id, day):
    """Check if member already has a slot booked on a specific day"""
    data = get_calling_schedule_data()
    for row in data:
        if row.get('Date') == day and row.get('Member_ID') == str(member_id):
            status = row.get('Status', '')
            if status in ['booked', 'called']:
                return True
    return False

def book_slot(member_id, member_name, day, time_slot):
    """Add a booking to Google Sheet"""
    sheet = get_sheet()
    if not sheet:
        return False
    try:
        worksheet = sheet.worksheet(CALLING_SHEET_TAB)
        now = get_bd_time().strftime('%a %I:%M%p')
        worksheet.append_row([
            day,
            time_slot,
            str(member_id),
            member_name,
            'booked',
            now,
            '',
            '',
            '',
            ''
        ])
        return True
    except Exception as e:
        print(f"Error booking slot: {e}")
        return False

def cancel_slot(member_id, day, time_slot):
    """Cancel a booking"""
    sheet = get_sheet()
    if not sheet:
        return False
    try:
        worksheet = sheet.worksheet(CALLING_SHEET_TAB)
        data = worksheet.get_all_records()
        for i, row in enumerate(data):
            if (row.get('Date') == day and 
                row.get('Time_Slot') == time_slot and 
                row.get('Member_ID') == str(member_id)):
                worksheet.update_cell(i + 2, 5, 'cancelled')  # Status column
                worksheet.update_cell(i + 2, 9, get_bd_time().strftime('%a %I:%M%p'))  # Cancelled_At
                return True
        return False
    except Exception as e:
        print(f"Error cancelling slot: {e}")
        return False

def mark_as_called(member_id, day, time_slot):
    """Mark a slot as 'called' when member reacts"""
    sheet = get_sheet()
    if not sheet:
        return False
    try:
        worksheet = sheet.worksheet(CALLING_SHEET_TAB)
        data = worksheet.get_all_records()
        for i, row in enumerate(data):
            if (row.get('Date') == day and 
                row.get('Time_Slot') == time_slot and 
                row.get('Member_ID') == str(member_id)):
                worksheet.update_cell(i + 2, 5, 'called')  # Status
                worksheet.update_cell(i + 2, 7, get_bd_time().strftime('%a %I:%M%p'))  # Called_At
                return True
        return False
    except Exception as e:
        print(f"Error marking as called: {e}")
        return False

def mark_as_no_show(day, time_slot):
    """Mark a slot as 'no-show'"""
    sheet = get_sheet()
    if not sheet:
        return
    try:
        worksheet = sheet.worksheet(CALLING_SHEET_TAB)
        data = worksheet.get_all_records()
        for i, row in enumerate(data):
            if (row.get('Date') == day and 
                row.get('Time_Slot') == time_slot and 
                row.get('Status') == 'booked'):
                worksheet.update_cell(i + 2, 5, 'no-show')  # Status
                return
    except Exception as e:
        print(f"Error marking no-show: {e}")

def mark_slot_complete(member_id, day, time_slot):
    """Mark slot as complete with duration"""
    sheet = get_sheet()
    if not sheet:
        return
    try:
        worksheet = sheet.worksheet(CALLING_SHEET_TAB)
        data = worksheet.get_all_records()
        for i, row in enumerate(data):
            if (row.get('Date') == day and 
                row.get('Time_Slot') == time_slot and 
                row.get('Member_ID') == str(member_id) and
                row.get('Status') == 'called'):
                worksheet.update_cell(i + 2, 8, 60)  # Duration in minutes
                return
    except Exception as e:
        print(f"Error marking complete: {e}")

def get_member_slots(member_id):
    """Get all slots for a member"""
    data = get_calling_schedule_data()
    slots = []
    for row in data:
        if row.get('Member_ID') == str(member_id):
            slots.append({
                'day': row.get('Date'),
                'time': row.get('Time_Slot'),
                'status': row.get('Status'),
                'name': row.get('Member_Name')
            })
    return slots

def can_cancel_slot(day, time_slot):
    """Check if cancellation is allowed (3+ hours before slot)"""
    now = get_bd_time()
    
    slot_hours = {
        '7pm': 19, '8pm': 20, '9pm': 21, '10pm': 22, '11pm': 23,
        '12am': 0, '1am': 1, '2am': 2, '3am': 3
    }
    
    slot_hour = slot_hours.get(time_slot)
    if slot_hour is None:
        return False
    
    day_map = {d: i for i, d in enumerate(DAYS)}
    slot_day_index = day_map.get(day)
    
    if slot_day_index is None:
        return False
    
    # Create datetime for the slot
    slot_datetime = now.replace(hour=slot_hour, minute=0, second=0, microsecond=0)
    
    # If slot is in the past today, it's next week
    if slot_datetime < now:
        slot_datetime += timedelta(days=7)
    
    # Check if 3+ hours away
    time_until_slot = slot_datetime - now
    return time_until_slot.total_seconds() >= 3 * 3600

def format_available_slots(day):
    """Get available slots for a day"""
    available = []
    for slot in TIME_SLOTS:
        if not is_slot_booked(day, slot):
            available.append(slot)
    return available

def get_future_days(days_ahead=7):
    """Get list of days from today to days_ahead (inclusive)"""
    today = get_bd_time()
    result = []
    for i in range(days_ahead):
        future_date = today + timedelta(days=i)
        days_list = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        day_name = days_list[future_date.weekday()]
        result.append({
            'name': day_name,
            'date': future_date.date(),
            'is_today': i == 0
        })
    return result

def build_dashboard_embed(day):
    """Build dashboard embed for a specific day"""
    data = get_calling_schedule_data()
    
    embed = discord.Embed(
        title="🎙️ ZERO2HIRE CALLING SCHEDULER",
        color=discord.Color.gold()
    )
    
    # Find who's live now
    live_member = None
    current_slot = get_current_time_slot()
    
    if current_slot:
        calling_day = get_current_calling_day()
        if calling_day:
            for row in data:
                if (row.get('Date') == calling_day and 
                    row.get('Time_Slot') == current_slot and 
                    row.get('Status') == 'called'):
                    live_member = row.get('Member_Name', '')
                    break
    
    if live_member:
        embed.add_field(
            name="🔴 LIVE NOW",
            value=f"**{live_member}** is calling ({current_slot})",
            inline=False
        )
    else:
        embed.add_field(
            name="🔴 LIVE NOW",
            value="(No one calling right now)",
            inline=False
        )
    
    # Find next caller
    next_slot = get_next_time_slot()
    next_member = None
    calling_day = get_current_calling_day()
    
    if next_slot and calling_day:
        for row in data:
            if (row.get('Date') == calling_day and 
                row.get('Time_Slot') == next_slot and 
                row.get('Status') in ['booked', 'called']):
                next_member = row.get('Member_Name', '')
                break
    
    if next_member:
        embed.add_field(
            name="⏭️ NEXT UP",
            value=f"**{next_member}** ({next_slot})",
            inline=False
        )
    else:
        embed.add_field(
            name="⏭️ NEXT UP",
            value="(Check back soon)",
            inline=False
        )
    
    # Show all slots for the selected day
    queue_text = ""
    for slot in TIME_SLOTS:
        booked_member = None
        for row in data:
            if (row.get('Date') == day and 
                row.get('Time_Slot') == slot):
                status = row.get('Status', '')
                if status in ['booked', 'called']:
                    booked_member = row.get('Member_Name', '')
                break
        
        if booked_member:
            queue_text += f"✅ {slot} → **{booked_member}**\n"
        else:
            queue_text += f"⏳ {slot} → (Open)\n"
    
    embed.add_field(
        name=f"👥 {day.upper()}'S QUEUE",
        value=queue_text or "All slots open",
        inline=False
    )
    
    embed.set_footer(text="📋 /book to book a slot | /help for more info")
    
    return embed

# ============================================================================
# DISCORD BOT EVENTS
# ============================================================================

@bot.event
async def on_ready():
    """Bot startup event"""
    global DIALING_QUEUE_CHANNEL_ID, CURRENT_DASHBOARD_DAY
    try:
        print(f'{bot.user} has connected to Discord!')
        
        # Find #dialing-queue channel
        for guild in bot.guilds:
            for channel in guild.channels:
                if channel.name == 'dialing-queue':
                    DIALING_QUEUE_CHANNEL_ID = channel.id
                    break
        
        print(f"Dialing queue channel ID: {DIALING_QUEUE_CHANNEL_ID}")
        
        CURRENT_DASHBOARD_DAY = get_calendar_day()
        
        try:
            synced = await bot.tree.sync()
            print(f'Synced {len(synced)} command(s)')
        except Exception as e:
            print(f"Error syncing commands: {e}")
        
        # Start background tasks with error handling
        try:
            if not update_dashboard.is_running():
                update_dashboard.start()
                print("✅ Dashboard task started")
        except Exception as e:
            print(f"❌ Error starting dashboard task: {e}")
        
        try:
            if not post_turn_announcements.is_running():
                post_turn_announcements.start()
                print("✅ Turn announcements task started")
        except Exception as e:
            print(f"❌ Error starting turn announcements task: {e}")
        
        try:
            if not check_no_shows.is_running():
                check_no_shows.start()
                print("✅ No-shows task started")
        except Exception as e:
            print(f"❌ Error starting no-shows task: {e}")
        
        try:
            if not check_slot_completion.is_running():
                check_slot_completion.start()
                print("✅ Slot completion task started")
        except Exception as e:
            print(f"❌ Error starting slot completion task: {e}")
        
        try:
            if not update_summary_tab.is_running():
                update_summary_tab.start()
                print("✅ Summary tab task started")
        except Exception as e:
            print(f"❌ Error starting summary tab task: {e}")
        
        print("🚀 Bot fully initialized and ready!")
    except Exception as e:
        print(f"❌ CRITICAL ERROR IN ON_READY: {e}")
        import traceback
        traceback.print_exc()

@bot.event
async def on_reaction_add(reaction, user):
    """Handle when a member reacts with ✅"""
    # Ignore bot's own reactions
    if user == bot.user:
        return
    
    # Only care about ✅ reactions
    if reaction.emoji != '✅':
        return
    
    # Only in #dialing-queue
    if reaction.message.channel.id != DIALING_QUEUE_CHANNEL_ID:
        return
    
    # Check if this is a turn announcement (has "YOUR TURN" or "SLOT OPEN" in embed)
    if not reaction.message.embeds:
        return
    
    embed = reaction.message.embeds[0]
    if "YOUR TURN" not in embed.title and "SLOT OPEN" not in embed.title:
        return
    
    # Extract day and time from embed
    description = embed.description or ""
    
    day = None
    time_slot = None
    
    for line in description.split('\n'):
        if 'Slot:' in line or 'slot' in line.lower():
            parts = line.split('Slot:')
            if len(parts) > 1:
                slot_info = parts[1].strip()
                # Parse "Monday 9pm" format
                for d in DAYS:
                    if d in slot_info:
                        day = d
                        for t in TIME_SLOTS:
                            if t in slot_info:
                                time_slot = t
                                break
                        break
    
    if not day or not time_slot:
        return
    
    member_id = user.id
    member_name = str(user)
    
    # Check if slot is still available
    if is_slot_booked(day, time_slot):
        await user.send(f"❌ The {day} {time_slot} slot was already claimed by someone else.")
        return
    
    # Book the slot for this member
    if book_slot(member_id, member_name, day, time_slot):
        # Mark as called immediately
        mark_as_called(member_id, day, time_slot)
        
        # Update in-memory tracker
        MEMBERS_REACTED[f"{day}_{time_slot}"] = member_id
        
        # Notify member
        embed_confirm = discord.Embed(
            title="✅ YOU'RE LIVE!",
            description=f"You're now calling the {day} {time_slot} slot!",
            color=discord.Color.green()
        )
        await user.send(embed=embed_confirm)
    else:
        await user.send("❌ Error claiming slot. Try again.")

# ============================================================================
# SLASH COMMANDS
# ============================================================================

@bot.tree.command(name="book", description="Book a calling slot")
async def book_command(interaction: discord.Interaction):
    """Book a slot - opens interactive menu"""
    await interaction.response.defer()
    
    member = interaction.user
    member_id = member.id
    
    # Get future days (next 7 days)
    future_days = get_future_days(7)
    
    # Filter only Mon-Fri
    valid_days = [d for d in future_days if d['name'] in DAYS]
    
    # Create day selector view
    class DayView(discord.ui.View):
        selected_day = None
        
        async def on_timeout(self):
            await interaction.followup.send("❌ Booking timed out.", ephemeral=True)
    
    # Add day buttons
    for day_obj in valid_days:
        day_name = day_obj['name']
        label = f"{day_name}" + (" (Today)" if day_obj['is_today'] else "")
        
        async def day_callback(interaction: discord.Interaction, d=day_name):
            await interaction.response.defer()
            await show_time_slots(interaction, d, member_id)
        
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
        button.callback = day_callback
        DayView.add_item(button)
    
    async def show_time_slots(interaction: discord.Interaction, day: str, mid: int):
        """Show available time slots for selected day"""
        
        # Check if member already has a slot on this day
        if member_has_slot_on_day(mid, day):
            await interaction.followup.send(
                f"❌ You already have a slot booked for {day}. You can only book ONE slot per day.",
                ephemeral=True
            )
            return
        
        available = format_available_slots(day)
        
        if not available:
            await interaction.followup.send(
                f"❌ All slots full for {day}. Try another day.",
                ephemeral=True
            )
            return
        
        class TimeView(discord.ui.View):
            pass
        
        for time_slot in available:
            async def time_btn_callback(interaction: discord.Interaction, ts=time_slot, d=day):
                if book_slot(mid, str(member), d, ts):
                    embed = discord.Embed(
                        title="✅ CONFIRMED!",
                        description=f"You're booked for **{d} {ts}**.",
                        color=discord.Color.green()
                    )
                    embed.add_field(name="📍 Calling window", value=f"{ts} Bangladesh Time", inline=False)
                    embed.add_field(name="📢 What to do", value="When it's your turn, a message will appear in #dialing-queue.\nReact ✅ to mark yourself as calling!", inline=False)
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await interaction.response.send_message(
                        f"❌ Error booking {d} {ts}. Someone else may have claimed it. Try another slot.",
                        ephemeral=True
                    )
            
            button = discord.ui.Button(label=time_slot, style=discord.ButtonStyle.success)
            button.callback = time_btn_callback
            TimeView.add_item(button)
        
        embed = discord.Embed(
            title=f"📅 Available slots for {day}",
            description="Select a time to book:",
            color=discord.Color.blue()
        )
        await interaction.followup.send(embed=embed, view=TimeView(), ephemeral=True)
    
    embed = discord.Embed(
        title="📅 Select a day",
        description="Pick a day from the next 7 days (Mon–Fri only)",
        color=discord.Color.blue()
    )
    await interaction.followup.send(embed=embed, view=DayView(), ephemeral=True)

@bot.tree.command(name="cancel", description="Cancel your booking")
async def cancel_command(interaction: discord.Interaction, day: str, time: str):
    """Cancel a booked slot"""
    member = interaction.user
    member_id = member.id
    
    # Validate inputs
    if day not in DAYS or time not in TIME_SLOTS:
        await interaction.response.send_message(
            "❌ Invalid day or time.",
            ephemeral=True
        )
        return
    
    # Check if member has this slot
    member_slots = get_member_slots(member_id)
    if not any(s['day'] == day and s['time'] == time for s in member_slots):
        await interaction.response.send_message(
            f"❌ You don't have {day} {time} booked.",
            ephemeral=True
        )
        return
    
    # Check if cancellation is allowed
    if not can_cancel_slot(day, time):
        await interaction.response.send_message(
            f"❌ Too late to cancel. Must cancel 3+ hours before slot start.",
            ephemeral=True
        )
        return
    
    # Confirm cancellation
    class ConfirmView(discord.ui.View):
        result = None
        
        @discord.ui.button(label="CONFIRM CANCEL", style=discord.ButtonStyle.danger)
        async def confirm_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
            self.result = True
            await interaction.response.defer()
        
        @discord.ui.button(label="KEEP MY SLOT", style=discord.ButtonStyle.primary)
        async def keep_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
            self.result = False
            await interaction.response.defer()
    
    view = ConfirmView()
    embed = discord.Embed(
        title="⚠️ Cancel Booking?",
        description=f"Are you sure you want to cancel **{day} {time}**?",
        color=discord.Color.orange()
    )
    
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    await asyncio.sleep(60)
    
    if view.result:
        if cancel_slot(member_id, day, time):
            # Announce in dialing-queue
            channel = bot.get_channel(DIALING_QUEUE_CHANNEL_ID)
            if channel:
                embed = discord.Embed(
                    title=f"⚠️ SLOT OPEN: {day} {time}",
                    description=f"{member.mention} just cancelled their {time} slot.",
                    color=discord.Color.orange()
                )
                embed.add_field(name="Slot Info", value=f"⏰ Slot: {day} {time}", inline=False)
                embed.add_field(name="React ✅", value="to claim this slot!", inline=False)
                await channel.send(embed=embed)
            
            await interaction.followup.send(
                f"✅ **Cancelled!**\nYour {day} {time} slot has been released.",
                ephemeral=True
            )
        else:
            await interaction.followup.send("❌ Error cancelling. Try again.", ephemeral=True)
    else:
        await interaction.followup.send("✅ Slot kept.", ephemeral=True)

@bot.tree.command(name="myslot", description="See your booked slots")
async def myslot_command(interaction: discord.Interaction):
    """Show member's booked slots"""
    member = interaction.user
    member_id = member.id
    slots = get_member_slots(member_id)
    
    if not slots:
        embed = discord.Embed(
            title="📅 Your Slots",
            description="You have no upcoming slots booked.",
            color=discord.Color.greyple()
        )
    else:
        embed = discord.Embed(
            title="📅 Your Booked Slots",
            description="Your upcoming calling slots:",
            color=discord.Color.blue()
        )
        for slot in slots:
            emoji = "✅" if slot['status'] == 'booked' else "🔴" if slot['status'] == 'called' else "❌"
            embed.add_field(
                name=f"{emoji} {slot['day']} {slot['time']}",
                value=f"Status: {slot['status']}",
                inline=False
            )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="help", description="Show help")
async def help_command(interaction: discord.Interaction):
    """Show help message"""
    embed = discord.Embed(
        title="📚 Zero2Hire Calling Scheduler - Help",
        description="How to book and manage your calling slots",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="/book",
        value="Book a 1-hour calling slot (7pm–3am, Mon–Fri)\nYou can only book ONE slot per day",
        inline=False
    )
    embed.add_field(
        name="/cancel [day] [time]",
        value="Cancel a booked slot (must be 3+ hours before)",
        inline=False
    )
    embed.add_field(
        name="/myslot",
        value="See all your booked slots",
        inline=False
    )
    embed.add_field(
        name="📍 How Calling Works",
        value="1. Book a slot\n2. 30 min before, an announcement appears in #dialing-queue\n3. When it's your turn, react ✅ to the message\n4. Dashboard shows you're live\n5. Call for 1 hour",
        inline=False
    )
    embed.add_field(
        name="⚠️ No-Shows",
        value="If you don't react ✅ within 10 min of your slot, it opens for others.",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================================
# BACKGROUND TASKS
# ============================================================================

@tasks.loop(minutes=1)
async def post_turn_announcements():
    """Post turn announcements 30 min before AND at slot start time"""
    if not DIALING_QUEUE_CHANNEL_ID:
        return
    
    now = get_bd_time()
    current_hour = now.hour
    current_minute = now.minute
    
    channel = bot.get_channel(DIALING_QUEUE_CHANNEL_ID)
    if not channel:
        return
    
    # Check at X:30 (30 minutes before the next hour)
    if current_minute == 30:
        next_hour = (current_hour + 1) % 24
        slot_map = {
            19: '7pm', 20: '8pm', 21: '9pm', 22: '10pm', 23: '11pm',
            0: '12am', 1: '1am', 2: '2am', 3: '3am'
        }
        next_slot = slot_map.get(next_hour)
        
        if next_slot:
            calling_day = get_current_calling_day()
            if calling_day:
                announcement_key = f"{calling_day}_{next_slot}_30min"
                
                if announcement_key not in TURN_ANNOUNCEMENTS:
                    data = get_calling_schedule_data()
                    for row in data:
                        if (row.get('Date') == calling_day and 
                            row.get('Time_Slot') == next_slot and 
                            row.get('Status') in ['booked', 'called']):
                            member_name = row.get('Member_Name', 'Unknown')
                            
                            try:
                                embed = discord.Embed(
                                    title=f"⏰ {next_slot.upper()} — YOUR TURN IN 30 MIN!",
                                    description=f"**{member_name}** — Get ready! Your slot starts in 30 minutes.",
                                    color=discord.Color.blue()
                                )
                                embed.add_field(
                                    name="📍 Slot Info",
                                    value=f"⏰ Slot: {calling_day} {next_slot}",
                                    inline=False
                                )
                                embed.add_field(
                                    name="📢 What to do",
                                    value="React ✅ below when you're ready at slot time.\nYou'll get another message when it's time!",
                                    inline=False
                                )
                                msg = await channel.send(embed=embed)
                                TURN_ANNOUNCEMENTS[announcement_key] = msg.id
                            except Exception as e:
                                print(f"Error posting 30-min announcement: {e}")
    
    # Check at X:00 (slot start time)
    elif current_minute == 0:
        slot_map = {
            19: '7pm', 20: '8pm', 21: '9pm', 22: '10pm', 23: '11pm',
            0: '12am', 1: '1am', 2: '2am', 3: '3am'
        }
        current_slot = slot_map.get(current_hour)
        
        if current_slot:
            calling_day = get_current_calling_day()
            if calling_day:
                announcement_key = f"{calling_day}_{current_slot}_now"
                
                if announcement_key not in TURN_ANNOUNCEMENTS:
                    data = get_calling_schedule_data()
                    for row in data:
                        if (row.get('Date') == calling_day and 
                            row.get('Time_Slot') == current_slot and 
                            row.get('Status') in ['booked', 'called']):
                            member_name = row.get('Member_Name', 'Unknown')
                            
                            try:
                                embed = discord.Embed(
                                    title=f"🎙️ {current_slot.upper()} — YOUR TURN NOW!",
                                    description=f"**{member_name}** — It's your turn!",
                                    color=discord.Color.green()
                                )
                                embed.add_field(
                                    name="📍 Slot Info",
                                    value=f"⏰ Slot: {calling_day} {current_slot}",
                                    inline=False
                                )
                                embed.add_field(
                                    name="⚡ React ✅ NOW",
                                    value="React with ✅ below to mark yourself as live and start calling!",
                                    inline=False
                                )
                                msg = await channel.send(embed=embed)
                                TURN_ANNOUNCEMENTS[announcement_key] = msg.id
                            except Exception as e:
                                print(f"Error posting now announcement: {e}")

@tasks.loop(minutes=1)
async def check_no_shows():
    """Check for no-shows (10 min after slot start with no reaction) - NO DM SENT"""
    if not DIALING_QUEUE_CHANNEL_ID:
        return
    
    now = get_bd_time()
    current_hour = now.hour
    current_minute = now.minute
    
    # Check if we're at X:10 (10 minutes into an hour slot)
    if current_minute != 10:
        return
    
    slot_map = {
        19: '7pm', 20: '8pm', 21: '9pm', 22: '10pm', 23: '11pm',
        0: '12am', 1: '1am', 2: '2am', 3: '3am'
    }
    current_slot = slot_map.get(current_hour)
    
    if not current_slot:
        return
    
    calling_day = get_current_calling_day()
    if not calling_day:
        return
    
    announcement_key = f"{calling_day}_{current_slot}"
    
    # Only announce no-show once
    if announcement_key in NO_SHOWS_ANNOUNCED:
        return
    
    # Check if member has reacted
    if announcement_key in MEMBERS_REACTED:
        return
    
    # Check who should have started this slot
    data = get_calling_schedule_data()
    for row in data:
        if row.get('Date') == calling_day and row.get('Time_Slot') == current_slot:
            status = row.get('Status', '')
            member_name = row.get('Member_Name', '')
            
            if status == 'booked':
                # Mark as no-show
                mark_as_no_show(calling_day, current_slot)
                NO_SHOWS_ANNOUNCED[announcement_key] = True
                
                try:
                    # Announce in #dialing-queue ONLY (NO DM)
                    channel = bot.get_channel(DIALING_QUEUE_CHANNEL_ID)
                    if channel:
                        embed = discord.Embed(
                            title=f"⚠️ SLOT OPEN: {current_slot}",
                            description=f"**{member_name}** didn't show up for their {current_slot} slot.",
                            color=discord.Color.orange()
                        )
                        embed.add_field(
                            name="📍 Slot Info",
                            value=f"⏰ Slot: {calling_day} {current_slot}",
                            inline=False
                        )
                        embed.add_field(
                            name="⏳ Time remaining",
                            value=f"50 minutes (slot ends at next hour)",
                            inline=False
                        )
                        embed.add_field(
                            name="React ✅",
                            value="to claim this slot and start calling!",
                            inline=False
                        )
                        await channel.send(embed=embed)
                except Exception as e:
                    print(f"Error handling no-show: {e}")

@tasks.loop(minutes=1)
async def check_slot_completion():
    """Check if slots have completed and mark them done"""
    now = get_bd_time()
    current_hour = now.hour
    current_minute = now.minute
    
    # Check at the top of each hour (X:00)
    if current_minute != 0:
        return
    
    # Get the previous hour's slot (the one that just ended)
    if current_hour == 0:
        prev_hour = 23
    else:
        prev_hour = current_hour - 1
    
    slot_map = {
        19: '7pm', 20: '8pm', 21: '9pm', 22: '10pm', 23: '11pm',
        0: '12am', 1: '1am', 2: '2am', 3: '3am'
    }
    completed_slot = slot_map.get(prev_hour)
    
    if not completed_slot:
        return
    
    # Get the calling day for the completed slot
    calling_day = get_current_calling_day()
    if not calling_day:
        # If we're outside the calling window, use calendar day
        calling_day = get_calendar_day()
    
    # Mark slot as complete in sheet
    sheet = get_sheet()
    if sheet:
        try:
            worksheet = sheet.worksheet(CALLING_SHEET_TAB)
            data = worksheet.get_all_records()
            for i, row in enumerate(data):
                if (row.get('Date') == calling_day and 
                    row.get('Time_Slot') == completed_slot and 
                    row.get('Status') == 'called'):
                    # Set duration to 60 minutes
                    mark_slot_complete(row.get('Member_ID'), calling_day, completed_slot)
        except Exception as e:
            print(f"Error completing slot: {e}")
    
    # Announce completion
    try:
        channel = bot.get_channel(DIALING_QUEUE_CHANNEL_ID)
        if channel:
            data = get_calling_schedule_data()
            for row in data:
                if (row.get('Date') == calling_day and 
                    row.get('Time_Slot') == completed_slot and 
                    row.get('Status') == 'called'):
                    member_name = row.get('Member_Name', '')
                    embed = discord.Embed(
                        title=f"✅ Slot Completed!",
                        description=f"**{member_name}** finished their {completed_slot} calling session!",
                        color=discord.Color.green()
                    )
                    embed.add_field(name="💪", value="Great work! Keep it up!", inline=False)
                    await channel.send(embed=embed)
                    break
    except Exception as e:
        print(f"Error announcing completion: {e}")

@tasks.loop(seconds=10)
async def update_dashboard():
    """Update the pinned dashboard every 10 seconds with 7-day navigation"""
    global DASHBOARD_MESSAGE_ID, CURRENT_DASHBOARD_DAY
    
    if not DIALING_QUEUE_CHANNEL_ID:
        return
    
    channel = bot.get_channel(DIALING_QUEUE_CHANNEL_ID)
    if not channel:
        return
    
    try:
        # Get the day to display (default to today)
        if not CURRENT_DASHBOARD_DAY:
            CURRENT_DASHBOARD_DAY = get_calendar_day()
        
        # Build embed for the current displayed day
        embed = build_dashboard_embed(CURRENT_DASHBOARD_DAY)
        
        # Create day navigation view instance
        class DayNavigationView(discord.ui.View):
            async def on_timeout(self):
                pass
        
        view = DayNavigationView()
        
        # Get all future days (7 days)
        future_days = get_future_days(7)
        valid_days = [d for d in future_days if d['name'] in DAYS]
        
        # Add buttons for each day to the view instance
        for day_obj in valid_days:
            day_name = day_obj['name']
            label = f"{day_name}" + (" (Today)" if day_obj['is_today'] else "")
            
            async def day_btn_callback(interaction: discord.Interaction, d=day_name):
                global CURRENT_DASHBOARD_DAY
                CURRENT_DASHBOARD_DAY = d
                await interaction.response.defer()
            
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
            button.callback = day_btn_callback
            view.add_item(button)
        
        # Try to find and edit existing pinned message
        found = False
        async for msg in channel.history(limit=20):
            if msg.author == bot.user and msg.embeds:
                if "ZERO2HIRE CALLING SCHEDULER" in msg.embeds[0].title:
                    await msg.edit(embed=embed, view=view)
                    found = True
                    DASHBOARD_MESSAGE_ID = msg.id
                    break
        
        # If no existing message, send a new one and pin it
        if not found:
            msg = await channel.send(embed=embed, view=view)
            await msg.pin()
            DASHBOARD_MESSAGE_ID = msg.id
    
    except Exception as e:
        print(f"Error updating dashboard: {e}")

@tasks.loop(hours=1)
async def update_summary_tab():
    """Update the Summary tab with session counts per member"""
    sheet = get_sheet()
    if not sheet:
        return
    
    try:
        # Get all calling data
        worksheet = sheet.worksheet(CALLING_SHEET_TAB)
        data = worksheet.get_all_records()
        
        # Count sessions per member
        member_sessions = {}
        for row in data:
            if row.get('Status') == 'called':
                member_id = row.get('Member_ID', '')
                member_name = row.get('Member_Name', '')
                
                if member_id not in member_sessions:
                    member_sessions[member_id] = {
                        'name': member_name,
                        'count': 0
                    }
                member_sessions[member_id]['count'] += 1
        
        # Update Summary tab
        summary_worksheet = sheet.worksheet(SUMMARY_SHEET_TAB)
        summary_worksheet.clear()
        
        # Add headers
        summary_worksheet.append_row(['Member_ID', 'Member_Name', 'Total_Sessions', 'Last_Called'])
        
        # Add data
        for member_id, info in member_sessions.items():
            summary_worksheet.append_row([
                member_id,
                info['name'],
                info['count'],
                get_bd_time().strftime('%a %I:%M%p')
            ])
    except Exception as e:
        print(f"Error updating summary tab: {e}")

# ============================================================================
# RUN BOT
# ============================================================================

if __name__ == '__main__':
    bot.run(TOKEN)
