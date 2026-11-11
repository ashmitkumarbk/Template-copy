import asyncio
import logging
import os
import re
import sys
import discord
from discord.ext import commands

# 1. Structured Logging for Railway Deployment
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("RailwayBot")

# 2. Bot Intents Configuration
intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def extract_template_code(template_input: str) -> str:
    """Extracts raw template code from URLs like discord.new/code or raw codes."""
    match = re.search(r"(?:discord\.new/|template/)?([a-zA-Z0-9]{8,16})", template_input)
    return match.group(1) if match else template_input.strip()


@bot.event
async def on_ready():
    logger.info(f"Bot connected successfully as {bot.user.name} (ID: {bot.user.id})")
    logger.info(f"Connected to {len(bot.guilds)} guild(s). Ready to process templates.")


@bot.command(name="applytemplate")
@commands.has_permissions(administrator=True)
async def apply_template(ctx: commands.Context, template_url_or_code: str):
    guild = ctx.guild
    code = extract_template_code(template_url_or_code)

    logger.info(f"Received template apply request for guild '{guild.name}' ({guild.id}) with code: {code}")

    # Fetch template metadata
    try:
        template = await bot.fetch_template(code)
    except discord.NotFound:
        await ctx.send("❌ Error: Invalid template code or URL.")
        return
    except discord.HTTPException as e:
        logger.error(f"HTTP error fetching template {code}: {e}")
        await ctx.send(f"❌ Failed to fetch template: {e}")
        return

    # Notify command author in DMs (the current channel will be deleted)
    try:
        await ctx.author.send(
            f"🔄 Starting template application for **{guild.name}** using `{template.name}`.\n"
            "Previous channels and roles are being removed now..."
        )
    except discord.Forbidden:
        logger.warning(f"Could not DM author {ctx.author.id}; DMs are blocked/closed.")

    # 1. Delete Existing Channels
    logger.info(f"Starting channel purge in '{guild.name}'...")
    for channel in list(guild.channels):
        try:
            await channel.delete(reason="Template application: resetting guild structure")
            await asyncio.sleep(0.35)  # Prevents Discord REST 429 rate limit
        except discord.HTTPException as e:
            logger.warning(f"Could not delete channel {channel.name}: {e}")

    # 2. Delete Existing Roles (skips @everyone, bot roles, and higher-hierarchy roles)
    logger.info(f"Starting role purge in '{guild.name}'...")
    for role in list(guild.roles):
        if role.is_default() or role.managed or role >= guild.me.top_role:
            continue
        try:
            await role.delete(reason="Template application: resetting guild structure")
            await asyncio.sleep(0.35)
        except discord.HTTPException as e:
            logger.warning(f"Could not delete role {role.name}: {e}")

    source = template.source_guild
    role_map = {}

    # 3. Re-create Roles
    logger.info("Re-creating roles from template...")
    temp_everyone = next((r for r in source.roles if r.id == source.id), None)
    if temp_everyone:
        try:
            await guild.default_role.edit(
                permissions=temp_everyone.permissions,
                reason="Template application: update @everyone"
            )
            role_map[temp_everyone.id] = guild.default_role
        except discord.HTTPException as e:
            logger.warning(f"Could not update @everyone permissions: {e}")
    else:
        role_map[source.id] = guild.default_role

    custom_roles = [r for r in source.roles if r.id != source.id]
    custom_roles.sort(key=lambda r: getattr(r, "position", 0))

    for t_role in custom_roles:
        try:
            new_role = await guild.create_role(
                name=t_role.name,
                permissions=t_role.permissions,
                colour=t_role.colour,
                hoist=t_role.hoist,
                mentionable=t_role.mentionable,
                reason="Template application: creating role"
            )
            role_map[t_role.id] = new_role
            await asyncio.sleep(0.35)
        except discord.HTTPException as e:
            logger.warning(f"Failed to create role {t_role.name}: {e}")

    # Overwrite Resolver Helper
    def resolve_overwrites(template_channel):
        overwrites = {}
        for target_id, overwrite_data in template_channel.overwrites.items():
            target = role_map.get(target_id)
            if target:
                overwrites[target] = overwrite_data
        return overwrites

    # 4. Re-create Categories
    logger.info("Re-creating categories from template...")
    category_map = {}
    categories = [c for c in source.channels if c.type == discord.ChannelType.category]
    categories.sort(key=lambda c: getattr(c, "position", 0))

    for t_cat in categories:
        try:
            new_cat = await guild.create_category(
                name=t_cat.name,
                overwrites=resolve_overwrites(t_cat),
                position=t_cat.position,
                reason="Template application: creating category"
            )
            category_map[t_cat.id] = new_cat
            await asyncio.sleep(0.35)
        except discord.HTTPException as e:
            logger.warning(f"Failed to create category {t_cat.name}: {e}")

    # 5. Re-create Text, Voice, and Stage Channels
    logger.info("Re-creating sub-channels from template...")
    sub_channels = [c for c in source.channels if c.type != discord.ChannelType.category]
    sub_channels.sort(key=lambda c: getattr(c, "position", 0))

    for t_chan in sub_channels:
        parent_cat = category_map.get(t_chan.category_id)
        overwrites = resolve_overwrites(t_chan)

        try:
            if t_chan.type == discord.ChannelType.text:
                await guild.create_text_channel(
                    name=t_chan.name,
                    category=parent_cat,
                    topic=t_chan.topic,
                    slowmode_delay=getattr(t_chan, "slowmode_delay", 0),
                    nsfw=t_chan.is_nsfw(),
                    overwrites=overwrites,
                    reason="Template application: creating text channel"
                )
            elif t_chan.type == discord.ChannelType.voice:
                await guild.create_voice_channel(
                    name=t_chan.name,
                    category=parent_cat,
                    bitrate=t_chan.bitrate or 64000,
                    user_limit=t_chan.user_limit or 0,
                    overwrites=overwrites,
                    reason="Template application: creating voice channel"
                )
            elif t_chan.type == discord.ChannelType.stage_voice:
                await guild.create_stage_channel(
                    name=t_chan.name,
                    category=parent_cat,
                    overwrites=overwrites,
                    reason="Template application: creating stage channel"
                )
            await asyncio.sleep(0.35)
        except discord.HTTPException as e:
            logger.warning(f"Failed to create channel {t_chan.name}: {e}")

    logger.info(f"Finished applying template '{template.name}' to guild '{guild.name}'.")

    try:
        await ctx.author.send(f"✅ Template `{template.name}` successfully applied to **{guild.name}**!")
    except discord.Forbidden:
        pass


@apply_template.error
async def apply_template_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need **Administrator** permissions to run this command.")
    else:
        logger.error(f"Command execution error: {error}")
        await ctx.send(f"❌ Execution failed: {error}")


# 6. Startup Entry Point (Railway Environment Variable Loader)
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.critical("FATAL: 'DISCORD_TOKEN' environment variable is missing. Set it in Railway Variables.")
        sys.exit(1)

    try:
        bot.run(token)
    except discord.LoginFailure:
        logger.critical("FATAL: Provided token is invalid. Check DISCORD_TOKEN in Railway.")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Unexpected shutdown error: {e}")
        sys.exit(1)
