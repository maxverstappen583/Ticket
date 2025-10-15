# main.py
# Minimal, robust, Render-ready Py-Cord bot with slash commands that reliably respond.
# - Paste into main.py, set DISCORD_TOKEN (and optionally GUILD_ID), deploy to Render.
# - requirements.txt: py-cord==2.6.1, python-dotenv, flask, aiofiles

import os
os.environ["DISCORD_DISABLE_VOICE"] = "1"  # fix for some hosts (must be before discord import)

from threading import Thread
from flask import Flask
from dotenv import load_dotenv
load_dotenv()

import json
import io
import asyncio
import datetime
from datetime import timedelta
from typing import List, Optional

import discord
from discord import Embed, File
from discord.ui import View, Button, Modal
from discord import ui  # we'll use ui.TextInput where available

# --- Compatibility: TextInput / TextStyle might live in slightly different places ---
TextInput = getattr(ui, "TextInput", None)
TextStyle = getattr(discord, "TextStyle", None) or getattr(ui, "TextStyle", None)
# -------------------------------------------------------------------------------

# ---- Flask keepalive (Render expects a web process with an open port) ----
app = Flask("ticket_bot_keepalive")

@app.route("/")
def index():
    return "✅ Maxy Ticket Bot is running."

def run_web():
    port = int(os.environ.get("PORT", 8080))
    # production (no reloader)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

Thread(target=run_web).start()
# -------------------------------------------------------------------------

# --- Config file ---
CONFIG_FILE = "ticket_config.json"
DEFAULT_CONFIG = {
    "title": "Maxy Does Tickets – Support System",
    "description": "Need help? Open a ticket by clicking a button below!\nOur staff will assist you as soon as possible.",
    "image": None,
    "buttons": ["Hosting", "Issues", "Suspension", "Other"],
    "category_id": None,
    "log_channel_id": None,
    "notify_role_id": None,
    "creation_text": "please wait until one of our staffs assist u.",
    "autoclose_hours": 0
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
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
    return cfg

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

# load initial config
config = load_config()

# --- Intents & Bot ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = discord.Bot(intents=intents)

# If you want fast command visibility during testing, set GUILD_ID env var.
GUILD_ID = os.getenv("GUILD_ID")
if GUILD_ID:
    try:
        GUILD_ID_INT = int(GUILD_ID)
        GUILD_IDS = [GUILD_ID_INT]
    except Exception:
        GUILD_IDS = None
else:
    GUILD_IDS = None  # global registration (may take up to 1 hour)

print("ℹ️ Using GUILD_IDS:", GUILD_IDS)

# --- utilities ---
def is_admin(user: discord.Member) -> bool:
    try:
        return user.guild_permissions.administrator
    except Exception:
        return False

# --- views and buttons ---
class TicketButton(Button):
    def __init__(self, label: str, style: discord.ButtonStyle = discord.ButtonStyle.primary):
        super().__init__(label=label, style=style)

    async def callback(self, interaction: discord.Interaction):
        await handle_ticket_button(interaction, self.label)

class TicketPanelView(View):
    def __init__(self, buttons: List[str]):
        super().__init__(timeout=None)
        for name in buttons:
            style = discord.ButtonStyle.primary
            ln = name.lower()
            if "suspend" in ln or "suspension" in ln:
                style = discord.ButtonStyle.danger
            elif "other" in ln:
                style = discord.ButtonStyle.secondary
            self.add_item(TicketButton(label=name, style=style))

class CloseTicketButton(Button):
    def __init__(self, label: str = "Close Ticket"):
        super().__init__(label=label, style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Only administrators can close tickets.", ephemeral=True)
            return
        await handle_close(interaction)

def make_close_view() -> View:
    v = View(timeout=None)
    v.add_item(CloseTicketButton())
    return v

# --- ticket creation handler ---
async def handle_ticket_button(interaction: discord.Interaction, issue_type: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    member = interaction.user
    cfg = load_config()

    # one ticket per user: check topic for ID
    for ch in guild.text_channels:
        try:
            if ch.topic and str(member.id) in ch.topic:
                await interaction.followup.send(f"You already have an open ticket: {ch.mention}", ephemeral=True)
                return
        except Exception:
            continue

    # create name, unique
    safe = member.name.lower().replace(" ", "-")[:50]
    base = f"ticket-{safe}"
    name = base
    i = 1
    while discord.utils.get(guild.text_channels, name=name) is not None:
        i += 1
        name = f"{base}-{i}"

    # category
    category = None
    try:
        if cfg.get("category_id"):
            category = guild.get_channel(cfg["category_id"])
            if not isinstance(category, discord.CategoryChannel):
                category = None
    except Exception:
        category = None

    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    for role in guild.roles:
        try:
            if role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        except Exception:
            pass

    try:
        created = await guild.create_text_channel(
            name=name,
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

    embed = Embed(
        title=f"Ticket — {issue_type}",
        description=(f"Hello {member.mention},\n\n{cfg.get('creation_text')}\n\n**Issue:** {issue_type}\n\nMade by Max ❤️"),
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

    # log channel
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

    await interaction.followup.send(f"Your ticket has been created: {created.mention}", ephemeral=True)

# --- close handler with transcript ---
async def handle_close(interaction: discord.Interaction):
    channel = interaction.channel
    if channel is None:
        await interaction.response.send_message("This must be used inside a ticket channel.", ephemeral=True)
        return
    if not (channel.name.startswith("ticket-") or (channel.topic and "Ticket for" in channel.topic)):
        await interaction.response.send_message("This doesn't appear to be a ticket channel.", ephemeral=True)
        return
    if not is_admin(interaction.user):
        await interaction.response.send_message("Only administrators can close tickets.", ephemeral=True)
        return

    cfg = load_config()
    try:
        await interaction.response.send_message("Deleting the ticket in a few seconds...", ephemeral=False)
    except Exception:
        try:
            await channel.send("Deleting the ticket in a few seconds...")
        except Exception:
            pass

    await asyncio.sleep(4)

    lines: List[str] = []
    try:
        async for msg in channel.history(limit=None, oldest_first=True):
            ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            author = f"{msg.author} ({msg.author.id})"
            content = msg.content or ""
            attachments = " ".join(a.url for a in msg.attachments) if msg.attachments else ""
            lines.append(f"[{ts}] {author}: {content} {attachments}")
    except Exception:
        lines.append("Failed to fetch full history due to permissions.")

    transcript = "\n".join(lines) if lines else "No messages."
    transcript_bytes = transcript.encode("utf-8")

    ticket_owner_id = None
    if channel.topic and "ID:" in channel.topic:
        try:
            ticket_owner_id = int(channel.topic.split("ID:")[1].split(")")[0].strip())
        except Exception:
            ticket_owner_id = None

    # log
    if cfg.get("log_channel_id"):
        lc = channel.guild.get_channel(cfg["log_channel_id"])
        if isinstance(lc, discord.TextChannel):
            del_embed = Embed(title="Ticket Closed & Deleted", color=discord.Color.red(), timestamp=datetime.datetime.utcnow())
            del_embed.add_field(name="Channel", value=channel.name, inline=False)
            if ticket_owner_id:
                del_embed.add_field(name="Ticket Owner ID", value=str(ticket_owner_id), inline=False)
            try:
                await lc.send(embed=del_embed)
                await lc.send(file=File(io.BytesIO(transcript_bytes), filename=f"transcript-{channel.name}.txt"))
            except Exception:
                pass

    # DM owner
    if ticket_owner_id:
        try:
            user = await bot.fetch_user(ticket_owner_id)
            if user:
                await user.send(content=f"Your ticket **{channel.name}** has been closed. Transcript attached.", file=File(io.BytesIO(transcript_bytes), filename=f"transcript-{channel.name}.txt"))
        except Exception:
            pass

    try:
        await channel.delete(reason=f"Ticket closed by {interaction.user}")
    except Exception:
        try:
            await interaction.followup.send("Failed to delete; please delete manually.", ephemeral=True)
        except Exception:
            pass

# --- autoclose background task ---
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
                            last = None
                            async for msg in channel.history(limit=1, oldest_first=False):
                                last = msg
                            if last and last.created_at.replace(tzinfo=None) < cutoff:
                                try:
                                    await channel.send("🕐 No activity detected. Deleting in a few seconds...")
                                    await asyncio.sleep(5)
                                    # gather transcript
                                    lines = []
                                    async for msg in channel.history(limit=None, oldest_first=True):
                                        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                                        author = f"{msg.author} ({msg.author.id})"
                                        content = msg.content or ""
                                        attachments = " ".join(a.url for a in msg.attachments) if msg.attachments else ""
                                        lines.append(f"[{ts}] {author}: {content} {attachments}")
                                    transcript = "\n".join(lines) if lines else "No messages."
                                    tb = transcript.encode("utf-8")
                                    # log
                                    if cfg.get("log_channel_id"):
                                        lc = guild.get_channel(cfg["log_channel_id"])
                                        if isinstance(lc, discord.TextChannel):
                                            del_embed = Embed(title="Ticket Auto-Closed (Inactivity)", color=discord.Color.orange(), timestamp=datetime.datetime.utcnow())
                                            del_embed.add_field(name="Channel", value=channel.name, inline=False)
                                            try:
                                                await lc.send(embed=del_embed)
                                                await lc.send(file=File(io.BytesIO(tb), filename=f"transcript-{channel.name}.txt"))
                                            except Exception:
                                                pass
                                    # DM owner
                                    owner_id = None
                                    if channel.topic and "ID:" in channel.topic:
                                        try:
                                            owner_id = int(channel.topic.split("ID:")[1].split(")")[0].strip())
                                        except Exception:
                                            owner_id = None
                                    if owner_id:
                                        try:
                                            user = await bot.fetch_user(owner_id)
                                            if user:
                                                await user.send(content=f"Your ticket **{channel.name}** was auto-closed due to inactivity. Transcript attached.", file=File(io.BytesIO(tb), filename=f"transcript-{channel.name}.txt"))
                                        except Exception:
                                            pass
                                    await channel.delete(reason="Auto-closed due to inactivity")
                                except Exception as e:
                                    print("Auto-close error:", e)
                        except Exception as e:
                            print("Auto-close iteration error:", e)
        await asyncio.sleep(300)  # 5 min checks

# --- Modals for setup (safe TextInput / TextStyle usage) ---
class SimpleModal(Modal):
    def __init__(self, title: str, label: str, placeholder: str = "", multiline: bool = False, custom_id: str = "simple_modal"):
        super().__init__(title=title, custom_id=custom_id)
        # if TextInput not present, Py-Cord may expose different names; try ui.TextInput else fallback to TextInput variable
        TI = TextInput or getattr(ui, "TextInput", None)
        # style: paragraph if multiline and TextStyle present
        style = None
        if multiline and TextStyle is not None:
            # prefer paragraph
            try:
                style = TextStyle.paragraph
            except Exception:
                style = None
        if TI is None:
            # extremely rare: no TextInput available — fallback to plain modal without inputs (shouldn't happen with py-cord 2.6.1)
            raise RuntimeError("TextInput is not available in this environment. Install py-cord 2.6.1.")
        if style is not None:
            self.input = TI(label=label, placeholder=placeholder, style=style, required=True)
        else:
            self.input = TI(label=label, placeholder=placeholder, required=True)
        self.add_item(self.input)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("Saved.", ephemeral=True)

# --- Ticket setup view (buttons to open modals) ---
class TicketSetupView(View):
    def __init__(self):
        super().__init__(timeout=None)
        labels = [
            ("Set Panel Title", "set_title"),
            ("Set Panel Description", "set_desc"),
            ("Set Panel Image (URL)", "set_image"),
            ("Set Buttons (comma sep.)", "set_buttons"),
            ("Set Ticket Category (ID)", "set_category"),
            ("Set Log Channel (ID)", "set_log"),
            ("Set Notify Role (ID)", "set_role"),
            ("Set Creation Text", "set_creation"),
            ("Set Autoclose Hours", "set_autoclose"),
            ("Preview Panel", "preview"),
            ("Send Panel Here", "send_panel")
        ]
        for lbl, cid in labels:
            b = Button(label=lbl, custom_id=cid, style=discord.ButtonStyle.primary if "Set" in lbl or "Preview" in lbl else discord.ButtonStyle.secondary)
            self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # only admins allowed to configure
        if not is_admin(interaction.user):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return False
        return True

# central handler for TicketSetupView buttons
@bot.event
async def on_interaction(interaction: discord.Interaction):
    # handle only our setup buttons or allow other interactions to be processed normally
    try:
        if interaction.type == discord.InteractionType.component and interaction.data and "custom_id" in interaction.data:
            cid = interaction.data["custom_id"]
            if cid == "set_title":
                await interaction.response.send_modal(SimpleModal("Set Panel Title", "Title", placeholder=config.get("title", DEFAULT_CONFIG["title"]), custom_id="modal_set_title"))
                return
            if cid == "set_desc":
                await interaction.response.send_modal(SimpleModal("Set Panel Description", "Description", placeholder=config.get("description", DEFAULT_CONFIG["description"]), multiline=True, custom_id="modal_set_desc"))
                return
            if cid == "set_image":
                await interaction.response.send_modal(SimpleModal("Set Panel Image", "Image URL", placeholder=config.get("image") or "", custom_id="modal_set_image"))
                return
            if cid == "set_buttons":
                await interaction.response.send_modal(SimpleModal("Set Buttons", "Buttons (comma separated)", placeholder="Hosting, Issues, Suspension, Other", custom_id="modal_set_buttons"))
                return
            if cid == "set_category":
                await interaction.response.send_modal(SimpleModal("Set Category", "Category ID (0 to clear)", placeholder=str(config.get("category_id") or "0"), custom_id="modal_set_category"))
                return
            if cid == "set_log":
                await interaction.response.send_modal(SimpleModal("Set Log Channel", "Log Channel ID (0 to disable)", placeholder=str(config.get("log_channel_id") or "0"), custom_id="modal_set_log"))
                return
            if cid == "set_role":
                await interaction.response.send_modal(SimpleModal("Set Notify Role", "Role ID or 0 to disable", placeholder=str(config.get("notify_role_id") or "0"), custom_id="modal_set_role"))
                return
            if cid == "set_creation":
                await interaction.response.send_modal(SimpleModal("Set Creation Text", "Text sent when ticket is made", placeholder=config.get("creation_text") or DEFAULT_CONFIG["creation_text"], multiline=True, custom_id="modal_set_creation"))
                return
            if cid == "set_autoclose":
                await interaction.response.send_modal(SimpleModal("Set Autoclose Hours", "Hours (0 to disable)", placeholder=str(config.get("autoclose_hours") or 0), custom_id="modal_set_autoclose"))
                return
            if cid == "preview":
                cfg = load_config()
                embed = Embed(title=cfg.get("title"), description=f"{cfg.get('description')}\n\nMade by Max ❤️", color=discord.Color.dark_gray())
                if cfg.get("image"):
                    try: embed.set_thumbnail(url=cfg.get("image"))
                    except Exception: pass
                await interaction.response.send_message("Panel preview (ephemeral):", embed=embed, ephemeral=True)
                return
            if cid == "send_panel":
                cfg = load_config()
                embed = Embed(title=cfg.get("title"), description=f"{cfg.get('description')}\n\nMade by Max ❤️", color=discord.Color.dark_gray())
                if cfg.get("image"):
                    try: embed.set_thumbnail(url=cfg.get("image"))
                    except Exception: pass
                view = TicketPanelView(buttons=cfg.get("buttons", DEFAULT_CONFIG["buttons"]))
                try:
                    sent = await interaction.channel.send(embed=embed, view=view)
                    cfg["panel_message_id"] = sent.id
                    cfg["panel_channel_id"] = sent.channel.id
                    save_config(cfg)
                    await interaction.response.send_message("Ticket panel posted to this channel.", ephemeral=True)
                except Exception as e:
                    await interaction.response.send_message(f"Failed to send panel: {e}", ephemeral=True)
                return
    except Exception:
        # not our interaction — allow Pycord to continue processing
        pass

# --- Modal submit handlers: we receive modal callback events via names (Pycord passes them through)
@bot.event
async def on_modal_submit(interaction: discord.Interaction):
    # identify by custom_id set earlier
    cid = interaction.data.get("custom_id")
    cfg = load_config()
    try:
        if cid == "modal_set_title":
            cfg["title"] = interaction.data["components"][0]["components"][0]["value"]
            save_config(cfg); await interaction.response.send_message("Panel title updated.", ephemeral=True); return
        if cid == "modal_set_desc":
            cfg["description"] = interaction.data["components"][0]["components"][0]["value"]
            save_config(cfg); await interaction.response.send_message("Panel description updated.", ephemeral=True); return
        if cid == "modal_set_image":
            v = interaction.data["components"][0]["components"][0]["value"].strip()
            cfg["image"] = v or None
            save_config(cfg); await interaction.response.send_message("Panel image updated.", ephemeral=True); return
        if cid == "modal_set_buttons":
            v = interaction.data["components"][0]["components"][0]["value"]
            labels = [s.strip() for s in v.split(",") if s.strip()]
            if labels:
                cfg["buttons"] = labels
                save_config(cfg)
                await interaction.response.send_message(f"Buttons updated: {', '.join(labels)}", ephemeral=True)
            else:
                await interaction.response.send_message("Provide at least one button label.", ephemeral=True)
            return
        if cid == "modal_set_category":
            v = interaction.data["components"][0]["components"][0]["value"].strip()
            try:
                cidv = int(v)
                if cidv == 0:
                    cfg["category_id"] = None
                else:
                    cfg["category_id"] = cidv
                save_config(cfg)
                await interaction.response.send_message("Category updated.", ephemeral=True)
            except Exception:
                await interaction.response.send_message("Category ID must be numeric.", ephemeral=True)
            return
        if cid == "modal_set_log":
            v = interaction.data["components"][0]["components"][0]["value"].strip()
            try:
                cidv = int(v)
                if cidv == 0:
                    cfg["log_channel_id"] = None
                else:
                    cfg["log_channel_id"] = cidv
                save_config(cfg)
                await interaction.response.send_message("Log channel updated.", ephemeral=True)
            except Exception:
                await interaction.response.send_message("Channel ID must be numeric.", ephemeral=True)
            return
        if cid == "modal_set_role":
            v = interaction.data["components"][0]["components"][0]["value"].strip()
            try:
                rid = int(v)
                if rid == 0:
                    cfg["notify_role_id"] = None
                else:
                    cfg["notify_role_id"] = rid
                save_config(cfg)
                await interaction.response.send_message("Notify role updated.", ephemeral=True)
            except Exception:
                await interaction.response.send_message("Role must be a numeric ID (or 0).", ephemeral=True)
            return
        if cid == "modal_set_creation":
            v = interaction.data["components"][0]["components"][0]["value"]
            cfg["creation_text"] = v
            save_config(cfg)
            await interaction.response.send_message("Creation text updated.", ephemeral=True)
            return
        if cid == "modal_set_autoclose":
            v = interaction.data["components"][0]["components"][0]["value"].strip()
            try:
                hours = int(v)
                cfg["autoclose_hours"] = max(0, hours)
                save_config(cfg)
                await interaction.response.send_message(f"Autoclose set to {hours} hours.", ephemeral=True)
            except Exception:
                await interaction.response.send_message("Please provide a numeric number of hours.", ephemeral=True)
            return
    except Exception as e:
        print("on_modal_submit error:", e)
        try:
            await interaction.response.send_message("Something went wrong while processing the modal.", ephemeral=True)
        except Exception:
            pass

# --- Slash commands: register to GUILD_IDS if provided so they appear instantly there ---
if GUILD_IDS:
    @bot.slash_command(guild_ids=GUILD_IDS, name="ticket_setup", description="Open the ticket setup menu (admins only).")
    async def ticket_setup_cmd(ctx: discord.ApplicationContext):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        await ctx.respond("Ticket setup — use the buttons to configure the panel.", view=TicketSetupView(), ephemeral=True)

    @bot.slash_command(guild_ids=GUILD_IDS, name="settings", description="Show ticket settings (admins only).")
    async def settings_cmd(ctx: discord.ApplicationContext):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        cfg = load_config()
        embed = Embed(title="Ticket Settings", color=discord.Color.blurple(), timestamp=datetime.datetime.utcnow())
        embed.add_field(name="Title", value=cfg.get("title") or "—", inline=False)
        desc = cfg.get("description") or "—"
        if len(desc) > 1000:
            desc = desc[:1000] + "..."
        embed.add_field(name="Description", value=desc, inline=False)
        embed.add_field(name="Buttons", value=", ".join(cfg.get("buttons", [])) or "—", inline=False)
        embed.add_field(name="Log Channel", value=str(cfg.get("log_channel_id") or "None"), inline=False)
        embed.add_field(name="Notify Role", value=str(cfg.get("notify_role_id") or "None"), inline=False)
        embed.add_field(name="Autoclose (hours)", value=str(cfg.get("autoclose_hours", 0)), inline=False)
        await ctx.respond(embed=embed, ephemeral=True)
else:
    # global registration (warning: may take up to 1 hour)
    @bot.slash_command(name="ticket_setup", description="Open the ticket setup menu (admins only).")
    async def ticket_setup_cmd(ctx: discord.ApplicationContext):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        await ctx.respond("Ticket setup — use the buttons to configure the panel.", view=TicketSetupView(), ephemeral=True)

    @bot.slash_command(name="settings", description="Show ticket settings (admins only).")
    async def settings_cmd(ctx: discord.ApplicationContext):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        cfg = load_config()
        embed = Embed(title="Ticket Settings", color=discord.Color.blurple(), timestamp=datetime.datetime.utcnow())
        embed.add_field(name="Title", value=cfg.get("title") or "—", inline=False)
        desc = cfg.get("description") or "—"
        if len(desc) > 1000:
            desc = desc[:1000] + "..."
        embed.add_field(name="Description", value=desc, inline=False)
        embed.add_field(name="Buttons", value=", ".join(cfg.get("buttons", [])) or "—", inline=False)
        embed.add_field(name="Log Channel", value=str(cfg.get("log_channel_id") or "None"), inline=False)
        embed.add_field(name="Notify Role", value=str(cfg.get("notify_role_id") or "None"), inline=False)
        embed.add_field(name="Autoclose (hours)", value=str(cfg.get("autoclose_hours", 0)), inline=False)
        await ctx.respond(embed=embed, ephemeral=True)

# --- on_ready & start autoclose task ---
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    # If GUILD_IDS provided, commands were registered to that guild already via decorator.
    # Start autoclose background task:
    bot.loop.create_task(auto_close_checker())

# --- run ---
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set in env (Render dashboard or local .env).")
    # run the bot
    bot.run(TOKEN)
