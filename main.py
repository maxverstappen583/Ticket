# main.py - Maxy Does Tickets (full, Render-ready)
# ------------------------------------------------
# - Keep-alive Flask web server for Render web service
# - audioop disable for Pycord on Python 3.13+
# - Full ticket system with persistent JSON config
# - Save this file as main.py
# ------------------------------------------------

# --- Keep-Alive Web Server (for Render Web Service) ---
from threading import Thread
from flask import Flask
import os

app = Flask('')

@app.route('/')
def home():
    return "✅ Maxy Ticket Bot is running."

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

Thread(target=run_web).start()
# ------------------------------------------------------

# --- Fix for Render (audioop issue) ---
# Must be set BEFORE importing discord so Pycord won't attempt voice imports
os.environ["DISCORD_DISABLE_VOICE"] = "1"
# -------------------------------------

# Standard imports
import json
import io
import asyncio
import datetime
from datetime import timedelta
from typing import List, Optional
from dotenv import load_dotenv
load_dotenv()

# Discord / Pycord imports
import discord
from discord import Embed, File
from discord.ui import View, Button

# ---------- Config / Persistence ----------
CONFIG_FILE = "ticket_config.json"
DEFAULT_CONFIG = {
    "title": "Maxy Does Tickets – Support System",
    "description": "Need help? Open a ticket by clicking a button below!\nOur staff will assist you as soon as possible.",
    "image": None,
    "buttons": ["Hosting", "Issues", "Suspension", "Other"],
    "category_id": None,
    "panel_message_id": None,
    "panel_channel_id": None,
    "close_text": "please wait until one of our staffs assist u.",
    "notify_role_id": None,
    "log_channel_id": None,
    "autoclose_hours": 0  # 0 = disabled
}

def load_config() -> dict:
    if not os.path.isfile(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        return DEFAULT_CONFIG.copy()
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        try:
            cfg = json.load(f)
        except Exception:
            cfg = DEFAULT_CONFIG.copy()
    # ensure all defaults present
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
    return cfg

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

config = load_config()

# ---------- Intents and Bot creation (with safe fallback) ----------
# Use explicit intents (privileged) — if Discord rejects, fallback to limited mode.
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
intents.guilds = True

try:
    bot = discord.Bot(intents=intents)
except discord.PrivilegedIntentsRequired:
    # Fallback: disable privileged intents so bot can still run in limited mode
    intents.members = False
    intents.presences = False
    bot = discord.Bot(intents=intents)
    print("⚠️ Privileged intents not enabled in Developer Portal — running in limited mode (members/presences disabled).")

# Utility: check admin
def is_admin(user: discord.Member) -> bool:
    try:
        return user.guild_permissions.administrator
    except Exception:
        return False

# ---------- Views and Buttons ----------
class TicketButton(Button):
    def __init__(self, label: str, style: discord.ButtonStyle = discord.ButtonStyle.primary):
        super().__init__(label=label, style=style)

    async def callback(self, interaction: discord.Interaction):
        await handle_ticket_button(interaction, self.label)

class TicketPanelView(View):
    def __init__(self, buttons: List[str], timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        for name in buttons:
            style = discord.ButtonStyle.primary
            nlower = name.lower()
            if "suspension" in nlower or "suspend" in nlower:
                style = discord.ButtonStyle.danger
            elif "other" in nlower:
                style = discord.ButtonStyle.secondary
            self.add_item(TicketButton(label=name, style=style))

class CloseTicketButton(Button):
    def __init__(self, label: str = "Close Ticket"):
        super().__init__(label=label, style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        # Only admins can close via button
        if not is_admin(interaction.user):
            await interaction.response.send_message("Only administrators can close tickets.", ephemeral=True)
            return
        await handle_close(interaction)

def make_close_view() -> View:
    v = View()
    v.add_item(CloseTicketButton())
    return v

# ---------- Ticket Creation Handler ----------
async def handle_ticket_button(interaction: discord.Interaction, issue_type: str):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    member = interaction.user
    cfg = load_config()

    # One-ticket-per-user: check existing channels with user's ID in topic
    for ch in guild.text_channels:
        if ch.topic and str(member.id) in ch.topic:
            try:
                await interaction.followup.send(f"You already have an open ticket: {ch.mention}", ephemeral=True)
            except Exception:
                pass
            return

    # create channel name (ensure uniqueness)
    safe_name = member.name.lower().replace(" ", "-")
    base_name = f"ticket-{safe_name}"
    channel_name = base_name
    counter = 1
    while discord.utils.get(guild.text_channels, name=channel_name) is not None:
        counter += 1
        channel_name = f"{base_name}-{counter}"

    # category
    category = None
    if cfg.get("category_id"):
        category = guild.get_channel(cfg["category_id"])
        if category is None or not isinstance(category, discord.CategoryChannel):
            category = None

    # overwrites
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    # allow roles with admin perms
    for role in guild.roles:
        try:
            if role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        except Exception:
            pass

    # create channel
    try:
        created = await guild.create_text_channel(
            name=channel_name,
            topic=f"Ticket for {member} (ID: {member.id}) | Issue: {issue_type}",
            category=category,
            overwrites=overwrites,
            reason=f"Ticket created by {member} via button: {issue_type}"
        )
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to create channels. Check my permissions.", ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(f"Failed to create ticket channel: {e}", ephemeral=True)
        return

    # prepare embed and send with close button
    close_text = cfg.get("close_text", DEFAULT_CONFIG["close_text"])
    embed = Embed(
        title=f"Ticket — {issue_type}",
        description=(f"Hello {member.mention},\n\n"
                     f"{close_text}\n\n"
                     f"**Issue:** {issue_type}\n\nMade by Max ❤️"),
        color=discord.Color.blurple(),
        timestamp=datetime.datetime.utcnow()
    )
    if cfg.get("image"):
        try:
            embed.set_thumbnail(url=cfg["image"])
        except Exception:
            pass

    try:
        await created.send(content=f"{member.mention}", embed=embed, view=make_close_view())
    except Exception:
        pass

    # ping notify role if set
    if cfg.get("notify_role_id"):
        role = guild.get_role(cfg["notify_role_id"])
        if role:
            try:
                await created.send(f"{role.mention} New ticket opened: {created.mention}")
            except Exception:
                pass

    # log to log channel
    log_ch = None
    if cfg.get("log_channel_id"):
        log_ch = guild.get_channel(cfg["log_channel_id"])
    if isinstance(log_ch, discord.TextChannel):
        log_embed = Embed(title="Ticket Created", color=discord.Color.green(), timestamp=datetime.datetime.utcnow())
        log_embed.add_field(name="User", value=f"{member} ({member.id})", inline=False)
        log_embed.add_field(name="Issue", value=issue_type, inline=False)
        log_embed.add_field(name="Channel", value=created.mention, inline=False)
        try:
            await log_ch.send(embed=log_embed)
        except Exception:
            pass

    # confirm ephemeral to user
    await interaction.followup.send(f"Your ticket has been created: {created.mention}", ephemeral=True)

# ---------- Ticket Close Handler ----------
async def handle_close(interaction: discord.Interaction):
    channel = interaction.channel
    if channel is None:
        await interaction.response.send_message("This must be used inside a ticket channel.", ephemeral=True)
        return

    # validate ticket channel
    if not (channel.name.startswith("ticket-") or (channel.topic and "Ticket for" in channel.topic)):
        await interaction.response.send_message("This doesn't appear to be a ticket channel.", ephemeral=True)
        return

    # only admins allowed (double-check)
    if not is_admin(interaction.user):
        await interaction.response.send_message("Only administrators can close tickets.", ephemeral=True)
        return

    cfg = load_config()

    # announce deletion countdown
    try:
        await interaction.response.send_message("Deleting the ticket in a few seconds...", ephemeral=False)
    except Exception:
        try:
            await channel.send("Deleting the ticket in a few seconds...")
        except Exception:
            pass

    # short wait
    await asyncio.sleep(4)

    # collect transcript
    lines = []
    try:
        async for msg in channel.history(limit=None, oldest_first=True):
            ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            author = f"{msg.author} ({msg.author.id})"
            content = msg.content or ""
            attachments = " ".join(a.url for a in msg.attachments) if msg.attachments else ""
            line = f"[{ts}] {author}: {content} {attachments}"
            lines.append(line)
    except Exception:
        lines.append("Failed to fetch full history due to permissions.")

    transcript_text = "\n".join(lines) if lines else "No messages found."
    transcript_bytes = transcript_text.encode("utf-8")
    transcript_file = File(io.BytesIO(transcript_bytes), filename=f"transcript-{channel.name}.txt")

    # extract ticket owner id from topic if present
    ticket_owner_id = None
    if channel.topic:
        try:
            if "ID:" in channel.topic:
                after = channel.topic.split("ID:")[1]
                ticket_owner_id = int(after.split(")")[0].strip())
        except Exception:
            ticket_owner_id = None

    # log deletion to log channel
    log_ch = None
    if cfg.get("log_channel_id"):
        log_ch = channel.guild.get_channel(cfg["log_channel_id"])
    if isinstance(log_ch, discord.TextChannel):
        del_embed = Embed(title="Ticket Closed & Deleted", color=discord.Color.red(), timestamp=datetime.datetime.utcnow())
        del_embed.add_field(name="Channel", value=channel.name, inline=False)
        if ticket_owner_id:
            del_embed.add_field(name="Ticket Owner ID", value=str(ticket_owner_id), inline=False)
        try:
            await log_ch.send(embed=del_embed)
            # send transcript file
            # recreate file object since it may have been consumed elsewhere
            await log_ch.send(file=File(io.BytesIO(transcript_bytes), filename=f"transcript-{channel.name}.txt"))
        except Exception:
            pass

    # DM transcript to ticket owner
    if ticket_owner_id:
        try:
            user = await bot.fetch_user(ticket_owner_id)
            if user:
                await user.send(content=f"Your ticket **{channel.name}** has been closed. Transcript attached:", file=File(io.BytesIO(transcript_bytes), filename=f"transcript-{channel.name}.txt"))
        except Exception:
            pass

    # finally delete channel
    try:
        await channel.delete(reason=f"Ticket closed by {interaction.user}")
    except Exception:
        try:
            await interaction.followup.send("Failed to delete the ticket channel; please remove it manually.", ephemeral=True)
        except Exception:
            pass

# ---------- Auto-close background checker ----------
async def auto_close_checker():
    await bot.wait_until_ready()
    while not bot.is_closed():
        cfg = load_config()
        hours = cfg.get("autoclose_hours", 0)
        if hours and hours > 0:
            cutoff = datetime.datetime.utcnow() - timedelta(hours=hours)
            for guild in bot.guilds:
                for channel in guild.text_channels:
                    if channel.name.startswith("ticket-"):
                        try:
                            last_msg = None
                            async for msg in channel.history(limit=1, oldest_first=False):
                                last_msg = msg
                            # if no messages except the initial bot message, consider created time
                            # If last_msg is None, skip (rare)
                            if last_msg:
                                # compare times (convert to UTC naive)
                                if last_msg.created_at.replace(tzinfo=None) < cutoff:
                                    try:
                                        await channel.send("🕐 No activity detected for a while. Deleting the ticket in a few seconds...")
                                        await asyncio.sleep(5)
                                        # reuse handle_close-like sequence: create a fake interaction? simpler: generate transcript & delete
                                        # gather transcript
                                        lines = []
                                        async for msg in channel.history(limit=None, oldest_first=True):
                                            ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                                            author = f"{msg.author} ({msg.author.id})"
                                            content = msg.content or ""
                                            attachments = " ".join(a.url for a in msg.attachments) if msg.attachments else ""
                                            line = f"[{ts}] {author}: {content} {attachments}"
                                            lines.append(line)
                                        transcript_text = "\n".join(lines) if lines else "No messages found."
                                        transcript_bytes = transcript_text.encode("utf-8")
                                        # log
                                        log_ch = None
                                        if cfg.get("log_channel_id"):
                                            log_ch = guild.get_channel(cfg["log_channel_id"])
                                        if isinstance(log_ch, discord.TextChannel):
                                            del_embed = Embed(title="Ticket Auto-Closed (Inactivity)", color=discord.Color.orange(), timestamp=datetime.datetime.utcnow())
                                            del_embed.add_field(name="Channel", value=channel.name, inline=False)
                                            try:
                                                await log_ch.send(embed=del_embed)
                                                await log_ch.send(file=File(io.BytesIO(transcript_bytes), filename=f"transcript-{channel.name}.txt"))
                                            except Exception:
                                                pass
                                        # DM owner if possible
                                        ticket_owner_id = None
                                        if channel.topic and "ID:" in channel.topic:
                                            try:
                                                ticket_owner_id = int(channel.topic.split("ID:")[1].split(")")[0].strip())
                                            except Exception:
                                                ticket_owner_id = None
                                        if ticket_owner_id:
                                            try:
                                                user = await bot.fetch_user(ticket_owner_id)
                                                if user:
                                                    await user.send(content=f"Your ticket **{channel.name}** has been auto-closed due to inactivity. Transcript attached.", file=File(io.BytesIO(transcript_bytes), filename=f"transcript-{channel.name}.txt"))
                                            except Exception:
                                                pass
                                        # delete
                                        await channel.delete(reason="Auto-closed due to inactivity")
                                    except Exception as e:
                                        print(f"Auto-close error for {channel.name}: {e}")
                        except Exception as e:
                            print(f"Auto-close check error in {channel.name}: {e}")
        await asyncio.sleep(300)  # check every 5 minutes

# ---------- Slash Commands (admin-only where specified) ----------
@bot.slash_command(name="setup_ticket", description="Send the ticket panel (admins only).")
async def setup_ticket(ctx: discord.ApplicationContext):
    if not is_admin(ctx.author):
        await ctx.respond("You must have Administrator permission to use this.", ephemeral=True)
        return
    cfg = load_config()
    embed = Embed(title=cfg.get("title"), description=f"{cfg.get('description')}\n\nMade by Max ❤️", color=discord.Color.dark_gray())
    if cfg.get("image"):
        try:
            embed.set_thumbnail(url=cfg.get("image"))
        except Exception:
            pass
    view = TicketPanelView(buttons=cfg.get("buttons", []))
    try:
        sent = await ctx.channel.send(embed=embed, view=view)
        cfg["panel_message_id"] = sent.id
        cfg["panel_channel_id"] = sent.channel.id
        save_config(cfg)
        await ctx.respond("Ticket panel sent.", ephemeral=True)
    except discord.Forbidden:
        await ctx.respond("I lack permission to send messages or embeds here.", ephemeral=True)
    except Exception as e:
        await ctx.respond(f"Failed to send panel: {e}", ephemeral=True)

@bot.slash_command(name="resend_ticket_panel", description="Re-send the configured ticket panel (admins only).")
async def resend_ticket_panel(ctx: discord.ApplicationContext):
    if not is_admin(ctx.author):
        await ctx.respond("Admins only.", ephemeral=True); return
    cfg = load_config()
    embed = Embed(title=cfg.get("title"), description=f"{cfg.get('description')}\n\nMade by Max ❤️", color=discord.Color.dark_gray())
    if cfg.get("image"):
        try:
            embed.set_thumbnail(url=cfg.get("image"))
        except Exception:
            pass
    view = TicketPanelView(buttons=cfg.get("buttons", []))
    try:
        s = await ctx.channel.send(embed=embed, view=view)
        cfg["panel_message_id"] = s.id
        cfg["panel_channel_id"] = s.channel.id
        save_config(cfg)
        await ctx.respond("Ticket panel re-sent.", ephemeral=True)
    except Exception as e:
        await ctx.respond(f"Failed to send panel: {e}", ephemeral=True)

@bot.slash_command(name="set_ticket_title", description="Set the ticket embed title (admins only).")
async def set_ticket_title(ctx: discord.ApplicationContext, title: str):
    if not is_admin(ctx.author):
        await ctx.respond("Admins only.", ephemeral=True); return
    cfg = load_config()
    cfg["title"] = title
    save_config(cfg)
    await ctx.respond("Ticket title updated.", ephemeral=True)

@bot.slash_command(name="set_ticket_desc", description="Set the ticket embed description (admins only).")
async def set_ticket_desc(ctx: discord.ApplicationContext, description: str):
    if not is_admin(ctx.author):
        await ctx.respond("Admins only.", ephemeral=True); return
    cfg = load_config()
    cfg["description"] = description
    save_config(cfg)
    await ctx.respond("Ticket description updated.", ephemeral=True)

@bot.slash_command(name="set_ticket_image", description="Set the ticket embed image URL (admins only).")
async def set_ticket_image(ctx: discord.ApplicationContext, url: str):
    if not is_admin(ctx.author):
        await ctx.respond("Admins only.", ephemeral=True); return
    if not (url.startswith("http://") or url.startswith("https://")):
        await ctx.respond("Provide a valid http/https URL.", ephemeral=True); return
    cfg = load_config()
    cfg["image"] = url
    save_config(cfg)
    await ctx.respond("Ticket image updated.", ephemeral=True)

@bot.slash_command(name="set_ticket_buttons", description="Set the ticket button labels (comma separated) (admins only).")
async def set_ticket_buttons(ctx: discord.ApplicationContext, buttons: str):
    if not is_admin(ctx.author):
        await ctx.respond("Admins only.", ephemeral=True); return
    labels = [b.strip() for b in buttons.split(",") if b.strip()]
    if not labels:
        await ctx.respond("Provide at least one label.", ephemeral=True); return
    cfg = load_config()
    cfg["buttons"] = labels
    save_config(cfg)
    await ctx.respond(f"Ticket buttons updated: {', '.join(labels)}", ephemeral=True)

@bot.slash_command(name="set_ticket_category", description="Set the category ID where tickets are created (admins only).")
async def set_ticket_category(ctx: discord.ApplicationContext, category_id: str):
    if not is_admin(ctx.author):
        await ctx.respond("Admins only.", ephemeral=True); return
    try:
        cid = int(category_id)
    except ValueError:
        await ctx.respond("Category ID must be numeric.", ephemeral=True); return
    cat = ctx.guild.get_channel(cid)
    if not cat or not isinstance(cat, discord.CategoryChannel):
        await ctx.respond("Category not found in this server.", ephemeral=True); return
    cfg = load_config()
    cfg["category_id"] = cid
    save_config(cfg)
    await ctx.respond(f"Ticket category set to: {cat.name}", ephemeral=True)

@bot.slash_command(name="set_close_text", description="Set the close message text shown inside new tickets (admins only).")
async def set_close_text(ctx: discord.ApplicationContext, text: str):
    if not is_admin(ctx.author):
        await ctx.respond("Admins only.", ephemeral=True); return
    cfg = load_config()
    cfg["close_text"] = text
    save_config(cfg)
    await ctx.respond("Close text updated.", ephemeral=True)

@bot.slash_command(name="set_notify_role", description="Set a role to ping when a ticket is created (mention or ID). Use 0 to disable. (admins only)")
async def set_notify_role(ctx: discord.ApplicationContext, role: str):
    if not is_admin(ctx.author):
        await ctx.respond("Admins only.", ephemeral=True); return
    cfg = load_config()
    if role.strip() == "0":
        cfg["notify_role_id"] = None
        save_config(cfg)
        await ctx.respond("Notify role disabled.", ephemeral=True)
        return
    role_id = None
    if role.isdigit():
        role_id = int(role)
    else:
        if role.startswith("<@&") and role.endswith(">"):
            try:
                role_id = int(role[3:-1])
            except Exception:
                role_id = None
    if role_id is None:
        await ctx.respond("Could not parse role. Provide mention or ID or 0 to disable.", ephemeral=True); return
    r = ctx.guild.get_role(role_id)
    if not r:
        await ctx.respond("Role not found.", ephemeral=True); return
    cfg["notify_role_id"] = role_id
    save_config(cfg)
    await ctx.respond(f"Notify role set to: {r.name}", ephemeral=True)

@bot.slash_command(name="set_log_channel", description="Set the log channel ID where ticket logs/transcripts will be sent. Use 0 to disable. (admins only)")
async def set_log_channel(ctx: discord.ApplicationContext, channel_id: str):
    if not is_admin(ctx.author):
        await ctx.respond("Admins only.", ephemeral=True); return
    cfg = load_config()
    if channel_id.strip() == "0":
        cfg["log_channel_id"] = None
        save_config(cfg)
        await ctx.respond("Log channel disabled.", ephemeral=True)
        return
    try:
        cid = int(channel_id)
    except ValueError:
        await ctx.respond("Channel ID must be numeric.", ephemeral=True); return
    ch = ctx.guild.get_channel(cid)
    if not ch or not isinstance(ch, discord.TextChannel):
        await ctx.respond("Text channel not found in this server.", ephemeral=True); return
    cfg["log_channel_id"] = cid
    save_config(cfg)
    await ctx.respond(f"Log channel set to: {ch.mention}", ephemeral=True)

@bot.slash_command(name="set_autoclose", description="Set hours before inactive tickets auto-close (0 to disable). Admins only.")
async def set_autoclose(ctx: discord.ApplicationContext, hours: int):
    if not is_admin(ctx.author):
        await ctx.respond("Admins only.", ephemeral=True); return
    if hours < 0:
        await ctx.respond("Hours cannot be negative.", ephemeral=True); return
    cfg = load_config()
    cfg["autoclose_hours"] = hours
    save_config(cfg)
    if hours == 0:
        await ctx.respond("Auto-close disabled.", ephemeral=True)
    else:
        await ctx.respond(f"Tickets will auto-close after {hours} hours of inactivity.", ephemeral=True)

@bot.slash_command(name="close_ticket", description="Close the ticket (admins only).")
async def close_ticket(ctx: discord.ApplicationContext):
    if not is_admin(ctx.author):
        await ctx.respond("Admins only.", ephemeral=True); return
    # use the same close handler, requires the command to be run inside ticket channel
    await handle_close(ctx.interaction)

# ---------- Bot Events ----------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print("Bot ready. Use /setup_ticket to post the panel.")
    # start background autoclose checker
    bot.loop.create_task(auto_close_checker())

# ---------- Run ----------
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        raise ValueError("❌ No DISCORD_TOKEN found in environment. Put DISCORD_TOKEN in your .env or Render env vars.")
    try:
        # asyncio.run is safer in some hosting environments
        asyncio.run(bot.start(TOKEN))
    except KeyboardInterrupt:
        print("🛑 Bot stopped manually.")
