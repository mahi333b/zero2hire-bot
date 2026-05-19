import discord
from discord.ext import commands, tasks
from notion_client import Client
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import asyncio
import pytz
from typing import Optional, List, Dict

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
BOOKINGS_DATABASE_ID = os.getenv('BOOKINGS_DATABASE_ID')

# Bangladesh timezone
BD_TZ = pytz.timezone('Asia/Dhaka')

# Initialize Notion client
try:
    notion = Client(auth=NOTION_TOKEN)
except Exception as e:
    print(f"Error initializing Notion: {e}")
    notion = None

# Discord bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix='/', intents=intents)

# Global variables
DIALING_QUEUE_CHANNEL_ID = None
DASHBOARD_MESSAGE_ID = None

TIME_SLOTS = ['7pm', '8pm', '9pm', '10pm', '11pm', '12am', '1am', '2am', '3am']
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']

# Track turn announcements (clean up daily to prevent memory leak)
TURN_ANNOUNCEMENTS = {}  # {day_slot: message_id}
NO_SHOWS_ANNOUNCED = {}  # {day_slot: True}

# ============================================================================
# NOTION HELPER FUNCTIONS
# ============================================================================

def get_bd_time():
    """Get current Bangladesh time"""
    return datetime.now(BD_TZ)

def get_current_calling_day():
    """Get which day's calling window is currently active"""
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
    """Get current calendar day"""
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    return days[get_bd_time().weekday()]

def get_current_time_slot():
    """Get current time slot"""
    hour = get_bd_time().hour
    slot_map = {
        19: '7pm', 20: '8pm', 21: '9pm', 22: '10pm', 23: '11pm',
        0: '12am', 1: '1am', 2: '2am', 3: '3am'
    }
    return slot_map.get(hour)

def get_next_time_slot():
    """Get next time slot"""
    hour = get_bd_time().hour
    slot_map = {
        19: '7pm', 20: '8pm', 21: '9pm', 22: '10pm', 23: '11pm',
        0: '12am', 1: '1am', 2: '2am', 3: '3am'
    }
    next_hour = (hour + 1) % 24
    return slot_map.get(next_hour)

def query_notion_database(filters=None):
    """Query Bookings database"""
    if not notion:
        return []

    try:
        results = []
        has_more = True
        start_cursor = None

        while has_more:
            kwargs = {
                "filter": {"property": "object", "value": "page"},
                "page_size": 100
            }
            if start_cursor:
                kwargs["start_cursor"] = start_cursor

            response = notion.search(**kwargs)

            for page in response.get('results', []):
                parent = page.get('parent', {})
                db_id = parent.get('database_id', '').replace('-', '')
                target_id = BOOKINGS_DATABASE_ID.replace('-', '')
                if db_id == target_id:
                    results.append(page)

            has_more = response.get('has_more', False)
            start_cursor = response.get('next_cursor')

        return results
    except Exception as e:
        print(f"Error querying Notion: {e}")
        return []

def get_all_bookings():
    """Get all bookings from Notion"""
    results = query_notion_database()
    bookings = []
    
    for page in results:
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
    
    return bookings

def get_slot_booking(day, time_slot):
    """Get booking for a specific slot - ATOMIC CHECK"""
    bookings = get_all_bookings()
    for booking in bookings:
        if (booking['day'] == day and 
            booking['Time_Slot'] == time_slot and 
            booking['Status'] in ['booked', 'called']):
            return booking
    return None

def member_has_slot_on_day(member_id, day):
    """Check if member already has a slot on this day"""
    bookings = get_all_bookings()
    for booking in bookings:
        if (booking['day'] == day and 
            booking['Member_ID'] == str(member_id) and
            booking['Status'] in ['booked', 'called']):
            return True
    return False

def create_booking(member_id, member_name, day, time_slot):
    """Create a new booking - ATOMIC OPERATION"""
    if not notion:
        return False
    
    # Double-check slot is still free (race condition prevention)
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
        return True
    except Exception as e:
        print(f"Error creating booking: {e}")
        return False

def update_booking_status(page_id, new_status, field_to_update=None, value=None):
    """Update booking status and optional other field"""
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
        return True
    except Exception as e:
        print(f"Error updating booking: {e}")
        return False

def cancel_slot_by_member(member_id, day, time_slot):
    """Cancel a booking"""
    bookings = get_all_bookings()
    for booking in bookings:
        if (booking['day'] == day and 
            booking['Time_Slot'] == time_slot and 
            booking['Member_ID'] == str(member_id)):
            update_booking_status(
                booking['id'], 
                'cancelled',
                'Cancelled_At',
                get_bd_time().isoformat()
            )
            return True
    return False

def mark_as_called(member_id, day, time_slot):
    """Mark slot as called"""
    bookings = get_all_bookings()
    for booking in bookings:
        if (booking['day'] == day and 
            booking['Time_Slot'] == time_slot and 
            booking['Member_ID'] == str(member_id)):
            update_booking_status(
                booking['id'],
                'called',
                'Called_At',
                get_bd_time().isoformat()
            )
            return True
    return False

def mark_as_no_show(day, time_slot):
    """Mark slot as no-show"""
    bookings = get_all_bookings()
    for booking in bookings:
        if (booking['day'] == day and 
            booking['Time_Slot'] == time_slot and 
            booking['Status'] == 'booked'):
            update_booking_status(booking['id'], 'no-show')
            return True
    return False

def mark_slot_complete(member_id, day, time_slot):
    """Mark slot complete with 60-min duration"""
    bookings = get_all_bookings()
    for booking in bookings:
        if (booking['day'] == day and 
            booking['Time_Slot'] == time_slot and 
            booking['Member_ID'] == str(member_id) and
            booking['Status'] == 'called'):
            update_booking_status(booking['id'], 'called', 'Duration_Minutes', 60)
            return True
    return False

def can_cancel_slot(day, time_slot):
    """Check if 3+ hours before slot"""
    now = get_bd_time()
    
    slot_hours = {
        '7pm': 19, '8pm': 20, '9pm': 21, '10pm': 22, '11pm': 23,
        '12am': 0, '1am': 1, '2am': 2, '3am': 3
    }
    
    slot_hour = slot_hours.get(time_slot)
    if slot_hour is None:
        return False
    
    # Get day of week indices
    days_list = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    today_idx = now.weekday()
    target_idx = days_list.index(day)
    
    # Calculate days ahead to reach target day
    days_ahead = (target_idx - today_idx) % 7
    
    # Create datetime for the slot on target day
    slot_datetime = now.replace(hour=slot_hour, minute=0, second=0, microsecond=0)
    slot_datetime += timedelta(days=days_ahead)
    
    # If calculated datetime is in the past, it's next week's occurrence
    if slot_datetime < now:
        slot_datetime += timedelta(days=7)
    
    time_until = slot_datetime - now
    return time_until.total_seconds() >= 3 * 3600

def get_available_slots(day):
    """Get available slots for a day"""
    available = []
    for slot in TIME_SLOTS:
        if not get_slot_booking(day, slot):
            available.append(slot)
    return available

def get_member_slots(member_id):
    """Get only FUTURE active slots for a member (booked or called status)"""
    bookings = get_all_bookings()
    slots = []
    now = get_bd_time()
    days_list = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    
    for booking in bookings:
        # Filter 1: Only this member
        if booking['Member_ID'] != str(member_id):
            continue
        
        # Filter 2: Only active statuses (no cancelled, no-show)
        if booking['Status'] not in ['booked', 'called']:
            continue
        
        # Filter 3: Only future slots
        if not booking['day'] or not booking['Time_Slot']:
            continue
        
        try:
            slot_hours = {
                '7pm': 19, '8pm': 20, '9pm': 21, '10pm': 22, '11pm': 23,
                '12am': 0, '1am': 1, '2am': 2, '3am': 3
            }
            slot_hour = slot_hours[booking['Time_Slot']]
            today_idx = now.weekday()
            target_idx = days_list.index(booking['day'])
            days_ahead = (target_idx - today_idx) % 7
            
            slot_datetime = now.replace(hour=slot_hour, minute=0, second=0, microsecond=0)
            slot_datetime += timedelta(days=days_ahead)
            
            if slot_datetime < now:
                slot_datetime += timedelta(days=7)
            
            # Only add if slot is in the future
            if slot_datetime > now:
                slots.append(booking)
        except (ValueError, KeyError):
            continue
    
    return slots

def get_future_days(days_ahead=7):
    """Get list of days from today onwards"""
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
    bookings = get_all_bookings()
    
    embed = discord.Embed(
        title="🎙️ ZERO2HIRE CALLING SCHEDULER",
        color=discord.Color.gold()
    )
    
    # Find who's live now
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
    
    if next_slot and calling_day:
        for booking in bookings:
            if (booking['day'] == calling_day and 
                booking['Time_Slot'] == next_slot and 
                booking['Status'] in ['booked', 'called']):
                next_member = booking['Member_Name']
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
    
    # Show all slots for selected day
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
    global DIALING_QUEUE_CHANNEL_ID
    try:
        print(f'{bot.user} has connected to Discord!')
        
        # Find #dialing-queue channel
        for guild in bot.guilds:
            for channel in guild.channels:
                if channel.name == 'dialing-queue':
                    DIALING_QUEUE_CHANNEL_ID = channel.id
                    break
        
        print(f"✅ Dialing queue channel ID: {DIALING_QUEUE_CHANNEL_ID}")
        
        if not DIALING_QUEUE_CHANNEL_ID:
            print("❌ #dialing-queue channel not found!")
        
        try:
            synced = await bot.tree.sync()
            print(f'✅ Synced {len(synced)} command(s)')
        except Exception as e:
            print(f"❌ Error syncing commands: {e}")
        
        # Start background tasks
        try:
            if not update_dashboard.is_running():
                update_dashboard.start()
                print("✅ Dashboard task started (2-sec refresh)")
        except Exception as e:
            print(f"❌ Dashboard task error: {e}")
        
        try:
            if not post_turn_announcements.is_running():
                post_turn_announcements.start()
                print("✅ Turn announcements task started")
        except Exception as e:
            print(f"❌ Turn announcements error: {e}")
        
        try:
            if not check_no_shows.is_running():
                check_no_shows.start()
                print("✅ No-shows detection started")
        except Exception as e:
            print(f"❌ No-shows error: {e}")
        
        try:
            if not check_slot_completion.is_running():
                check_slot_completion.start()
                print("✅ Slot completion started")
        except Exception as e:
            print(f"❌ Slot completion error: {e}")
        
        try:
            if not cleanup_memory.is_running():
                cleanup_memory.start()
                print("✅ Memory cleanup started (daily)")
        except Exception as e:
            print(f"❌ Memory cleanup error: {e}")
        
        print("🚀 Bot fully initialized and ready!")
    except Exception as e:
        print(f"❌ CRITICAL ERROR IN ON_READY: {e}")
        import traceback
        traceback.print_exc()

@bot.event
async def on_reaction_add(reaction, user):
    """Handle ✅ reactions to turn announcements"""
    if user == bot.user:
        return
    
    if reaction.emoji != '✅':
        return
    
    if reaction.message.channel.id != DIALING_QUEUE_CHANNEL_ID:
        return
    
    if not reaction.message.embeds:
        return
    
    embed = reaction.message.embeds[0]
    
    # Get metadata from embed footer
    footer_text = embed.footer.text if embed.footer else ""
    
    if not footer_text.startswith("day=") or not ("slot=" in footer_text):
        return
    
    # Parse metadata: "day=Monday|slot=9pm"
    parts = footer_text.split("|")
    day = None
    time_slot = None
    
    for part in parts:
        if part.startswith("day="):
            day = part.replace("day=", "")
        elif part.startswith("slot="):
            time_slot = part.replace("slot=", "")
    
    if not day or not time_slot:
        return
    
    member_id = user.id
    member_name = str(user)
    
    # Check if slot is still free (race condition prevention)
    if get_slot_booking(day, time_slot):
        await user.send(f"❌ The {day} {time_slot} slot was already claimed by someone else.")
        return
    
    # Book the slot
    if create_booking(member_id, member_name, day, time_slot):
        mark_as_called(member_id, day, time_slot)
        
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
    """Book a slot"""
    await interaction.response.defer()
    
    member = interaction.user
    member_id = member.id
    
    future_days = get_future_days(7)
    valid_days = [d for d in future_days if d['name'] in DAYS]
    
    class DayView(discord.ui.View):
        async def on_timeout(self):
            await interaction.followup.send("❌ Booking timed out.", ephemeral=True)
    
    day_view = DayView()
    
    for day_obj in valid_days:
        day_name = day_obj['name']
        label = f"{day_name}" + (" (Today)" if day_obj['is_today'] else "")
        
        async def day_callback(interaction: discord.Interaction, d=day_name):
            await interaction.response.defer()
            await show_time_slots(interaction, d, member_id, member)
        
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
        button.callback = day_callback
        day_view.add_item(button)
    
    async def show_time_slots(interaction: discord.Interaction, day: str, mid: int, user):
        """Show available times"""
        if member_has_slot_on_day(mid, day):
            await interaction.followup.send(
                f"❌ You already have a slot booked for {day}. One slot per day!",
                ephemeral=True
            )
            return
        
        available = get_available_slots(day)
        
        if not available:
            await interaction.followup.send(
                f"❌ All slots full for {day}. Try another day.",
                ephemeral=True
            )
            return
        
        class TimeView(discord.ui.View):
            pass
        
        time_view = TimeView()
        
        for time_slot in available:
            async def time_callback(interaction: discord.Interaction, ts=time_slot, d=day):
                if create_booking(mid, str(user), d, ts):
                    embed = discord.Embed(
                        title="✅ CONFIRMED!",
                        description=f"You're booked for **{d} {ts}**.",
                        color=discord.Color.green()
                    )
                    embed.add_field(
                        name="📍 Calling window",
                        value=f"{ts} Bangladesh Time",
                        inline=False
                    )
                    embed.add_field(
                        name="📢 What to do",
                        value="Wait for announcement in #dialing-queue.\nReact ✅ when instructed!",
                        inline=False
                    )
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await interaction.response.send_message(
                        f"❌ Slot taken! Someone booked {d} {ts} just now. Try another.",
                        ephemeral=True
                    )
            
            button = discord.ui.Button(label=time_slot, style=discord.ButtonStyle.success)
            button.callback = time_callback
            time_view.add_item(button)
        
        embed = discord.Embed(
            title=f"📅 Available slots for {day}",
            description="Select a time:",
            color=discord.Color.blue()
        )
        await interaction.followup.send(embed=embed, view=time_view, ephemeral=True)
    
    embed = discord.Embed(
        title="📅 Select a day",
        description="Pick a day from the next 7 days (Mon–Fri only)",
        color=discord.Color.blue()
    )
    await interaction.followup.send(embed=embed, view=day_view, ephemeral=True)

@bot.tree.command(name="cancel", description="Cancel your booking")
async def cancel_command(interaction: discord.Interaction):
    """Cancel a slot - interactive version"""
    member = interaction.user
    member_id = member.id
    
    await interaction.response.defer(ephemeral=True)
    
    # Get user's future active slots
    slots = get_member_slots(member_id)
    
    if not slots:
        await interaction.followup.send("❌ You have no upcoming bookings to cancel.", ephemeral=True)
        return
    
    # Build selection view
    class SlotSelectView(discord.ui.View):
        selected_slot = None
    
    view = SlotSelectView()
    
    for slot in slots:
        day = slot['day']
        time_slot = slot['Time_Slot']
        label = f"{day} {time_slot}"
        
        async def select_callback(interaction: discord.Interaction, d=day, ts=time_slot):
            # Check if can cancel
            if not can_cancel_slot(d, ts):
                await interaction.response.send_message(
                    "❌ Too late to cancel. Must cancel 3+ hours before slot.",
                    ephemeral=True
                )
                return
            
            # Confirm cancellation
            class ConfirmView(discord.ui.View):
                result = None
                
                @discord.ui.button(label="CONFIRM CANCEL", style=discord.ButtonStyle.danger)
                async def confirm(self, button_interaction: discord.Interaction):
                    self.result = True
                    await button_interaction.response.defer()
                
                @discord.ui.button(label="KEEP MY SLOT", style=discord.ButtonStyle.primary)
                async def keep(self, button_interaction: discord.Interaction):
                    self.result = False
                    await button_interaction.response.defer()
            
            confirm_view = ConfirmView()
            embed = discord.Embed(
                title="⚠️ Confirm Cancellation",
                description=f"Cancel **{d} {ts}**?",
                color=discord.Color.orange()
            )
            
            await interaction.response.send_message(embed=embed, view=confirm_view, ephemeral=True)
            await asyncio.sleep(60)
            
            if confirm_view.result is True:
                if cancel_slot_by_member(member_id, d, ts):
                    channel = bot.get_channel(DIALING_QUEUE_CHANNEL_ID)
                    if channel:
                        embed = discord.Embed(
                            title=f"⚠️ SLOT OPEN: {d} {ts}",
                            description=f"{member.mention} just cancelled their {ts} slot.",
                            color=discord.Color.orange()
                        )
                        embed.add_field(name="Slot Info", value=f"⏰ Slot: {d} {ts}", inline=False)
                        embed.add_field(name="React ✅", value="to claim this slot!", inline=False)
                        embed.set_footer(text=f"day={d}|slot={ts}")
                        await channel.send(embed=embed)
                    
                    await interaction.followup.send(f"✅ **Cancelled!** {d} {ts} released.", ephemeral=True)
                else:
                    await interaction.followup.send("❌ Error cancelling. Try again.", ephemeral=True)
            elif confirm_view.result is False:
                await interaction.followup.send("✅ Slot kept.", ephemeral=True)
            else:
                await interaction.followup.send("❌ Confirmation timed out.", ephemeral=True)
        
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.danger)
        button.callback = select_callback
        view.add_item(button)
    
    embed = discord.Embed(
        title="📅 Select slot to cancel",
        description="Pick a booking to cancel:",
        color=discord.Color.blue()
    )
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="myslot", description="See your booked slots")
async def myslot_command(interaction: discord.Interaction):
    """Show member's FUTURE slots only"""
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
            title="📅 Your Upcoming Slots",
            description="Your future calling slots:",
            color=discord.Color.blue()
        )
        for slot in slots:
            emoji = "✅" if slot['Status'] == 'booked' else "🔴"
            embed.add_field(
                name=f"{emoji} {slot['day']} {slot['Time_Slot']}",
                value=f"Status: {slot['Status']}",
                inline=False
            )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="help", description="Show help")
async def help_command(interaction: discord.Interaction):
    """Show help"""
    embed = discord.Embed(
        title="📚 Zero2Hire Calling Scheduler - Help",
        description="How to use the bot",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="/book",
        value="Book a 1-hour slot (7pm–3am, Mon–Fri)\nOne slot per day!",
        inline=False
    )
    embed.add_field(
        name="/cancel",
        value="Cancel a slot (select from your bookings, 3+ hours before)",
        inline=False
    )
    embed.add_field(
        name="/myslot",
        value="See your upcoming booked slots",
        inline=False
    )
    embed.add_field(
        name="📍 How it works",
        value="1. Book via /book\n2. Get announcement 30 min before\n3. React ✅ when it's your turn\n4. Call for 1 hour",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================================
# BACKGROUND TASKS
# ============================================================================

@tasks.loop(minutes=1)
async def post_turn_announcements():
    """Post turn announcements 30 min before and at slot start"""
    if not DIALING_QUEUE_CHANNEL_ID:
        return
    
    now = get_bd_time()
    current_hour = now.hour
    current_minute = now.minute
    
    channel = bot.get_channel(DIALING_QUEUE_CHANNEL_ID)
    if not channel:
        return
    
    # 30 minutes before
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
                    booking = get_slot_booking(calling_day, next_slot)
                    if booking:
                        try:
                            embed = discord.Embed(
                                title=f"⏰ {next_slot.upper()} — YOUR TURN IN 30 MIN!",
                                description=f"**{booking['Member_Name']}** — Get ready!",
                                color=discord.Color.blue()
                            )
                            embed.add_field(
                                name="📍 Slot Info",
                                value=f"{calling_day} {next_slot}",
                                inline=False
                            )
                            embed.set_footer(text=f"day={calling_day}|slot={next_slot}")
                            msg = await channel.send(embed=embed)
                            TURN_ANNOUNCEMENTS[announcement_key] = msg.id
                        except Exception as e:
                            print(f"Error posting 30-min announcement: {e}")
    
    # At slot start
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
                    booking = get_slot_booking(calling_day, current_slot)
                    if booking:
                        try:
                            embed = discord.Embed(
                                title=f"🎙️ {current_slot.upper()} — YOUR TURN NOW!",
                                description=f"**{booking['Member_Name']}** — It's your turn!",
                                color=discord.Color.green()
                            )
                            embed.add_field(
                                name="⚡ React ✅ NOW",
                                value="React below to mark yourself as live!",
                                inline=False
                            )
                            embed.set_footer(text=f"day={calling_day}|slot={current_slot}")
                            msg = await channel.send(embed=embed)
                            TURN_ANNOUNCEMENTS[announcement_key] = msg.id
                        except Exception as e:
                            print(f"Error posting now announcement: {e}")

@tasks.loop(minutes=1)
async def check_no_shows():
    """Check for no-shows at X:10"""
    if not DIALING_QUEUE_CHANNEL_ID:
        return
    
    now = get_bd_time()
    current_hour = now.hour
    current_minute = now.minute
    
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
    
    if announcement_key in NO_SHOWS_ANNOUNCED:
        return
    
    booking = get_slot_booking(calling_day, current_slot)
    if not booking or booking['Status'] != 'booked':
        return
    
    # Mark as no-show
    mark_as_no_show(calling_day, current_slot)
    NO_SHOWS_ANNOUNCED[announcement_key] = True
    
    try:
        channel = bot.get_channel(DIALING_QUEUE_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title=f"⚠️ SLOT OPEN: {current_slot}",
                description=f"**{booking['Member_Name']}** didn't show up.",
                color=discord.Color.orange()
            )
            embed.add_field(name="⏳ Time remaining", value="50 minutes left", inline=False)
            embed.add_field(name="React ✅", value="to claim this slot!", inline=False)
            embed.set_footer(text=f"day={calling_day}|slot={current_slot}")
            await channel.send(embed=embed)
    except Exception as e:
        print(f"Error handling no-show: {e}")

@tasks.loop(minutes=1)
async def check_slot_completion():
    """Mark slots complete after 60 minutes"""
    now = get_bd_time()
    current_hour = now.hour
    current_minute = now.minute
    
    if current_minute != 0:
        return
    
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
    
    calling_day = get_current_calling_day()
    if not calling_day:
        calling_day = get_calendar_day()
    
    booking = get_slot_booking(calling_day, completed_slot)
    if booking and booking['Status'] == 'called':
        mark_slot_complete(booking['Member_ID'], calling_day, completed_slot)
        
        try:
            channel = bot.get_channel(DIALING_QUEUE_CHANNEL_ID)
            if channel:
                embed = discord.Embed(
                    title="✅ Slot Completed!",
                    description=f"**{booking['Member_Name']}** finished their {completed_slot} session!",
                    color=discord.Color.green()
                )
                embed.add_field(name="💪", value="Great work!", inline=False)
                await channel.send(embed=embed)
        except Exception as e:
            print(f"Error announcing completion: {e}")

@tasks.loop(seconds=2)
async def update_dashboard():
    """Update dashboard every 2 seconds"""
    global DASHBOARD_MESSAGE_ID
    
    if not DIALING_QUEUE_CHANNEL_ID:
        return
    
    channel = bot.get_channel(DIALING_QUEUE_CHANNEL_ID)
    if not channel:
        return
    
    try:
        today = get_calendar_day()
        embed = build_dashboard_embed(today)
        
        # Try to edit existing dashboard message
        if DASHBOARD_MESSAGE_ID:
            try:
                msg = await channel.fetch_message(DASHBOARD_MESSAGE_ID)
                await msg.edit(embed=embed)
                return
            except:
                DASHBOARD_MESSAGE_ID = None
        
        # If no tracked message, find it in channel
        async for msg in channel.history(limit=20):
            if msg.author == bot.user and msg.embeds:
                if "ZERO2HIRE CALLING SCHEDULER" in msg.embeds[0].title:
                    DASHBOARD_MESSAGE_ID = msg.id
                    await msg.edit(embed=embed)
                    return
        
        # Create new dashboard if not found
        msg = await channel.send(embed=embed)
        await msg.pin()
        DASHBOARD_MESSAGE_ID = msg.id
    
    except Exception as e:
        print(f"Error updating dashboard: {e}")

@tasks.loop(hours=24)
async def cleanup_memory():
    """Clean up old tracking dicts daily - prevent memory leaks"""
    global TURN_ANNOUNCEMENTS, NO_SHOWS_ANNOUNCED
    
    if len(TURN_ANNOUNCEMENTS) > 100:
        TURN_ANNOUNCEMENTS = dict(list(TURN_ANNOUNCEMENTS.items())[-100:])
    
    if len(NO_SHOWS_ANNOUNCED) > 100:
        NO_SHOWS_ANNOUNCED = dict(list(NO_SHOWS_ANNOUNCED.items())[-100:])
    
    print(f"✅ Memory cleaned up. Tracking {len(TURN_ANNOUNCEMENTS)} announcements.")

# ============================================================================
# RUN BOT
# ============================================================================

if __name__ == '__main__':
    bot.run(TOKEN)
