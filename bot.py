import discord
from discord.ext import commands
import datetime
from dotenv import load_dotenv
import os

load_dotenv()

TOKEN = os.getenv("TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))

intents = discord.Intents.default()
intents.members = True
intents.moderation = True
intents.message_content = True

bot = commands.Bot(command_prefix=".", intents=intents)


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
async def ban(ctx, member: discord.Member, *, reason="No reason provided", appeals=True):
    if appeals == True:
        apl = "You may appeal by emailing appeals@samtendo.net"
    else:
        apl = "You may not appeal this ban."
    try:
        await member.send(f"You have been banned from **{ctx.guild.name}**.\nReason: {reason}\n\n{apl}")
    except discord.Forbidden:
        pass

    await ctx.guild.ban(member, reason=reason)
    await ctx.send(f"Banned {member} | Reason: {reason}")

bot.run(TOKEN)
