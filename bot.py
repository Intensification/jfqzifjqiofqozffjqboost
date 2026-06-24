import os
import asyncio
import sqlite3
import re
from datetime import datetime, timedelta, timezone
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Button, View, Select
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID"))
BLACKLIST_ROLE_ID = int(os.getenv("BLACKLIST_ROLE_ID"))

REVIEW_LOG_CHANNEL_ID = int(os.getenv("REVIEW_LOG_CHANNEL_ID"))
VERIFIED_CUSTOMER_ROLE_ID = int(os.getenv("VERIFIED_CUSTOMER_ROLE_ID"))

# New Menu Rates Configuration Matrix
PRICES = {
    "7x": {"1 Month": "$5.00 / £4.00", "3 Months": "$11.00 / £9.00", "6 Months": "$20.00 / £16.00"},
    "14x": {"1 Month": "$8.00 / £6.50", "3 Months": "$18.00 / £14.50", "6 Months": "$32.00 / £26.00"},
}

CRYPTO_ADDRESSES = {
    "BTC": os.getenv("BTC_ADDRESS"),
    "LTC": os.getenv("LTC_ADDRESS"),
    "ETH": os.getenv("ETH_ADDRESS"),
}

# --- Database Initialization ---
DB_FILE = "database.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS tickets 
                 (channel_id INTEGER PRIMARY KEY, user_id INTEGER, status TEXT, last_msg_at TEXT, claimed_by INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS order_details
                 (channel_id INTEGER PRIMARY KEY, package_tier TEXT, duration TEXT, price TEXT, crypto_used TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS warnings 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, guild_id INTEGER, reason TEXT, moderator_id INTEGER, timestamp TEXT)''')
    c.execute('''INSERT OR IGNORE INTO config (key, value) VALUES ('orders_completed', '0')''')
    conn.commit()
    conn.close()

init_db()

def get_db_value(key, default="0"):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default

def set_db_value(key, value):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def increment_orders():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key='orders_completed'")
    val = int(c.fetchone()[0]) + 1
    c.execute("UPDATE config SET value=? WHERE key='orders_completed'", (str(val),))
    conn.commit()
    conn.close()
    return val

# --- Helper Moderation Log Broadcaster ---
async def send_mod_log(guild: discord.Guild, action: str, moderator: discord.Member, target: str, reason: str):
    log_channel = guild.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return
    embed = discord.Embed(title=f"🛡️ Mod Log — {action}", color=0xE74C3C, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Target Entity", value=target, inline=True)
    embed.add_field(name="Responsible Agent", value=moderator.mention, inline=True)
    embed.add_field(name="Reason Provided", value=reason, inline=False)
    await log_channel.send(embed=embed)

# --- Parse Duration Text ---
def parse_duration(duration_str: str) -> int:
    match = re.match(r"(\d+)([smhd])", duration_str.lower())
    if not match:
        return 0
    amount, unit = int(match.group(1)), match.group(2)
    if unit == "s": return amount
    if unit == "m": return amount * 60
    if unit == "h": return amount * 3600
    if unit == "d": return amount * 86400
    return 0

# --- Helper Function: Generate Live Transcript Array ---
async def build_transcript_file(channel: discord.TextChannel):
    transcript = f"--- Transcript for Ticket Channel: {channel.name} ---\n"
    async for message in channel.history(limit=1000, oldest_first=True):
        time_str = message.created_at.strftime('%Y-%m-%d %H:%M:%S')
        transcript += f"[{time_str}] {message.author}: {message.content}\n"
        if message.attachments:
            for attach in message.attachments:
                transcript += f"   [Attachment: {attach.url}]\n"
                
    file_path = f"transcript-{channel.name}.txt"
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(transcript)
    return file_path

# --- Helper Function: Standard Ticket Deletion Database Cleanup ---
def purge_ticket_db(channel_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM tickets WHERE channel_id=?", (channel_id,))
    c.execute("DELETE FROM order_details WHERE channel_id=?", (channel_id,))
    conn.commit()
    conn.close()

# --- Custom Bot Class ---
class NexusBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix=",", intents=intents)

    async def setup_hook(self):
        self.loop.create_task(self.initialize_views())
        self.inactivity_check.start()
        
        try:
            guild_object = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild_object)
            synced = await self.tree.sync(guild=guild_object)
            print(f"🌲 Clean Sync: Registered {len(synced)} slash commands directly to Guild {GUILD_ID}.")
        except Exception as e:
            print(f"Failed to sync commands: {e}")

    async def initialize_views(self):
        self.add_view(MainTicketPanel())

    @tasks.loop(minutes=10)
    async def inactivity_check(self):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        threshold = datetime.now(timezone.utc) - timedelta(hours=24)
        c.execute("SELECT channel_id, user_id FROM tickets WHERE status='open'")
        open_tickets = c.fetchall()
        
        for ch_id, u_id in open_tickets:
            channel = self.get_channel(ch_id)
            if not channel:
                c.execute("DELETE FROM tickets WHERE channel_id=?", (ch_id,))
                continue
                
            last_msg_time = None
            async for msg in channel.history(limit=1):
                last_msg_time = msg.created_at
            
            if last_msg_time and last_msg_time < threshold:
                await channel.send("⏳ **Ticket automatically closing due to 24 hours of absolute inactivity.**")
                purge_ticket_db(channel.id)
                await channel.delete()
                
        conn.commit()
        conn.close()

bot = NexusBot()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} | Systems Operational.")

@bot.event
async def on_member_join(member):
    autorole_id = get_db_value(f"autorole_{member.guild.id}", None)
    if autorole_id:
        role = member.guild.get_role(int(autorole_id))
        if role:
            try:
                await member.add_roles(role)
            except Exception as e:
                print(f"Failed to apply autorole to {member.name}: {e}")

    welcome_ch_id = get_db_value(f"welcome_{member.guild.id}", None)
    if welcome_ch_id:
        channel = member.guild.get_channel(int(welcome_ch_id))
        if channel:
            await channel.send(f"Welcome To NexusBoosts {member.mention}!")

# --- Main Ticket Dashboard View ---
class MainTicketPanel(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.secondary, emoji="📩", custom_id="persistent_create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: Button):
        if interaction.user.get_role(BLACKLIST_ROLE_ID):
            return await interaction.response.send_message("❌ You are currently blacklisted from creating support tickets.", ephemeral=True)

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT channel_id FROM tickets WHERE user_id=? AND status='open'", (interaction.user.id,))
        existing = c.fetchone()
        
        if existing:
            conn.close()
            return await interaction.response.send_message("❌ You already have an active checkout ticket open.", ephemeral=True)

        guild = interaction.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }

        channel_name = f"ticket-{interaction.user.name}"
        ticket_channel = await guild.create_text_channel(name=channel_name, overwrites=overwrites)

        c.execute("INSERT INTO tickets VALUES (?, ?, 'open', ?, NULL)", (ticket_channel.id, interaction.user.id, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()

        await interaction.response.send_message(f"Ticket opened! Check out {ticket_channel.mention}", ephemeral=True)

        embed = discord.Embed(
            title="nexusboosts — order selection",
            description="Welcome to your private checkout window. Please select the boosting tier you would like to purchase below.",
            color=0x2B2D31,
        )
        await ticket_channel.send(content=interaction.user.mention, embed=embed, view=OrderSelectionView())

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(f"⏳ Slow down! Please wait {error.retry_after:.1f}s before trying again.", ephemeral=True)

class OrderSelectionView(View):
    def __init__(self):
        super().__init__(timeout=None)

    async def disable_all_except_close(self, view: View):
        for item in view.children:
            if hasattr(item, "label") and item.label != "Close Ticket":
                item.disabled = True

    @discord.ui.button(label="7x Server Boosts", style=discord.ButtonStyle.secondary, emoji="✨")
    async def seven_boosts(self, interaction: discord.Interaction, button: Button):
        await self.disable_all_except_close(self)
        await interaction.message.edit(view=self)
        embed = discord.Embed(title="📦 Select Duration — 7x Boosts", description="Choose your deployment window:", color=0x2B2D31)
        await interaction.response.send_message(embed=embed, view=DurationSelectionView(package_tier="7x"))

    @discord.ui.button(label="14x Server Boosts", style=discord.ButtonStyle.secondary, emoji="🚀")
    async def fourteen_boosts(self, interaction: discord.Interaction, button: Button):
        await self.disable_all_except_close(self)
        await interaction.message.edit(view=self)
        embed = discord.Embed(title="📦 Select Duration — 14x Boosts", description="Choose your deployment window:", color=0x2B2D31)
        await interaction.response.send_message(embed=embed, view=DurationSelectionView(package_tier="14x"))

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒")
    async def close_ticket(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("Closing ticket channel space immediately...")
        purge_ticket_db(interaction.channel.id)
        await asyncio.sleep(2)
        await interaction.channel.delete()


class DurationSelectionView(View):
    def __init__(self, package_tier: str):
        super().__init__(timeout=None)
        self.package_tier = package_tier

    async def handle_duration_selection(self, interaction: discord.Interaction, label: str):
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

        price = PRICES[self.package_tier][label]
        embed = discord.Embed(
            title="💳 Payment Methods Available",
            description=f"Selected: **{self.package_tier} Boosts for {label}**\nCost: **{price}**\n\nChoose payment gateway:",
            color=0x2B2D31,
        )
        await interaction.response.send_message(embed=embed, view=PaymentSelectionView(price=price, tier=self.package_tier, duration=label))

    @discord.ui.button(label="1 Month", style=discord.ButtonStyle.secondary)
    async def one_month(self, interaction: discord.Interaction, button: Button):
        await self.handle_duration_selection(interaction, "1 Month")

    @discord.ui.button(label="3 Months", style=discord.ButtonStyle.secondary)
    async def three_months(self, interaction: discord.Interaction, button: Button):
        await self.handle_duration_selection(interaction, "3 Months")

    @discord.ui.button(label="6 Months", style=discord.ButtonStyle.secondary)
    async def six_months(self, interaction: discord.Interaction, button: Button):
        await self.handle_duration_selection(interaction, "6 Months")


class PaymentSelectionView(View):
    def __init__(self, price: str, tier: str, duration: str):
        super().__init__(timeout=None)
        self.price = price
        self.tier = tier
        self.duration = duration

    async def send_invoice(self, interaction: discord.Interaction, crypto_type: str):
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO order_details VALUES (?, ?, ?, ?, ?)", 
                  (interaction.channel.id, self.tier, self.duration, self.price, crypto_type))
        conn.commit()
        conn.close()

        address = CRYPTO_ADDRESSES[crypto_type]
        embed = discord.Embed(title=f"💸 Complete Payment — {crypto_type}", description=f"Send exact payment equivalent of **{self.price}**.", color=0x2B2D31)
        embed.add_field(name="Address", value=f"`{address}`", inline=False)
        embed.set_footer(text="Upload payment validation confirmation/screenshot here when completed.")
        await interaction.response.send_message(embed=embed)

        log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(title="⚡ Order Awaiting Inbound Payment", color=0x2B2D31)
            log_embed.add_field(name="Buyer", value=interaction.user.mention, inline=True)
            log_embed.add_field(name="Channel Link", value=interaction.channel.mention, inline=True)
            log_embed.add_field(name="Details", value=f"{self.tier} Boosts ({self.duration}) | **{crypto_type}**", inline=False)
            
            await log_channel.send(content=f"<@&{STAFF_ROLE_ID}>", embed=log_embed, view=StaffOrderConfirmationView(buyer_id=interaction.user.id, detail_str=f"{self.tier} ({self.duration})", channel_id=interaction.channel.id))

    @discord.ui.button(label="BTC", style=discord.ButtonStyle.primary)
    async def pay_btc(self, interaction: discord.Interaction, button: Button):
        await self.send_invoice(interaction, "BTC")

    @discord.ui.button(label="LTC", style=discord.ButtonStyle.success)
    async def pay_ltc(self, interaction: discord.Interaction, button: Button):
        await self.send_invoice(interaction, "LTC")

    @discord.ui.button(label="ETH", style=discord.ButtonStyle.secondary)
    async def pay_eth(self, interaction: discord.Interaction, button: Button):
        await self.send_invoice(interaction, "ETH")


class StaffOrderConfirmationView(View):
    def __init__(self, buyer_id: int, detail_str: str, channel_id: int):
        super().__init__(timeout=None)
        self.buyer_id = buyer_id
        self.detail_str = detail_str
        self.channel_id = channel_id

    @discord.ui.button(label="Confirm Paid", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_paid(self, interaction: discord.Interaction, button: Button):
        button.disabled = True
        await interaction.message.edit(view=self)
        
        new_count = increment_orders()
        await interaction.response.send_message(f"🟢 Order validated by {interaction.user.mention}. Internal Order Counter updated to: **{new_count}**")

        buyer_member = interaction.guild.get_member(self.buyer_id)
        if buyer_member:
            role_object = interaction.guild.get_role(VERIFIED_CUSTOMER_ROLE_ID)
            if role_object:
                try:
                    await buyer_member.add_roles(role_object)
                except Exception as e:
                    print(f"Failed allocating customer role permissions: {e}")

        target_ch = interaction.guild.get_channel(self.channel_id)
        if target_ch:
            embed = discord.Embed(title="🎉 Payment Confirmed!", description="Your transaction has been cleared by administration. Deployment processing will complete momentarily.", color=0x2ECC71)
            await target_ch.send(content=f"<@{self.buyer_id}>", embed=embed)
            await target_ch.send(view=ReviewSystemView(buyer_id=self.buyer_id))

class ReviewSystemView(View):
    def __init__(self, buyer_id: int):
        super().__init__(timeout=None)
        self.buyer_id = buyer_id

    @discord.ui.select(
        placeholder="⭐ Rate your experience with NexusBoosts!",
        options=[
            discord.SelectOption(label="⭐⭐⭐⭐⭐ 5 Stars - Perfect", value="5"),
            discord.SelectOption(label="⭐⭐⭐⭐ 4 Stars - Great", value="4"),
            discord.SelectOption(label="⭐⭐⭐ 3 Stars - Average", value="3"),
            discord.SelectOption(label="⭐⭐ 2 Stars - Subpar", value="2"),
            discord.SelectOption(label="⭐ 1 Star - Poor", value="1"),
        ]
    )
    async def select_rating(self, interaction: discord.Interaction, select: Select):
        if interaction.user.id != self.buyer_id:
            return await interaction.response.send_message("❌ Only the order purchaser can fill out this submission.", ephemeral=True)
            
        select.disabled = True
        await interaction.message.edit(view=self)
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT package_tier, duration, price, crypto_used FROM order_details WHERE channel_id=?", (interaction.channel.id,))
        row = c.fetchone()
        conn.close()

        pkg = row[0] if row else "Unknown Tier"
        dur = row[1] if row else "Unknown Duration"
        prc = row[2] if row else "N/A"
        crp = row[3] if row else "N/A"

        review_channel = interaction.guild.get_channel(REVIEW_LOG_CHANNEL_ID)
        if review_channel:
            rev_embed = discord.Embed(title="✨ New Feedback Verification Record", color=0xF1C40F)
            rev_embed.add_field(name="User Node Identity", value=interaction.user.mention, inline=True)
            rev_embed.add_field(name="Rating Evaluated", value=f"**{select.values[0]} / 5 Stars**", inline=True)
            rev_embed.add_field(name="Item Order Package", value=f"`{pkg} Boosts ({dur})`", inline=False)
            rev_embed.add_field(name="Cost Verified", value=f"`{prc}`", inline=True)
            rev_embed.add_field(name="Currency Processing", value=f"`{crp}`", inline=True)
            await review_channel.send(embed=rev_embed)
            
        await interaction.response.send_message("💖 Thank you for your feedback validation! Your response has been securely filed.")


# --- Unified Hybrid Command Matrix (Dual operational on both ',' and '/') ---

@bot.hybrid_command(name="transcript", description="Manually requests and pipes the conversation log file into the active channel.")
@commands.has_permissions(manage_messages=True)
async def transcript(ctx: commands.Context):
    await ctx.defer()
    path = await build_transcript_file(ctx.channel)
    await ctx.send(content="📄 **Manual Channel Transcript Compiled:**", file=discord.File(path))
    try:
        os.remove(path)
    except:
        pass

@bot.hybrid_command(name="setstatus", description="Lets staff update a custom bot status/presence message on demand.")
@commands.has_permissions(manage_messages=True)
async def setstatus(ctx: commands.Context, *, status_text: str):
    await bot.change_presence(activity=discord.CustomActivity(name=status_text))
    await ctx.send(f"✅ Status payload revised to: *{status_text}*")

@bot.hybrid_command(name="slowmode", description="Sets slowmode on a channel to prevent message spam from people.")
@commands.has_permissions(manage_channels=True)
async def slowmode(ctx: commands.Context, seconds: int):
    await ctx.channel.edit(slowmode_delay=seconds)
    await ctx.send(f"⏱️ Slowmode set to `{seconds}` seconds for this channel.")

@bot.hybrid_command(name="warn", description="Issues a warning to a user, logged silently to a mod-log channel.")
@commands.has_permissions(manage_messages=True)
async def warn(ctx: commands.Context, user: discord.Member, *, reason: str = "No reason provided."):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO warnings (user_id, guild_id, reason, moderator_id, timestamp) VALUES (?, ?, ?, ?, ?)",
              (user.id, ctx.guild.id, reason, ctx.author.id, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    
    await ctx.send(f"⚠️ {user.mention} has been warned successfully.", ephemeral=True)
    await send_mod_log(ctx.guild, "User Warning Issued", ctx.author, user.mention, reason)

@bot.hybrid_command(name="warnings", description="Displays how many warnings a user has accumulated.")
@commands.has_permissions(manage_messages=True)
async def warnings(ctx: commands.Context, user: discord.Member):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT reason, moderator_id, timestamp FROM warnings WHERE user_id=? AND guild_id=?", (user.id, ctx.guild.id))
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        return await ctx.send(f"✅ {user.mention} holds a pristine file with 0 active warnings.")
        
    embed = discord.Embed(title=f"📋 Infraction History: {user.name}", color=0xF1C40F)
    for index, (reason, mod_id, ts) in enumerate(rows, 1):
        parsed_time = datetime.fromisoformat(ts).strftime('%Y-%m-%d %H:%M')
        embed.add_field(name=f"Infraction #{index} ({parsed_time})", value=f"**Reason:** {reason}\n**Issued By:** <@{mod_id}>", inline=False)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="clearwarnings", description="Removes all warnings from a user, admin only.")
@commands.has_permissions(administrator=True)
async def clearwarnings(ctx: commands.Context, user: discord.Member):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM warnings WHERE user_id=? AND guild_id=?", (user.id, ctx.guild.id))
    conn.commit()
    conn.close()
    
    await ctx.send(f"♻️ Cleared all warning histories completely for {user.mention}.")
    await send_mod_log(ctx.guild, "Warning Track Purged", ctx.author, user.mention, "Administrative file reset.")

@bot.hybrid_command(name="kick", description="Kicks a user with a reason, logged to mod-log channel.")
@commands.has_permissions(kick_members=True)
async def kick(ctx: commands.Context, user: discord.Member, *, reason: str = "No reason provided."):
    await user.kick(reason=reason)
    await ctx.send(f"👢 successfully booted {user.name} from the server.")
    await send_mod_log(ctx.guild, "Member Kicked", ctx.author, f"{user.name} ({user.id})", reason)

@bot.hybrid_command(name="ban", description="Bans a user with a reason, logged to mod-log channel.")
@commands.has_permissions(ban_members=True)
async def ban(ctx: commands.Context, user: discord.Member, *, reason: str = "No reason provided."):
    await user.ban(reason=reason)
    await ctx.send(f"🔨 Perm-banned {user.name} from the guild infrastructure.")
    await send_mod_log(ctx.guild, "User Banned", ctx.author, f"{user.name} ({user.id})", reason)

@bot.hybrid_command(name="unban", description="Unbans a user by ID.")
@commands.has_permissions(ban_members=True)
async def unban(ctx: commands.Context, user_id: str, *, reason: str = "No reason provided."):
    try:
        uid = int(user_id)
        user_obj = await bot.fetch_user(uid)
        await ctx.guild.unban(user_obj, reason=reason)
        await ctx.send(f"🔓 Restored structural access to user ID: {user_id}.")
        await send_mod_log(ctx.guild, "User Unbanned", ctx.author, f"{user_obj.name} ({user_id})", reason)
    except Exception:
        await ctx.send("❌ Valid Target entry not located. Verify User ID configuration syntax.", ephemeral=True)

@bot.hybrid_command(name="mute", description="Times out a user for a specified duration e.g. ,mute @user 10m")
@commands.has_permissions(moderate_members=True)
async def mute(ctx: commands.Context, user: discord.Member, duration: str, *, reason: str = "No reason provided."):
    seconds = parse_duration(duration)
    if seconds <= 0:
        return await ctx.send("❌ Invalid timeframe notation. Format examples: `10m`, `2h`, `1d`.", ephemeral=True)
        
    delta = timedelta(seconds=seconds)
    await user.timeout(delta, reason=reason)
    await ctx.send(f"🔇 Put {user.mention} on timeout for {duration}.")
    await send_mod_log(ctx.guild, "Timeout / Mute Action", ctx.author, user.mention, f"Duration: {duration} | Reason: {reason}")

@bot.hybrid_command(name="unmute", description="Removes timeout restrictions from a user.")
@commands.has_permissions(moderate_members=True)
async def unmute(ctx: commands.Context, user: discord.Member, *, reason: str = "No reason provided."):
    await user.timeout(None, reason=reason)
    await ctx.send(f"🔊 Restored voice/text privileges to {user.mention}.")
    await send_mod_log(ctx.guild, "Timeout Terminated", ctx.author, user.mention, reason)

@bot.hybrid_command(name="lock", description="Locks down writing permissions on the active channel for general members.")
@commands.has_permissions(manage_channels=True)
async def lock(ctx: commands.Context):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send("🔒 **Channel locked down.** Structural permissions modified.")

@bot.hybrid_command(name="unlock", description="Unlocks channel text writing loops back to normal parameters.")
@commands.has_permissions(manage_channels=True)
async def unlock(ctx: commands.Context):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=None)
    await ctx.send("🔓 **Channel unlocked.** Standard interaction channels restored.")

@bot.hybrid_command(name="role", description="Adds or removes a role from a user e.g. ,role @user RoleName")
@commands.has_permissions(manage_roles=True)
async def role(ctx: commands.Context, user: discord.Member, *, role_name: str):
    target_role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not target_role:
        return await ctx.send(f"❌ Role target string `{role_name}` could not be resolved.", ephemeral=True)
        
    if target_role in user.roles:
        await user.remove_roles(target_role)
        await ctx.send(f"🚫 Stripped role {target_role.name} from profile: {user.mention}")
    else:
        await user.add_roles(target_role)
        await ctx.send(f"✅ Allocated role {target_role.name} to target user profile: {user.mention}")

@bot.hybrid_command(name="autorole", description="Configure the automatic role assigned to joining members.")
@commands.has_permissions(administrator=True)
async def autorole(ctx: commands.Context, role: discord.Role):
    set_db_value(f"autorole_{ctx.guild.id}", role.id)
    await ctx.send(f"✨ **Success:** New users will automatically receive the {role.mention} role upon joining.")

@bot.hybrid_command(name="purge", description="Deletes a specified volume of historical messages from the active channel.")
@commands.has_permissions(manage_messages=True)
async def purge(ctx: commands.Context, amount: int):
    if amount < 1:
        return await ctx.send("❌ Amount must be greater than zero.", ephemeral=True)
    deleted_items = await ctx.channel.purge(limit=amount)
    await ctx.send(f"🧹 Cleaned up and deleted `{len(deleted_items)}` messages.", delete_after=5.0)

@bot.hybrid_command(name="welcome", description="Configure server greeting channel layout.")
@commands.has_permissions(administrator=True)
async def welcome(ctx: commands.Context, channel: discord.TextChannel):
    set_db_value(f"welcome_{ctx.guild.id}", channel.id)
    await ctx.send(f"✨ **Success:** Global greetings piped directly into {channel.mention}.")

@bot.hybrid_command(name="setup_ticket", description="Deploys the production framework panel layout configuration dashboard.")
@commands.has_permissions(administrator=True)
async def setup_ticket(ctx: commands.Context):
    embed = discord.Embed(
        title="═══ nexusboosts — premium menu ═══",
        description=(
            "\n📦 [ package level 2 ] — 7x server boosts\n"
            "├─ 🕒 1 month  │ $5.00 / £4.00\n"
            "├─ 🗓️ 3 months │ $11.00 / £9.00\n"
            "└─ 💎 6 months │ $20.00 / £16.00\n\n"
            "💎 [ package level 3 ] — 14x server boosts\n"
            "├─ 🕒 1 month  │ $8.00 / £6.50\n"
            "├─ 🗓️ 3 months │ $18.00 / £14.50\n"
            "└─ 💎 6 months │ $32.00 / £26.00\n"
            "└─ 🛡️ full replacement warranty included\n\n"
            "💳 [ payment methods ]\n"
            "└─ 🪙 crypto (btc, ltc, eth)\n\n"
            "Ready to order? Click the button below to secure your boosts."
        ),
        color=0x2B2D31,
    )
    await ctx.send(embed=embed, view=MainTicketPanel())

@bot.hybrid_command(name="rename", description="Rename current session room interface pipeline mid conversation.")
@commands.has_permissions(manage_channels=True)
async def rename(ctx: commands.Context, new_name: str):
    await ctx.channel.edit(name=new_name.lower().replace(" ", "-"))
    await ctx.send(f"✅ Context channel interface assigned locally to: `{new_name}`")

@bot.hybrid_command(name="claim", description="Claim administrative handling responsibility ownership rights over active room.")
@commands.has_permissions(manage_messages=True)
async def claim(ctx: commands.Context):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT claimed_by FROM tickets WHERE channel_id=?", (ctx.channel.id,))
    row = c.fetchone()
    
    if not row:
        conn.close()
        return await ctx.send("❌ Command execution domain failed: This channel is not registered as an active checkout ticket.", ephemeral=True)
    if row[0] is not None:
        conn.close()
        return await ctx.send("❌ This support thread session has already been claimed by another specialist handling agent.", ephemeral=True)
        
    c.execute("UPDATE tickets SET claimed_by=? WHERE channel_id=?", (ctx.author.id, ctx.channel.id))
    conn.commit()
    conn.close()
    
    clean_name = ctx.channel.name.replace("-claimed", "")
    await ctx.channel.edit(name=f"{clean_name}-claimed-{ctx.author.name}")
    await ctx.send(f"📋 Ticket handling assignments officially locked down by user: {ctx.author.mention}")

@bot.hybrid_command(name="unclaim", description="Relinquish processing ownership assignments safely.")
@commands.has_permissions(manage_messages=True)
async def unclaim(ctx: commands.Context):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT claimed_by FROM tickets WHERE channel_id=?", (ctx.channel.id,))
    row = c.fetchone()
    
    if not row or row[0] is None:
        conn.close()
        return await ctx.send("❌ Ticket context state reports that this window is not currently claimed.", ephemeral=True)
        
    c.execute("UPDATE tickets SET claimed_by=NULL WHERE channel_id=?", (ctx.channel.id,))
    conn.commit()
    conn.close()
    
    base_name = ctx.channel.name.split("-claimed-")[0]
    await ctx.channel.edit(name=base_name)
    await ctx.send("🔓 Processing state reset to unassigned. Ticket opened back up to staff pooling.")

@bot.hybrid_command(name="stats", description="Query total historical volumetric processing transaction data loops.")
async def stats(ctx: commands.Context):
    count = get_db_value("orders_completed", "0")
    embed = discord.Embed(title="📊 NexusBoosts Performance Metrics", color=0x3498DB)
    embed.add_field(name="Fulfilled Volume Orders Counter", value=f"🚀 **{count} Total Orders Completed**", inline=False)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="close", description="De-provision access pipeline context framework via automated administrative cleanup routine.")
@commands.has_permissions(manage_channels=True)
async def close(ctx: commands.Context):
    await ctx.send("Closing connection space layout mapping loops completely...")
    purge_ticket_db(ctx.channel.id)
    await asyncio.sleep(2)
    await ctx.channel.delete()

@bot.hybrid_command(name="adduser", description="Explicitly add external tracking profile entity context to channel array mappings.")
@commands.has_permissions(manage_channels=True)
async def adduser(ctx: commands.Context, user: discord.Member):
    await ctx.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True)
    await ctx.send(f"✅ Access privilege matrix opened up safely to input profile: {user.mention}")

@bot.hybrid_command(name="removeuser", description="De-authorize target profile identity clearance parameters immediately.")
@commands.has_permissions(manage_channels=True)
async def removeuser(ctx: commands.Context, user: discord.Member):
    await ctx.channel.set_permissions(user, overwrite=None)
    await ctx.send(f"🚫 Revoked viewing clearances completely for candidate target: {user.mention}")

@bot.hybrid_command(name="blacklist", description="Restrict specific context profile tracking matrix parameters from initiating ticket setups.")
@commands.has_permissions(administrator=True)
async def blacklist(ctx: commands.Context, user: discord.Member):
    role = ctx.guild.get_role(BLACKLIST_ROLE_ID)
    if role:
        await user.add_roles(role)
        await ctx.send(f"🔒 Operational lock assigned. User profile {user.mention} is now completely blacklisted from tickets.")
    else:
        await ctx.send("❌ Configuration structure mismatch.", ephemeral=True)

@bot.hybrid_command(name="unblacklist", description="Restore operational creation clearance properties to input tracking target node mapping parameters.")
@commands.has_permissions(administrator=True)
async def unblacklist(ctx: commands.Context, user: discord.Member):
    role = ctx.guild.get_role(BLACKLIST_ROLE_ID)
    if role:
        await user.remove_roles(role)
        await ctx.send(f"🔓 Operational lock removed successfully. Clearance attributes re-established for target: {user.mention}")
    else:
        await ctx.send("❌ Config processing breakdown structural exception.", ephemeral=True)

bot.run(TOKEN)
