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

# Global variables
DIALING_QUEUE_CHANNEL_ID = None
DASHBOARD_MESSAGE_ID = None
CURRENT_DASHBOARD_DAY = None

TIME_SLOTS = ['7pm', '8pm', '9pm', '10pm', '11pm', '12am', '1am', '2am', '3am']
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']

# Track confirmation reactions
PENDING_CONFIRMATIONS = {}  # {f"{day}_{slot}": member_id}

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_bd_time():
    """Get current Bangladesh time"""
    return datetime.now(BD_TZ)

def get_calendar_day():
    """Get current calendar day"""
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    return days[get_bd_time().weekday()]

def get_current_calling_day():
    """Get which day's calling window is currently active"""
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

def query_notion_database():
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
    """Get booking for a specific slot"""
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
    """Create a new booking"""
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
        return True
    except Exception as e:
        print(f"Error creating booking: {e}")
        return False

def update_booking_status(page_id, new_status, field_to_update=None, value=None):
    """Update booking status"""
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
    """Mark slot complete"""
    bookings = get_all_bookings()
    for booking in bookings:
        if (booking['day'] == day and 
            booking['Time_Slot'] == time_slot and 
            booking['Member_ID'] == str(member_id) and
            booking['Status'] == 'called'):
            update_booking_status(booking['id'], 'called', 'Duration_Minutes', 60)
            return True
    return False

def get_member_slots(member_id):
    """Get only FUTURE active slots for a member"""
    bookings = get_all_bookings()
    slots = []
    now = get_bd_time()
    days_list = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    
    for booking in bookings:
        if booking['Member_ID'] != str(member_id):
            continue
        
        if booking['Status'] not in ['booked', 'called']:
            continue
        
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
            
            if slot_datetime > now:
                slots.append(booking)
        except (ValueError, KeyError):
            continue
    
    return slots

def get_available_slots(day):
    """Get available slots for a day"""
    available = []
    for slot in TIME_SLOTS:
        if not get_slot_booking(day, slot):
            available.append(slot)
    return available

def get_future_days():
    """Get Mon-Fri only"""
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
    """Build dashboard embed"""
    bookings = get_all_bookings()
    
    embed = discord.Embed(
        title="🎙️ ZERO2HIRE CALLING SCHEDULER",
        color=discord.Color.gold()
    )
    
    # Live now
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
    
    # Next up
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
            queue_text += f"✅ {slot} → **{booked_member}**\n"
        else:
            queue_text += f"⏳ {slot} → (Open)\n"
    
    embed.add_field(
        name=f"👥 {day.upper()}'S QUEUE",
        value=queue_text or "All slots open",
        inline=False
    )
    
    embed.set_footer(text="📋 /book to book a slot")
    
    return embed

# ============================================================================
# DISCORD BOT EVENTS
# ============================================================================

@bot.event
async def on_ready():
    """Bot startup"""
    global DIALING_QUEUE_CHANNEL_ID, CURRENT_DASHBOARD_DAY
    try:
        print(f'{bot.user} has connected to Discord!')
        
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
    except Exception as e:
        print(f"❌ Error: {e}")

@bot.event
async def on_reaction_add(reaction, user):
    """Handle 👍 reactions for confirmation"""
    if user == bot.user:
        return
    
    if reaction.emoji != '👍':
        return
    
    # Check if this is a DM
    if not isinstance(reaction.message.channel, discord.DMChannel):
        return
    
    # Find matching pending confirmation
    for key, member_id in list(PENDING_CONFIRMATIONS.items()):
        if member_id == user.id:
            day, time_slot = key.split('_')
            if mark_as_called(user.id, day, time_slot):
                await user.send(f"✅ Confirmed! You're now calling **{day} {time_slot}**. You have 60 minutes.")
                del PENDING_CONFIRMATIONS[key]
            return

# ============================================================================
# SLASH COMMANDS
# ============================================================================

@bot.tree.command(name="book", description="Book a calling slot")
async def book_command(interaction: discord.Interaction):
    """Book a slot"""
    await interaction.response.defer(ephemeral=True)
    
    member = interaction.user
    member_id = member.id
    
    future_days = get_future_days()
    
    class DayView(discord.ui.View):
        pass
    
    day_view = DayView()
    
    for day_obj in future_days:
        day_name = day_obj['name']
        label = f"{day_name}" + (" (Today)" if day_obj['is_today'] else "")
        
        async def day_callback(day_interaction: discord.Interaction, d=day_name):
            await day_interaction.response.defer()
            
            if member_has_slot_on_day(member_id, d):
                await day_interaction.followup.send(
                    f"❌ You already have a slot on {d}. One per day!",
                    ephemeral=True
                )
                return
            
            available = get_available_slots(d)
            
            if not available:
                await day_interaction.followup.send(
                    f"❌ All slots full for {d}.",
                    ephemeral=True
                )
                return
            
            class TimeView(discord.ui.View):
                pass
            
            time_view = TimeView()
            
            for time_slot in available:
                async def time_callback(time_interaction: discord.Interaction, ts=time_slot, day_name=d):
                    if create_booking(member_id, str(member), day_name, ts):
                        embed = discord.Embed(
                            title="✅ BOOKED!",
                            description=f"You're booked for **{day_name} {ts}**",
                            color=discord.Color.green()
                        )
                        embed.add_field(
                            name="⏰ Time",
                            value=f"{ts} Bangladesh Time",
                            inline=False
                        )
                        embed.add_field(
                            name="📍 What to expect",
                            value="30 min before: Reminder DM\nAt slot time: DM with react 👍 to confirm\nYou have 60 minutes to call",
                            inline=False
                        )
                        await time_interaction.response.send_message(embed=embed, ephemeral=True)
                    else:
                        await time_interaction.response.send_message(
                            f"❌ Slot taken! Try another.",
                            ephemeral=True
                        )
                
                button = discord.ui.Button(label=time_slot, style=discord.ButtonStyle.success)
                button.callback = time_callback
                time_view.add_item(button)
            
            embed = discord.Embed(
                title=f"Select time for {d}",
                description="Pick a slot:",
                color=discord.Color.blue()
            )
            await day_interaction.followup.send(embed=embed, view=time_view, ephemeral=True)
        
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
        button.callback = day_callback
        day_view.add_item(button)
    
    embed = discord.Embed(
        title="📅 Select a day",
        description="Pick Mon-Fri:",
        color=discord.Color.blue()
    )
    await interaction.followup.send(embed=embed, view=day_view, ephemeral=True)

@bot.tree.command(name="myslot", description="See your booked slots")
async def myslot_command(interaction: discord.Interaction):
    """Show member's slots"""
    member = interaction.user
    member_id = member.id
    slots = get_member_slots(member_id)
    
    if not slots:
        embed = discord.Embed(
            title="📅 Your Slots",
            description="No upcoming bookings.",
            color=discord.Color.greyple()
        )
    else:
        embed = discord.Embed(
            title="📅 Your Upcoming Slots",
            description="Your bookings:",
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
        title="📚 Zero2Hire Calling Scheduler",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="/book",
        value="Book a 1-hour slot (Mon-Fri, 7pm-3am)\nOne slot per day!",
        inline=False
    )
    embed.add_field(
        name="/myslot",
        value="See your upcoming bookings",
        inline=False
    )
    embed.add_field(
        name="📍 How it works",
        value="1. Book via /book\n2. 30 min before: Get reminder DM\n3. At your time: DM with React 👍 to confirm\n4. Call for 60 minutes\n5. Auto-marked complete",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================================
# BACKGROUND TASKS
# ============================================================================

@tasks.loop(minutes=1)
async def send_reminders():
    """Send DM reminders"""
    now = get_bd_time()
    hour = now.hour
    minute = now.minute
    
    slot_hours = {
        19: '7pm', 20: '8pm', 21: '9pm', 22: '10pm', 23: '11pm',
        0: '12am', 1: '1am', 2: '2am', 3: '3am'
    }
    
    # 30 minutes before
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
                            title=f"⏰ Reminder: {next_slot}",
                            description=f"Your slot is in 30 minutes!\n\n**{calling_day} {next_slot}**\n\nGet ready!",
                            color=discord.Color.blue()
                        )
                        await user.send(embed=embed)
                    except:
                        pass
    
    # At slot start
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
                            title=f"🎙️ YOUR TURN NOW!",
                            description=f"**{calling_day} {current_slot}**\n\nReact 👍 to confirm you're calling!\n\nYou have 10 minutes to react.",
                            color=discord.Color.green()
                        )
                        msg = await user.send(embed=embed)
                        await msg.add_reaction('👍')
                        
                        # Track this pending confirmation
                        PENDING_CONFIRMATIONS[f"{calling_day}_{current_slot}"] = int(booking['Member_ID'])
                    except:
                        pass

@tasks.loop(minutes=1)
async def check_confirmations():
    """Check for no-shows (10 min no reaction)"""
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
    if key in PENDING_CONFIRMATIONS:
        # Still pending = no-show
        booking = get_slot_booking(calling_day, current_slot)
        if booking and booking['Status'] == 'booked':
            mark_as_no_show(calling_day, current_slot)
            
            try:
                user = await bot.fetch_user(int(booking['Member_ID']))
                embed = discord.Embed(
                    title="⚠️ No-Show",
                    description=f"You didn't confirm for **{calling_day} {current_slot}**.\n\nMarked as no-show.",
                    color=discord.Color.orange()
                )
                await user.send(embed=embed)
            except:
                pass
            
            del PENDING_CONFIRMATIONS[key]

@tasks.loop(minutes=1)
async def auto_complete_slots():
    """Auto-complete slots after 60 minutes"""
    now = get_bd_time()
    hour = now.hour
    minute = now.minute
    
    if minute != 0:
        return
    
    if hour == 0:
        prev_hour = 23
    else:
        prev_hour = hour - 1
    
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
                description=f"Great work on **{calling_day} {completed_slot}**!\n\n60 minutes logged.",
                color=discord.Color.green()
            )
            await user.send(embed=embed)
        except:
            pass

@tasks.loop(seconds=2)
async def update_dashboard():
    """Update dashboard"""
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
        
        # Build day navigation
        class DayNav(discord.ui.View):
            pass
        
        view = DayNav()
        future_days = get_future_days()
        
        for day_obj in future_days:
            day_name = day_obj['name']
            label = f"{day_name}" + (" (Today)" if day_obj['is_today'] else "")
            
            async def day_btn(nav_interaction: discord.Interaction, d=day_name):
                global CURRENT_DASHBOARD_DAY
                CURRENT_DASHBOARD_DAY = d
                await nav_interaction.response.defer()
            
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
            button.callback = day_btn
            view.add_item(button)
        
        # Edit or create dashboard message
        if DASHBOARD_MESSAGE_ID:
            try:
                msg = await channel.fetch_message(DASHBOARD_MESSAGE_ID)
                await msg.edit(embed=embed, view=view)
                return
            except:
                DASHBOARD_MESSAGE_ID = None
        
        # Find existing dashboard
        async for msg in channel.history(limit=20):
            if msg.author == bot.user and msg.embeds:
                if "ZERO2HIRE CALLING SCHEDULER" in msg.embeds[0].title:
                    DASHBOARD_MESSAGE_ID = msg.id
                    await msg.edit(embed=embed, view=view)
                    return
        
        # Create new
        msg = await channel.send(embed=embed, view=view)
        await msg.pin()
        DASHBOARD_MESSAGE_ID = msg.id
    
    except Exception as e:
        print(f"Dashboard error: {e}")

# ============================================================================
# RUN BOT
# ============================================================================

if __name__ == '__main__':
    bot.run(TOKEN)
