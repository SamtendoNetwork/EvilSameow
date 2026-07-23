import discord
from discord.ext import commands
import asyncio
import datetime
import json
from dotenv import load_dotenv
import os
from supabase import create_client, Client

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

bot = commands.Bot(command_prefix=".", intents=intents)

PROTECTED_ROLE_ID = int(os.getenv("PROTECTED_ROLE_ID"))
IMMUNE_BYPASS_ROLE_ID = int(os.getenv("IMMUNE_BYPASS_ROLE_ID"))
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


@bot.event
async def on_ready():
    global timed_bans_restored
    print(f"Logged in as {bot.user} ({bot.user.id})")
    if not timed_bans_restored:
        restore_timed_bans()
        timed_bans_restored = True


@bot.event
async def on_member_join(member: discord.Member):
    ch = log_channel(member.guild)
    if not ch:
        return
    embed = base_embed("Member Joined", discord.Color.green())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="User", value=f"<@{member.id}> ({member} - {member.id})", inline=False)
    embed.add_field(name="Account Created", value=discord.utils.format_dt(member.created_at, "R"))
    await ch.send(embed=embed)


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



@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

@bot.command()
async def meow(ctx):
    if ctx.author.id == 1258819818887319658: # me :3
        user = ctx.bot.get_user(415606064856301589) # aep
        await user.send('Meow')
        user = ctx.bot.get_user(1258819818887319658) # sam
        await user.send('Meow')
        user = ctx.bot.get_user(1420061774165835938) # faz
        await user.send('Meow')
        await ctx.send("Meow")
    else:
        await ctx.send("lmao who are you")



@bot.command()
async def hi(ctx):
    await ctx.reply("wassup")

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

    await ctx.guild.ban(user, reason=reason)
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

bot.run(TOKEN)
