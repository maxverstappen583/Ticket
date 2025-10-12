"""
Ticket Bot with close button, role ping, transcript logs, one-ticket-per-user.

Requirements:
  - python 3.10+
  - discord.py v2.x (pip install -U "discord.py>=2.0.0")
  - python-dotenv (pip install python-dotenv)

Environment variables:
  - DISCORD_TOKEN (required)
  - GUILD_ID (optional, for faster command sync to a single guild)

Save file as ticket_bot.py and run: python ticket_bot.py
"""

import os
import json
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button
from typing import List, Optional
from dotenv import load_dotenv
import io
import datetime

# Load .env (optional)
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional for guild-scoped commands

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is required.")

CONFIG_FILE = "ticket_config.json"

DEFAULT_CONFIG = {
    "title": "Orihost.com - Ticketing System",
    "description": "Need help? Open a ticket by clicking the button below!\nWe will help you right away!",
    "image": None,
    "buttons": ["Hosting", "Issues", "Suspension", "Other"],
    "category_id": None,
    "panel_message_id": None,
    "panel_channel_id": None,
    "close_text": "please wait until one of our staffs assist u.",
    "notify_role_id": None,
    "log_channel_id": None
}


def load_config() -> dict:
    if not os.path.isfile(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        return DEFAULT_CONFIG.copy()
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except Exception:
            data = DEFAULT_CONFIG.copy()
    for k, v in DEFAULT_CONFIG.items():
        if k not in data:
            data[k] = v
    return data


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


config = load_config()

intents = discord.Intents.default()
intents.message_content = True  # needed to get messages for transcript
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)


def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.administrator


# ----- Views and Buttons -----
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
            if "suspension" in name.lower() or "suspend" in name.lower():
                style = discord.ButtonStyle.danger
            elif "other" in name.lower():
                style = discord.ButtonStyle.secondary
            self.add_item(TicketButton(label=name, style=style))


class TicketCloseButton(Button):
    def __init__(self, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        # Only admins can close ticket
        if not is_admin(interaction):
            await interaction.response.send_message("Only administrators can close tickets.", ephemeral=True)
            return
        await handle_close(interaction)


def make_close_view(close_text: str) -> View:
    view = View()
    view.add_item(TicketCloseButton(label="Close Ticket"))
    # We'll display the close_text inside the ticket embed; button label remains "Close Ticket"
    return view


# ----- Ticket Creation Handler -----
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

    # Build channel name
    clean_name = member.name.lower().replace(" ", "-")
    channel_name = f"ticket-{clean_name}"
    base = channel_name
    count = 1
    while discord.utils.get(guild.text_channels, name=channel_name) is not None:
        count += 1
        channel_name = f"{base}-{count}"

    # Category
    category = None
    if cfg.get("category_id"):
        category = guild.get_channel(cfg["category_id"])
        if category is None or not isinstance(category, discord.CategoryChannel):
            category = None

    # Overwrites: @everyone denied, ticket user allowed, admin roles allowed
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    for role in guild.roles:
        if role.permissions.administrator:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    # Create channel
    try:
        created_channel = await guild.create_text_channel(
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

    # Prepare ticket embed and close button view
    close_text = cfg.get("close_text", DEFAULT_CONFIG["close_text"])
    ticket_embed = discord.Embed(
        title=f"Ticket — {issue_type}",
        description=(f"Hello {member.mention},\n\n"
                     f"{close_text}\n\n"
                     f"**Issue:** {issue_type}\n\nMade by Max❤️"),
        color=discord.Color.blurple(),
        timestamp=datetime.datetime.utcnow()
    )
    if cfg.get("image"):
        ticket_embed.set_thumbnail(url=cfg["image"])

    # Send initial message with close button (admins only can press)
    close_view = make_close_view(close_text=close_text)
    try:
        await created_channel.send(content=f"{member.mention}", embed=ticket_embed, view=close_view)
    except Exception:
        pass

    # Ping notify role if set
    if cfg.get("notify_role_id"):
        role = guild.get_role(cfg["notify_role_id"])
        if role:
            try:
                await created_channel.send(f"{role.mention} New ticket opened: {created_channel.mention}")
            except Exception:
                pass

    # Log to log channel (embed)
    log_channel = None
    if cfg.get("log_channel_id"):
        log_channel = guild.get_channel(cfg["log_channel_id"])
    if log_channel and isinstance(log_channel, discord.TextChannel):
        embed = discord.Embed(title="Ticket Created", color=discord.Color.green(), timestamp=datetime.datetime.utcnow())
        embed.add_field(name="User", value=f"{member} ({member.id})", inline=False)
        embed.add_field(name="Issue", value=issue_type, inline=False)
        embed.add_field(name="Channel", value=created_channel.mention, inline=False)
        try:
            await log_channel.send(embed=embed)
        except Exception:
            pass

    # Confirm to user
    await interaction.followup.send(f"Your ticket has been created: {created_channel.mention}", ephemeral=True)


# ----- Ticket Close Handler -----
async def handle_close(interaction: discord.Interaction):
    channel = interaction.channel
    if channel is None:
        await interaction.response.send_message("This command must be used inside the ticket channel.", ephemeral=True)
        return

    # Check it's a ticket channel (name or topic)
    if not (channel.name.startswith("ticket-") or (channel.topic and "Ticket for" in channel.topic)):
        await interaction.response.send_message("This does not appear to be a ticket channel.", ephemeral=True)
        return

    # Only admins can close (checked earlier) but double-check
    if not is_admin(interaction):
        await interaction.response.send_message("Only administrators can close tickets.", ephemeral=True)
        return

    cfg = load_config()

    # Announce deletion countdown
    try:
        await interaction.response.send_message("Deleting the ticket in a few seconds...", ephemeral=False)
    except Exception:
        pass

    # Wait a few seconds
    await asyncio.sleep(4)

    # Collect transcript
    transcript_lines = []
    try:
        async for msg in channel.history(limit=None, oldest_first=True):
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            author = f"{msg.author} ({msg.author.id})"
            content = msg.content
            attachments = " ".join(a.url for a in msg.attachments) if msg.attachments else ""
            line = f"[{timestamp}] {author}: {content} {attachments}"
            transcript_lines.append(line)
    except Exception:
        transcript_lines.append("Failed to fetch some messages for transcript due to permissions.")

    transcript_text = "\n".join(transcript_lines) if transcript_lines else "No messages found."

    # Create a text file in memory
    transcript_file = io.StringIO(transcript_text)
    transcript_file.seek(0)
    discord_file = discord.File(fp=io.BytesIO(transcript_text.encode("utf-8")), filename=f"transcript-{channel.name}.txt")

    # Send transcript to log channel and DM ticket author
    log_channel = None
    if cfg.get("log_channel_id"):
        log_channel = channel.guild.get_channel(cfg["log_channel_id"])

    # Build embed for deletion
    del_embed = discord.Embed(title="Ticket Closed & Deleted", color=discord.Color.red(), timestamp=datetime.datetime.utcnow())
    # Try to extract user id from topic
    ticket_owner_id = None
    if channel.topic:
        # topic format: "Ticket for <Member> (ID: <id>) | Issue: <issue>"
        try:
            if "ID:" in channel.topic:
                part = channel.topic.split("ID:")[1]
                ticket_owner_id = int(part.split(")")[0].strip())
        except Exception:
            ticket_owner_id = None

    del_embed.add_field(name="Channel", value=channel.name, inline=False)
    if ticket_owner_id:
        del_embed.add_field(name="Ticket Owner ID", value=str(ticket_owner_id), inline=False)

    # Send to log channel
    if log_channel and isinstance(log_channel, discord.TextChannel):
        try:
            await log_channel.send(embed=del_embed)
            # send transcript file
            await log_channel.send(file=discord_file)
        except Exception:
            pass

    # DM the ticket owner the transcript
    if ticket_owner_id:
        try:
            user = await bot.fetch_user(ticket_owner_id)
            if user:
                await user.send(f"Your ticket **{channel.name}** has been closed. Here is the transcript:", file=discord_file)
        except Exception:
            pass

    # Finally delete the channel
    try:
        await channel.delete(reason=f"Ticket closed by {interaction.user}")
    except Exception:
        # If deletion failed, send a message that deletion failed
        try:
            await interaction.followup.send("Failed to delete the ticket channel. Please remove it manually.", ephemeral=True)
        except Exception:
            pass


# ----- Slash commands for admin configuration -----
@app_commands.command(name="setup_ticket", description="Send the ticket panel (admins only).")
async def setup_ticket(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("You must have Administrator permission to use this.", ephemeral=True)
        return

    cfg = load_config()
    title = cfg.get("title", DEFAULT_CONFIG["title"])
    desc = cfg.get("description", DEFAULT_CONFIG["description"])
    image = cfg.get("image")
    buttons = cfg.get("buttons", DEFAULT_CONFIG["buttons"])

    embed = discord.Embed(title=title, description=f"{desc}\n\nMade by Max❤️", color=discord.Color.dark_gray())
    if image:
        embed.set_thumbnail(url=image)
    view = TicketPanelView(buttons=buttons)

    try:
        sent = await interaction.channel.send(embed=embed, view=view)
        cfg["panel_message_id"] = sent.id
        cfg["panel_channel_id"] = sent.channel.id
        save_config(cfg)
        await interaction.response.send_message("Ticket panel sent.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to send messages here or create embeds.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to send panel: {e}", ephemeral=True)


@app_commands.command(name="set_ticket_title", description="Set the ticket embed title (admins only).")
@app_commands.describe(title="New embed title (e.g., Maxy Support - Ticket System)")
async def set_ticket_title(interaction: discord.Interaction, title: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True); return
    cfg = load_config()
    cfg["title"] = title
    save_config(cfg)
    await interaction.response.send_message("Ticket title updated.", ephemeral=True)


@app_commands.command(name="set_ticket_desc", description="Set the ticket embed description (admins only).")
@app_commands.describe(description="New description text (you can use newlines).")
async def set_ticket_desc(interaction: discord.Interaction, description: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True); return
    cfg = load_config()
    cfg["description"] = description
    save_config(cfg)
    await interaction.response.send_message("Ticket description updated.", ephemeral=True)


@app_commands.command(name="set_ticket_image", description="Set the ticket embed thumbnail image URL (admins only).")
@app_commands.describe(url="Image URL to use as thumbnail in the ticket panel embed.")
async def set_ticket_image(interaction: discord.Interaction, url: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True); return
    if not (url.startswith("http://") or url.startswith("https://")):
        await interaction.response.send_message("Please provide a valid http/https image URL.", ephemeral=True); return
    cfg = load_config()
    cfg["image"] = url
    save_config(cfg)
    await interaction.response.send_message("Ticket image updated.", ephemeral=True)


@app_commands.command(name="set_ticket_buttons", description="Set the ticket button labels (comma-separated) (admins only).")
@app_commands.describe(buttons="Comma-separated labels e.g. Hosting, Issues, Suspension, Other")
async def set_ticket_buttons(interaction: discord.Interaction, buttons: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True); return
    labels = [b.strip() for b in buttons.split(",") if b.strip()]
    if not labels:
        await interaction.response.send_message("Provide at least one button label.", ephemeral=True); return
    cfg = load_config()
    cfg["buttons"] = labels
    save_config(cfg)
    await interaction.response.send_message(f"Ticket buttons updated: {', '.join(labels)}", ephemeral=True)


@app_commands.command(name="set_ticket_category", description="Set the category to create ticket channels under (admins only). Use category ID.")
@app_commands.describe(category_id="The category channel ID (numbers).")
async def set_ticket_category(interaction: discord.Interaction, category_id: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True); return
    try:
        cid = int(category_id)
    except ValueError:
        await interaction.response.send_message("Category ID must be an integer ID.", ephemeral=True); return
    category = interaction.guild.get_channel(cid)
    if category is None or not isinstance(category, discord.CategoryChannel):
        await interaction.response.send_message("Could not find a category with that ID in this server.", ephemeral=True); return
    cfg = load_config()
    cfg["category_id"] = cid
    save_config(cfg)
    await interaction.response.send_message(f"Ticket category set to: {category.name}", ephemeral=True)


@app_commands.command(name="set_close_text", description="Set the close message text shown inside new tickets (admins only).")
@app_commands.describe(text="Close message text (default: please wait until one of our staffs assist u.)")
async def set_close_text(interaction: discord.Interaction, text: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True); return
    cfg = load_config()
    cfg["close_text"] = text
    save_config(cfg)
    await interaction.response.send_message("Close text updated.", ephemeral=True)


@app_commands.command(name="set_notify_role", description="Set a role to ping when a ticket is created (admins only).")
@app_commands.describe(role="Role mention or ID to ping (use @role or provide ID). Use 0 to disable.")
async def set_notify_role(interaction: discord.Interaction, role: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True); return
    cfg = load_config()
    if role.strip() == "0":
        cfg["notify_role_id"] = None
        save_config(cfg)
        await interaction.response.send_message("Notify role disabled.", ephemeral=True)
        return
    # Try parse mention or ID
    role_id = None
    if role.isdigit():
        role_id = int(role)
    else:
        # mention format: <@&ID>
        if role.startswith("<@&") and role.endswith(">"):
            try:
                role_id = int(role[3:-1])
            except Exception:
                role_id = None
    if role_id is None:
        await interaction.response.send_message("Could not parse role. Provide role mention or ID, or 0 to disable.", ephemeral=True); return
    found = interaction.guild.get_role(role_id)
    if not found:
        await interaction.response.send_message("Role not found in this server.", ephemeral=True); return
    cfg["notify_role_id"] = role_id
    save_config(cfg)
    await interaction.response.send_message(f"Notify role set to: {found.name}", ephemeral=True)


@app_commands.command(name="set_log_channel", description="Set the channel to send ticket logs & transcripts to (admins only).")
@app_commands.describe(channel_id="Text channel ID (numbers). Use 0 to disable.")
async def set_log_channel(interaction: discord.Interaction, channel_id: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True); return
    cfg = load_config()
    if channel_id.strip() == "0":
        cfg["log_channel_id"] = None
        save_config(cfg)
        await interaction.response.send_message("Log channel disabled.", ephemeral=True)
        return
    try:
        cid = int(channel_id)
    except ValueError:
        await interaction.response.send_message("Channel ID must be numeric.", ephemeral=True); return
    ch = interaction.guild.get_channel(cid)
    if not ch or not isinstance(ch, discord.TextChannel):
        await interaction.response.send_message("Text channel not found in this server.", ephemeral=True); return
    cfg["log_channel_id"] = cid
    save_config(cfg)
    await interaction.response.send_message(f"Log channel set to: {ch.mention}", ephemeral=True)


# Add commands to tree
bot.tree.add_command(setup_ticket)
bot.tree.add_command(set_ticket_title)
bot.tree.add_command(set_ticket_desc)
bot.tree.add_command(set_ticket_image)
bot.tree.add_command(set_ticket_buttons)
bot.tree.add_command(set_ticket_category)
bot.tree.add_command(set_close_text)
bot.tree.add_command(set_notify_role)
bot.tree.add_command(set_log_channel)


# Close command (alternative to pressing button)
@app_commands.command(name="close_ticket", description="Close the ticket (admins only).")
async def close_ticket(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True); return
    await handle_close(interaction)


bot.tree.add_command(close_ticket)


# Helper to resend panel
@app_commands.command(name="resend_ticket_panel", description="Re-send the configured ticket panel (admins only).")
async def resend_ticket_panel(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True); return
    cfg = load_config()
    title = cfg.get("title", DEFAULT_CONFIG["title"])
    desc = cfg.get("description", DEFAULT_CONFIG["description"])
    image = cfg.get("image")
    buttons = cfg.get("buttons", DEFAULT_CONFIG["buttons"])
    embed = discord.Embed(title=title, description=f"{desc}\n\nMade by Max❤️", color=discord.Color.dark_gray())
    if image:
        embed.set_thumbnail(url=image)
    view = TicketPanelView(buttons=buttons)
    try:
        sent = await interaction.channel.send(embed=embed, view=view)
        cfg["panel_message_id"] = sent.id
        cfg["panel_channel_id"] = sent.channel.id
        save_config(cfg)
        await interaction.response.send_message("Ticket panel re-sent.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to send panel: {e}", ephemeral=True)


bot.tree.add_command(resend_ticket_panel)


# Bot events
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await sync_commands()
    print("Commands synced.")


async def sync_commands():
    await asyncio.sleep(1)
    if GUILD_ID:
        try:
            guild_obj = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild_obj)
            await bot.tree.sync(guild=guild_obj)
            print(f"Synced commands to guild {GUILD_ID}")
            return
        except Exception as e:
            print("Guild sync failed:", e)
    try:
        await bot.tree.sync()
        print("Synced global commands.")
    except Exception as e:
        print("Global sync failed:", e)


# Start bot
if __name__ == "__main__":
    bot.run(TOKEN)
