import asyncio
import logging
import os
import re
import sys
import discord
from discord.ext import commands

# 1. Logging Setup (Streams in real-time to Railway Deploy Logs)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("TemplateBot")

# 2. Intents Configuration
intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def extract_template_code(template_input: str) -> str:
    """Extracts raw template code from URLs or raw strings."""
    match = re.search(r"(?:discord\.new/|template/)?([a-zA-Z0-9]{8,16})", template_input)
    return match.group(1) if match else template_input.strip()


@bot.event
async def on_ready():
    logger.info(f"Bot connected as {bot.user.name} ({bot.user.id})")
    logger.info(f"Serving {len(bot.guilds)} guild(s). Ready for templates.")


@bot.command(name="applytemplate")
@commands.has_permissions(administrator=True)
async def apply_template(ctx: commands.Context, template_url_or_code: str):
    guild = ctx.guild
    code = extract_template_code(template_url_or_code)

    logger.info(f"Fetching template '{code}' for guild '{guild.name}' ({guild.id})...")

    try:
        template = await bot.fetch_template(code)
    except discord.NotFound:
        await ctx.send("❌ Error: Invalid template code or URL.")
        return
    except discord.HTTPException as e:
        logger.error(f"HTTP error fetching template {code}: {e}")
        await ctx.send(f"❌ Failed to fetch template: {e}")
        return

    # Notify command runner via DM before wiping channels
    try:
        await ctx.author.send(
            f"🔄 Starting template application for **{guild.name}** using template `{template.name}`.\n"
            "Previous channels and roles are being removed now..."
        )
    except discord.Forbidden:
        logger.warning(f"Could not DM author {ctx.author.id} (DMs closed).")

    # --- STEP 1: PURGE EXISTING CHANNELS ---
    logger.info(f"Deleting channels in '{guild.name}'...")
    for channel in list(guild.channels):
        try:
            await channel.delete(reason="Template application: resetting guild structure")
            await asyncio.sleep(0.35)
        except discord.HTTPException as e:
            logger.warning(f"Could not delete channel {channel.name}: {e}")

    # --- STEP 2: PURGE EXISTING ROLES ---
    logger.info(f"Deleting custom roles in '{guild.name}'...")
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

    # --- STEP 3: SYNC @everyone BASE PERMISSIONS ---
    # In Discord templates, @everyone role ID matches source.id or source.default_role.id
    temp_everyone = source.default_role or next((r for r in source.roles if r.id == source.id), None)
    if temp_everyone:
        try:
            await guild.default_role.edit(
                permissions=temp_everyone.permissions,
                reason="Template application: sync @everyone base permissions"
            )
            logger.info("Synchronized @everyone base permissions.")
        except discord.HTTPException as e:
            logger.warning(f"Failed to update @everyone base permissions: {e}")

        role_map[temp_everyone.id] = guild.default_role

    # Safety fallbacks for @everyone ID mapping
    role_map[source.id] = guild.default_role
    role_map[guild.default_role.id] = guild.default_role

    # --- STEP 4: RE-CREATE ROLES WITH FULL PERMISSIONS ---
    custom_roles = [r for r in source.roles if r != temp_everyone and r.id != source.id]
    custom_roles.sort(key=lambda r: getattr(r, "position", 0))

    logger.info(f"Recreating {len(custom_roles)} custom roles with permissions...")
    for t_role in custom_roles:
        try:
            new_role = await guild.create_role(
                name=t_role.name,
                permissions=t_role.permissions,  # Server-wide permissions (Admin, Kick, Manage, etc.)
                colour=t_role.colour,
                hoist=t_role.hoist,
                mentionable=t_role.mentionable,
                reason="Template application: recreate role with permissions"
            )
            role_map[t_role.id] = new_role
            logger.info(f"Created role '{new_role.name}' with permission value: {t_role.permissions.value}")
            await asyncio.sleep(0.35)
        except discord.HTTPException as e:
            logger.warning(f"Failed to create role {t_role.name}: {e}")

    # Sync role hierarchy (order positions below bot's top role)
    roles_to_reorder = {}
    bot_top_role = guild.me.top_role
    for t_role in custom_roles:
        new_role = role_map.get(t_role.id)
        if new_role and new_role < bot_top_role:
            roles_to_reorder[new_role] = t_role.position

    if roles_to_reorder:
        try:
            await guild.edit_role_positions(roles_to_reorder, reason="Template application: sync role hierarchy")
            logger.info("Synchronized role hierarchy positions.")
        except discord.HTTPException as e:
            logger.warning(f"Could not reorder role positions: {e}")

    # --- STEP 5: PERMISSION OVERWRITE RESOLVER ---
    def resolve_overwrites(template_channel):
        """
        Translates template channel permission overwrites to the new guild roles.
        Resolves discord.Role and discord.Object keys to new role references.
        """
        resolved = {}
        for target, overwrite in template_channel.overwrites.items():
            # In discord.py, target is a Role or Object instance
            target_id = getattr(target, "id", target)
            mapped_role = role_map.get(target_id)

            if mapped_role:
                # Creates an exact copy of allow and deny bitfield flags
                resolved[mapped_role] = discord.PermissionOverwrite.from_pair(
                    overwrite.pair()[0],
                    overwrite.pair()[1]
                )
            else:
                logger.debug(f"Target ID {target_id} not found in role_map for channel {template_channel.name}")

        return resolved

    # --- STEP 6: RE-CREATE CATEGORIES WITH PERMISSIONS ---
    category_map = {}
    categories = [c for c in source.channels if c.type == discord.ChannelType.category]
    categories.sort(key=lambda c: getattr(c, "position", 0))

    logger.info("Recreating categories and category permission overwrites...")
    for t_cat in categories:
        cat_overwrites = resolve_overwrites(t_cat)
        try:
            new_cat = await guild.create_category(
                name=t_cat.name,
                overwrites=cat_overwrites,
                position=t_cat.position,
                reason="Template application: recreate category with permissions"
            )
            category_map[t_cat.id] = new_cat
            logger.info(f"Created category '{new_cat.name}' with {len(cat_overwrites)} role overwrite(s).")
            await asyncio.sleep(0.35)
        except discord.HTTPException as e:
            logger.warning(f"Failed to create category {t_cat.name}: {e}")

    # --- STEP 7: RE-CREATE CHANNELS & SYNC PERMISSIONS ---
    sub_channels = [c for c in source.channels if c.type != discord.ChannelType.category]
    sub_channels.sort(key=lambda c: getattr(c, "position", 0))

    logger.info("Recreating channels and synchronizing permissions...")
    for t_chan in sub_channels:
        parent_cat = category_map.get(t_chan.category_id)
        parent_t_cat = next((c for c in categories if c.id == t_chan.category_id), None)

        # Check if the channel was synced with its parent category in the template
        is_synced = False
        if parent_t_cat:
            is_synced = (t_chan.overwrites == parent_t_cat.overwrites)

        # If it's synced with category, omit custom overwrites so it inherits cleanly
        chan_overwrites = None if is_synced else resolve_overwrites(t_chan)
        new_chan = None

        try:
            if t_chan.type == discord.ChannelType.text:
                new_chan = await guild.create_text_channel(
                    name=t_chan.name,
                    category=parent_cat,
                    topic=t_chan.topic,
                    slowmode_delay=getattr(t_chan, "slowmode_delay", 0),
                    nsfw=t_chan.is_nsfw(),
                    overwrites=chan_overwrites,
                    reason="Template application: create text channel"
                )
            elif t_chan.type == discord.ChannelType.voice:
                new_chan = await guild.create_voice_channel(
                    name=t_chan.name,
                    category=parent_cat,
                    bitrate=t_chan.bitrate or 64000,
                    user_limit=t_chan.user_limit or 0,
                    overwrites=chan_overwrites,
                    reason="Template application: create voice channel"
                )
            elif t_chan.type == discord.ChannelType.stage_voice:
                new_chan = await guild.create_stage_channel(
                    name=t_chan.name,
                    category=parent_cat,
                    overwrites=chan_overwrites,
                    reason="Template application: create stage channel"
                )
            elif t_chan.type == discord.ChannelType.forum:
                new_chan = await guild.create_forum_channel(
                    name=t_chan.name,
                    category=parent_cat,
                    topic=t_chan.topic,
                    overwrites=chan_overwrites,
                    reason="Template application: create forum channel"
                )

            # Explicitly lock category sync if the template had it synced
            if new_chan and parent_cat and is_synced:
                await new_chan.sync_permissions(reason="Template application: sync with parent category")

            await asyncio.sleep(0.35)
        except discord.HTTPException as e:
            logger.warning(f"Failed to create channel {t_chan.name}: {e}")

    logger.info(f"Template '{template.name}' applied successfully to '{guild.name}'.")

    try:
        await ctx.author.send(f"✅ Template `{template.name}` applied successfully to **{guild.name}** with all permissions synchronized!")
    except discord.Forbidden:
        pass


@apply_template.error
async def apply_template_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need **Administrator** permissions to run this command.")
    else:
        logger.error(f"Command error: {error}")
        await ctx.send(f"❌ Command failed: {error}")


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.critical("FATAL: 'DISCORD_TOKEN' environment variable is missing.")
        sys.exit(1)

    try:
        bot.run(token)
    except discord.LoginFailure:
        logger.critical("FATAL: Invalid Discord token.")
        sys.exit(1)
