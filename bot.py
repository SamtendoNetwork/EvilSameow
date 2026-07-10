import discord
from discord.ext import commands
import datetime
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

PROTECTED_ROLE_ID = 1512466821285154917
IMMUNE_BYPASS_ROLE_ID = 1469036732199469092

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


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


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
