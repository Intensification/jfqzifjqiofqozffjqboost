import os
import asyncio
import sqlite3
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

# Target ID structures updated as requested via your configuration profile mapping 
REVIEW_LOG_CHANNEL_ID = 1519098165645541447
VERIFIED_CUSTOMER_ROLE_ID = 1519094176350732368

PRICES = {
    "7x": {"1m": "$3.50 / £3.00", "3m": "$8.00 / £6.50", "6m": "$15.00 / £12.00"},
    "14x": {"1m": "$6.00 / £5.00", "3m": "$15.00 / £12.00", "6m": "$28.00 / £22.00"},
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
    # Track order data parameters within state context to properly bind variables to feedback objects
    c.execute('''CREATE TABLE IF NOT EXISTS order_details
                 (channel_id INTEGER PRIMARY KEY, package_tier TEXT, duration TEXT, price TEXT, crypto_used TEXT)''')
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

# --- Custom Bot Class ---
class NexusBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="/", intents=intents)

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
        """Auto-close open tickets after 24 hours of inactivity"""
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
                await handle_ticket_close(channel, self)
                
        conn.commit()
        conn.close()

bot = NexusBot()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} | Systems Operational.")

@bot.event
async def on_member_join(member):
    welcome_ch_id = get_db_value(f"welcome_{member.guild.id}", None)
    if welcome_ch_id:
        channel = member.guild.get_channel(int(welcome_ch_id))
        if channel:
            await channel.send(f"Welcome To NexusBoosts {member.mention}!")

# --- Helper Function: Transcript & Cleanup ---
async def handle_ticket_close(channel: discord.TextChannel, client: commands.Bot):
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

    log_channel = channel.guild.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(
            title="🔒 Ticket Closed & Archived",
            description=f"Ticket channel `{channel.name}` has been successfully cleaned up.",
            color=0xD32F2F
        )
        await log_channel.send(embed=embed, file=discord.File(file_path))

    try:
        os.remove(file_path)
    except:
        pass

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM tickets WHERE channel_id=?", (channel.id,))
    c.execute("DELETE FROM order_details WHERE channel_id=?", (channel.id,))
    conn.commit()
    conn.close()

    await asyncio.sleep(3)
    await channel.delete()

# --- Main Ticket Dashboard View ---
class MainTicketPanel(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.secondary, emoji="📩", custom_id="persistent_create_ticket")
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
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
        await interaction.response.send_message("Closing ticket and compiling logs...")
        await handle_ticket_close(interaction.channel, bot)


class DurationSelectionView(View):
    def __init__(self, package_tier: str):
        super().__init__(timeout=None)
        self.package_tier = package_tier

    async def handle_duration_selection(self, interaction: discord.Interaction, duration_key: str, label: str):
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

        price = PRICES[self.package_tier][duration_key]
        embed = discord.Embed(
            title="💳 Payment Methods Available",
            description=f"Selected: **{self.package_tier} Boosts for {label}**\nCost: **{price}**\n\nChoose payment gateway:",
            color=0x2B2D31,
        )
        await interaction.response.send_message(embed=embed, view=PaymentSelectionView(price=price, tier=self.package_tier, duration=label))

    @discord.ui.button(label="1 Month", style=discord.ButtonStyle.secondary)
    async def one_month(self, interaction: discord.Interaction, button: Button):
        await self.handle_duration_selection(interaction, "1m", "1 Month")

    @discord.ui.button(label="3 Months", style=discord.ButtonStyle.secondary)
    async def three_months(self, interaction: discord.Interaction, button: Button):
        await self.handle_duration_selection(interaction, "3m", "3 Months")

    @discord.ui.button(label="6 Months", style=discord.ButtonStyle.secondary)
    async def six_months(self, interaction: discord.Interaction, button: Button):
        await self.handle_duration_selection(interaction, "6m", "6 Months")


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

        # Store transactional variables safely inside relational database memory state
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


# --- Staff Confirmation & Feedback Selection Matrix ---
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

        # Assign verified customer role directly to purchasing target ID structure as requested
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
        
        # Read the stored tracking details from runtime state database storage maps
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT package_tier, duration, price, crypto_used FROM order_details WHERE channel_id=?", (interaction.channel.id,))
        row = c.fetchone()
        conn.close()

        pkg = row[0] if row else "Unknown Tier"
        dur = row[1] if row else "Unknown Duration"
        prc = row[2] if row else "N/A"
        crp = row[3] if row else "N/A"

        # Routing performance values directly into the custom requested feedback log channel endpoint configuration
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

# --- Upgraded Slash Commands Matrix ---
@bot.tree.command(name="purge", description="Deletes a specified volume of historical content records from active channel configuration array.")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: int):
    if amount < 1:
        return await interaction.response.send_message("❌ Amount argument parameters must hold a sequence integer evaluation greater than zero.", ephemeral=True)
    
    # Defer interaction to provide leeway processing message structures over thread contexts safely
    await interaction.response.defer(ephemeral=True)
    deleted_items = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🧹 Cleaned up and deleted `{len(deleted_items)}` active items from historical context logs.", ephemeral=True)

@bot.tree.command(name="welcome", description="Configure server greeting channel layout.")
@app_commands.checks.has_permissions(administrator=True)
async def welcome(interaction: discord.Interaction, channel: discord.TextChannel):
    set_db_value(f"welcome_{interaction.guild.id}", channel.id)
    await interaction.response.send_message(f"✨ **Success:** Global greetings piped directly into {channel.mention}.", ephemeral=True)

@bot.tree.command(name="setup_ticket", description="Deploys the production framework panel layout configuration dashboard layout.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_ticket(interaction: discord.Interaction):
    embed = discord.Embed(
        title="═══ nexusboosts — premium menu ═══",
        description=(
            "✨ **[ package level 2 ]** — 7x server boosts\n├─ 1 month | $3.50 / £3.00\n├─ 3 months | $8.00 / £6.50\n└─ 6 months | $15.00 / £12.00\n\n"
            "🚀 **[ package level 3 ]** — 14x server boosts\n├─ 1 month | $6.00 / £5.00\n├─ 3 months | $15.00 / £12.00\n└─ 6 months | $28.00 / £22.00\n└─ *full replacement warranty included*\n\n"
            "💳 **[ payment methods ]**\n└─ crypto (btc, ltc, eth)\n\nReady to order? Click the button below to secure your boosts."
        ),
        color=0x2B2D31,
    )
    await interaction.response.send_message(embed=embed, view=MainTicketPanel())

@bot.tree.command(name="rename", description="Rename current session room interface pipeline mid conversation.")
@app_commands.checks.has_permissions(manage_channels=True)
async def rename(interaction: discord.Interaction, new_name: str):
    await interaction.channel.edit(name=new_name.lower().replace(" ", "-"))
    await interaction.response.send_message(f"✅ Context channel interface assigned locally to: `{new_name}`")

@bot.tree.command(name="claim", description="Claim administrative handling responsibility ownership rights over active room.")
@app_commands.checks.has_permissions(manage_messages=True)
async def claim(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT claimed_by FROM tickets WHERE channel_id=?", (interaction.channel.id,))
    row = c.fetchone()
    
    if not row:
        conn.close()
        return await interaction.response.send_message("❌ Command execution domain failed: This channel is not registered as an active checkout ticket.", ephemeral=True)
    if row[0] is not None:
        conn.close()
        return await interaction.response.send_message("❌ This support thread session has already been claimed by another specialist handling agent.", ephemeral=True)
        
    c.execute("UPDATE tickets SET claimed_by=? WHERE channel_id=?", (interaction.user.id, interaction.channel.id))
    conn.commit()
    conn.close()
    
    clean_name = interaction.channel.name.replace("-claimed", "")
    await interaction.channel.edit(name=f"{clean_name}-claimed-{interaction.user.name}")
    await interaction.response.send_message(f"📋 Ticket handling assignments officially locked down by user: {interaction.user.mention}")

@bot.tree.command(name="unclaim", description="Relinquish processing ownership assignments safely.")
@app_commands.checks.has_permissions(manage_messages=True)
async def unclaim(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT claimed_by FROM tickets WHERE channel_id=?", (interaction.channel.id,))
    row = c.fetchone()
    
    if not row or row[0] is None:
        conn.close()
        return await interaction.response.send_message("❌ Ticket context state reports that this window is not currently claimed.", ephemeral=True)
        
    c.execute("UPDATE tickets SET claimed_by=NULL WHERE channel_id=?", (interaction.channel.id,))
    conn.commit()
    conn.close()
    
    base_name = interaction.channel.name.split("-claimed-")[0]
    await interaction.channel.edit(name=base_name)
    await interaction.response.send_message("🔓 Processing state reset to unassigned. Ticket opened back up to staff pooling.")

@bot.tree.command(name="stats", description="Query total historical volumetric processing transaction data loops.")
async def stats(interaction: discord.Interaction):
    count = get_db_value("orders_completed", "0")
    embed = discord.Embed(title="📊 NexusBoosts Performance Metrics", color=0x3498DB)
    embed.add_field(name="Fulfilled Volume Orders Counter", value=f"🚀 **{count} Total Orders Completed**", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="close", description="De-provision access pipeline context framework via automated administrative cleanup routine.")
@app_commands.checks.has_permissions(manage_channels=True)
async def close(interaction: discord.Interaction):
    await interaction.response.send_message("Closing connection space layout mapping loops completely...")
    await handle_ticket_close(interaction.channel, bot)

@bot.tree.command(name="adduser", description="Explicitly add external tracking profile entity context to channel array mappings.")
@app_commands.checks.has_permissions(manage_channels=True)
async def adduser(interaction: discord.Interaction, user: discord.Member):
    await interaction.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True)
    await interaction.response.send_message(f"✅ Access privilege matrix opened up safely to input profile: {user.mention}")

@bot.tree.command(name="removeuser", description="De-authorize target profile identity clearance parameters immediately.")
@app_commands.checks.has_permissions(manage_channels=True)
async def removeuser(interaction: discord.Interaction, user: discord.Member):
    await interaction.channel.set_permissions(user, overwrite=None)
    await interaction.response.send_message(f"🚫 Revoked viewing clearances completely for candidate target: {user.mention}")

@bot.tree.command(name="blacklist", description="Restrict specific context profile tracking matrix parameters from initiating ticket setups.")
@app_commands.checks.has_permissions(administrator=True)
async def blacklist(interaction: discord.Interaction, user: discord.Member):
    role = interaction.guild.get_role(BLACKLIST_ROLE_ID)
    if role:
        await user.add_roles(role)
        await interaction.response.send_message(f"🔒 Operational lock assigned. User profile {user.mention} is now completely blacklisted from tickets.")
    else:
        await interaction.response.send_message("❌ Configuration structure mismatch: Blacklist Role Identifier could not be resolved from values mapping array.", ephemeral=True)

@bot.tree.command(name="unblacklist", description="Restore operational creation clearance properties to input tracking target node mapping parameters.")
@app_commands.checks.has_permissions(administrator=True)
async def unblacklist(interaction: discord.Interaction, user: discord.Member):
    role = interaction.guild.get_role(BLACKLIST_ROLE_ID)
    if role:
        await user.remove_roles(role)
        await interaction.response.send_message(f"🔓 Operational lock removed successfully. Clearance attributes re-established for target: {user.mention}")
    else:
        await interaction.response.send_message("❌ Config processing breakdown structural exception.", ephemeral=True)

bot.run(TOKEN)
