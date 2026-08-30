import discord
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


def can_ban_target(author: discord.Member, target) -> bool:
    if not isinstance(target, discord.Member):
        return True
    has_protected_role = any(r.id == PROTECTED_ROLE_ID for r in target.roles)
    if not has_protected_role:
        return True
    return any(r.id == IMMUNE_BYPASS_ROLE_ID for r in author.roles)


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
            await message.channel.send(embeds=embeds)

    await bot.process_commands(message)


@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")


@bot.command()
async def meow(ctx):
    if ctx.author.id == 1258819818887319658:  # me :3
        user = ctx.bot.get_user(415606064856301589)  # aep
        await user.send('Meow')
        user = ctx.bot.get_user(1258819818887319658)  # sam
        await user.send('Meow')
        user = ctx.bot.get_user(1420061774165835938)  # faz
        await user.send('Meow')
        await ctx.send("Meow")
    else:
        await ctx.send("lmao who are you")


@bot.command()
async def hi(ctx):
    await ctx.reply("wassup")


@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    if not can_ban_target(ctx.author, member):
        await ctx.reply("You do not have permission to kick this member.")
        return
    try:
        await member.send(f"You have been kicked from **{ctx.guild.name}**.\nReason: {reason}")
    except discord.Forbidden:
        pass
    await ctx.guild.kick(member, reason=reason)
    await ctx.send(f"Kicked {member} | Reason: {reason}")


@bot.command()
@commands.has_any_role(PROTECTED_ROLE_ID)
async def probate(ctx, member: discord.Member):
    role = ctx.guild.get_role(PROBATION_ROLE_ID)
    if role is None:
        await ctx.reply("Probation role not found.")
        return

    try:
        await member.add_roles(role, reason=f"Probated by {ctx.author}")
    except discord.Forbidden:
        await ctx.reply("I do not have permission to add that role.")
        return

    await ctx.reply(f"Probated {member}.")


@bot.command()
@commands.has_any_role(PROTECTED_ROLE_ID)
async def unprobate(ctx, member: discord.Member):
    role = ctx.guild.get_role(PROBATION_ROLE_ID)
    if role is None:
        await ctx.reply("Probation role not found.")
        return

    try:
        await member.remove_roles(role, reason=f"Unprobated by {ctx.author}")
    except discord.Forbidden:
        await ctx.reply("I do not have permission to remove that role.")
        return

    await ctx.reply(f"Unprobated {member}.")


@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(ctx, user: discord.User, *, reason="No reason provided"):
    try:
        await ctx.guild.fetch_ban(user)
    except discord.errors.NotFound:
        return await ctx.send(f"{user} is not banned!")
    await ctx.guild.unban(user, reason=reason)
    await ctx.send(f"{user} is now unbanned. | Reason: {reason}")


@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, user: discord.User, *, reason="No reason provided"):
    if not can_ban_target(ctx.author, user):
        await ctx.reply("You do not have permission to ban this member.")
        return
    apl = "You may appeal by emailing appeals@samtendo.net"
    try:
        await user.send(f"You have been banned from **{ctx.guild.name}**.\nReason: {reason}\n\n{apl}")
    except discord.Forbidden:
        pass

    await ctx.guild.ban(user, reason=reason, delete_message_seconds=0)
    await ctx.send(f"Banned {user} | Reason: {reason}")


@bot.command()
@commands.has_permissions(ban_members=True)
async def tban(ctx, user: discord.User, unban_at: str, *, reason="No reason provided"):
    if not can_ban_target(ctx.author, user):
        await ctx.reply("You do not have permission to ban this member.")
        return

    try:
        unban_time = parse_iso8601_timestamp(unban_at)
    except ValueError:
        await ctx.reply("Please provide a valid ISO 8601 timestamp for the unban time.")
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    if unban_time <= now:
        await ctx.reply("The unban timestamp must be in the future.")
        return

    apl = "You may appeal by emailing appeals@samtendo.net"
    try:
        await user.send(
            f"You have been banned from **{ctx.guild.name}**.\nReason: {reason}\nUnban time: {format_full_discord_timestamp(unban_time)}\n\n{apl}"
        )
    except discord.Forbidden:
        pass

    await ctx.guild.ban(user, reason=reason)
    add_timed_ban_entry(ctx.guild.id, user.id, unban_time, reason, "tban")
    queue_unban(ctx.guild, user, unban_time, reason)
    await ctx.send(f"Timed ban given to {user} | Unban at: {format_full_discord_timestamp(unban_time)} | Reason: {reason}")


@bot.command()
@commands.has_permissions(ban_members=True)
async def hban(ctx, user: discord.User, *, reason="No reason provided"):
    if not can_ban_target(ctx.author, user):
        await ctx.reply("You do not have permission to ban this member.")
        return
    apl = "You may not appeal this ban."
    try:
        await user.send(f"You have been banned from **{ctx.guild.name}**.\nReason: {reason}\n\n{apl}")
    except discord.Forbidden:
        pass

    await ctx.guild.ban(user, reason=reason)
    await ctx.send(f"No appeal ban given to {user} | Reason: {reason}")


@bot.command()
@commands.has_permissions(ban_members=True)
async def thban(ctx, user: discord.User, unban_at: str, *, reason="No reason provided"):
    if not can_ban_target(ctx.author, user):
        await ctx.reply("You do not have permission to ban this member.")
        return

    try:
        unban_time = parse_iso8601_timestamp(unban_at)
    except ValueError:
        await ctx.reply("Please provide a valid ISO 8601 timestamp for the unban time.")
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    if unban_time <= now:
        await ctx.reply("The unban timestamp must be in the future.")
        return

    apl = "You may not appeal this ban."
    try:
        await user.send(
            f"You have been banned from **{ctx.guild.name}**.\nReason: {reason}\nUnban time: {format_full_discord_timestamp(unban_time)}\n\n{apl}"
        )
    except discord.Forbidden:
        pass

    await ctx.guild.ban(user, reason=reason)
    add_timed_ban_entry(ctx.guild.id, user.id, unban_time, reason, "thban")
    queue_unban(ctx.guild, user, unban_time, reason)
    await ctx.send(f"Timed no appeal ban given to {user} | Unban at: {format_full_discord_timestamp(unban_time)} | Reason: {reason}")


@bot.command()
@commands.has_permissions(ban_members=True)
async def kban(ctx, user: discord.User, *, reason="N/A"):
    if not can_ban_target(ctx.author, user):
        await ctx.reply("You do not have permission to ban this member.")
        return
    apl = "You may appeal by emailing appeals@samtendo.net"
    if reason == "N/A":
        await ctx.reply("Please provide a knowledgeban reason.")
        return

    result = supabase.table("kbans").select("full").eq("shortcut", reason).execute()

    if not result.data:
        await ctx.reply("Please provide a valid knowledgeban reason. Otherwise, you should just ban them normally.")
        return

    ban_reason = result.data[0]["full"]

    try:
        await user.send(f"You have been banned from **{ctx.guild.name}**.\nReason: {ban_reason}\n\n{apl}")
    except discord.Forbidden:
        pass

    await ctx.guild.ban(user, reason=ban_reason)
    await ctx.send(f"Knowledgeban given to {user} | Reason: {ban_reason}")


@bot.command()
@commands.has_any_role(IMMUNE_BYPASS_ROLE_ID)
async def speak(ctx, *, msg="Please provide a message to send."):
    await ctx.send(msg)


@bot.command()
@commands.has_any_role(PROTECTED_ROLE_ID)
async def purge(ctx, limit: int):
    if limit < 1:
        await ctx.reply("Please provide a valid number of messages to purge.")
        return
    deleted = await ctx.channel.purge(
        limit=(limit + 1),
        check=lambda message: message.author.id != BOT_USER_ID and not message.pinned,
    )
    await ctx.send(f"Purged {len(deleted)} message(s), I skipped any of my own", delete_after=5)


@bot.command()
@commands.has_permissions(administrator=True)
async def loaderr(ctx):
    cleared = len(error_code_cache)
    error_code_cache.clear()
    await ctx.send(f"Error code cache cleared ({cleared} entr{'y' if cleared == 1 else 'ies'} dropped). Codes will be refetched from GitHub on next mention.")


# Tags!
@bot.command()
async def guide(ctx):
    embed = discord.Embed(
        title="Setting up Samtendo Network on Wii U",
        description="https://guide.samtendo.net/",
        color=discord.Color.from_str("#4ABFFF")
    )

    await ctx.send(embed=embed)


@bot.command()
async def com(ctx):
    embed = discord.Embed(
        title="We are not affiliated with samtendo.com",
        description="Our official website is https://samtendo.net. We are not and will not ever be affiliated with them.",
        color=discord.Color.from_str("#FF0000")
    )

    await ctx.send(embed=embed)


bot.run(TOKEN)