# main.py - Maxy Does Tickets (final, Render-ready)
# ------------------------------------------------
# - Flask keep-alive for Render Web Service
# - audioop disable for Pycord on Python 3.13+
# - Uses ui.TextInput + discord.enums.TextStyle for compatibility
# - Full ticket system (button-driven setup UI, /settings, autoclose, transcripts)
# ------------------------------------------------

# ------------------- Keep-alive (Flask) -------------------
from threading import Thread
from flask import Flask
import os

app = Flask('')

@app.route('/')
def home():
    return "✅ Maxy Ticket Bot is running."

def run_web():
    port = int(os.environ.get("PORT", 8080))
    # disable Flask reloader in hosting environments
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

Thread(target=run_web).start()
# ---------------------------------------------------------

# ------------------- audioop fix (must run BEFORE discord import) -------------------
os.environ["DISCORD_DISABLE_VOICE"] = "1"
# -----------------------------------------------------------------------------------

# Standard imports
import json
import io
import asyncio
import datetime
from datetime import timedelta
from typing import List, Optional
from dotenv import load_dotenv
load_dotenv()  # local testing: reads .env; Render uses environment variables

# Discord / Pycord imports
import discord
from discord import Embed, File
from discord.ui import View, Button, Modal
from discord import ui  # use ui.TextInput for compatibility
from discord.enums import TextStyle  # TextStyle location for py-cord 2.6.1+

# debug version print
print("✅ Pycord / discord version:", getattr(discord, "__version__", "unknown"))

# ------------------- Config / Persistence -------------------
CONFIG_FILE = "ticket_config.json"
DEFAULT_CONFIG = {
    "title": "Maxy Does Tickets – Support System",
    "description": "Need help? Open a ticket by clicking a button below!\nOur staff will assist you as soon as possible.",
    "image": None,
    "buttons": ["Hosting", "Issues", "Suspension", "Other"],
    "category_id": None,
    "panel_message_id": None,
    "panel_channel_id": None,
    "creation_text": "please wait until one of our staffs assist u.",  # text shown when ticket is created
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
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
    return cfg

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

# load config initially
config = load_config()

# ------------------- Intents & Bot -------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
intents.guilds = True

try:
    bot = discord.Bot(intents=intents)
except discord.PrivilegedIntentsRequired:
    # fallback to limited mode if portal not configured: still runs
    intents.members = False
    intents.presences = False
    bot = discord.Bot(intents=intents)
    print("⚠️ Privileged intents not enabled in Developer Portal — running limited mode (members/presences disabled).")

# ------------------- Utility -------------------
def is_admin(user: discord.Member) -> bool:
    try:
        return user.guild_permissions.administrator
    except Exception:
        return False

# ------------------- Views & Buttons -------------------
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
        if not is_admin(interaction.user):
            await interaction.response.send_message("Only administrators can close tickets.", ephemeral=True)
            return
        await handle_close(interaction)

def make_close_view() -> View:
    v = View(timeout=None)
    v.add_item(CloseTicketButton())
    return v

# ------------------- Ticket creation -------------------
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

    # create channel name (unique)
    safe_name = member.name.lower().replace(" ", "-")[:50]
    base_name = f"ticket-{safe_name}"
    channel_name = base_name
    counter = 1
    while discord.utils.get(guild.text_channels, name=channel_name) is not None:
        counter += 1
        channel_name = f"{base_name}-{counter}"

    # category if set
    category = None
    if cfg.get("category_id"):
        category = guild.get_channel(cfg["category_id"])
        if category is None or not isinstance(category, discord.CategoryChannel):
            category = None

    # permission overwrites
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
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

    creation_text = cfg.get("creation_text", DEFAULT_CONFIG["creation_text"])
    embed = Embed(
        title=f"Ticket — {issue_type}",
        description=(f"Hello {member.mention},\n\n{creation_text}\n\n**Issue:** {issue_type}\n\nMade by Max ❤️"),
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

    # log creation
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

    await interaction.followup.send(f"Your ticket has been created: {created.mention}", ephemeral=True)

# ------------------- Close handler & transcript -------------------
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

    transcript_text = "\n".join(lines) if lines else "No messages found."
    transcript_bytes = transcript_text.encode("utf-8")

    ticket_owner_id = None
    if channel.topic:
        try:
            if "ID:" in channel.topic:
                ticket_owner_id = int(channel.topic.split("ID:")[1].split(")")[0].strip())
        except Exception:
            ticket_owner_id = None

    # log deletion and transcript
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

    try:
        await channel.delete(reason=f"Ticket closed by {interaction.user}")
    except Exception:
        try:
            await interaction.followup.send("Failed to delete the ticket channel; please remove it manually.", ephemeral=True)
        except Exception:
            pass

# ------------------- Auto-close background task -------------------
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
                            if last_msg and last_msg.created_at.replace(tzinfo=None) < cutoff:
                                try:
                                    await channel.send("🕐 No activity detected for a while. Deleting the ticket in a few seconds...")
                                    await asyncio.sleep(5)
                                    # gather transcript
                                    lines = []
                                    async for msg in channel.history(limit=None, oldest_first=True):
                                        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                                        author = f"{msg.author} ({msg.author.id})"
                                        content = msg.content or ""
                                        attachments = " ".join(a.url for a in msg.attachments) if msg.attachments else ""
                                        lines.append(f"[{ts}] {author}: {content} {attachments}")
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
                                    # DM owner
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

# ------------------- Modal classes (use ui.TextInput + TextStyle) -------------------
class SimpleModal(Modal):
    def __init__(self, title: str, label: str, placeholder: str = "", style=TextStyle.short, custom_id: str = "simple_modal"):
        super().__init__(title=title, custom_id=custom_id)
        # ui.TextInput is used for compatibility across py-cord installs
        self.input = ui.TextInput(label=label, placeholder=placeholder, style=style, required=True)
        self.add_item(self.input)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("Saved.", ephemeral=True)

# Specific modals:
class TitleModal(SimpleModal):
    def __init__(self):
        super().__init__(title="Set Panel Title", label="Panel title", placeholder="Maxy Does Tickets – Support System", custom_id="title_modal")
    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user): await interaction.response.send_message("Admins only.", ephemeral=True); return
        cfg = load_config(); cfg["title"] = self.input.value; save_config(cfg)
        await interaction.response.send_message("Panel title updated.", ephemeral=True)

class DescModal(SimpleModal):
    def __init__(self):
        super().__init__(title="Set Panel Description", label="Panel description", placeholder="Need help? Open a ticket...", style=TextStyle.paragraph, custom_id="desc_modal")
    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user): await interaction.response.send_message("Admins only.", ephemeral=True); return
        cfg = load_config(); cfg["description"] = self.input.value; save_config(cfg)
        await interaction.response.send_message("Panel description updated.", ephemeral=True)

class ImageModal(SimpleModal):
    def __init__(self):
        super().__init__(title="Set Panel Image URL", label="Image URL", placeholder="https://example.com/image.png", custom_id="image_modal")
    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user): await interaction.response.send_message("Admins only.", ephemeral=True); return
        url = self.input.value.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await interaction.response.send_message("Provide a valid http/https URL.", ephemeral=True); return
        cfg = load_config(); cfg["image"] = url; save_config(cfg)
        await interaction.response.send_message("Panel image updated.", ephemeral=True)

class ButtonsModal(SimpleModal):
    def __init__(self):
        super().__init__(title="Set Panel Buttons", label="Buttons (comma separated)", placeholder="Hosting, Issues, Suspension, Other", custom_id="buttons_modal")
    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user): await interaction.response.send_message("Admins only.", ephemeral=True); return
        labels = [b.strip() for b in self.input.value.split(",") if b.strip()]
        if not labels:
            await interaction.response.send_message("Provide at least one button label.", ephemeral=True); return
        cfg = load_config(); cfg["buttons"] = labels; save_config(cfg)
        await interaction.response.send_message(f"Panel buttons updated: {', '.join(labels)}", ephemeral=True)

class CategoryModal(SimpleModal):
    def __init__(self):
        super().__init__(title="Set Ticket Category ID", label="Category ID", placeholder="123456789012345678", custom_id="category_modal")
    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user): await interaction.response.send_message("Admins only.", ephemeral=True); return
        try: cid = int(self.input.value.strip())
        except Exception: await interaction.response.send_message("Category ID must be numeric.", ephemeral=True); return
        cat = interaction.guild.get_channel(cid)
        if not cat or not isinstance(cat, discord.CategoryChannel): await interaction.response.send_message("Category not found in this server.", ephemeral=True); return
        cfg = load_config(); cfg["category_id"] = cid; save_config(cfg); await interaction.response.send_message(f"Ticket category set to: {cat.name}", ephemeral=True)

class LogChannelModal(SimpleModal):
    def __init__(self):
        super().__init__(title="Set Log Channel ID", label="Channel ID (0 to disable)", placeholder="123456789012345678 or 0", custom_id="log_modal")
    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user): await interaction.response.send_message("Admins only.", ephemeral=True); return
        v = self.input.value.strip()
        if v == "0":
            cfg = load_config(); cfg["log_channel_id"] = None; save_config(cfg); await interaction.response.send_message("Log channel disabled.", ephemeral=True); return
        try: cid = int(v)
        except Exception: await interaction.response.send_message("Channel ID must be numeric or 0.", ephemeral=True); return
        ch = interaction.guild.get_channel(cid)
        if not ch or not isinstance(ch, discord.TextChannel): await interaction.response.send_message("Text channel not found in this server.", ephemeral=True); return
        cfg = load_config(); cfg["log_channel_id"] = cid; save_config(cfg); await interaction.response.send_message(f"Log channel set to: {ch.mention}", ephemeral=True)

class NotifyRoleModal(SimpleModal):
    def __init__(self):
        super().__init__(title="Set Notify Role", label="Role (mention or ID) or 0 to disable", placeholder="@Support or 0", custom_id="notify_modal")
    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user): await interaction.response.send_message("Admins only.", ephemeral=True); return
        v = self.input.value.strip()
        if v == "0":
            cfg = load_config(); cfg["notify_role_id"] = None; save_config(cfg); await interaction.response.send_message("Notify role disabled.", ephemeral=True); return
        role_id = None
        if v.isdigit(): role_id = int(v)
        else:
            if v.startswith("<@&") and v.endswith(">"):
                try: role_id = int(v[3:-1])
                except Exception: role_id = None
        if role_id is None: await interaction.response.send_message("Could not parse role. Provide mention or ID or 0 to disable.", ephemeral=True); return
        r = interaction.guild.get_role(role_id)
        if not r: await interaction.response.send_message("Role not found.", ephemeral=True); return
        cfg = load_config(); cfg["notify_role_id"] = role_id; save_config(cfg); await interaction.response.send_message(f"Notify role set to: {r.name}", ephemeral=True)

class CreationTextModal(SimpleModal):
    def __init__(self):
        super().__init__(title="Set Text Sent When Ticket Is Made", label="Text shown when ticket created", style=TextStyle.paragraph, custom_id="creation_text_modal")
    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user): await interaction.response.send_message("Admins only.", ephemeral=True); return
        cfg = load_config(); cfg["creation_text"] = self.input.value; save_config(cfg); await interaction.response.send_message("Text updated.", ephemeral=True)

class AutocloseModal(SimpleModal):
    def __init__(self):
        super().__init__(title="Set Autoclose Hours", label="Hours (0 to disable)", placeholder="3", custom_id="autoclose_modal")
    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user): await interaction.response.send_message("Admins only.", ephemeral=True); return
        try: hours = int(self.input.value.strip())
        except Exception: await interaction.response.send_message("Please provide a number (0 to disable).", ephemeral=True); return
        if hours < 0: await interaction.response.send_message("Hours cannot be negative.", ephemeral=True); return
        cfg = load_config(); cfg["autoclose_hours"] = hours; save_config(cfg)
        if hours == 0: await interaction.response.send_message("Auto-close disabled.", ephemeral=True)
        else: await interaction.response.send_message(f"Auto-close set to {hours} hours.", ephemeral=True)

# ------------------- Ticket setup view (buttons) -------------------
class TicketSetupView(View):
    def __init__(self):
        super().__init__(timeout=None)
        ui_buttons = [
            ("Set Panel Title", "btn_title", discord.ButtonStyle.primary),
            ("Set Panel Description", "btn_desc", discord.ButtonStyle.primary),
            ("Set Panel Image", "btn_image", discord.ButtonStyle.primary),
            ("Set Panel Buttons", "btn_buttons", discord.ButtonStyle.primary),
            ("Set Ticket Category", "btn_category", discord.ButtonStyle.secondary),
            ("Set Log Channel", "btn_log", discord.ButtonStyle.secondary),
            ("Set Notify Role", "btn_notify", discord.ButtonStyle.secondary),
            ("Text Sent When Ticket Is Made", "btn_creation_text", discord.ButtonStyle.secondary),
            ("Set Autoclose Hours", "btn_autoclose", discord.ButtonStyle.danger),
            ("Preview Panel", "btn_preview", discord.ButtonStyle.success),
            ("Send Panel Here", "btn_send_panel", discord.ButtonStyle.success)
        ]
        for label, cid, style in ui_buttons:
            btn = Button(label=label, custom_id=cid, style=style)
            self.add_item(btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction.user):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return False
        return True

# ------------------- Central interaction handler for setup buttons -------------------
@bot.event
async def on_interaction(interaction: discord.Interaction):
    try:
        if interaction.type == discord.InteractionType.component and interaction.data and "custom_id" in interaction.data:
            cid = interaction.data["custom_id"]
            if cid.startswith("btn_"):
                if not is_admin(interaction.user):
                    await interaction.response.send_message("Admins only.", ephemeral=True); return
                # map ids to modals/actions
                if cid == "btn_title": await interaction.response.send_modal(TitleModal()); return
                if cid == "btn_desc": await interaction.response.send_modal(DescModal()); return
                if cid == "btn_image": await interaction.response.send_modal(ImageModal()); return
                if cid == "btn_buttons": await interaction.response.send_modal(ButtonsModal()); return
                if cid == "btn_category": await interaction.response.send_modal(CategoryModal()); return
                if cid == "btn_log": await interaction.response.send_modal(LogChannelModal()); return
                if cid == "btn_notify": await interaction.response.send_modal(NotifyRoleModal()); return
                if cid == "btn_creation_text": await interaction.response.send_modal(CreationTextModal()); return
                if cid == "btn_autoclose": await interaction.response.send_modal(AutocloseModal()); return
                if cid == "btn_preview":
                    cfg = load_config()
                    embed = Embed(title=cfg.get("title"), description=f"{cfg.get('description')}\n\nMade by Max ❤️", color=discord.Color.dark_gray())
                    if cfg.get("image"):
                        try: embed.set_thumbnail(url=cfg.get("image"))
                        except Exception: pass
                    await interaction.response.send_message("Here is the current panel preview:", embed=embed, ephemeral=True)
                    return
                if cid == "btn_send_panel":
                    cfg = load_config()
                    embed = Embed(title=cfg.get("title"), description=f"{cfg.get('description')}\n\nMade by Max ❤️", color=discord.Color.dark_gray())
                    if cfg.get("image"):
                        try: embed.set_thumbnail(url=cfg.get("image"))
                        except Exception: pass
                    view = TicketPanelView(buttons=cfg.get("buttons", []))
                    try:
                        sent = await interaction.channel.send(embed=embed, view=view)
                        cfg["panel_message_id"] = sent.id
                        cfg["panel_channel_id"] = sent.channel.id
                        save_config(cfg)
                        await interaction.response.send_message("Ticket panel sent to this channel.", ephemeral=True)
                    except Exception as e:
                        await interaction.response.send_message(f"Failed to send panel: {e}", ephemeral=True)
                    return
    except Exception as e:
        print("on_interaction handler error:", e)
    # allow other interactions to be handled by pycord normally

# ------------------- Slash commands -------------------
ticket_group = bot.create_group("ticket", "Ticket related commands")

@ticket_group.command(name="setup", description="Open ticket setup menu (buttons). Admins only.")
async def ticket_setup(ctx: discord.ApplicationContext):
    if not is_admin(ctx.author):
        await ctx.respond("Admins only.", ephemeral=True); return
    view = TicketSetupView()
    await ctx.respond("Ticket setup — use the buttons to configure the panel. Each button will prompt for input.", view=view, ephemeral=True)

@bot.slash_command(name="settings", description="Show current ticket config (admins only).")
async def settings(ctx: discord.ApplicationContext):
    if not is_admin(ctx.author):
        await ctx.respond("Admins only.", ephemeral=True); return
    cfg = load_config()
    embed = Embed(title="Ticket Config", color=discord.Color.blurple(), timestamp=datetime.datetime.utcnow())
    embed.add_field(name="Title", value=cfg.get("title") or "—", inline=False)
    desc = cfg.get("description") or "—"
    if len(desc) > 1000: desc = desc[:1000] + "..."
    embed.add_field(name="Description", value=desc, inline=False)
    embed.add_field(name="Image", value=cfg.get("image") or "—", inline=False)
    embed.add_field(name="Buttons", value=", ".join(cfg.get("buttons", [])) or "—", inline=False)
    ch = "None"
    if cfg.get("category_id"):
        cat = ctx.guild.get_channel(cfg["category_id"])
        ch = cat.name if cat else f"ID: {cfg['category_id']}"
    embed.add_field(name="Ticket Category", value=ch, inline=False)
    log = "None"
    if cfg.get("log_channel_id"):
        lc = ctx.guild.get_channel(cfg["log_channel_id"])
        log = lc.mention if lc else f"ID: {cfg['log_channel_id']}"
    embed.add_field(name="Log Channel", value=log, inline=False)
    role = "None"
    if cfg.get("notify_role_id"):
        r = ctx.guild.get_role(cfg["notify_role_id"])
        role = r.name if r else f"ID: {cfg['notify_role_id']}"
    embed.add_field(name="Notify Role", value=role, inline=False)
    embed.add_field(name="Text Sent When Ticket Is Made", value=cfg.get("creation_text") or "—", inline=False)
    embed.add_field(name="Auto-close (hours)", value=str(cfg.get("autoclose_hours", 0)), inline=False)
    await ctx.respond(embed=embed, ephemeral=True)

# convenience / legacy commands still available
@bot.slash_command(name="setup_ticket", description="(legacy) send ticket panel to this channel (admins only).")
async def setup_ticket(ctx: discord.ApplicationContext):
    if not is_admin(ctx.author):
        await ctx.respond("Admins only.", ephemeral=True); return
    cfg = load_config()
    embed = Embed(title=cfg.get("title"), description=f"{cfg.get('description')}\n\nMade by Max ❤️", color=discord.Color.dark_gray())
    if cfg.get("image"):
        try: embed.set_thumbnail(url=cfg.get("image"))
        except Exception: pass
    view = TicketPanelView(buttons=cfg.get("buttons", []))
    try:
        sent = await ctx.channel.send(embed=embed, view=view)
        cfg["panel_message_id"] = sent.id
        cfg["panel_channel_id"] = sent.channel.id
        save_config(cfg)
        await ctx.respond("Ticket panel sent.", ephemeral=True)
    except Exception as e:
        await ctx.respond(f"Failed to send panel: {e}", ephemeral=True)

@bot.slash_command(name="close_ticket", description="Close the ticket (admins only).")
async def close_ticket(ctx: discord.ApplicationContext):
    if not is_admin(ctx.author):
        await ctx.respond("Admins only.", ephemeral=True); return
    await handle_close(ctx.interaction)

# ------------------- Bot events -------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print("Bot ready. Use /ticket setup to open the config menu, or /setup_ticket to post the panel.")
    # start background autoclose checker once
    bot.loop.create_task(auto_close_checker())

# ------------------- Run -------------------
if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_TOKEN")
    if not TOKEN:
        raise RuntimeError("❌ DISCORD_TOKEN not found. Set it in Render env vars or local .env for testing.")
    try:
        asyncio.run(bot.start(TOKEN))
    except KeyboardInterrupt:
        print("🛑 Bot stopped manually.")
