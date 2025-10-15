# main.py - Ticket Bot (fixed: no global on_interaction, per-component callbacks)
# -----------------------------------------------------------------------------
# Requirements: py-cord==2.6.1, python-dotenv, flask, aiofiles
# Env: DISCORD_TOKEN (required), GUILD_ID (optional; speeds slash command registration)
# -----------------------------------------------------------------------------

import os
os.environ["DISCORD_DISABLE_VOICE"] = "1"   # audio fix (must be before discord import)

from dotenv import load_dotenv
load_dotenv()

from threading import Thread
from flask import Flask
import json
import io
import asyncio
import datetime
from datetime import timedelta
from typing import List, Optional

import discord
from discord import Embed, File
from discord.ui import View, Button, Modal
from discord import ui

# Compatibility: ui.TextInput exists in py-cord 2.6.1
TextInput = getattr(ui, "TextInput", None)
TextStyle = getattr(ui, "TextStyle", None)

print("PyCord version:", getattr(discord, "__version__", "unknown"))
print("TextInput:", bool(TextInput), "TextStyle:", bool(TextStyle))

# ---------------- Flask keep-alive (Render) ----------------
app = Flask("ticket_bot_keepalive")

@app.route("/")
def index():
    return "✅ Maxy Ticket Bot is running."

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

Thread(target=run_web).start()
# ---------------------------------------------------------

# ---------------- Config ----------------
CONFIG_FILE = "ticket_config.json"
DEFAULT_CONFIG = {
    "title": "Maxy Does Tickets – Support System",
    "description": "Need help? Open a ticket by clicking a button below!\nOur staff will assist you as soon as possible.",
    "image": None,
    "buttons": ["Hosting", "Issues", "Suspension", "Other"],
    "category_id": None,
    "panel_message_id": None,
    "panel_channel_id": None,
    "creation_text": "please wait until one of our staffs assist u.",
    "notify_role_id": None,
    "log_channel_id": None,
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

# initial config in memory (we reload inside handlers)
config = load_config()

# ---------------- Bot & Intents ----------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = discord.Bot(intents=intents)

GUILD_ID = os.getenv("GUILD_ID")
if GUILD_ID:
    try:
        GUILD_IDS = [int(GUILD_ID)]
    except Exception:
        GUILD_IDS = None
else:
    GUILD_IDS = None

print("GUILD_IDS:", GUILD_IDS)

def is_admin(user: discord.Member) -> bool:
    try:
        return user.guild_permissions.administrator
    except Exception:
        return False

# ---------------- Ticket Panel / Ticket Button ----------------
class TicketButton(Button):
    def __init__(self, label: str, style: discord.ButtonStyle = discord.ButtonStyle.primary):
        super().__init__(label=label, style=style, custom_id=f"ticket_btn:{label}")

    async def callback(self, interaction: discord.Interaction):
        # immediately defer ephemeral so the client doesn't time out if we do extra work
        await interaction.response.defer(ephemeral=True)
        await handle_ticket_button(interaction, self.label)

class TicketPanelView(View):
    def __init__(self, buttons: List[str]):
        super().__init__(timeout=None)
        for name in buttons:
            style = discord.ButtonStyle.primary
            lower = name.lower()
            if "suspend" in lower or "suspension" in lower:
                style = discord.ButtonStyle.danger
            elif "other" in lower:
                style = discord.ButtonStyle.secondary
            self.add_item(TicketButton(label=name, style=style))

# Close button inside ticket channel
class CloseTicketButton(Button):
    def __init__(self, label: str = "Close Ticket"):
        super().__init__(label=label, style=discord.ButtonStyle.danger, custom_id="close_ticket_btn")

    async def callback(self, interaction: discord.Interaction):
        # admin check
        if not is_admin(interaction.user):
            await interaction.response.send_message("Only administrators can close tickets.", ephemeral=True)
            return
        # respond quickly then perform close
        await interaction.response.send_message("Deleting the ticket in a few seconds...", ephemeral=False)
        await handle_close(interaction)

def make_close_view() -> View:
    v = View(timeout=None)
    v.add_item(CloseTicketButton())
    return v

# ---------------- Ticket creation logic ----------------
async def handle_ticket_button(interaction: discord.Interaction, issue_type: str):
    guild = interaction.guild
    member = interaction.user
    cfg = load_config()

    # one ticket per user: search topics for user id
    for ch in guild.text_channels:
        if ch.topic and str(member.id) in ch.topic:
            await interaction.followup.send(f"You already have an open ticket: {ch.mention}", ephemeral=True)
            return

    safe_name = member.name.lower().replace(" ", "-")[:50]
    base_name = f"ticket-{safe_name}"
    channel_name = base_name
    counter = 1
    while discord.utils.get(guild.text_channels, name=channel_name) is not None:
        counter += 1
        channel_name = f"{base_name}-{counter}"

    # category handling
    category = None
    if cfg.get("category_id"):
        category = guild.get_channel(cfg["category_id"])
        if category is None or not isinstance(category, discord.CategoryChannel):
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

    # log
    if cfg.get("log_channel_id"):
        log_ch = guild.get_channel(cfg["log_channel_id"])
        if isinstance(log_ch, discord.TextChannel):
            le = Embed(title="Ticket Created", color=discord.Color.green(), timestamp=datetime.datetime.utcnow())
            le.add_field(name="User", value=f"{member} ({member.id})", inline=False)
            le.add_field(name="Issue", value=issue_type, inline=False)
            le.add_field(name="Channel", value=created.mention, inline=False)
            try:
                await log_ch.send(embed=le)
            except Exception:
                pass

    await interaction.followup.send(f"Your ticket has been created: {created.mention}", ephemeral=True)

# ---------------- Close handler & transcript ----------------
async def handle_close(interaction: discord.Interaction):
    channel = interaction.channel
    if not channel:
        return
    cfg = load_config()
    # collect transcript
    lines = []
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
    tb = transcript.encode("utf-8")

    # log send
    if cfg.get("log_channel_id"):
        lc = channel.guild.get_channel(cfg["log_channel_id"])
        if isinstance(lc, discord.TextChannel):
            de = Embed(title="Ticket Closed & Deleted", color=discord.Color.red(), timestamp=datetime.datetime.utcnow())
            de.add_field(name="Channel", value=channel.name, inline=False)
            try:
                await lc.send(embed=de)
                await lc.send(file=File(io.BytesIO(tb), filename=f"transcript-{channel.name}.txt"))
            except Exception:
                pass

    # dm owner from topic
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
                await user.send(content=f"Your ticket **{channel.name}** has been closed. Transcript attached.", file=File(io.BytesIO(tb), filename=f"transcript-{channel.name}.txt"))
        except Exception:
            pass

    # delete
    try:
        await channel.delete(reason=f"Ticket closed by {interaction.user}")
    except Exception:
        pass

# ---------------- Auto-close background ----------------
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
                                    await channel.send("🕐 No activity detected. Deleting the ticket in a few seconds...")
                                    await asyncio.sleep(5)
                                    await handle_close_simple(channel, cfg)
                                except Exception as e:
                                    print("Auto-close error:", e)
                        except Exception as e:
                            print("Auto-close iteration error:", e)
        await asyncio.sleep(300)

# helper to close given channel (used by auto-close)
async def handle_close_simple(channel: discord.TextChannel, cfg: dict):
    lines = []
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
    tb = transcript.encode("utf-8")

    if cfg.get("log_channel_id"):
        lc = channel.guild.get_channel(cfg["log_channel_id"])
        if isinstance(lc, discord.TextChannel):
            de = Embed(title="Ticket Auto-Closed (Inactivity)", color=discord.Color.orange(), timestamp=datetime.datetime.utcnow())
            de.add_field(name="Channel", value=channel.name, inline=False)
            try:
                await lc.send(embed=de)
                await lc.send(file=File(io.BytesIO(tb), filename=f"transcript-{channel.name}.txt"))
            except Exception:
                pass
    # delete channel
    try:
        await channel.delete(reason="Auto-closed due to inactivity")
    except Exception:
        pass

# ---------------- Setup modal classes ----------------
# All modal classes present a TextInput and save setting on submit. Each modal responds quickly.
class SimpleSettingModal(Modal):
    def __init__(self, title: str, label: str, placeholder: str = "", multiline: bool = False, custom_id: str = "simple_setting"):
        super().__init__(title=title, custom_id=custom_id)
        TI = TextInput or getattr(ui, "TextInput", None)
        if TI is None:
            raise RuntimeError("TextInput not found in this py-cord build.")
        style = TextStyle.paragraph if (multiline and TextStyle is not None) else None
        if style is not None:
            self.input = TI(label=label, placeholder=placeholder, style=style, required=True)
        else:
            self.input = TI(label=label, placeholder=placeholder, required=True)
        self.add_item(self.input)

    async def callback(self, interaction: discord.Interaction):
        # custom_id contains which setting to save (we will store mapping externally)
        await interaction.response.defer(ephemeral=True)
        # read custom_id mapping from self.custom_id (we set it to like "set:creation_text")
        cid = self.custom_id or ""
        value = self.input.value
        cfg = load_config()
        # mapping rules:
        if cid == "set:title":
            cfg["title"] = value
            save_config(cfg)
            await interaction.followup.send("Panel title updated.", ephemeral=True)
            return
        if cid == "set:description":
            cfg["description"] = value
            save_config(cfg)
            await interaction.followup.send("Panel description updated.", ephemeral=True)
            return
        if cid == "set:image":
            cfg["image"] = value or None
            save_config(cfg)
            await interaction.followup.send("Panel image updated.", ephemeral=True)
            return
        if cid == "set:buttons":
            labels = [s.strip() for s in value.split(",") if s.strip()]
            if labels:
                cfg["buttons"] = labels
                save_config(cfg)
                await interaction.followup.send(f"Buttons updated: {', '.join(labels)}", ephemeral=True)
            else:
                await interaction.followup.send("Provide at least one label.", ephemeral=True)
            return
        if cid == "set:creation_text":
            cfg["creation_text"] = value
            save_config(cfg)
            await interaction.followup.send("Creation text updated.", ephemeral=True)
            return
        if cid == "set:autoclose":
            try:
                hours = int(value.strip())
                cfg["autoclose_hours"] = max(0, hours)
                save_config(cfg)
                await interaction.followup.send(f"Auto-close set to {hours} hours.", ephemeral=True)
            except Exception:
                await interaction.followup.send("Please provide a valid number.", ephemeral=True)
            return
        # fallback
        await interaction.followup.send("Saved.", ephemeral=True)

# We'll create subclasses carrying the custom_id used above:
class TitleModal(SimpleSettingModal):
    def __init__(self):
        super().__init__(title="Set Panel Title", label="Title", placeholder=config.get("title") or DEFAULT_CONFIG["title"], custom_id="set:title")

class DescModal(SimpleSettingModal):
    def __init__(self):
        super().__init__(title="Set Panel Description", label="Description", placeholder=config.get("description") or DEFAULT_CONFIG["description"], multiline=True, custom_id="set:description")

class ImageModal(SimpleSettingModal):
    def __init__(self):
        super().__init__(title="Set Panel Image URL", label="Image URL", placeholder=config.get("image") or "", custom_id="set:image")

class ButtonsModal(SimpleSettingModal):
    def __init__(self):
        super().__init__(title="Set Panel Buttons", label="Buttons (comma separated)", placeholder="Hosting, Issues, Suspension, Other", custom_id="set:buttons")

class CreationTextModal(SimpleSettingModal):
    def __init__(self):
        super().__init__(title="Set Creation Text", label="Text shown when ticket is created", placeholder=config.get("creation_text") or DEFAULT_CONFIG["creation_text"], multiline=True, custom_id="set:creation_text")

class AutocloseModal(SimpleSettingModal):
    def __init__(self):
        super().__init__(title="Set Autoclose Hours", label="Hours (0 to disable)", placeholder=str(config.get("autoclose_hours") or 0), custom_id="set:autoclose")

# ---------------- Ticket Setup view (buttons open modals directly) ----------------
class TicketSetupButton(Button):
    def __init__(self, label: str, custom_id: str, style: discord.ButtonStyle = discord.ButtonStyle.primary):
        super().__init__(label=label, custom_id=custom_id, style=style)

    async def callback(self, interaction: discord.Interaction):
        # admin-only check
        if not is_admin(interaction.user):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        cid = self.custom_id
        # open modals or preview/send
        if cid == "btn_title":
            await interaction.response.send_modal(TitleModal())
            return
        if cid == "btn_desc":
            await interaction.response.send_modal(DescModal())
            return
        if cid == "btn_image":
            await interaction.response.send_modal(ImageModal())
            return
        if cid == "btn_buttons":
            await interaction.response.send_modal(ButtonsModal())
            return
        if cid == "btn_creation_text":
            await interaction.response.send_modal(CreationTextModal())
            return
        if cid == "btn_autoclose":
            await interaction.response.send_modal(AutocloseModal())
            return
        if cid == "btn_preview":
            cfg = load_config()
            embed = Embed(title=cfg.get("title"), description=f"{cfg.get('description')}\n\nMade by Max ❤️", color=discord.Color.dark_gray())
            if cfg.get("image"):
                try: embed.set_thumbnail(url=cfg.get("image"))
                except Exception: pass
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if cid == "btn_send_panel":
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
                await interaction.response.send_message("Ticket panel sent to this channel.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"Failed to send panel: {e}", ephemeral=True)
            return

class TicketSetupView(View):
    def __init__(self):
        super().__init__(timeout=None)
        items = [
            ("Set Panel Title", "btn_title"),
            ("Set Panel Description", "btn_desc"),
            ("Set Panel Image", "btn_image"),
            ("Set Panel Buttons", "btn_buttons"),
            ("Text Sent When Ticket Is Made", "btn_creation_text"),
            ("Set Autoclose Hours", "btn_autoclose"),
            ("Preview Panel", "btn_preview"),
            ("Send Panel Here", "btn_send_panel"),
        ]
        for label, cid in items:
            style = discord.ButtonStyle.primary if "Set" in label or "Preview" in label else discord.ButtonStyle.secondary
            self.add_item(TicketSetupButton(label=label, custom_id=cid, style=style))

# ---------------- Slash commands (register to guild for quick tests if GUILD_ID set) ------------
if GUILD_IDS:
    @bot.slash_command(guild_ids=GUILD_IDS, name="ticket_setup", description="Open ticket setup menu (admins only).")
    async def ticket_setup_cmd(ctx: discord.ApplicationContext):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        await ctx.respond("Ticket setup — tap a button to configure.", view=TicketSetupView(), ephemeral=True)

    @bot.slash_command(guild_ids=GUILD_IDS, name="ticket_settings", description="Show ticket settings (admins only).")
    async def ticket_settings_cmd(ctx: discord.ApplicationContext):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        cfg = load_config()
        embed = Embed(title="Ticket Settings", color=discord.Color.blurple(), timestamp=datetime.datetime.utcnow())
        embed.add_field(name="Title", value=cfg.get("title") or "—", inline=False)
        desc = cfg.get("description") or "—"
        if len(desc) > 1000: desc = desc[:1000] + "..."
        embed.add_field(name="Description", value=desc, inline=False)
        embed.add_field(name="Buttons", value=", ".join(cfg.get("buttons", [])) or "—", inline=False)
        embed.add_field(name="Log Channel", value=str(cfg.get("log_channel_id") or "None"), inline=False)
        embed.add_field(name="Notify Role", value=str(cfg.get("notify_role_id") or "None"), inline=False)
        embed.add_field(name="Auto-close hours", value=str(cfg.get("autoclose_hours", 0)), inline=False)
        await ctx.respond(embed=embed, ephemeral=True)
else:
    # global registration - may take up to 1 hour to propagate but still will work
    @bot.slash_command(name="ticket_setup", description="Open ticket setup menu (admins only).")
    async def ticket_setup_cmd(ctx: discord.ApplicationContext):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        await ctx.respond("Ticket setup — tap a button to configure.", view=TicketSetupView(), ephemeral=True)

    @bot.slash_command(name="ticket_settings", description="Show ticket settings (admins only).")
    async def ticket_settings_cmd(ctx: discord.ApplicationContext):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        cfg = load_config()
        embed = Embed(title="Ticket Settings", color=discord.Color.blurple(), timestamp=datetime.datetime.utcnow())
        embed.add_field(name="Title", value=cfg.get("title") or "—", inline=False)
        desc = cfg.get("description") or "—"
        if len(desc) > 1000: desc = desc[:1000] + "..."
        embed.add_field(name="Description", value=desc, inline=False)
        embed.add_field(name="Buttons", value=", ".join(cfg.get("buttons", [])) or "—", inline=False)
        embed.add_field(name="Log Channel", value=str(cfg.get("log_channel_id") or "None"), inline=False)
        embed.add_field(name="Notify Role", value=str(cfg.get("notify_role_id") or "None"), inline=False)
        embed.add_field(name="Auto-close hours", value=str(cfg.get("autoclose_hours", 0)), inline=False)
        await ctx.respond(embed=embed, ephemeral=True)

# ---------------- on_ready/start background ----------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    bot.loop.create_task(auto_close_checker())

# ---------------- Run ----------------
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set in environment.")
    bot.run(TOKEN)
