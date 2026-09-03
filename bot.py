import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import datetime
import json
import re
from dotenv import load_dotenv
import os
from supabase import create_client, Client
from aiohttp import web
import aiohttp

load_dotenv()

TOKEN = os.getenv("TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

intents = discord.Intents.default()
intents.members = True
intents.moderation = True
intents.message_content = True

bot = commands.Bot(command_prefix=".", intents=intents, activity=discord.Game("Evil Simulator 2026"))

PROTECTED_ROLE_ID = int(os.getenv("PROTECTED_ROLE_ID"))
IMMUNE_BYPASS_ROLE_ID = int(os.getenv("IMMUNE_BYPASS_ROLE_ID"))
PROBATION_CHANNEL_ID = int(os.getenv("PROBATION_CHANNEL_ID"))
PROBATION_LOG_CHANNEL_ID = int(os.getenv("PROBATION_LOG_CHANNEL_ID"))
PROBATION_ROLE_ID = int(os.getenv("PROBATION_ROLE_ID"))
BOT_USER_ID = 1522945518932725810
TIMED_BANS_PATH = os.path.join(os.path.dirname(__file__), "timed_bans.json")
scheduled_unban_tasks = {}
timed_bans_restored = False

PIRACY_REPORTS_CHANNEL_ID = 1510692904723546112
PIRACY_PING_ROLE_ID = 1466886144292557025
PIRACY_MOD_CHANNEL_ID = 1466887398704021689
tree_synced = False


def can_ban_target(author: discord.Member, target) -> bool:
    if not isinstance(target, discord.Member):
        return True
    has_protected_role = any(r.id == PROTECTED_ROLE_ID for r in target.roles)
    if not has_protected_role:
        return True
    return any(r.id == IMMUNE_BYPASS_ROLE_ID for r in author.roles)


def is_staff(member: discord.Member) -> bool:
    return isinstance(member, discord.Member) and any(r.id == PROTECTED_ROLE_ID for r in member.roles)


def is_immune_bypass(member: discord.Member) -> bool:
    return isinstance(member, discord.Member) and any(r.id == IMMUNE_BYPASS_ROLE_ID for r in member.roles)


def log_channel(guild: discord.Guild):
    return guild.get_channel(LOG_CHANNEL_ID)


def pro_log_channel(guild: discord.Guild):
    return guild.get_channel(PROBATION_LOG_CHANNEL_ID)


def base_embed(title, color):
    return discord.Embed(title=title, color=color, timestamp=datetime.datetime.utcnow())


def parse_iso8601_timestamp(timestamp_text: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def format_full_discord_timestamp(moment: datetime.datetime) -> str:
    return discord.utils.format_dt(moment, "F")


def load_timed_bans() -> list[dict]:
    if not os.path.exists(TIMED_BANS_PATH):
        return []

    with open(TIMED_BANS_PATH, "r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        return []

    return [entry for entry in data if isinstance(entry, dict)]


def save_timed_bans(entries: list[dict]) -> None:
    with open(TIMED_BANS_PATH, "w", encoding="utf-8") as file:
        json.dump(entries, file, indent=2, ensure_ascii=True)


def add_timed_ban_entry(guild_id: int, user_id: int, unban_time: datetime.datetime, reason: str, ban_type: str) -> None:
    entries = load_timed_bans()
    entries = [entry for entry in entries if not (entry.get("guild_id") == guild_id and entry.get("user_id") == user_id)]
    entries.append(
        {
            "guild_id": guild_id,
            "user_id": user_id,
            "unban_at": int(unban_time.timestamp()),
            "reason": reason,
            "ban_type": ban_type,
        }
    )
    save_timed_bans(entries)


def remove_timed_ban_entry(guild_id: int, user_id: int) -> None:
    entries = load_timed_bans()
    filtered_entries = [entry for entry in entries if not (entry.get("guild_id") == guild_id and entry.get("user_id") == user_id)]
    if len(filtered_entries) != len(entries):
        save_timed_bans(filtered_entries)


async def schedule_unban(guild: discord.Guild, user: discord.User, unban_time: datetime.datetime, reason: str):
    task_key = (guild.id, user.id)
    scheduled_unban_tasks.pop(task_key, None)
    delay = (unban_time - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)

    try:
        await guild.unban(user, reason=reason)
    except discord.NotFound:
        pass
    finally:
        remove_timed_ban_entry(guild.id, user.id)


def queue_unban(guild: discord.Guild, user: discord.User, unban_time: datetime.datetime, reason: str):
    task_key = (guild.id, user.id)
    existing_task = scheduled_unban_tasks.get(task_key)
    if existing_task and not existing_task.done():
        existing_task.cancel()

    scheduled_unban_tasks[task_key] = bot.loop.create_task(schedule_unban(guild, user, unban_time, reason))


def restore_timed_bans():
    now = datetime.datetime.now(datetime.timezone.utc)
    for entry in load_timed_bans():
        try:
            guild_id = int(entry.get("guild_id"))
            user_id = int(entry.get("user_id"))
            unban_value = entry.get("unban_at")
            if isinstance(unban_value, (int, float)):
                unban_time = datetime.datetime.fromtimestamp(float(unban_value), tz=datetime.timezone.utc)
            else:
                unban_time = parse_iso8601_timestamp(str(unban_value))
            reason = str(entry.get("reason", "No reason provided"))
        except (TypeError, ValueError):
            continue

        guild = bot.get_guild(guild_id)
        if guild is None:
            continue

        user = bot.get_user(user_id) or discord.Object(id=user_id)

        if unban_time <= now:
            bot.loop.create_task(schedule_unban(guild, user, now, reason))
            continue

        queue_unban(guild, user, unban_time, reason)


_awake_started = False


async def handle_awake(request):
    return web.Response(status=200, text="OK")


async def start_awake_server():
    app = web.Application()
    app.router.add_get("/awake", handle_awake)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 20068)
    await site.start()


ERROR_CODE_PATTERN = re.compile(r"\b(\d{3})-(\d{4})\b")
error_code_cache: dict[str, dict | None] = {}
_http_session: "aiohttp.ClientSession | None" = None


def get_http_session():
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def fetch_error_code(module: str, code: str) -> dict | None:
    cache_key = f"{module}-{code}"
    if cache_key in error_code_cache:
        return error_code_cache[cache_key]

    url = f"https://raw.githubusercontent.com/SamtendoNetwork/error-codes/master/data/{module}/{code}/en_US.json"
    session = get_http_session()
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                error_code_cache[cache_key] = None
                return None
            data = await resp.json(content_type=None)
    except Exception:
        return None

    try:
        info = data[module][code]
    except (KeyError, TypeError):
        error_code_cache[cache_key] = None
        return None

    error_code_cache[cache_key] = info
    return info


def build_error_code_embed(module: str, code: str, info: dict) -> discord.Embed:
    full_code = f"{module}-{code}"
    first_digit = module[0]
    if first_digit == "0":
        console = "3DS"
        color = discord.Color.from_str("#D12228")
    elif first_digit == "1":
        console = "Wii U"
        color = discord.Color.from_str("#0098C7")
    else:
        console = "Other"
        color = discord.Color.from_str("#FFFFFF")

    embed = discord.Embed(
        title=f"{full_code} ({console})",
        description="Information is WIP and may be incorrect",
        color=color,
    )
    embed.add_field(name="Error Name", value=info.get("name", "Unknown"), inline=True)
    embed.add_field(name="Error Description", value=info.get("short_description", "Unknown"), inline=True)
    embed.add_field(name="Solution", value=info.get("short_solution", "Unknown solution"), inline=False)
    return embed


@bot.tree.context_menu(name="Report Piracy")
async def report_piracy(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(ephemeral=True, thinking=True)

    reply_embed = discord.Embed(
        title="Potential Piracy Reported",
        description=(
            "A user has flagged this message as potentially relating to piracy. "
            "Samtendo Network does not support piracy of any kind. Please review Rule 5.\n\n"
            f"If you have any questions, please ask moderators here: <#{PIRACY_MOD_CHANNEL_ID}>."
        ),
        color=discord.Color.from_str("#FF0000"),
    )
    try:
        await message.reply(embed=reply_embed, mention_author=False)
    except discord.HTTPException:
        pass
    reports_channel = interaction.guild.get_channel(
        PIRACY_REPORTS_CHANNEL_ID
    ) or await interaction.guild.fetch_channel(PIRACY_REPORTS_CHANNEL_ID)

    content = message.content or "*[no text content - attachment/embed only]*"
    if len(content) > 950:
        content = content[:950] + "..."

    report_embed = discord.Embed(
        title="Message flagged for piracy",
        color=discord.Color.from_str("#FF0000"),
    )
    report_embed.add_field(name="Reporter", value=interaction.user.mention, inline=False)
    report_embed.add_field(name="Message Author", value=message.author.mention, inline=False)
    report_embed.add_field(name="Sent in", value=message.channel.mention, inline=False)
    report_embed.add_field(name="Message content", value=f"> *{content}*", inline=False)

    if message.jump_url:
        report_embed.add_field(name="Jump to message", value=f"[Click here]({message.jump_url})", inline=False)

    await reports_channel.send(
        content=f"New report",
        embed=report_embed,
        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
    )

    await interaction.followup.send("Report submitted. Thank you.", ephemeral=True)


@bot.event
async def on_ready():
    global _awake_started
    if not _awake_started:
        await start_awake_server()
        _awake_started = True
    global timed_bans_restored
    print(f"Logged in as {bot.user} ({bot.user.id})")
    if not timed_bans_restored:
        restore_timed_bans()
        timed_bans_restored = True

    global tree_synced
    if not tree_synced:
        try:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} application command(s).")
        except discord.HTTPException as e:
            print(f"Failed to sync application commands: {e}")
        tree_synced = True


@bot.event
async def on_member_join(member: discord.Member):
    welcome_channel = member.guild.system_channel
    if welcome_channel is not None:
        welcome_embed = discord.Embed(
            title=f"A wild {member.name} arrives!",
            description=(
                "Welcome to Samtendo Network! Make sure to read the "
                "https://discord.com/channels/1465775507034341439/1466171096729649226 "
                "and chat with everyone else to get to know us!\n\n"
                "**Get started today with Samtendo Network: https://guide.samtendo.net**"
            ),
            color=discord.Color.from_str("#4ABFFF"),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        welcome_embed.set_thumbnail(url="https://cdn.samtendo.net/images/NewSamtendoCircle.png")
        await welcome_channel.send(content=member.mention, embed=welcome_embed)

    ch = log_channel(member.guild)
    if not ch:
        return
    log_embed = base_embed("Member Joined", discord.Color.green())
    log_embed.set_thumbnail(url=member.display_avatar.url)
    log_embed.add_field(name="User", value=f"<@{member.id}> ({member} - {member.id})", inline=False)
    log_embed.add_field(name="Account Created", value=discord.utils.format_dt(member.created_at, "R"))
    await ch.send(embed=log_embed)


@bot.event
async def on_member_remove(member: discord.Member):
    ch = log_channel(member.guild)
    if not ch:
        return

    kicked_by = None
    async for entry in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
        if entry.target.id == member.id and (datetime.datetime.utcnow() - entry.created_at.replace(tzinfo=None)).total_seconds() < 5:
            kicked_by = entry.user
            break

    if kicked_by:
        embed = base_embed("Member Kicked", discord.Color.orange())
        embed.add_field(name="User", value=f"<@{member.id}> ({member} - {member.id})", inline=False)
        embed.add_field(name="Kicked By", value=str(kicked_by))
    else:
        embed = base_embed("Member Left", discord.Color.red())
        embed.add_field(name="User", value=f"<@{member.id}> ({member} - {member.id})", inline=False)

    await ch.send(embed=embed)


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    ch = log_channel(guild)
    if not ch:
        return

    banned_by = None
    reason = None
    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
        if entry.target.id == user.id:
            banned_by = entry.user
            reason = entry.reason
            break

    embed = base_embed("Member Banned", discord.Color.dark_red())
    embed.add_field(name="User", value=f"<@{user.id}> ({user} - {user.id})", inline=False)
    if banned_by:
        embed.add_field(name="Banned By", value=str(banned_by))
    if reason:
        embed.add_field(name="Reason", value=reason, inline=False)
    await ch.send(embed=embed)


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    ch = log_channel(guild)
    if not ch:
        return
    embed = base_embed("Member Unbanned", discord.Color.blurple())
    embed.add_field(name="User", value=f"<@{user.id}> ({user} - {user.id})", inline=False)
    await ch.send(embed=embed)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    ch = log_channel(after.guild)
    if not ch:
        return

    added = [r for r in after.roles if r not in before.roles]
    removed = [r for r in before.roles if r not in after.roles]

    if added or removed:
        changed_by = None
        async for entry in after.guild.audit_logs(limit=5, action=discord.AuditLogAction.member_role_update):
            if entry.target.id == after.id:
                changed_by = entry.user
                break

        embed = base_embed("Member Roles Updated", discord.Color.gold())
        embed.add_field(name="User", value=f"<@{after.id}> ({after} - {after.id})", inline=False)
        if added:
            embed.add_field(name="Roles Added", value=", ".join(r.mention for r in added), inline=False)
        if removed:
            embed.add_field(name="Roles Removed", value=", ".join(r.mention for r in removed), inline=False)
        if changed_by:
            embed.add_field(name="Changed By", value=str(changed_by))
        await ch.send(embed=embed)

    if before.nick != after.nick:
        embed = base_embed("Nickname Changed", discord.Color.light_grey())
        embed.add_field(name="User", value=f"<@{after.id}> ({after} - {after.id})", inline=False)
        embed.add_field(name="Before", value=before.nick or "(none)")
        embed.add_field(name="After", value=after.nick or "(none)")
        await ch.send(embed=embed)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.guild and message.channel.id == PROBATION_CHANNEL_ID:
        ch = pro_log_channel(message.guild)
        if ch:
            message_text = message.content or "(no content)"
            embed = base_embed("Probation Message", discord.Color.orange())
            embed.add_field(name="Author", value=f"<@{message.author.id}> ({message.author} - {message.author.id})", inline=False)
            embed.add_field(name="Message", value=message_text, inline=False)
            if len(message_text) > 4096:
                message_text = message_text[:4093] + "..."
            if message.attachments:
                attachment_urls = "\n".join(attachment.url for attachment in message.attachments)
                if len(attachment_urls) > 1024:
                    attachment_urls = attachment_urls[:1021] + "..."
                embed.add_field(
                    name="Attachments",
                    value=attachment_urls,
                    inline=False,
                )
            await ch.send(embed=embed)

    matches = ERROR_CODE_PATTERN.findall(message.content)
    if matches:
        embeds = []
        for module, code in matches[:10]:
            info = await fetch_error_code(module, code)
            if info:
                embeds.append(build_error_code_embed(module, code, info))
        if embeds:
            await message.reply(embeds=embeds)

    await bot.process_commands(message)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, (app_commands.MissingPermissions, app_commands.MissingRole, app_commands.MissingAnyRole, app_commands.CheckFailure)):
        msg = "You do not have permission to use this command."
    else:
        msg = "Something went wrong running that command."
        print(f"App command error: {error}")

    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="ping", description="Pong!")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")


@bot.tree.command(name="meow", description="Meow")
async def meow(interaction: discord.Interaction):
    if interaction.user.id == 1258819818887319658:  # me :3
        user = bot.get_user(415606064856301589)  # aep
        await user.send('Meow')
        user = bot.get_user(1258819818887319658)  # sam
        await user.send('Meow')
        user = bot.get_user(1420061774165835938)  # faz
        await user.send('Meow')
        await interaction.response.send_message("Meow")
    else:
        await interaction.response.send_message("lmao who are you")


@bot.tree.command(name="hi", description="Say hi")
async def hi(interaction: discord.Interaction):
    await interaction.response.send_message("wassup")


@bot.tree.command(name="kick", description="Kick a member")
@app_commands.describe(member="Member to kick", reason="Reason for the kick")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if not can_ban_target(interaction.user, member):
        await interaction.response.send_message("You do not have permission to kick this member.", ephemeral=True)
        return
    try:
        await member.send(f"You have been kicked from **{interaction.guild.name}**.\nReason: {reason}")
    except discord.Forbidden:
        pass
    await interaction.guild.kick(member, reason=reason)
    await interaction.response.send_message(f"Kicked {member} | Reason: {reason}")


@bot.tree.command(name="probate", description="Put a member on probation")
@app_commands.describe(member="Member to probate")
async def probate(interaction: discord.Interaction, member: discord.Member):
    if not is_staff(interaction.user):
        await interaction.response.send_message("You do not have permission to use this.", ephemeral=True)
        return

    role = interaction.guild.get_role(PROBATION_ROLE_ID)
    if role is None:
        await interaction.response.send_message("Probation role not found.", ephemeral=True)
        return

    try:
        await member.add_roles(role, reason=f"Probated by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("I do not have permission to add that role.", ephemeral=True)
        return

    await interaction.response.send_message(f"Probated {member}.")


@bot.tree.command(name="unprobate", description="Remove a member from probation")
@app_commands.describe(member="Member to unprobate")
async def unprobate(interaction: discord.Interaction, member: discord.Member):
    if not is_staff(interaction.user):
        await interaction.response.send_message("You do not have permission to use this.", ephemeral=True)
        return

    role = interaction.guild.get_role(PROBATION_ROLE_ID)
    if role is None:
        await interaction.response.send_message("Probation role not found.", ephemeral=True)
        return

    try:
        await member.remove_roles(role, reason=f"Unprobated by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("I do not have permission to remove that role.", ephemeral=True)
        return

    await interaction.response.send_message(f"Unprobated {member}.")


@bot.tree.command(name="unban", description="Unban a user")
@app_commands.describe(user="User to unban", reason="Reason for the unban")
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user: discord.User, reason: str = "No reason provided"):
    try:
        await interaction.guild.fetch_ban(user)
    except discord.errors.NotFound:
        await interaction.response.send_message(f"{user} is not banned!")
        return
    await interaction.guild.unban(user, reason=reason)
    await interaction.response.send_message(f"{user} is now unbanned. | Reason: {reason}")


@bot.tree.command(name="ban", description="Ban a user")
@app_commands.describe(user="User to ban", reason="Reason for the ban")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, user: discord.User, reason: str = "No reason provided"):
    if not can_ban_target(interaction.user, user):
        await interaction.response.send_message("You do not have permission to ban this member.", ephemeral=True)
        return
    apl = "You may appeal by emailing appeals@samtendo.net"
    try:
        await user.send(f"You have been banned from **{interaction.guild.name}**.\nReason: {reason}\n\n{apl}")
    except discord.Forbidden:
        pass

    await interaction.guild.ban(user, reason=reason, delete_message_seconds=0)
    await interaction.response.send_message(f"Banned {user} | Reason: {reason}")


@bot.tree.command(name="hban", description="No-appeal ban a user")
@app_commands.describe(user="User to ban", reason="Reason for the ban")
@app_commands.checks.has_permissions(ban_members=True)
async def hban(interaction: discord.Interaction, user: discord.User, reason: str = "No reason provided"):
    if not can_ban_target(interaction.user, user):
        await interaction.response.send_message("You do not have permission to ban this member.", ephemeral=True)
        return
    apl = "You may not appeal this ban."
    try:
        await user.send(f"You have been banned from **{interaction.guild.name}**.\nReason: {reason}\n\n{apl}")
    except discord.Forbidden:
        pass

    await interaction.guild.ban(user, reason=reason)
    await interaction.response.send_message(f"No appeal ban given to {user} | Reason: {reason}")

@bot.tree.command(name="kban", description="Knowledgeban a user using a stored reason")
@app_commands.describe(user="User to ban", reason="Knowledgeban shortcut reason")
@app_commands.checks.has_permissions(ban_members=True)
async def kban(interaction: discord.Interaction, user: discord.User, reason: str):
    if not can_ban_target(interaction.user, user):
        await interaction.response.send_message("You do not have permission to ban this member.", ephemeral=True)
        return

    result = supabase.table("kbans").select("full").eq("shortcut", reason).execute()

    if not result.data:
        await interaction.response.send_message("Please provide a valid knowledgeban reason. Otherwise, you should just ban them normally.", ephemeral=True)
        return

    ban_reason = result.data[0]["full"]
    apl = "You may appeal by emailing appeals@samtendo.net"

    try:
        await user.send(f"You have been banned from **{interaction.guild.name}**.\nReason: {ban_reason}\n\n{apl}")
    except discord.Forbidden:
        pass

    await interaction.guild.ban(user, reason=ban_reason)
    await interaction.response.send_message(f"Knowledgeban given to {user} | Reason: {ban_reason}")


@bot.tree.command(name="speak", description="Make the bot say something")
@app_commands.describe(msg="Message to send")
async def speak(interaction: discord.Interaction, msg: str = "Please provide a message to send."):
    if not is_immune_bypass(interaction.user):
        await interaction.response.send_message("You do not have permission to use this.", ephemeral=True)
        return
    await interaction.response.send_message(msg)


@bot.tree.command(name="purge", description="Purge messages from this channel")
@app_commands.describe(limit="Number of messages to purge")
async def purge(interaction: discord.Interaction, limit: int):
    if not is_staff(interaction.user):
        await interaction.response.send_message("You do not have permission to use this.", ephemeral=True)
        return
    if limit < 1:
        await interaction.response.send_message("Please provide a valid number of messages to purge.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    deleted = await interaction.channel.purge(
        limit=limit,
        check=lambda message: message.author.id != BOT_USER_ID and not message.pinned,
    )
    await interaction.followup.send(f"Purged {len(deleted)} message(s), I skipped any of my own", ephemeral=True)


@bot.tree.command(name="loaderr", description="Clear the error code cache")
@app_commands.checks.has_permissions(administrator=True)
async def loaderr(interaction: discord.Interaction):
    cleared = len(error_code_cache)
    error_code_cache.clear()
    await interaction.response.send_message(f"Error code cache cleared ({cleared} entr{'y' if cleared == 1 else 'ies'} dropped). Codes will be refetched from GitHub on next mention.")


@bot.tree.command(name="guide", description="Setting up Samtendo Network on Wii U")
async def guide(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Setting up Samtendo Network on Wii U",
        description="https://guide.samtendo.net/",
        color=discord.Color.from_str("#4ABFFF")
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="com", description="We are not affiliated with samtendo.com")
async def com(interaction: discord.Interaction):
    embed = discord.Embed(
        title="We are not affiliated with samtendo.com",
        description="Our official website is https://samtendo.net. We are not and will not ever be affiliated with them.",
        color=discord.Color.from_str("#FF0000")
    )
    await interaction.response.send_message(embed=embed)


warn_group = app_commands.Group(name="warn", description="Warning management")


@warn_group.command(name="list", description="List a user's warns")
@app_commands.describe(user="User to check warns for")
async def warn_list(interaction: discord.Interaction, user: discord.User):
    if not is_staff(interaction.user):
        await interaction.response.send_message("You do not have permission to use this.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    result = supabase.table("warns").select("*").eq("user_id", user.id).order("created_at", desc=True).execute()

    if not result.data:
        await interaction.followup.send(f"{user.mention} has no warns.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"Warns for {user}",
        color=discord.Color.orange(),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    for entry in result.data:
        staff_id = entry.get("staff_id")
        created_at = entry.get("created_at")
        try:
            ts = discord.utils.format_dt(parse_iso8601_timestamp(str(created_at)), "F")
        except (TypeError, ValueError):
            ts = str(created_at)
        embed.add_field(
            name=f"Warn #{entry.get('warn_id')}",
            value=f"**Reason:** {entry.get('reason', 'No reason provided')}\n**Staff:** <@{staff_id}>\n**Date:** {ts}",
            inline=False,
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


@warn_group.command(name="give", description="Give a user a warn")
@app_commands.describe(user="User to warn", reason="Reason for the warn")
async def warn_give(interaction: discord.Interaction, user: discord.User, reason: str):
    if not is_staff(interaction.user):
        await interaction.response.send_message("You do not have permission to use this.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    supabase.table("warns").insert(
        {
            "user_id": user.id,
            "staff_id": interaction.user.id,
            "reason": reason,
        }
    ).execute()

    try:
        await user.send(f"You have been warned in **{interaction.guild.name}**.\nReason: {reason}")
    except discord.Forbidden:
        pass

    await interaction.followup.send(f"Warned {user.mention} | Reason: {reason}")


@warn_group.command(name="remove", description="Remove a warn by ID")
@app_commands.describe(warn_id="ID of the warn to remove")
async def warn_remove(interaction: discord.Interaction, warn_id: int):
    if not is_staff(interaction.user):
        await interaction.response.send_message("You do not have permission to use this.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    result = supabase.table("warns").select("*").eq("warn_id", warn_id).execute()
    if not result.data:
        await interaction.followup.send(f"No warn found with ID {warn_id}.")
        return

    supabase.table("warns").delete().eq("warn_id", warn_id).execute()
    await interaction.followup.send(f"Removed warn #{warn_id}.")


bot.tree.add_command(warn_group)

bot.run(TOKEN)