# main.py - robust ticket bot with modal compatibility fallback
# - Tries to use modal TextInput (ui.TextInput or ui.InputText)
# - If not present, falls back to slash commands for settings (no crash)
# - Render-ready (Flask keep-alive), autclose, transcripts, notify role, 1-ticket-per-user
# - Thoughtfully handled to avoid import crashes on different py-cord builds

import os
os.environ["DISCORD_DISABLE_VOICE"] = "1"  # must run before importing discord in some hosts

from dotenv import load_dotenv
load_dotenv()

from threading import Thread
from flask import Flask
import json, io, asyncio, datetime
from datetime import timedelta
from typing import List, Optional

import discord
from discord import Embed, File
from discord.ui import View, Button, Modal
from discord import ui

# ---------- Compatibility: find TextInput / InputText ----------
# Try common names and locations across py-cord builds.
TextInput = None
for candidate in (
    getattr(ui, "TextInput", None),
    getattr(ui, "InputText", None),
    getattr(discord.ui, "TextInput", None),
    getattr(discord.ui, "InputText", None),
):
    if candidate is not None:
        TextInput = candidate
        break

# Also try direct discord.TextInput fallback (rare)
if TextInput is None:
    TextInput = getattr(discord, "TextInput", None)

# TextStyle might live in ui or discord
TextStyle = getattr(ui, "TextStyle", None) or getattr(discord, "TextStyle", None)

print("Pycord version:", getattr(discord, "__version__", "unknown"))
print("Modal TextInput resolved:", bool(TextInput), "TextStyle resolved:", bool(TextStyle))

# ---------- Flask keep-alive ----------
app = Flask("ticket_bot_keepalive")
@app.route("/")
def home():
    return "✅ Maxy Ticket Bot is running."

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

Thread(target=run_web).start()

# ---------- Config ----------
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

_config = load_config()

# ---------- Bot & intents ----------
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

# ---------- Ticket panel & ticket creation ----------
class TicketButton(Button):
    def __init__(self, label: str, style: discord.ButtonStyle = discord.ButtonStyle.primary):
        super().__init__(label=label, style=style)

    async def callback(self, interaction: discord.Interaction):
        # acknowledge quickly
        await interaction.response.defer(ephemeral=True)
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
        await interaction.response.send_message("Deleting the ticket in a few seconds...", ephemeral=False)
        await handle_close(interaction)

def make_close_view() -> View:
    v = View(timeout=None)
    v.add_item(CloseTicketButton())
    return v

async def handle_ticket_button(interaction: discord.Interaction, issue_type: str):
    guild = interaction.guild
    member = interaction.user
    cfg = load_config()

    # one ticket per user
    for ch in guild.text_channels:
        try:
            if ch.topic and str(member.id) in ch.topic:
                await interaction.followup.send(f"You already have an open ticket: {ch.mention}", ephemeral=True)
                return
        except Exception:
            continue

    safe = member.name.lower().replace(" ", "-")[:50]
    base = f"ticket-{safe}"
    name = base
    i = 1
    while discord.utils.get(guild.text_channels, name=name) is not None:
        i += 1
        name = f"{base}-{i}"

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
            name=name,
            topic=f"Ticket for {member} (ID: {member.id}) | Issue: {issue_type}",
            category=category,
            overwrites=overwrites,
            reason=f"Ticket created by {member}"
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

    # notify role
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

# ---------- Close & transcript ----------
async def handle_close(interaction: discord.Interaction):
    channel = interaction.channel
    if channel is None:
        return
    cfg = load_config()
    lines = []
    try:
        async for msg in channel.history(limit=None, oldest_first=True):
            ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            author = f"{msg.author} ({msg.author.id})"
            content = msg.content or ""
            attachments = " ".join(a.url for a in msg.attachments) if msg.attachments else ""
            lines.append(f"[{ts}] {author}: {content} {attachments}")
    except Exception:
        lines.append("Failed to fetch history due to permissions.")

    transcript = "\n".join(lines) if lines else "No messages."
    tb = transcript.encode("utf-8")

    # send to log channel
    if cfg.get("log_channel_id"):
        lc = channel.guild.get_channel(cfg["log_channel_id"])
        if isinstance(lc, discord.TextChannel):
            de = Embed(title="Ticket Closed & Deleted", color=discord.Color.red(), timestamp=datetime.datetime.utcnow())
            de.add_field(name="Channel", value=channel.name, inline=False)
            if channel.topic:
                de.add_field(name="Topic", value=channel.topic, inline=False)
            try:
                await lc.send(embed=de)
                await lc.send(file=File(io.BytesIO(tb), filename=f"transcript-{channel.name}.txt"))
            except Exception:
                pass

    # DM ticket owner if possible
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

    try:
        await channel.delete(reason=f"Ticket closed by {interaction.user}")
    except Exception:
        pass

# ---------- Auto-close background ----------
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
                                    # gather transcript and delete
                                    await _auto_close_and_log(channel, cfg)
                                except Exception as e:
                                    print("Auto-close error:", e)
                        except Exception as e:
                            print("Auto-close iteration error:", e)
        await asyncio.sleep(300)

async def _auto_close_and_log(channel: discord.TextChannel, cfg: dict):
    lines = []
    try:
        async for m in channel.history(limit=None, oldest_first=True):
            ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
            author = f"{m.author} ({m.author.id})"
            content = m.content or ""
            attachments = " ".join(a.url for a in m.attachments) if m.attachments else ""
            lines.append(f"[{ts}] {author}: {content} {attachments}")
    except Exception:
        lines.append("Failed to fetch history due to permissions.")
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
    # DM owner
    owner_id = None
    if channel.topic and "ID:" in channel.topic:
        try:
            owner_id = int(channel.topic.split("ID:")[1].split(")")[0].strip())
        except Exception:
            owner_id = None
    if owner_id:
        try:
            u = await bot.fetch_user(owner_id)
            if u:
                await u.send(content=f"Your ticket **{channel.name}** was auto-closed due to inactivity. Transcript attached.", file=File(io.BytesIO(tb), filename=f"transcript-{channel.name}.txt"))
        except Exception:
            pass
    try:
        await channel.delete(reason="Auto-closed due to inactivity")
    except Exception:
        pass

# ---------- Setup: use modals if available, else provide slash commands for settings ----------
MODAL_AVAILABLE = TextInput is not None
print("MODAL_AVAILABLE:", MODAL_AVAILABLE)

# If modals are available -> define modal classes and Setup view that opens modals.
if MODAL_AVAILABLE:
    # define tuned modals (with placeholders, required, max_length similar to your screenshot)
    class SetTitleModal(Modal):
        def __init__(self):
            super().__init__(title="Set Panel Title", custom_id="modal_set_title")
            self.input = TextInput(label="Panel Title", placeholder=_config.get("title") or DEFAULT_CONFIG["title"], required=True, max_length=100)
            self.add_item(self.input)
        async def callback(self, interaction: discord.Interaction):
            cfg = load_config(); cfg["title"] = self.input.value.strip(); save_config(cfg)
            await interaction.response.send_message("Panel title updated.", ephemeral=True)

    class SetDescriptionModal(Modal):
        def __init__(self):
            super().__init__(title="Set Panel Description", custom_id="modal_set_description")
            style = TextStyle.paragraph if TextStyle is not None else None
            if style is not None:
                self.input = TextInput(label="Panel description", placeholder="submit your suggestions here", style=style, required=True, max_length=500)
            else:
                self.input = TextInput(label="Panel description", placeholder="submit your suggestions here", required=True, max_length=500)
            self.add_item(self.input)
        async def callback(self, interaction: discord.Interaction):
            cfg = load_config(); cfg["description"] = self.input.value.strip(); save_config(cfg)
            await interaction.response.send_message("Panel description updated.", ephemeral=True)

    class SetImageModal(Modal):
        def __init__(self):
            super().__init__(title="Set Panel Image URL", custom_id="modal_set_image")
            self.input = TextInput(label="Image URL (http/https)", placeholder="https://example.com/image.png", required=False, max_length=500)
            self.add_item(self.input)
        async def callback(self, interaction: discord.Interaction):
            v = self.input.value.strip()
            if v and not (v.startswith("http://") or v.startswith("https://")):
                await interaction.response.send_message("Invalid URL. Must begin with http:// or https://", ephemeral=True); return
            cfg = load_config(); cfg["image"] = v or None; save_config(cfg)
            await interaction.response.send_message("Panel image updated.", ephemeral=True)

    class SetButtonsModal(Modal):
        def __init__(self):
            super().__init__(title="Set Panel Buttons", custom_id="modal_set_buttons")
            self.input = TextInput(label="Buttons (comma separated)", placeholder="Hosting, Issues, Suspension, Other", required=True, max_length=300)
            self.add_item(self.input)
        async def callback(self, interaction: discord.Interaction):
            labels = [s.strip() for s in self.input.value.split(",") if s.strip()]
            if not labels:
                await interaction.response.send_message("Provide at least one button label.", ephemeral=True); return
            cfg = load_config(); cfg["buttons"] = labels; save_config(cfg)
            await interaction.response.send_message(f"Buttons updated: {', '.join(labels)}", ephemeral=True)

    class SetCategoryModal(Modal):
        def __init__(self):
            super().__init__(title="Set Ticket Category ID", custom_id="modal_set_category")
            self.input = TextInput(label="Category ID (0 to clear)", placeholder=str(_config.get("category_id") or "0"), required=True, max_length=30)
            self.add_item(self.input)
        async def callback(self, interaction: discord.Interaction):
            v = self.input.value.strip()
            try:
                cid = int(v)
            except Exception:
                await interaction.response.send_message("Category ID must be numeric (or 0).", ephemeral=True); return
            cfg = load_config()
            cfg["category_id"] = None if cid == 0 else cid
            save_config(cfg)
            await interaction.response.send_message("Ticket category updated.", ephemeral=True)

    class SetLogChannelModal(Modal):
        def __init__(self):
            super().__init__(title="Set Log Channel ID", custom_id="modal_set_log")
            self.input = TextInput(label="Log Channel ID (0 to disable)", placeholder=str(_config.get("log_channel_id") or "0"), required=True, max_length=30)
            self.add_item(self.input)
        async def callback(self, interaction: discord.Interaction):
            v = self.input.value.strip()
            try:
                cid = int(v)
            except Exception:
                await interaction.response.send_message("Channel ID must be numeric or 0.", ephemeral=True); return
            cfg = load_config(); cfg["log_channel_id"] = None if cid == 0 else cid; save_config(cfg)
            await interaction.response.send_message("Log channel updated.", ephemeral=True)

    class SetNotifyRoleModal(Modal):
        def __init__(self):
            super().__init__(title="Set Notify Role", custom_id="modal_set_notify")
            self.input = TextInput(label="Role ID or mention (0 to disable)", placeholder="@Support or 0", required=True, max_length=100)
            self.add_item(self.input)
        async def callback(self, interaction: discord.Interaction):
            v = self.input.value.strip()
            if v == "0":
                cfg = load_config(); cfg["notify_role_id"] = None; save_config(cfg); await interaction.response.send_message("Notify role disabled.", ephemeral=True); return
            rid = None
            if v.isdigit(): rid = int(v)
            elif v.startswith("<@&") and v.endswith(">"):
                try: rid = int(v[3:-1])
                except Exception: rid = None
            if rid is None:
                await interaction.response.send_message("Provide role ID or mention like <@&123...> or 0 to disable.", ephemeral=True); return
            cfg = load_config(); cfg["notify_role_id"] = rid; save_config(cfg)
            await interaction.response.send_message("Notify role updated.", ephemeral=True)

    class SetCreationTextModal(Modal):
        def __init__(self):
            super().__init__(title="Set Text Sent When Ticket Is Made", custom_id="modal_set_creation")
            style = TextStyle.paragraph if TextStyle is not None else None
            if style is not None:
                self.input = TextInput(label="Text shown when ticket created", placeholder=_config.get("creation_text") or DEFAULT_CONFIG["creation_text"], style=style, required=True, max_length=500)
            else:
                self.input = TextInput(label="Text shown when ticket created", placeholder=_config.get("creation_text") or DEFAULT_CONFIG["creation_text"], required=True, max_length=500)
            self.add_item(self.input)
        async def callback(self, interaction: discord.Interaction):
            cfg = load_config(); cfg["creation_text"] = self.input.value.strip(); save_config(cfg)
            await interaction.response.send_message("Creation text updated.", ephemeral=True)

    class SetAutocloseModal(Modal):
        def __init__(self):
            super().__init__(title="Set Autoclose Hours", custom_id="modal_set_autoclose")
            self.input = TextInput(label="Hours (0 to disable)", placeholder=str(_config.get("autoclose_hours") or 0), required=True, max_length=6)
            self.add_item(self.input)
        async def callback(self, interaction: discord.Interaction):
            try:
                hours = int(self.input.value.strip())
            except Exception:
                await interaction.response.send_message("Provide a valid number (0 to disable).", ephemeral=True); return
            cfg = load_config(); cfg["autoclose_hours"] = max(0, hours); save_config(cfg)
            if hours == 0:
                await interaction.response.send_message("Auto-close disabled.", ephemeral=True)
            else:
                await interaction.response.send_message(f"Auto-close set to {hours} hours.", ephemeral=True)

    # Setup view using buttons that open modals
    class SetupButton(Button):
        def __init__(self, label: str, cid: str, style=discord.ButtonStyle.primary):
            super().__init__(label=label, custom_id=cid, style=style)

        async def callback(self, interaction: discord.Interaction):
            if not is_admin(interaction.user):
                await interaction.response.send_message("Admins only.", ephemeral=True); return
            cid = self.custom_id
            if cid == "btn_title": await interaction.response.send_modal(SetTitleModal()); return
            if cid == "btn_desc": await interaction.response.send_modal(SetDescriptionModal()); return
            if cid == "btn_image": await interaction.response.send_modal(SetImageModal()); return
            if cid == "btn_buttons": await interaction.response.send_modal(SetButtonsModal()); return
            if cid == "btn_category": await interaction.response.send_modal(SetCategoryModal()); return
            if cid == "btn_log": await interaction.response.send_modal(SetLogChannelModal()); return
            if cid == "btn_notify": await interaction.response.send_modal(SetNotifyRoleModal()); return
            if cid == "btn_creation_text": await interaction.response.send_modal(SetCreationTextModal()); return
            if cid == "btn_autoclose": await interaction.response.send_modal(SetAutocloseModal()); return
            if cid == "btn_preview":
                cfg = load_config()
                embed = Embed(title=cfg.get("title"), description=f"{cfg.get('description')}\n\nMade by Max ❤️", color=discord.Color.dark_gray())
                if cfg.get("image"):
                    try: embed.set_thumbnail(url=cfg.get("image"))
                    except Exception: pass
                await interaction.response.send_message("Panel preview (ephemeral):", embed=embed, ephemeral=True); return
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
                    await interaction.response.send_message("Ticket panel posted to this channel.", ephemeral=True)
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
                ("Set Ticket Category", "btn_category"),
                ("Set Log Channel", "btn_log"),
                ("Set Notify Role", "btn_notify"),
                ("Text Sent When Ticket Is Made", "btn_creation_text"),
                ("Set Autoclose Hours", "btn_autoclose"),
                ("Preview Panel", "btn_preview"),
                ("Send Panel Here", "btn_send_panel")
            ]
            for label, cid in items:
                style = discord.ButtonStyle.primary if "Set" in label or "Preview" in label or "Send" in label else discord.ButtonStyle.secondary
                self.add_item(SetupButton(label=label, cid=cid, style=style))

    # Register slash commands that open the SetupView (modals handle input)
    if GUILD_IDS:
        @bot.slash_command(guild_ids=GUILD_IDS, name="ticket_setup", description="Open ticket setup menu (admins only).")
        async def ticket_setup_cmd(ctx: discord.ApplicationContext):
            if not is_admin(ctx.author): await ctx.respond("Admins only.", ephemeral=True); return
            await ctx.respond("Ticket setup — use the buttons to configure the panel.", view=TicketSetupView(), ephemeral=True)

        @bot.slash_command(guild_ids=GUILD_IDS, name="ticket_settings", description="Show ticket settings (admins only).")
        async def ticket_settings_cmd(ctx: discord.ApplicationContext):
            if not is_admin(ctx.author): await ctx.respond("Admins only.", ephemeral=True); return
            cfg = load_config()
            embed = Embed(title="Ticket Settings", color=discord.Color.blurple(), timestamp=datetime.datetime.utcnow())
            embed.add_field(name="Title", value=cfg.get("title") or "—", inline=False)
            desc = cfg.get("description") or "—"
            if len(desc) > 1000: desc = desc[:1000] + "..."
            embed.add_field(name="Description", value=desc, inline=False)
            embed.add_field(name="Buttons", value=", ".join(cfg.get("buttons", [])) or "—", inline=False)
            embed.add_field(name="Log Channel", value=str(cfg.get("log_channel_id") or "None"), inline=False)
            embed.add_field(name="Notify Role", value=str(cfg.get("notify_role_id") or "None"), inline=False)
            embed.add_field(name="Autoclose (hours)", value=str(cfg.get("autoclose_hours", 0)), inline=False)
            await ctx.respond(embed=embed, ephemeral=True)
    else:
        @bot.slash_command(name="ticket_setup", description="Open ticket setup menu (admins only).")
        async def ticket_setup_cmd(ctx: discord.ApplicationContext):
            if not is_admin(ctx.author): await ctx.respond("Admins only.", ephemeral=True); return
            await ctx.respond("Ticket setup — use the buttons to configure the panel.", view=TicketSetupView(), ephemeral=True)

        @bot.slash_command(name="ticket_settings", description="Show ticket settings (admins only).")
        async def ticket_settings_cmd(ctx: discord.ApplicationContext):
            if not is_admin(ctx.author): await ctx.respond("Admins only.", ephemeral=True); return
            cfg = load_config()
            embed = Embed(title="Ticket Settings", color=discord.Color.blurple(), timestamp=datetime.datetime.utcnow())
            embed.add_field(name="Title", value=cfg.get("title") or "—", inline=False)
            desc = cfg.get("description") or "—"
            if len(desc) > 1000: desc = desc[:1000] + "..."
            embed.add_field(name="Description", value=desc, inline=False)
            embed.add_field(name="Buttons", value=", ".join(cfg.get("buttons", [])) or "—", inline=False)
            embed.add_field(name="Log Channel", value=str(cfg.get("log_channel_id") or "None"), inline=False)
            embed.add_field(name="Notify Role", value=str(cfg.get("notify_role_id") or "None"), inline=False)
            embed.add_field(name="Autoclose (hours)", value=str(cfg.get("autoclose_hours", 0)), inline=False)
            await ctx.respond(embed=embed, ephemeral=True)

# If modals are unavailable, provide slash commands to set config (fallback)
else:
    print("Modal support not available in this environment. Providing command-based fallback for settings.")
    # Provide commands such as /set_title, /set_description, etc.
    # These commands allow you to configure everything without modals.
    @bot.slash_command(guild_ids=GUILD_IDS if GUILD_IDS else None, name="set_title", description="Set the ticket panel title (admins only).")
    async def set_title(ctx: discord.ApplicationContext, title: str):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        cfg = load_config(); cfg["title"] = title.strip(); save_config(cfg)
        await ctx.respond("Panel title updated.", ephemeral=True)

    @bot.slash_command(guild_ids=GUILD_IDS if GUILD_IDS else None, name="set_description", description="Set the ticket panel description (admins only).")
    async def set_description(ctx: discord.ApplicationContext, *, description: str):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        cfg = load_config(); cfg["description"] = description.strip(); save_config(cfg)
        await ctx.respond("Panel description updated.", ephemeral=True)

    @bot.slash_command(guild_ids=GUILD_IDS if GUILD_IDS else None, name="set_image", description="Set panel image URL (admins only).")
    async def set_image(ctx: discord.ApplicationContext, image_url: str):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        if image_url and not (image_url.startswith("http://") or image_url.startswith("https://")):
            await ctx.respond("Invalid URL. Must start with http:// or https://", ephemeral=True); return
        cfg = load_config(); cfg["image"] = image_url or None; save_config(cfg)
        await ctx.respond("Panel image updated.", ephemeral=True)

    @bot.slash_command(guild_ids=GUILD_IDS if GUILD_IDS else None, name="set_buttons", description="Set panel buttons (comma separated) (admins only).")
    async def set_buttons(ctx: discord.ApplicationContext, *, buttons: str):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        labels = [s.strip() for s in buttons.split(",") if s.strip()]
        if not labels:
            await ctx.respond("Provide at least one label.", ephemeral=True); return
        cfg = load_config(); cfg["buttons"] = labels; save_config(cfg)
        await ctx.respond(f"Buttons updated: {', '.join(labels)}", ephemeral=True)

    @bot.slash_command(guild_ids=GUILD_IDS if GUILD_IDS else None, name="set_log", description="Set log channel ID (0 to disable) (admins only).")
    async def set_log(ctx: discord.ApplicationContext, channel_id: int):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        cfg = load_config(); cfg["log_channel_id"] = None if channel_id == 0 else channel_id; save_config(cfg)
        await ctx.respond("Log channel updated.", ephemeral=True)

    @bot.slash_command(guild_ids=GUILD_IDS if GUILD_IDS else None, name="set_notify_role", description="Set notify role (ID) (admins only).")
    async def set_notify_role(ctx: discord.ApplicationContext, role_id: int):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        cfg = load_config(); cfg["notify_role_id"] = None if role_id == 0 else role_id; save_config(cfg)
        await ctx.respond("Notify role updated.", ephemeral=True)

    @bot.slash_command(guild_ids=GUILD_IDS if GUILD_IDS else None, name="set_creation_text", description="Set text shown when ticket is created (admins only).")
    async def set_creation_text(ctx: discord.ApplicationContext, *, text: str):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        cfg = load_config(); cfg["creation_text"] = text.strip(); save_config(cfg)
        await ctx.respond("Creation text updated.", ephemeral=True)

    @bot.slash_command(guild_ids=GUILD_IDS if GUILD_IDS else None, name="set_autoclose", description="Set auto-close hours (0 disables) (admins only).")
    async def set_autoclose(ctx: discord.ApplicationContext, hours: int):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        cfg = load_config(); cfg["autoclose_hours"] = max(0, hours); save_config(cfg)
        await ctx.respond(f"Autoclose set to {hours} hours.", ephemeral=True)

    # Provide basic setup panel that only shows preview and send (since we don't have modals)
    class FallbackSetupView(View):
        def __init__(self):
            super().__init__(timeout=None)
            self.add_item(Button(label="Preview Panel", custom_id="fb_preview", style=discord.ButtonStyle.primary))
            self.add_item(Button(label="Send Panel Here", custom_id="fb_send", style=discord.ButtonStyle.success))

        async def on_timeout(self):
            return

    @bot.slash_command(guild_ids=GUILD_IDS if GUILD_IDS else None, name="ticket_setup", description="Open ticket setup (limited fallback).")
    async def ticket_setup_fallback(ctx: discord.ApplicationContext):
        if not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True); return
        await ctx.respond("Fallback setup — use slash commands to configure, or preview/send the panel here.", view=FallbackSetupView(), ephemeral=True)

# ---------- Common slash commands available in both flows ----------
# settings command (show current config)
if GUILD_IDS:
    @bot.slash_command(guild_ids=GUILD_IDS, name="ticket_settings", description="Show ticket settings (admins only).")
    async def ticket_settings(ctx: discord.ApplicationContext):
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
        embed.add_field(name="Autoclose (hours)", value=str(cfg.get("autoclose_hours", 0)), inline=False)
        await ctx.respond(embed=embed, ephemeral=True)
else:
    @bot.slash_command(name="ticket_settings", description="Show ticket settings (admins only).")
    async def ticket_settings(ctx: discord.ApplicationContext):
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
        embed.add_field(name="Autoclose (hours)", value=str(cfg.get("autoclose_hours", 0)), inline=False)
        await ctx.respond(embed=embed, ephemeral=True)

# ---------- Setup panel send helper ----------
@bot.slash_command(name="send_ticket_panel", description="Post the ticket panel in the current channel (admins only).", guild_ids=GUILD_IDS if GUILD_IDS else None)
async def send_ticket_panel(ctx: discord.ApplicationContext):
    if not is_admin(ctx.author):
        await ctx.respond("Admins only.", ephemeral=True); return
    cfg = load_config()
    embed = Embed(title=cfg.get("title"), description=f"{cfg.get('description')}\n\nMade by Max ❤️", color=discord.Color.dark_gray())
    if cfg.get("image"):
        try: embed.set_thumbnail(url=cfg.get("image"))
        except Exception: pass
    view = TicketPanelView(buttons=cfg.get("buttons", DEFAULT_CONFIG["buttons"]))
    try:
        sent = await ctx.channel.send(embed=embed, view=view)
        cfg["panel_message_id"] = sent.id
        cfg["panel_channel_id"] = sent.channel.id
        save_config(cfg)
        await ctx.respond("Ticket panel posted.", ephemeral=True)
    except Exception as e:
        await ctx.respond(f"Failed to post panel: {e}", ephemeral=True)

# ---------- on_ready ----------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    bot.loop.create_task(auto_close_checker())

# ---------- Run ----------
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set in environment.")
    # start bot
    bot.run(TOKEN)
