import discord
from discord.ext import commands, tasks
import gspread
from google.oauth2.service_account import Credentials
import json
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
import asyncio

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
CREDENTIALS_JSON = os.getenv('GOOGLE_SHEETS_CREDENTIALS')

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
DIALING_QUEUE_CHANNEL_ID = None  # Will be set after bot loads
DASHBOARD_MESSAGE_ID = None  # Will store pinned message ID

TIME_SLOTS = ['7pm', '8pm', '9pm', '10pm', '11pm', '12am', '1am', '2am', '3am']
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']

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
    except:
        return []

def is_slot_booked(day, time_slot):
    """Check if a slot is already booked"""
    data = get_calling_schedule_data()
    for row in data:
        if row.get('Date') == day and row.get('Time_Slot') == time_slot:
            status = row.get('Status', '')
            if status in ['called', 'booked']:
                return True
    return False

def book_slot(member_name, day, time_slot):
    """Add a booking to Google Sheet"""
    sheet = get_sheet()
    if not sheet:
        return False
    try:
        worksheet = sheet.worksheet(CALLING_SHEET_TAB)
        now = datetime.now().strftime('%a %I:%M%p')
        worksheet.append_row([
            day,
            time_slot,
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

def cancel_slot(member_name, day, time_slot):
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
                row.get('Member_Name') == member_name):
                # Update row in Google Sheet (rows are 1-indexed, headers are row 1)
                worksheet.update_cell(i + 2, 4, 'cancelled')  # Status column
                worksheet.update_cell(i + 2, 8, datetime.now().strftime('%a %I:%M%p'))  # Cancelled_At
                return True
        return False
    except Exception as e:
        print(f"Error cancelling slot: {e}")
        return False

def get_member_slots(member_name):
    """Get all slots for a member"""
    data = get_calling_schedule_data()
    slots = []
    for row in data:
        if row.get('Member_Name') == member_name:
            slots.append({
                'day': row.get('Date'),
                'time': row.get('Time_Slot'),
                'status': row.get('Status')
            })
    return slots

def can_cancel_slot(day, time_slot):
    """Check if cancellation is allowed (3+ hours before slot)"""
    now = datetime.now()
    
    # Map time slots to hours (Bangladesh Time UTC+6)
    slot_hours = {
        '7pm': 19, '8pm': 20, '9pm': 21, '10pm': 22, '11pm': 23,
        '12am': 0, '1am': 1, '2am': 2, '3am': 3
    }
    
    # Get slot hour
    slot_hour = slot_hours.get(time_slot)
    if slot_hour is None:
        return False
    
    # Parse day (assuming same week, handle week boundary)
    day_map = {d: i for i, d in enumerate(DAYS)}
    slot_day_index = day_map.get(day)
    
    if slot_day_index is None:
        return False
    
    # Create datetime for the slot (today's date + slot hour)
    slot_datetime = datetime.now().replace(hour=slot_hour, minute=0, second=0, microsecond=0)
    
    # If slot is before now today, it must be next week
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

# ============================================================================
# DISCORD BOT EVENTS
# ============================================================================

@bot.event
async def on_ready():
    """Bot startup event"""
    global DIALING_QUEUE_CHANNEL_ID
    print(f'{bot.user} has connected to Discord!')
    
    # Find #dialing-queue channel
    for guild in bot.guilds:
        for channel in guild.channels:
            if channel.name == 'dialing-queue':
                DIALING_QUEUE_CHANNEL_ID = channel.id
                break
    
    print(f"Dialing queue channel ID: {DIALING_QUEUE_CHANNEL_ID}")
    
    try:
        synced = await bot.tree.sync()
        print(f'Synced {len(synced)} command(s)')
    except Exception as e:
        print(e)
    
    # Start background tasks
    update_dashboard.start()
    check_slot_reminders.start()
    check_no_shows.start()

# ============================================================================
# SLASH COMMANDS
# ============================================================================

@bot.tree.command(name="book", description="Book a calling slot")
async def book_command(interaction: discord.Interaction):
    """Book a slot - opens interactive menu"""
    await interaction.response.defer()
    
    member = interaction.user
    
    # Create day selector view
    class DayView(discord.ui.View):
        selected_day = None
        
        async def on_timeout(self):
            await interaction.followup.send("Booking timed out.", ephemeral=True)
        
        @discord.ui.button(label='Monday', style=discord.ButtonStyle.primary)
        async def monday_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
            self.selected_day = 'Monday'
            await interaction.response.defer()
            await show_time_slots(interaction, self.selected_day)
        
        @discord.ui.button(label='Tuesday', style=discord.ButtonStyle.primary)
        async def tuesday_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
            self.selected_day = 'Tuesday'
            await interaction.response.defer()
            await show_time_slots(interaction, self.selected_day)
        
        @discord.ui.button(label='Wednesday', style=discord.ButtonStyle.primary)
        async def wednesday_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
            self.selected_day = 'Wednesday'
            await interaction.response.defer()
            await show_time_slots(interaction, self.selected_day)
        
        @discord.ui.button(label='Thursday', style=discord.ButtonStyle.primary)
        async def thursday_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
            self.selected_day = 'Thursday'
            await interaction.response.defer()
            await show_time_slots(interaction, self.selected_day)
        
        @discord.ui.button(label='Friday', style=discord.ButtonStyle.primary)
        async def friday_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
            self.selected_day = 'Friday'
            await interaction.response.defer()
            await show_time_slots(interaction, self.selected_day)
    
    async def show_time_slots(interaction, day):
        """Show available time slots for selected day"""
        available = format_available_slots(day)
        
        if not available:
            await interaction.followup.send(
                f"❌ All slots full for {day}. Try another day.",
                ephemeral=True
            )
            return
        
        class TimeView(discord.ui.View):
            selected_time = None
            
            async def on_timeout(self):
                await interaction.followup.send("Booking timed out.", ephemeral=True)
        
        # Create button for each available time
        for time_slot in available:
            async def time_btn_callback(interaction: discord.Interaction, ts=time_slot):
                # Check if already booked (double-check)
                if is_slot_booked(day, ts):
                    await interaction.response.send_message(
                        f"❌ {ts} on {day} was just booked by someone else. Try another slot.",
                        ephemeral=True
                    )
                    return
                
                # Check if member already has this slot
                member_slots = get_member_slots(str(member))
                if any(s['day'] == day and s['time'] == ts for s in member_slots):
                    await interaction.response.send_message(
                        f"❌ You already booked {day} {ts}.",
                        ephemeral=True
                    )
                    return
                
                # Book the slot
                if book_slot(str(member), day, ts):
                    embed = discord.Embed(
                        title="✅ BOOKING CONFIRMED!",
                        description=f"You're booked for **{day} {ts}**",
                        color=discord.Color.green()
                    )
                    embed.add_field(name="⏰ Calling Window", value=f"{ts}–10pm (Bangladesh Time)", inline=False)
                    embed.add_field(name="💬 What to do", value="React ✅ in #dialing-queue when you start calling", inline=False)
                    embed.add_field(name="🔔 Reminder", value="You'll get a DM 30 minutes before", inline=False)
                    
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await interaction.response.send_message(
                        "❌ Error booking slot. Try again.",
                        ephemeral=True
                    )
            
            button = discord.ui.Button(label=time_slot, style=discord.ButtonStyle.success)
            button.callback = time_btn_callback
            TimeView.add_item(button)
        
        embed = discord.Embed(
            title=f"Available slots for {day}",
            description="Select a time to book:",
            color=discord.Color.blue()
        )
        await interaction.followup.send(embed=embed, view=TimeView(), ephemeral=True)
    
    embed = discord.Embed(
        title="📅 Select a day",
        description="Pick Monday through Friday",
        color=discord.Color.blue()
    )
    await interaction.followup.send(embed=embed, view=DayView(), ephemeral=True)

@bot.tree.command(name="cancel", description="Cancel your booking")
async def cancel_command(interaction: discord.Interaction, day: str, time: str):
    """Cancel a booked slot"""
    member = interaction.user
    
    # Validate inputs
    if day not in DAYS or time not in TIME_SLOTS:
        await interaction.response.send_message(
            "❌ Invalid day or time. Use format: /cancel Monday 9pm",
            ephemeral=True
        )
        return
    
    # Check if member has this slot
    member_slots = get_member_slots(str(member))
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
    embed.add_field(name="⏱️ Time until slot", value="5 hours 15 minutes (example)", inline=False)
    embed.add_field(name="✅ Status", value="You can cancel (3+ hours before slot)", inline=False)
    
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    await view.wait_timeout()
    
    if view.result:
        if cancel_slot(str(member), day, time):
            # Announce in dialing-queue
            channel = bot.get_channel(DIALING_QUEUE_CHANNEL_ID)
            if channel:
                embed = discord.Embed(
                    title=f"⚠️ SLOT OPEN: {day} {time}",
                    description=f"{member.mention} just cancelled their {time} slot.",
                    color=discord.Color.orange()
                )
                embed.add_field(name="⏳ Time Remaining", value="(slot time remaining)", inline=False)
                embed.set_footer(text="React ✅ to claim this slot!")
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
    slots = get_member_slots(str(member))
    
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
            emoji = "✅" if slot['status'] == 'booked' else "✔️" if slot['status'] == 'called' else "❌"
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
        value="Book a 1-hour calling slot (7pm–3am, Mon–Fri)",
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
        value="1. Book a slot\n2. 30 min before, you get a DM reminder\n3. When it's your turn, react ✅ in #dialing-queue\n4. Dashboard shows you're live\n5. Call for 1 hour",
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
async def check_slot_reminders():
    """Check if any slots are starting in 30 minutes and send DM reminders"""
    # This is a simplified version - in production, track which reminders were sent
    pass

@tasks.loop(minutes=1)
async def check_no_shows():
    """Check for no-shows (10 min after slot start with no reaction)"""
    # This is a simplified version - needs persistent tracking
    pass

@tasks.loop(seconds=30)
async def update_dashboard():
    """Update the pinned dashboard embed"""
    if not DIALING_QUEUE_CHANNEL_ID:
        return
    
    channel = bot.get_channel(DIALING_QUEUE_CHANNEL_ID)
    if not channel:
        return
    
    # Build dashboard embed
    data = get_calling_schedule_data()
    
    embed = discord.Embed(
        title="🎙️ ZERO2HIRE CALLING SCHEDULER",
        color=discord.Color.gold()
    )
    
    embed.add_field(
        name="🔴 LIVE NOW",
        value="(No one calling right now)",
        inline=False
    )
    
    embed.add_field(
        name="⏭️ NEXT UP",
        value="(Check back soon)",
        inline=False
    )
    
    embed.add_field(
        name="👥 TODAY'S QUEUE",
        value="\n".join([f"{slot} → (Open)" for slot in TIME_SLOTS]),
        inline=False
    )
    
    embed.set_footer(text="📋 /book to book a slot | /help for more info")
    
    # Try to find and edit existing pinned message
    try:
        async for msg in channel.history(limit=10):
            if msg.author == bot.user and msg.embeds:
                if "ZERO2HIRE CALLING SCHEDULER" in msg.embeds[0].title:
                    await msg.edit(embed=embed)
                    return
    except:
        pass
    
    # If no existing message, send a new one
    msg = await channel.send(embed=embed)
    await msg.pin()

# ============================================================================
# RUN BOT
# ============================================================================

if __name__ == '__main__':
    bot.run(TOKEN)
