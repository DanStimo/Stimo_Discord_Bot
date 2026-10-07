import discord
from discord import app_commands
import httpx
import json
from fuzzywuzzy import process, fuzz
import os
import random
import asyncio
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import re
import asyncpg
import logging
from discord.utils import escape_markdown
import math
from io import BytesIO
from PIL import Image

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CLUB_ID = os.getenv("CLUB_ID", "167054")
PLATFORM = os.getenv("PLATFORM", "common-gen5")
UEX_API_KEY = os.getenv("UEX_API_KEY", "").strip()
UEX_API_BASE = os.getenv("UEX_API_BASE", "https://api.uexcorp.space/2.0").rstrip("/")

OFFSIDE_KEY = "offside.json"

MATCH_TYPE_LABELS = {
    "leagueMatch": "League",
    "playoffMatch": "Playoff",
    "friendlyMatch": "Friendly"
}

# --- EA HTTP client (shared) ---
EA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Origin": "https://www.ea.com",
    "Referer": "https://www.ea.com/ea-sports-fc/pro-clubs",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

_client_ea = httpx.AsyncClient(
    timeout=12,
    headers=EA_HEADERS,
    http2=True,
    follow_redirects=True,
)

# --- Twitch live announce config ---
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_CHANNEL_LOGIN = (os.getenv("TWITCH_CHANNEL_LOGIN") or "").lower().strip()
TWITCH_ANNOUNCE_CHANNEL_IDS = [
    int(x.strip())
    for x in os.getenv("TWITCH_ANNOUNCE_CHANNEL_IDS", "").split(",")
    if x.strip()
]
TWITCH_LIVE_ROLE_IDS = {
    int(k): int(v)
    for k, v in (
        pair.split(":")
        for pair in os.getenv("TWITCH_LIVE_ROLE_IDS", "").split(",")
        if ":" in pair
    )
}
TWITCH_POLL_INTERVAL = int(os.getenv("TWITCH_POLL_INTERVAL", "60"))

# In-memory state (we'll also persist if your DB helpers exist)
_twitch_token = None  # {"access_token": "...", "expires_at": datetime}
TWITCH_STATE_KEY = "twitch_live_state.json"  # for optional persistence

# Event & template config
EVENT_CREATOR_ROLE_ID = int(os.getenv("EVENT_CREATOR_ROLE_ID", "0")) if os.getenv("EVENT_CREATOR_ROLE_ID") else 0
EVENT_CREATOR_ROLE_NAME = "Moderator"
EVENTS_FILE = os.getenv("EVENTS_FILE", "events.json")
TEMPLATES_FILE = os.getenv("TEMPLATES_FILE", "templates.json")
ATTEND_EMOJI = "✅"
ABSENT_EMOJI = "❌"
MAYBE_EMOJI  = "🤷"
LATE_EMOJI   = "🕒"
EVENT_EMBED_COLOR_HEX = os.getenv("EVENT_EMBED_COLOR_HEX", "#3498DB")
DEFAULT_TZ = ZoneInfo("Europe/London")

# --- Intents ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Channel where typing a club name without a command should trigger stats
FREE_STATS_CHANNEL_ID = int(os.getenv("FREE_STATS_CHANNEL_ID", "0"))

# Persistent club-search leaderboard
SEARCH_LEADERBOARD_FILE = os.getenv(
    "SEARCH_LEADERBOARD_FILE",
    "search_leaderboard.json",
)


def load_search_leaderboard() -> dict:
    try:
        if not os.path.exists(SEARCH_LEADERBOARD_FILE):
            return {"guilds": {}}

        with open(
            SEARCH_LEADERBOARD_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if not isinstance(data, dict):
            return {"guilds": {}}

        data.setdefault("guilds", {})
        return data

    except Exception as error:
        print(f"[LEADERBOARD] Failed to load data: {error}")
        return {"guilds": {}}


search_leaderboard_data = load_search_leaderboard()


def save_search_leaderboard() -> None:
    temporary_file = f"{SEARCH_LEADERBOARD_FILE}.tmp"

    try:
        with open(
            temporary_file,
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                search_leaderboard_data,
                file,
                indent=2,
                ensure_ascii=False,
            )

        os.replace(temporary_file, SEARCH_LEADERBOARD_FILE)

    except Exception as error:
        print(f"[LEADERBOARD] Failed to save data: {error}")


def record_club_search(
    guild: discord.Guild | None,
    user: discord.abc.User,
) -> None:
    if guild is None or user.bot:
        return

    guild_id = str(guild.id)
    user_id = str(user.id)

    guild_data = search_leaderboard_data["guilds"].setdefault(
        guild_id,
        {"users": {}},
    )

    users = guild_data.setdefault("users", {})

    entry = users.setdefault(
        user_id,
        {
            "count": 0,
            "display_name": user.name,
        },
    )

    entry["count"] = int(entry.get("count", 0)) + 1
    entry["display_name"] = getattr(
        user,
        "display_name",
        user.name,
    )

    save_search_leaderboard()

    print(
        f"[LEADERBOARD] {entry['display_name']} now has "
        f"{entry['count']} successful searches in guild {guild_id}"
    )

# =========================================================
# PHONICS SELF-SELECT ROLES
# =========================================================

SELF_ROLE_CHANNEL_ID = 1376174726258360471
SELF_ROLE_MESSAGE_ID = 1376183419280818286

SELF_SELECT_ROLES = {
    "👮": 1375523553742553118,  # Security
    "💗": 1375523406144864357,  # Medical
    "👷": 1375523774195175444,  # Industry
    "🌐": 1375523125290336306,  # Logistics
    "🌍": 1375523873671479406,  # Exploration
    "📷": 1375523226133987329,  # Media
}

# Channel where we log free-typed stats lookups
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
STAR_LOG_CHANNEL_ID = int(os.getenv("STAR_LOG_CHANNEL_ID", str(LOG_CHANNEL_ID)))
STAR_COMMAND_DELETE_SECONDS = int(os.getenv("STAR_COMMAND_DELETE_SECONDS", "90"))

CREST_URL_TEMPLATE = os.getenv("CREST_URL_TEMPLATE", "").strip()

def build_crest_url(team_id: str | int) -> str | None:
    """Return a crest URL from teamId using your template, or None if not set."""
    if not team_id or not CREST_URL_TEMPLATE:
        return None
    return CREST_URL_TEMPLATE.format(teamId=str(team_id))

async def get_crest_asset_id_for_club(club_id: str | int) -> str | None:
    """
    Find the EA crestAssetId used by the crest image CDN.
    """
    club_id = str(club_id)

    # Try overallStats first, in case customKit is included.
    try:
        response = await _client_ea.get(
            "https://proclubs.ea.com/api/fc/clubs/overallStats",
            params={"platform": PLATFORM, "clubIds": club_id},
        )

        if response.status_code == 200:
            data = response.json() or []

            if isinstance(data, list) and data:
                row = data[0] or {}
                custom_kit = row.get("customKit") or {}
                crest_id = (
                    custom_kit.get("crestAssetId")
                    or row.get("crestAssetId")
                )

                if crest_id is not None and str(crest_id):
                    return str(crest_id)

    except Exception as e:
        print(f"[crest] overallStats lookup failed: {e}")

    # Recent matches reliably include details.customKit.crestAssetId.
    try:
        match_types = ["leagueMatch", "playoffMatch", "friendlyMatch"]
        newest = []

        for match_type in match_types:
            response = await _client_ea.get(
                "https://proclubs.ea.com/api/fc/clubs/matches",
                params={
                    "matchType": match_type,
                    "platform": PLATFORM,
                    "clubIds": club_id,
                },
            )

            if response.status_code == 404:
                continue

            response.raise_for_status()
            newest.extend(response.json() or [])

        newest.sort(
            key=lambda match: match.get("timestamp", 0),
            reverse=True,
        )

        for match in newest:
            clubs = match.get("clubs") or {}
            club = clubs.get(club_id) or {}
            details = club.get("details") or {}

            custom_kit = (
                details.get("customKit")
                or club.get("customKit")
                or {}
            )

            crest_id = custom_kit.get("crestAssetId")
            team_id = details.get("teamId")

            # EA normally displays the base team logo when teamId has
            # a valid image. Custom clubs may have a generated teamId
            # with no image, so fall back to crestAssetId.
            candidates = [
                (team_id, "teamId/base asset"),
                (crest_id, "custom crest asset"),
            ]

            for image_id, source in candidates:
                if image_id is None or not str(image_id):
                    continue

                crest_url = build_crest_url(image_id)
                if not crest_url:
                    continue

                try:
                    image_response = await _client_ea.get(crest_url)
                    content_type = image_response.headers.get(
                        "content-type", ""
                    ).lower()

                    if (
                        image_response.status_code == 200
                        and content_type.startswith("image/")
                    ):
                        print(
                            f"[crest] Club {club_id}: "
                            f"teamId={team_id}, "
                            f"crestAssetId={crest_id}, "
                            f"selected={image_id} ({source})"
                        )
                        return str(image_id)

                except Exception as image_error:
                    print(
                        f"[crest] Could not test asset "
                        f"{image_id}: {image_error}"
                    )

    except Exception as e:
        print(f"[crest] matches lookup failed: {e}")

    print(f"[crest] No crestAssetId found for club {club_id}")
    return None

# === Welcome Feature ===
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0"))
WELCOME_COLOR_HEX = os.getenv("WELCOME_COLOR_HEX", "#17c1ff")

welcome_config = {
    "channel_id": WELCOME_CHANNEL_ID,
    "color_hex": WELCOME_COLOR_HEX,
}

# --- Lineups config/persistence ---
LINEUPS_FILE = os.getenv("LINEUPS_FILE", "lineups.json")

# ---- Admin role restriction for lineup controls ----
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0")) if os.getenv("ADMIN_ROLE_ID") else 0
ADMIN_ROLE_NAME = os.getenv("ADMIN_ROLE_NAME", "Administrator")

def has_admin_role(member: discord.Member) -> bool:
    if not member:
        return False
    # Prefer explicit role id if provided, else fall back to name
    if ADMIN_ROLE_ID:
        if any(r.id == ADMIN_ROLE_ID for r in member.roles):
            return True
    if any(r.name == ADMIN_ROLE_NAME for r in member.roles):
        return True
    # (Optional) also treat Discord permission as admin
    if getattr(member.guild_permissions, "administrator", False):
        return True
    return False

# Common football formations -> ordered positions (11)
FORMATIONS: dict[str, list[str]] = {
    "4-3-3 D": ["GK", "RB", "RCB", "LCB", "LB", "RCM", "CDM", "LCM", "RW", "ST", "LW"],
    "4-3-3 A": ["GK", "RB", "RCB", "LCB", "LB", "RCM", "CAM", "LCM", "RW", "ST", "LW"],
    "4-2-3-1": ["GK", "RB", "RCB", "LCB", "LB", "RDM", "LDM", "RAM", "CAM", "LAM", "ST"],
    "4-4-2": ["GK", "RB", "RCB", "LCB", "LB", "RM", "RCM", "LCM", "LM", "RST", "LST"],
    "3-5-2": ["GK", "RCB", "CB", "LCB", "RM", "RDM", "CAM", "LDM", "LM", "RST", "LST"],
    "5-3-2": ["GK", "RWB", "RCB", "CB", "LCB", "LWB", "RCM", "CM", "LCM", "RST", "LST"],
    "3-4-3": ["GK", "RCB", "CB", "LCB", "RM", "RCM", "LCM", "LM", "RW", "ST", "LW"],
    "4-1-2-1-2": ["GK", "RB", "RCB", "LCB", "LB", "CDM", "RCM", "LCM", "CAM", "RST", "LST"],
}

def load_lineups_store():
    return load_json_file(LINEUPS_FILE, {"next_id": 1, "lineups": {}})

def save_lineups_store():
    save_json_file(LINEUPS_FILE, lineups_store)

def _color_from_hex(h: str) -> discord.Color:
    h = (h or "#17c1ff").strip().lstrip("#")
    return discord.Color(int(h, 16))

def _twitch_url_from_input(value: str | None) -> str | None:
    """
    Accepts a Twitch username OR a full twitch URL and returns
    a normalized 'https://twitch.tv/<username>' or None.
    """
    if not value:
        return None
    v = value.strip()
    if not v:
        return None

    # Strip protocol and www
    v = v.replace("https://", "").replace("http://", "")
    if v.startswith("www."):
        v = v[4:]

    # If they pasted a URL, pull out the username
    if v.lower().startswith("twitch.tv/"):
        v = v.split("/", 1)[1]

    # Keep only the username (alnum + underscore)
    m = re.match(r"^([A-Za-z0-9_]+)$", v)
    if not m:
        # fallback: take the first path segment
        v = v.split("/", 1)[0]

    username = v
    return f"https://twitch.tv/{username}"

@tree.command(
    name="refreshwelcomefooters",
    description="Update existing welcome message styling."
)
async def refresh_welcome_footers(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command must be used in the server.",
            ephemeral=True,
        )
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "You must be an administrator to use this command.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    welcome_channel_id = 1551149703167475744
    channel = interaction.guild.get_channel(welcome_channel_id)

    if channel is None:
        await interaction.followup.send(
            "I could not find the welcome channel.",
            ephemeral=True,
        )
        return

    footer_icon = client.user.display_avatar.url
    updated = 0

    async for message in channel.history(limit=None):
        if message.author.id != client.user.id:
            continue

        if not message.embeds:
            continue

        embed = message.embeds[0]

        if embed.title != "Welcome aboard! 👋":
            continue

        updated_embed = embed.copy()
        existing_description = updated_embed.description or ""
        first_section = existing_description.split("\n\n", 1)[0]
        
        updated_embed.description = (
            f"{first_section}\n\n"
            f"• **Say hi!** 👋"
        )
        updated_embed.colour = discord.Colour(0x17C1FF)
        updated_embed.set_footer(
            text="Phonics Bot",
            icon_url=footer_icon,
        )

        try:
            await message.edit(embed=updated_embed)
            updated += 1
        except discord.HTTPException as error:
            print(
                f"[WELCOME] Could not update message "
                f"{message.id}: {error}"
            )

    await interaction.followup.send(
        f"✅ Updated **{updated}** existing welcome messages.",
        ephemeral=True,
    )

@client.event
async def on_member_join(member: discord.Member):
    print(f"[JOIN] on_member_join fired for {member} (id={member.id})")

    # --- Hardcoded config ---
    WELCOME_CONFIG = {
        1551149701972103208: {
            "server_name": member.guild.name,
            "welcome_channel_id": 1551149703167475744,
            "member_role_id": 1551154383113429032,
            "rules_channel_id": 1551159502273904731,
        },
    }
    
    config = WELCOME_CONFIG.get(member.guild.id)
    if not config:
        print(f"[WARN] No welcome config for guild {member.guild.id}")
        return
    
    WELCOME_CHANNEL_ID = config["welcome_channel_id"]
    WELCOME_COLOR = 0x17C1FF
    MEMBER_ROLE_ID = config["member_role_id"]

    # --- Resolve channel ---
    channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
    if channel is None:
        print(f"[ERROR] Could not resolve welcome channel {WELCOME_CHANNEL_ID}")
        return

    # --- Auto-assign the Member role ---
    role = member.guild.get_role(MEMBER_ROLE_ID)
    if role:
        try:
            await member.add_roles(role, reason="Auto member role on join")
            print(f"[INFO] Gave {member} the role: {role.name}")
        except discord.Forbidden:
            print("[ERROR] Cannot add role: missing Manage Roles or role hierarchy issue.")
        except Exception as e:
            print(f"[ERROR] Failed to add Member role: {e}")
    else:
        print(f"[WARN] Member role with ID {MEMBER_ROLE_ID} not found in guild.")

    # --- Build embed ---
    embed = discord.Embed(
        title="Welcome aboard! 👋",
        description=(
            f"{member.mention}, you've reached the **{config['server_name']}** Discord server!\n\n"
            f"• **Say hi!** 👋"
        ),
        color=WELCOME_COLOR,
        timestamp=datetime.now(timezone.utc)
    )

    # Author: "<display_name> has arrived!" with avatar
    embed.set_author(
        name=f"{member.display_name} has arrived!",
        icon_url=member.display_avatar.url
    )

    # Thumbnail: guild icon (fallback to member avatar)
    if member.guild.icon:
        embed.set_thumbnail(url=member.guild.icon.url)
    else:
        embed.set_thumbnail(url=member.display_avatar.url)

    # Footer
    footer_icon = client.user.display_avatar.url if client.user else None
    embed.set_footer(text="Phonics Bot", icon_url=footer_icon)

    # --- Send and react ---
    try:
        perms = channel.permissions_for(channel.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links and perms.add_reactions):
            print("[ERROR] Missing one of: View Channel / Send Messages / Embed Links / Add Reactions in welcome channel.")
            return

        message = await channel.send(content=member.mention, embed=embed)

        # react with custom emoji named "Wave"
        await message.add_reaction("👋")

        print(f"[INFO] Welcome message posted for {member} in #{channel.name}")

    except Exception as e:
        print(f"[ERROR] Failed to send welcome embed or add reaction: {e}")

async def safe_delete(msg: discord.Message, delay: float | None = None):
    try:
        if delay:
            await asyncio.sleep(delay)
        await msg.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass

async def warn_search_channel(
    message: discord.Message,
    reason: str,
):
    await safe_delete(message)

    warning = await message.channel.send(
        f"{message.author.mention} {reason}\n"
        f"This channel is only for **EA FC club searches**. "
        f"Enter a club name containing **2–15 letters, numbers or spaces**, "
        f"with no punctuation."
    )

    asyncio.create_task(safe_delete(warning, delay=15))

async def send_temp_followup(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = False,
    delete_after: int | None = None
):
    """
    Send a followup message and auto-delete it after N seconds
    (non-ephemeral only).
    """
    send_kwargs = {
        "ephemeral": ephemeral,
        "wait": True,
    }

    if content is not None:
        send_kwargs["content"] = content

    if embed is not None:
        send_kwargs["embed"] = embed

    if view is not None:
        send_kwargs["view"] = view

    msg = await interaction.followup.send(**send_kwargs)

    if not ephemeral:
        delay = STAR_COMMAND_DELETE_SECONDS if delete_after is None else delete_after
        asyncio.create_task(safe_delete(msg, delay))

    return msg

async def log_star_command_usage(
    interaction: discord.Interaction,
    command_name: str,
    message: discord.Message | None = None,
    extra_text: str | None = None
):
    """
    Log ALL Star Citizen commands ONLY into the Phonics server log channel.
    """

    PHONICS_GUILD_ID = 1373595733403631677

    # PUT YOUR PHONICS LOG CHANNEL ID HERE
    PHONICS_STAR_LOG_CHANNEL_ID = 1504020053992149062

    # Ignore commands used outside Phonics
    if not interaction.guild:
        return

    if interaction.guild.id != PHONICS_GUILD_ID:
        print(f"[STAR LOG] Ignoring /{command_name} from non-Phonics guild")
        return

    try:
        # Always fetch the channel directly from the Phonics guild
        phonics_guild = client.get_guild(PHONICS_GUILD_ID)

        if not phonics_guild:
            print("[STAR LOG] Could not find Phonics guild")
            return

        log_ch = phonics_guild.get_channel(PHONICS_STAR_LOG_CHANNEL_ID)

        if not log_ch:
            print(f"[STAR LOG] Could not find Phonics log channel {PHONICS_STAR_LOG_CHANNEL_ID}")
            return

        user_name = (
            interaction.user.display_name
            if isinstance(interaction.user, discord.Member)
            else interaction.user.name
        )

        channel_mention = (
            interaction.channel.mention
            if interaction.channel
            else "#unknown"
        )

        header = f"📦 /{command_name} by {user_name} in {channel_mention}:"

        # Embed logging
        if message and message.embeds:
            await log_ch.send(content=header, embeds=message.embeds)
            return

        # Text logging
        if message and message.content:
            await log_ch.send(content=f"{header}\n{message.content}")
            return

        # Extra text fallback
        if extra_text:
            await log_ch.send(content=f"{header}\n{extra_text}")
            return

        # Final fallback
        await log_ch.send(content=header)

    except Exception as e:
        print(f"[ERROR] Failed to log Star Citizen command /{command_name}: {e}")
        
async def log_stats_embed_for_request(
    *, guild: discord.Guild, author: discord.abc.User, origin_channel: discord.TextChannel, embed: discord.Embed
):
    """
    Send a log entry that visually matches the /stats output:
    a header like '/stats by @User in #channel:' + the stats embed.
    """
    log_ch = guild.get_channel(LOG_CHANNEL_ID) or (client.get_channel(LOG_CHANNEL_ID) if guild else None)
    if not log_ch:
        print(f"[WARN] Log channel {LOG_CHANNEL_ID} not found")
        return
    header = f"📥/stats by {author.name} in {origin_channel.mention}:"
    await log_ch.send(content=header, embed=embed)

# -------------------------
# Honeypot anti-spam (configured explicitly with /honeypot setup)
# -------------------------
HONEYPOT_CHANNEL_NAME = "‧₊˚✧do-not-post✧˚₊‧"
HONEYPOT_FILE = os.getenv("HONEYPOT_FILE", "honeypot_state.json")
_honeypot_state = {"guilds": {}}
_honeypot_loaded = False
_honeypot_lock = asyncio.Lock()
_honeypot_recent = {}  # Short-lived deduplication for messages already in flight.


async def honeypot_load():
    global _honeypot_state, _honeypot_loaded
    if _honeypot_loaded:
        return
    try:
        if DB_POOL:
            data = await db_load_json(HONEYPOT_FILE, {"guilds": {}})
        elif os.path.exists(HONEYPOT_FILE):
            with open(HONEYPOT_FILE, encoding="utf-8") as file:
                data = json.load(file)
        else:
            data = {"guilds": {}}
        if not isinstance(data, dict) or not isinstance(data.get("guilds"), dict):
            raise ValueError("Invalid honeypot state; restore the state file before setup")
        _honeypot_state = data
        _honeypot_loaded = True
    except Exception:
        logging.exception("[HONEYPOT] Could not load configuration")
        raise


async def honeypot_save():
    if DB_POOL:
        await db_save_json(HONEYPOT_FILE, _honeypot_state)
    else:
        temporary = HONEYPOT_FILE + ".tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(_honeypot_state, file, ensure_ascii=False, indent=2)
        os.replace(temporary, HONEYPOT_FILE)


def honeypot_warning(config):
    embed = discord.Embed(
        title="DO NOT SEND MESSAGES IN THIS CHANNEL" if config.get("enabled") else "HONEYPOT DISABLED",
        description=(
            "This channel is used to catch spam bots. Any messages sent here "
            "will result in **a softban**, and everything you posted goes with you."
        ) if config.get("enabled") else "Automatic moderation is currently disabled in this channel.",
        colour=0x202225,
    )
    embed.set_thumbnail(url="https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/26d4.png")
    embed.set_footer(text="Softban = removal without a permanent ban. Messages from the last 24 hours are deleted.")
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label=f"Bans: {config.get('bans', 0)}",
        style=discord.ButtonStyle.secondary,
        disabled=True,
        custom_id="honeypot_ban_counter",
    ))
    return embed, view


async def honeypot_refresh(guild, config):
    channel = guild.get_channel(config.get("channel_id", 0))
    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError("Honeypot channel missing; run /honeypot setup again")
    embed, view = honeypot_warning(config)
    try:
        message = await channel.fetch_message(config.get("message_id", 0))
        if message.author.id != client.user.id:
            raise RuntimeError("Saved warning does not belong to this bot")
        await message.edit(embed=embed, view=view)
    except discord.NotFound:
        message = await channel.send(embed=embed, view=view)
        config["message_id"] = message.id
        await honeypot_save()


async def honeypot_log(guild, config, text):
    logging.info("[HONEYPOT] guild=%s %s", guild.id, text)
    channel = guild.get_channel(config.get("log_channel_id", 0))
    if channel and channel.id != config.get("channel_id"):
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logging.exception("[HONEYPOT] Could not send moderation log")


async def honeypot_handle(message):
    if not _honeypot_loaded:
        return False
    config = _honeypot_state["guilds"].get(str(message.guild.id))
    # Match the configured ID, never merely a channel name.
    channel_id = message.channel.parent_id if isinstance(message.channel, discord.Thread) else message.channel.id
    if not config or not config.get("enabled") or channel_id != config.get("channel_id"):
        return False
    member = message.author
    if not isinstance(member, discord.Member) or member.bot or message.webhook_id:
        return True
    permissions = member.guild_permissions
    if (member.id == message.guild.owner_id or permissions.administrator
            or permissions.manage_guild or permissions.ban_members
            or permissions.kick_members or permissions.moderate_members):
        return True
    guild = message.guild
    me = guild.me
    if not me or not me.guild_permissions.ban_members or member.top_role >= me.top_role:
        await honeypot_log(guild, config, f"⚠️ Cannot softban {member} ({member.id}): check Ban Members permission and role order.")
        return True
    async with _honeypot_lock:
        if not config.get("enabled"):
            return True
        key = (guild.id, member.id)
        now = asyncio.get_running_loop().time()
        for old_key, when in list(_honeypot_recent.items()):
            if now - when > 30:
                del _honeypot_recent[old_key]
        if key in _honeypot_recent:
            return True
        # Persist the intended unban before taking action, so a restart can recover.
        pending = config.setdefault("pending_unbans", [])
        if member.id not in pending:
            pending.append(member.id)
        try:
            await honeypot_save()
        except Exception:
            pending.remove(member.id)
            await honeypot_log(guild, config, "⚠️ Softban skipped: could not save recovery state.")
            return True
        reason = f"Honeypot: posted in {HONEYPOT_CHANNEL_NAME} ({message.id})"
        try:
            await guild.ban(member, delete_message_days=1, reason=reason)
        except discord.HTTPException as exc:
            # Keep recovery record: an ambiguous network failure might have banned them.
            await honeypot_log(guild, config, f"⚠️ Ban request failed for {member} ({member.id}): {exc}. Recovery will check on restart.")
            return True
        _honeypot_recent[key] = now
        config["bans"] = config.get("bans", 0) + 1
        outcome = "✅ Softbanned"
        try:
            await guild.unban(discord.Object(id=member.id), reason="Honeypot softban: allow rejoining")
            pending.remove(member.id)
        except discord.HTTPException as exc:
            outcome = f"⚠️ Banned, but unban failed ({exc}); manually unban or restart the bot to retry:"
        try:
            await honeypot_save()
            await honeypot_refresh(guild, config)
        except Exception:
            logging.exception("[HONEYPOT] Could not save/update ban counter")
        await honeypot_log(guild, config, f"{outcome} {member} ({member.id}). Deleted messages from the last 24 hours. Total bans: {config['bans']}.")
    return True


async def honeypot_startup():
    async with _honeypot_lock:
        await honeypot_load()
        for guild_id, config in _honeypot_state["guilds"].items():
            guild = client.get_guild(int(guild_id))
            if not guild:
                continue
            # Only undo our own recorded honeypot bans, never unrelated bans.
            for user_id in list(config.get("pending_unbans", [])):
                try:
                    entry = await guild.fetch_ban(discord.Object(id=user_id))
                    if (entry.reason or "").startswith(f"Honeypot: posted in {HONEYPOT_CHANNEL_NAME}"):
                        await guild.unban(entry.user, reason="Recover interrupted Honeypot softban")
                        await honeypot_log(guild, config, f"✅ Recovered pending softban: {user_id} is now unbanned.")
                    config["pending_unbans"].remove(user_id)
                except discord.NotFound:
                    config["pending_unbans"].remove(user_id)
                except discord.HTTPException:
                    logging.exception("[HONEYPOT] Could not recover pending unban %s", user_id)
            await honeypot_save()
            if config.get("enabled"):
                try:
                    await honeypot_refresh(guild, config)
                except Exception:
                    logging.exception("[HONEYPOT] Warning refresh failed for %s", guild_id)


honeypot_commands = app_commands.Group(name="honeypot", description="Set up and manage the anti-spam trap channel")


@honeypot_commands.command(name="setup", description="Create the do-not-post channel and enable automatic softbans")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def honeypot_setup(interaction: discord.Interaction, category: discord.CategoryChannel | None = None, log_channel: discord.TextChannel | None = None):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    me = guild.me
    required = ("manage_channels", "ban_members", "view_channel", "send_messages", "embed_links", "read_message_history")
    missing = [name for name in required if not getattr(me.guild_permissions, name)]
    if missing:
        await interaction.followup.send("Bot permissions missing: " + ", ".join(missing), ephemeral=True)
        return
    async with _honeypot_lock:
        await honeypot_load()
        old = _honeypot_state["guilds"].get(str(guild.id), {})
        channel = guild.get_channel(old.get("channel_id", 0))
        if not isinstance(channel, discord.TextChannel):
            # Never adopt an existing channel by name: it could contain legitimate conversation.
            channel = await guild.create_text_channel(
                HONEYPOT_CHANNEL_NAME, category=category,
                topic="DO NOT POST — posting here causes an automatic softban and deletes your last 24 hours of messages.",
                overwrites={
                    guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, create_public_threads=False, create_private_threads=False),
                    me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True, manage_messages=True),
                },
                reason="Administrator requested Honeypot setup",
            )
        elif category is not None:
            await channel.edit(category=category, sync_permissions=False)
        selected_log = log_channel or guild.get_channel(old.get("log_channel_id", 0)) or guild.get_channel(LOG_CHANNEL_ID)
        if selected_log and selected_log.id == channel.id:
            await interaction.followup.send("Choose a different log channel from the honeypot channel.", ephemeral=True)
            return
        config = dict(old)
        config.update(channel_id=channel.id, enabled=True, bans=old.get("bans", 0), log_channel_id=selected_log.id if selected_log else 0)
        _honeypot_state["guilds"][str(guild.id)] = config
        try:
            await honeypot_refresh(guild, config)
            await honeypot_save()
        except Exception:
            _honeypot_state["guilds"][str(guild.id)] = old
            await honeypot_save()
            raise
    await interaction.followup.send(
        f"✅ Honeypot enabled in {channel.mention}. Posting there softbans ordinary members and deletes their last 24 hours of messages. "
        "Owners, moderators and bots are exempt. Keep the bot's role above member roles. "
        + (f"Logs: {selected_log.mention}." if selected_log else "No Discord log channel selected; actions go to the bot logs."),
        ephemeral=True,
    )


@honeypot_commands.command(name="disable", description="Stop automatic moderation in the honeypot channel")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def honeypot_disable(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with _honeypot_lock:
        await honeypot_load()
        config = _honeypot_state["guilds"].get(str(interaction.guild_id))
        if config:
            config["enabled"] = False
            await honeypot_save()
            try:
                await honeypot_refresh(interaction.guild, config)
            except Exception:
                logging.exception("[HONEYPOT] Could not update disabled warning")
    await interaction.followup.send("Honeypot disabled. The channel and ban counter are preserved. Run /honeypot setup to enable it again.", ephemeral=True)


@honeypot_commands.error
async def honeypot_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logging.error("[HONEYPOT] Command failed: %s", error)
    text = "Honeypot command failed. You need Administrator permission; also check the bot permissions and bot logs."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


tree.add_command(honeypot_commands)


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    if await honeypot_handle(message):
        return

    if message.channel.id == EA_TOP100_CHANNEL_ID:
        await handle_ea_top100_channel_search(message)
        return

    if message.channel.id != FREE_STATS_CHANNEL_ID:
        return

    content = (message.content or "").strip()

    valid_club_name = (
        2 <= len(content) <= 15
        and all(
            character.isalnum() or character == " "
            for character in content
        )
    )

    if not valid_club_name:
        await warn_search_channel(
            message,
            "That message is not a valid EA FC club name.",
        )
        return

    try:
        async with message.channel.typing():
            # Direct club ID search
            if content.isdigit():
                club_id = content
                found = await search_clubs_ea(content)

                club_name = (
                    str(found[0]["clubInfo"]["name"])
                    if found
                    else f"Club {club_id}"
                )

                asyncio.create_task(safe_delete(message))

                await send_stats_message_to_channel(
                    message.channel,
                    club_id,
                    club_name,
                    origin_message=message,
                )
                return

            # Club name search
            matches = await search_clubs_ea(content)

            if not matches:
                asyncio.create_task(safe_delete(message))

                no_result_message = await message.channel.send(
                    f"{message.author.mention} no EA FC club was found "
                    f"matching **{content}**. Please check the spelling "
                    f"and try again."
                )

                asyncio.create_task(
                    safe_delete(no_result_message, delay=15)
                )
                return

            if len(matches) == 1:
                club = matches[0]["clubInfo"]

                asyncio.create_task(safe_delete(message))

                await send_stats_message_to_channel(
                    message.channel,
                    str(club["clubId"]),
                    club["name"],
                    origin_message=message,
                )
                return

            # Multiple matches
            asyncio.create_task(safe_delete(message))

            view = FreeStatsDropdown(
                matches,
                original_query=content,
                request_message=message,
            )

            selector = await message.channel.send(
                "Multiple clubs found. Please select:",
                view=view,
            )

            asyncio.create_task(
                delete_after_delay(selector, 90)
            )

    except Exception as error:
        print(f"[ERROR] free-typed stats failed: {error}")
        
# Load or initialize club mapping
try:
    with open('club_mapping.json', 'r') as f:
        club_mapping = json.load(f)
except FileNotFoundError:
    club_mapping = {}

def normalize(name):
    return ''.join(name.lower().split())

def streak_emoji(value):
    try:
        value = int(value)
        if value <= 5:
            return "❄️"
        elif value <= 9:
            return "🔥"
        elif value <= 19:
            return "🔥🔥"
        else:
            return "🔥🔥🔥"
    except:
        return "❓"

class PrintRecordButton(discord.ui.View):
    def __init__(self, stats, club_name):
        super().__init__(timeout=900)
        self.stats = stats
        self.club_name = club_name
        self.message = None

    @discord.ui.button(label="🖨️ Print Record", style=discord.ButtonStyle.primary)
    async def print_record(self, interaction: discord.Interaction, button: discord.ui.Button):
        wins = self.stats.get("wins", "N/A")
        draws = self.stats.get("draws", "N/A")
        losses = self.stats.get("losses", "N/A")

        embed = discord.Embed(
            title=f"{self.club_name} W-D-L Record",
            description=f"**{wins}** Wins | **{draws}** Draws | **{losses}** Losses",
            color=0xB30000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except Exception as e:
                print(f"[ERROR] Failed to remove view after timeout: {e}")

import random
import urllib.parse
import asyncio

async def _ea_get_json(url: str, params: dict, retries: int = 5) -> dict | list | None:
    """GET JSON with retries + short body log on non-200."""
    for attempt in range(retries):
        try:
            r = await _client_ea.get(url, params=params)

            if r.status_code == 200:
                return r.json()

            print(f"[EA] {r.status_code} {url} try {attempt+1}/{retries} :: {r.text[:200]}")

            # For anti-bot / transient blocking, back off a bit more
            if r.status_code in (403, 429, 500, 502, 503, 504):
                await asyncio.sleep(1.2 + attempt * 1.5 + random.random())
                continue

        except Exception as e:
            print(f"[EA] exception {url} try {attempt+1}/{retries} :: {e}")

        await asyncio.sleep(0.8 + random.random())

    return None

async def search_clubs_ea(query: str) -> list:
    """Partial-name search with retries/backoff."""
    if not query or not query.strip():
        return []

    data = await _ea_get_json(
        "https://proclubs.ea.com/api/fc/allTimeLeaderboard/search",
        {
            "platform": PLATFORM,
            "clubName": query.strip().upper()
        }
    )

    if not isinstance(data, list):
        return []

    return [
        c for c in data
        if c.get("clubInfo", {}).get("name", "").strip().lower() != "none of these"
    ]
    
from datetime import datetime, timezone

async def get_current_squad(club_id: str) -> list[str]:
    """
    Fetch current squad/member list from the members/stats endpoint (or sensible fallbacks).
    Returns a list of player names (may be empty).
    """
    club_id = str(club_id)
    try:
        # try the members/stats endpoint you referenced
        data = await _ea_get_json(
            "https://proclubs.ea.com/api/fc/members/stats",
            {"platform": PLATFORM, "clubId": club_id}
        ) or {}

        # common shapes:
        # 1) dict with "members": [ { "name": "...", ...}, ... ]
        if isinstance(data, dict):
            members = data.get("members") or data.get("players") or []
        # 2) list of members
        elif isinstance(data, list):
            members = data
        else:
            members = []

        names = []
        for m in members:
            if not isinstance(m, dict):
                continue
            # try common name keys (robust)
            name = m.get("name") or m.get("playername") or m.get("displayName") or m.get("playerName")
            if name:
                names.append(str(name))
        # unique & preserve order
        seen = set()
        out = []
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out

    except Exception as e:
        print(f"[ERROR] Failed to fetch current squad for {club_id}: {e}")
        return []

async def get_last_played_timestamp(club_id: str | int) -> datetime | None:
    """
    Returns a timezone-aware datetime of the club's most recent match
    across league, playoff, and friendly — or None if no matches.
    """
    club_id = str(club_id)
    match_types = ["leagueMatch", "playoffMatch", "friendlyMatch"]
    latest_ts = 0

    try:
        for mt in match_types:
            data = await _ea_get_json(
                "https://proclubs.ea.com/api/fc/clubs/matches",
                {"matchType": mt, "platform": PLATFORM, "clubIds": club_id},
            ) or []
            # Find max timestamp among returned matches (if any)
            for m in data:
                ts = int(m.get("timestamp", 0) or 0)
                if ts > latest_ts:
                    latest_ts = ts
    except Exception as e:
        print(f"[ERROR] get_last_played_timestamp({club_id}): {e}")

    if latest_ts <= 0:
        return None
    return datetime.fromtimestamp(latest_ts, tz=timezone.utc)

    def format_last_played(dt: datetime | None) -> str:
        if not dt:
            return "—"
        now = datetime.now(timezone.utc)
        delta = now - dt
        days = delta.days
        hours = int(delta.total_seconds() // 3600)
        if days >= 1:
            return f"{days}d ago"
        if hours >= 1:
            return f"{hours}h ago"
        mins = int(delta.total_seconds() // 60)
        return f"{mins}m ago"

def format_last_played(dt: datetime | None) -> str:
    """Format a datetime into a human-friendly 'last played' string."""
    if not dt:
        return "—"
    now = datetime.now(timezone.utc)
    delta = now - dt
    days = delta.days
    hours = int(delta.total_seconds() // 3600)
    if days >= 1:
        return f"{days}d ago"
    if hours >= 1:
        return f"{hours}h ago"
    mins = int(delta.total_seconds() // 60)
    return f"{mins}m ago"

def md_escape(s: str) -> str:
    """Escape Discord markdown meta so club names render cleanly."""
    if not isinstance(s, str):
        s = str(s or "")
    return s.replace("\\", "\\\\").replace("*", r"\*").replace("_", r"\_").replace("`", r"\`").replace("|", r"\|")

def build_crest_url(team_id: str | int | None) -> str | None:
    """
    Build the crest image URL from a teamId.
    EA hosts them as .../crests/256x256/l{teamId}.png
    """
    if not team_id:
        return None
    return f"https://eafc24.content.easports.com/fifa/fltOnlineAssets/24B23FDE-7835-41C2-87A2-F453DFDB2E82/2024/fcweb/crests/256x256/l{team_id}.png"

_CREST_ACCENT_CACHE: dict[str, int] = {}


def _dominant_colour_from_bytes(image_bytes: bytes) -> int:
    """Extract a useful dominant colour while ignoring the background."""
    with Image.open(BytesIO(image_bytes)) as source:
        image = source.convert("RGBA")
        image.thumbnail((96, 96))

        pixels = [
            (r, g, b)
            for r, g, b, alpha in image.getdata()
            if alpha >= 96
            and not (r >= 245 and g >= 245 and b >= 245)
            and not (r <= 15 and g <= 15 and b <= 15)
        ]

        if not pixels:
            return 0xB30000

        palette_source = Image.new("RGB", (len(pixels), 1))
        palette_source.putdata(pixels)
        quantized = palette_source.quantize(colors=8)

        palette = quantized.getpalette() or []
        ranked_colours = []

        for count, palette_index in quantized.getcolors() or []:
            position = palette_index * 3
            if position + 2 >= len(palette):
                continue

            rgb = tuple(palette[position:position + 3])
            saturation = max(rgb) - min(rgb)

            # Slightly prefer distinctive colours over greys.
            score = count * (1 + saturation / 255)
            ranked_colours.append((score, rgb))

        if not ranked_colours:
            return 0xB30000

        _, (r, g, b) = max(ranked_colours, key=lambda item: item[0])

        # Prevent very dark accents from disappearing in Discord dark mode.
        if (r + g + b) / 3 < 45:
            r = min(255, r + 55)
            g = min(255, g + 55)
            b = min(255, b + 55)

        return (r << 16) | (g << 8) | b


async def get_crest_accent_colour(crest_asset_id: str | int | None) -> int:
    fallback = 0xB30000
    crest_url = build_crest_url(crest_asset_id) if crest_asset_id else None

    if not crest_url:
        return fallback

    if crest_url in _CREST_ACCENT_CACHE:
        return _CREST_ACCENT_CACHE[crest_url]

    try:
        response = await _client_ea.get(crest_url)
        response.raise_for_status()

        colour = await asyncio.to_thread(
            _dominant_colour_from_bytes,
            response.content,
        )
    except Exception as error:
        print(f"[CREST COLOUR] Could not analyse {crest_url}: {error}")
        colour = fallback

    _CREST_ACCENT_CACHE[crest_url] = colour
    return colour

# --- Web helpers for EA endpoints ---
async def warm_ea_session():
    try:
        print("[EA] Warming session...")
        await _ea_get_json(
            "https://proclubs.ea.com/api/fc/allTimeLeaderboard",
            {"platform": PLATFORM},
            retries=3,
        )
        await asyncio.sleep(1.5)
        print("[EA] Warm session complete.")
    except Exception as e:
        print(f"[EA] Warm session failed: {e}")

async def warm_scwiki_ship_cache():
    try:
        await get_all_ships_scwiki()
        print("[SCWIKI] ship cache warmed")
    except Exception as e:
        print(f"[SCWIKI] failed to warm cache: {e}")
        
async def get_club_stats(club_id):
    data = await _ea_get_json(
        "https://proclubs.ea.com/api/fc/clubs/overallStats",
        {"platform": PLATFORM, "clubIds": club_id},
    )
    try:
        if isinstance(data, list) and data:
            club = data[0]
            return {
                "matchesPlayed": club.get("gamesPlayed", "N/A"),
                "wins": club.get("wins", "N/A"),
                "draws": club.get("ties", "N/A"),
                "losses": club.get("losses", "N/A"),
                "winStreak": club.get("wstreak", "0"),
                "unbeatenStreak": club.get("unbeatenstreak", "0"),
                "skillRating": club.get("skillRating", "N/A"),
            }
    except Exception as e:
        print(f"Error parsing club stats: {e}")
    return {
        "matchesPlayed": "N/A", "wins": "N/A", "draws": "N/A", "losses": "N/A",
        "winStreak": "0", "unbeatenStreak": "0", "skillRating": "N/A"
    }
    
async def get_recent_form(club_id):
    match_types = ["leagueMatch", "playoffMatch", "friendlyMatch"]
    all_matches = []
    try:
        for match_type in match_types:
            data = await _ea_get_json(
                "https://proclubs.ea.com/api/fc/clubs/matches",
                {"matchType": match_type, "platform": PLATFORM, "clubIds": club_id},
            ) or []
            all_matches.extend(data)

        all_matches.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        results = []
        for match in all_matches[:5]:
            clubs_data = match.get("clubs", {}) or {}
            club_data = clubs_data.get(str(club_id))
            opponent_id = next((cid for cid in clubs_data if cid != str(club_id)), None)
            opponent_data = clubs_data.get(opponent_id) if opponent_id else None
            if not club_data or not opponent_data:
                continue
            our_score = int(club_data.get("goals", 0))
            opponent_score = int(opponent_data.get("goals", 0))
            if our_score > opponent_score:
                results.append("✅")
            elif our_score < opponent_score:
                results.append("❌")
            else:
                results.append("➖")
        return results
    except Exception as e:
        print(f"[ERROR] Failed to fetch recent form: {e}")
        return []

async def get_last5_matches_summary(club_id: str) -> str:
    """
    Returns a tidy multi-line string of the last 5 matches across
    league, playoff, friendly. Example line:
    • League — vs Onion Bag (2–1) ✅
    """
    club_id = str(club_id)
    match_types = ["leagueMatch", "playoffMatch", "friendlyMatch"]
    all_matches = []
    try:
        for mt in match_types:
            data = await _client_ea.get(
                "https://proclubs.ea.com/api/fc/clubs/matches",
                params={"matchType": mt, "platform": PLATFORM, "clubIds": club_id},
            )
            if data.status_code == 404:
                continue
            data.raise_for_status()
            arr = data.json() or []
            for m in arr:
                m["_matchType"] = mt
            all_matches.extend(arr)

        if not all_matches:
            return "No recent matches"

        # newest first
        all_matches.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        take = all_matches[:5]

        lines = []
        for m in take:
            raw_mt = m.get("_matchType") or m.get("matchType")
            label = MATCH_TYPE_LABELS.get(raw_mt, raw_mt or "Match")

            clubs = m.get("clubs", {}) or {}
            our = clubs.get(club_id) or {}
            opp_id = next((cid for cid in clubs if cid != club_id), None)
            opp = clubs.get(opp_id) or {}

            opp_name = (
                (opp.get("details") or {}).get("name")
                or opp.get("name")
                or "Unknown"
            )

            our_goals = int(our.get("goals", 0))
            opp_goals = int(opp.get("goals", 0))
            if our_goals > opp_goals:
                res = "✅"
            elif our_goals < opp_goals:
                res = "❌"
            else:
                res = "➖"

            lines.append(f"{res} {label} — vs {opp_name} ({our_goals}–{opp_goals})")

        return "\n".join(lines)

    except Exception as e:
        print(f"[ERROR] get_last5_matches_summary: {e}")
        return "No recent matches"

async def get_last_match(club_id):
    match_types = ["leagueMatch", "playoffMatch", "friendlyMatch"]
    all_matches = []
    try:
        for match_type in match_types:
            data = await _ea_get_json(
                "https://proclubs.ea.com/api/fc/clubs/matches",
                {"matchType": match_type, "platform": PLATFORM, "clubIds": club_id},
            ) or []
            for m in data:
                m["_matchType"] = match_type
            all_matches.extend(data)

        all_matches.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        if not all_matches:
            return "Last match data not available."

        match = all_matches[0]
        raw_type = match.get("_matchType") or match.get("matchType")
        label = MATCH_TYPE_LABELS.get(raw_type, raw_type or "Unknown")

        clubs_data = match.get("clubs", {}) or {}
        club_data = clubs_data.get(str(club_id))
        opponent_id = next((cid for cid in clubs_data if cid != str(club_id)), None)
        opponent_data = clubs_data.get(opponent_id) if opponent_id else None
        if not club_data or not opponent_data:
            return "Last match data not available."

        opponent_name = (
            opponent_data.get("name")
            or (opponent_data.get("details", {}) or {}).get("name")
            or (match.get("opponentClub", {}) or {}).get("name")
            or "Unknown"
        )
        our_score = int(club_data.get("goals", 0))
        opponent_score = int(opponent_data.get("goals", 0))
        result = "✅" if our_score > opponent_score else ("❌" if our_score < opponent_score else "➖")
        return f"{result} - {label} - {opponent_name} ({our_score}-{opponent_score})"
    except Exception as e:
        print(f"[ERROR] Failed to fetch last match: {e}")
        return "Last match data not available."

POSITION_ID_GROUPS = {
    "Goalkeepers": {0},
    "Defenders": {1, 2, 3, 4, 5, 6, 7, 8},
    "Midfielders": {9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19},
    "Forwards": {20, 21, 22, 23, 24, 25, 26, 27},
}

POSITION_NAME_GROUPS = {
    "GK": "Goalkeepers",
    "SW": "Defenders", "RWB": "Defenders", "RB": "Defenders",
    "RCB": "Defenders", "CB": "Defenders", "LCB": "Defenders",
    "LB": "Defenders", "LWB": "Defenders",
    "RDM": "Midfielders", "CDM": "Midfielders", "LDM": "Midfielders",
    "RM": "Midfielders", "RCM": "Midfielders", "CM": "Midfielders",
    "LCM": "Midfielders", "LM": "Midfielders", "RAM": "Midfielders",
    "CAM": "Midfielders", "LAM": "Midfielders",
    "RF": "Forwards", "CF": "Forwards", "LF": "Forwards",
    "RW": "Forwards", "RS": "Forwards", "ST": "Forwards",
    "LS": "Forwards", "LW": "Forwards",
}

def _player_position_group(player: dict) -> str:
    """Turn EA's numeric or text position into an embed group."""
    raw = player.get("position", player.get("pos", ""))
    text = str(raw).strip().upper()

    if text.lstrip("-").isdigit():
        position_id = int(text)
        for group, position_ids in POSITION_ID_GROUPS.items():
            if position_id in position_ids:
                return group

    if text in POSITION_NAME_GROUPS:
        return POSITION_NAME_GROUPS[text]
    if "KEEP" in text or "GOAL" in text:
        return "Goalkeepers"
    if "DEF" in text or "BACK" in text:
        return "Defenders"
    if "MID" in text:
        return "Midfielders"
    if "FOR" in text or "ATT" in text or "STRIK" in text or "WING" in text:
        return "Forwards"

    return "Players"

def _match_rating(player: dict) -> str:
    rating = _to_number(player.get("rating"))
    return f"{float(rating):.1f}" if rating is not None else "—"

def _percentage(made, attempted) -> int:
    made_num = int(_to_number(made) or 0)
    attempted_num = int(_to_number(attempted) or 0)
    return round((made_num / attempted_num) * 100) if attempted_num else 0

def _made_attempted(player: dict, made_key: str, attempted_key: str) -> str:
    made = int(_to_number(player.get(made_key)) or 0)
    attempted = int(_to_number(player.get(attempted_key)) or 0)
    return f"{made}/{attempted}"

def _format_last_match_player(player: dict, group: str) -> str:
    name = escape_markdown(_player_display_name(player))
    rating = _match_rating(player)
    goals = int(_to_number(player.get("goals")) or 0)
    assists = int(_to_number(player.get("assists")) or 0)
    shots = int(_to_number(player.get("shots")) or 0)

    passes_made = int(_to_number(player.get("passesmade")) or 0)
    pass_attempts = int(_to_number(player.get("passattempts")) or 0)
    pass_pct = round((passes_made / pass_attempts) * 100) if pass_attempts else 0

    tackles_made = int(_to_number(player.get("tacklesmade")) or 0)
    tackle_attempts = int(_to_number(player.get("tackleattempts")) or 0)
    tackle_pct = round((tackles_made / tackle_attempts) * 100) if tackle_attempts else 0

    yellow_cards = int(_to_number(player.get("yellowcards")) or 0)
    red_cards = int(_to_number(player.get("redcards")) or 0)

    if group == "Forwards":
        return (
            f"**{name}**\n"
            f"`G {goals} · A {assists} · Sh {shots}`\n"
            f"`P {passes_made}/{pass_attempts} · P% {pass_pct}`\n"
            f"`YC {yellow_cards} · RC {red_cards}`\n"
            f"`Rt {rating}`"
        )

    if group in ("Midfielders", "Defenders"):
        return (
            f"**{name}**\n"
            f"`G {goals} · A {assists}`\n"
            f"`P {passes_made}/{pass_attempts} · P% {pass_pct}`\n"
            f"`T {tackles_made}/{tackle_attempts} · T% {tackle_pct}`\n"
            f"`YC {yellow_cards} · RC {red_cards}`\n"
            f"`Rt {rating}`"
        )

    if group == "Goalkeepers":
        saves = int(_to_number(player.get("saves")) or 0)
        conceded = int(_to_number(player.get("goalsconceded")) or 0)
        clean_sheets = int(_to_number(player.get("cleansheetsgk")) or 0)
        return (
            f"**{name}**\n"
            f"`Sv {saves} · Con {conceded} · CS {clean_sheets}`\n"
            f"`YC {yellow_cards} · RC {red_cards}`\n"
            f"`Rt {rating}`"
        )

    return (
        f"**{name}**\n"
        f"`G {goals} · A {assists} · Sh {shots}`\n"
        f"`P {passes_made}/{pass_attempts} · P% {pass_pct}`\n"
        f"`YC {yellow_cards} · RC {red_cards}`\n"
        f"`Rt {rating}`"
    )


def _format_last_match_team_totals(
    players: list[dict],
    our_score: int,
    opponent_score: int,
) -> str:
    assists = 0
    shots = 0
    passes_made = 0
    pass_attempts = 0
    tackles_made = 0
    tackle_attempts = 0
    saves = 0
    yellow_cards = 0
    red_cards = 0
    rating_total = 0.0
    rating_count = 0

    for player in players:
        assists += int(_to_number(player.get("assists")) or 0)
        shots += int(_to_number(player.get("shots")) or 0)
        passes_made += int(_to_number(player.get("passesmade")) or 0)
        pass_attempts += int(_to_number(player.get("passattempts")) or 0)
        tackles_made += int(_to_number(player.get("tacklesmade")) or 0)
        tackle_attempts += int(_to_number(player.get("tackleattempts")) or 0)
        saves += int(_to_number(player.get("saves")) or 0)
        yellow_cards += int(_to_number(player.get("yellowcards")) or 0)
        red_cards += int(_to_number(player.get("redcards")) or 0)

        rating = _to_number(player.get("rating"))
        if rating is not None:
            rating_total += float(rating)
            rating_count += 1

    pass_pct = round((passes_made / pass_attempts) * 100) if pass_attempts else 0
    tackle_pct = round((tackles_made / tackle_attempts) * 100) if tackle_attempts else 0
    average_rating = rating_total / rating_count if rating_count else 0.0
    clean_sheet = 1 if opponent_score == 0 else 0

    return (
        f"`GF {our_score} · GA {opponent_score} · CS {clean_sheet}`\n"
        f"`A {assists} · Sh {shots} · Sv {saves}`\n"
        f"`P {passes_made}/{pass_attempts} · P% {pass_pct}`\n"
        f"`T {tackles_made}/{tackle_attempts} · T% {tackle_pct}`\n"
        f"`YC {yellow_cards} · RC {red_cards}`\n"
        f"`Rt {average_rating:.1f}`"
    )

async def get_last_match_details(club_id: str | int) -> dict | None:
    """Return the newest match plus position-aware player lines."""
    club_id = str(club_id)
    all_matches = []

    try:
        for match_type in ("leagueMatch", "playoffMatch", "friendlyMatch"):
            matches = await _ea_get_json(
                "https://proclubs.ea.com/api/fc/clubs/matches",
                {"matchType": match_type, "platform": PLATFORM, "clubIds": club_id},
            ) or []
            for match in matches:
                match["_matchType"] = match_type
            all_matches.extend(matches)

        if not all_matches:
            return None

        all_matches.sort(key=lambda match: match.get("timestamp", 0), reverse=True)

        last5_lines = []
        for recent_match in all_matches[:5]:
            recent_clubs = recent_match.get("clubs") or {}
            recent_ours = recent_clubs.get(club_id) or {}
            recent_opponent_id = next(
                (cid for cid in recent_clubs if str(cid) != club_id),
                None,
            )
            recent_opponent = recent_clubs.get(recent_opponent_id) or {}
            recent_our_score = int(recent_ours.get("goals", 0) or 0)
            recent_opponent_score = int(recent_opponent.get("goals", 0) or 0)
            recent_result = (
                "✅" if recent_our_score > recent_opponent_score
                else "❌" if recent_our_score < recent_opponent_score
                else "➖"
            )
            recent_raw_type = recent_match.get("_matchType") or recent_match.get("matchType")
            recent_type = MATCH_TYPE_LABELS.get(
                recent_raw_type,
                recent_raw_type or "Match",
            )
            recent_opponent_name = (
                (recent_opponent.get("details") or {}).get("name")
                or recent_opponent.get("name")
                or "Unknown"
            )
            last5_lines.append(
                f"{recent_result} {recent_type} — vs "
                f"{escape_markdown(recent_opponent_name)} "
                f"({recent_our_score}–{recent_opponent_score})"
            )

        match = all_matches[0]
        clubs = match.get("clubs") or {}
        our_club = clubs.get(club_id) or {}
        opponent_id = next((cid for cid in clubs if str(cid) != club_id), None)
        opponent = clubs.get(opponent_id) or {}

        our_score = int(our_club.get("goals", 0) or 0)
        opponent_score = int(opponent.get("goals", 0) or 0)
        if our_score > opponent_score:
            result_text, result_emoji = "WIN", "🟢"
        elif our_score < opponent_score:
            result_text, result_emoji = "LOSS", "🔴"
        else:
            result_text, result_emoji = "DRAW", "🟡"

        raw_type = match.get("_matchType") or match.get("matchType")
        match_type = MATCH_TYPE_LABELS.get(raw_type, raw_type or "Match")
        opponent_name = (
            (opponent.get("details") or {}).get("name")
            or opponent.get("name")
            or "Unknown"
        )

        grouped_players = {
            "Forwards": [],
            "Midfielders": [],
            "Defenders": [],
            "Goalkeepers": [],
            "Players": [],
        }
        players = ((match.get("players") or {}).get(club_id) or {}).values()
        for player in players:
            if not isinstance(player, dict):
                continue
            group = _player_position_group(player)
            grouped_players[group].append(player)

        for group_players in grouped_players.values():
            group_players.sort(
                key=lambda player: float(_to_number(player.get("rating")) or 0),
                reverse=True,
            )

        return {
            "last5": "\n".join(last5_lines) or "No recent matches",
            "summary": (
                f"{result_emoji} **{result_text}** · {match_type}\n"
                f"vs **{escape_markdown(opponent_name)}** · **{our_score}–{opponent_score}**"
            ),
            "players": {
                group: [_format_last_match_player(player, group) for player in group_players]
                for group, group_players in grouped_players.items()
                if group_players
            },
        }
    except Exception as e:
        print(f"[ERROR] Failed to build last-match details: {e}")
        return None

async def get_club_rank(club_id: str | int):
    club_id = str(club_id)

    try:
        resp = await _client_ea.get(
            "https://proclubs.ea.com/api/fc/allTimeLeaderboard/club",
            params={"platform": PLATFORM, "clubIds": club_id},
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                raw = data.get("raw") or []
                if raw and isinstance(raw, list):
                    rank = raw[0].get("rank")
                    if rank is not None:
                        return rank  # int or str like "42"
        elif resp.status_code != 404:
            # non-404 error; log and continue to fallback
            print(f"[RANK] club endpoint {resp.status_code}: {resp.text[:160]}")
    except Exception as e:
        print(f"[RANK] exception (club endpoint): {e}")

    try:
        resp2 = await _client_ea.get(
            "https://proclubs.ea.com/api/fc/allTimeLeaderboard",
            params={"platform": PLATFORM},
        )
        if resp2.status_code == 200:
            data2 = resp2.json()
            if isinstance(data2, list):
                for entry in data2:
                    if str(entry.get("clubId")) == club_id:
                        return entry.get("rank", "Unranked")
            else:
                print(f"[RANK] unexpected list payload: {type(data2)}")
        else:
            print(f"[RANK] list endpoint {resp2.status_code}: {resp2.text[:160]}")
    except Exception as e:
        print(f"[RANK] exception (list fallback): {e}")

    return "Unranked"

async def get_days_since_last_match(club_id):
    match_types = ["leagueMatch", "playoffMatch", "friendlyMatch"]
    all_matches = []
    try:
        for match_type in match_types:
            data = await _ea_get_json(
                "https://proclubs.ea.com/api/fc/clubs/matches",
                {"matchType": match_type, "platform": PLATFORM, "clubIds": club_id},
            ) or []
            all_matches.extend(data)

        all_matches.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        if not all_matches:
            return None

        last_timestamp = all_matches[0].get("timestamp", 0)
        last_datetime = datetime.fromtimestamp(last_timestamp, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - last_datetime).days
    except Exception as e:
        print(f"[ERROR] Failed to calculate days since last match: {e}")
        return None

async def get_squad_names(club_id):
    url = f"https://proclubs.ea.com/api/fc/club/members?platform={PLATFORM}&clubId={club_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(timeout=10) as client_http:
            response = await client_http.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                members = data.get("members", [])
                names = [member.get("playername") for member in members if member.get("playername")]
                return names
    except Exception as e:
        print(f"[ERROR] Failed to fetch squad names: {e}")
    return []

async def fetch_all_stats_for_club(club_id: str):
    club_id = str(club_id)
    stats_task = asyncio.create_task(get_club_stats(club_id))
    form_task = asyncio.create_task(get_recent_form(club_id))
    days_task = asyncio.create_task(get_days_since_last_match(club_id))
    rank_task = asyncio.create_task(get_club_rank(club_id))
    last_match_task = asyncio.create_task(get_last_match_details(club_id))
    crestid_task = asyncio.create_task(get_crest_asset_id_for_club(club_id))
    squad_task = asyncio.create_task(get_current_squad(club_id))

    stats = await stats_task
    recent_form = await form_task
    days_since = await days_task
    rank = await rank_task
    last_match = await last_match_task
    last5 = (
        last_match.get("last5", "No recent matches")
        if last_match
        else "No recent matches"
    )
    crest_asset_id = await crestid_task
    current_squad = await squad_task
    accent_color = await get_crest_accent_colour(crest_asset_id)

    rank_display = f"#{rank}" if (isinstance(rank, int) or (isinstance(rank, str) and str(rank).isdigit())) else "Unranked"
    days_display = f"{days_since} day(s) ago" if days_since is not None else "—"
    form_string = " ".join(recent_form) if recent_form else "No recent matches"

    return {
        "stats": stats or {},
        "rank_display": rank_display,
        "recent_form": form_string,
        "last5": last5 or "No recent matches",
        "last_match": last_match,
        "days_display": days_display,
        "crestAssetId": crest_asset_id,
        "current_squad": current_squad,
        "accent_color": accent_color,
    }

STAT_LABELS = {
    "appearances": "Apps",
    "goals": "Goals",
    "assists": "Assists",
    "shots": "Shots",
    "shotson": "Shots On",
    "passesmade": "Passes",
    "passesintercepted": "Int",
    "passattempts": "Pass Att",
    "dribblesmade": "Dribbles",
    "tacklesmade": "Tackles",
    "tacklesuccessful": "Tackle Won",
    "blocks": "Blocks",
    "interceptions": "Interceptions",
    "fouls": "Fouls",
    "foulssuffered": "Won Fouls",
    "yellowcards": "YC",
    "redcards": "RC",
    "saves": "Saves",
    "goalsconceded": "Conceded",
    "cleansheets": "CS",
    "rating": "Rating Total",
    "motm": "POTM",
    "possession": "Poss",
    "corners": "Corners",
    "offsides": "Offsides",
}

NON_STAT_KEYS = {
    "playername", "name", "avatar", "kitno", "kitnumber", "position",
    "pos", "isCaptain", "captain", "slot", "userId", "proName"
}

def _to_number(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    try:
        s = str(value).strip()
        if not s:
            return None
        if "." in s:
            return float(s)
        return int(s)
    except Exception:
        return None

def _pretty_stat_name(key: str) -> str:
    return STAT_LABELS.get(key, key.replace("_", " ").title())

def _player_display_name(player: dict) -> str:
    return (
        player.get("playername")
        or player.get("name")
        or player.get("proName")
        or "Unknown"
    )

def _build_stats5_leaders_text(totals: dict) -> str:
    if not totals:
        return "No player data."

    def avg_rating(stats: dict) -> float:
        apps = int(stats.get("appearances", 0) or 0)
        rating_total = float(stats.get("rating", 0) or 0)
        return round(rating_total / apps, 1) if apps else 0.0

    def join_names(names: list[str]) -> str:
        escaped = [f"**{escape_markdown(name)}**" for name in names]
        if len(escaped) == 1:
            return escaped[0]
        if len(escaped) == 2:
            return f"{escaped[0]} and {escaped[1]}"
        return ", ".join(escaped[:-1]) + f", and {escaped[-1]}"

    best_goals = max(int(stats.get("goals", 0) or 0) for stats in totals.values())
    top_scorers = sorted(
        name for name, stats in totals.items()
        if int(stats.get("goals", 0) or 0) == best_goals
    )

    best_assists = max(int(stats.get("assists", 0) or 0) for stats in totals.values())
    top_assisters = sorted(
        name for name, stats in totals.items()
        if int(stats.get("assists", 0) or 0) == best_assists
    )

    best_rating = max(avg_rating(stats) for stats in totals.values())
    best_rated_players = sorted(
        name for name, stats in totals.items()
        if avg_rating(stats) == best_rating
    )

    def best_percentage_players(
        made_key: str,
        attempted_key: str,
        minimum_attempts: int,
        minimum_appearances: int = 3,
    ) -> tuple[list[str], int, int, int] | None:
        candidates = []
        for name, stats in totals.items():
            appearances = int(stats.get("appearances", 0) or 0)
            made = int(stats.get(made_key, 0) or 0)
            attempted = int(stats.get(attempted_key, 0) or 0)
            if (
                appearances < minimum_appearances
                or attempted < minimum_attempts
            ):
                continue
            percentage = round((made / attempted) * 100)
            candidates.append((name, made, attempted, percentage))

        if not candidates:
            return None

        best_percentage = max(item[3] for item in candidates)
        best_attempts = max(
            item[2]
            for item in candidates
            if item[3] == best_percentage
        )
        winners = [
            item
            for item in candidates
            if item[3] == best_percentage
            and item[2] == best_attempts
        ]
        names = sorted(item[0] for item in winners)
        _, made, attempted, percentage = winners[0]
        return names, made, attempted, percentage

    best_passer = best_percentage_players(
        "passesmade", "passattempts", minimum_attempts=50
    )
    best_tackler = best_percentage_players(
        "tacklesmade", "tackleattempts", minimum_attempts=10
    )

    extra_awards = []
    if best_passer:
        names, made, attempted, percentage = best_passer
        extra_awards.append(
            f"🎯 Best passer: {join_names(names)} "
            f"({percentage}% · {made}/{attempted})"
        )
    if best_tackler:
        names, made, attempted, percentage = best_tackler
        extra_awards.append(
            f"🛡️ Best tackler: {join_names(names)} "
            f"({percentage}% · {made}/{attempted})"
        )

    leaders = (
        f"⚽ Top scorer: {join_names(top_scorers)} ({best_goals})\n"
        f"🅰️ Top assister: {join_names(top_assisters)} ({best_assists})\n"
        f"⭐ Best avg rating: {join_names(best_rated_players)} ({best_rating:.1f})"
    )
    if extra_awards:
        leaders += "\n" + "\n".join(extra_awards)
    return leaders

def _sort_players_for_stats5(item: tuple[str, dict]):
    _, stats = item
    return (
        -int(stats.get("appearances", 0)),
        -float(stats.get("goals", 0)),
        -float(stats.get("assists", 0)),
        -float(stats.get("rating", 0)),
        item[0].lower(),
    )

async def get_last5_player_totals(club_id: str):
    """
    Aggregate all numeric player stats from the club's last 5 matches
    across league/playoff/friendly.
    """
    club_id = str(club_id)
    match_types = ["leagueMatch", "playoffMatch", "friendlyMatch"]
    all_matches = []

    for match_type in match_types:
        data = await _ea_get_json(
            "https://proclubs.ea.com/api/fc/clubs/matches",
            {"matchType": match_type, "platform": PLATFORM, "clubIds": club_id},
        ) or []
        for m in data:
            m["_matchType"] = match_type
        all_matches.extend(data)

    all_matches.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    last_5 = all_matches[:5]

    if not last_5:
        return {
            "matches": [],
            "totals": {},
            "stat_keys": [],
        }

    totals: dict[str, dict] = {}

    for match in last_5:
        club_players = ((match.get("players") or {}).get(club_id) or {})

        for _, player in club_players.items():
            if not isinstance(player, dict):
                continue

            name = _player_display_name(player)
            if name not in totals:
                totals[name] = {
                    "appearances": 0,
                    "_position_counts": {},
                }

            totals[name]["appearances"] += 1
            position_group = _player_position_group(player)
            position_counts = totals[name]["_position_counts"]
            position_counts[position_group] = (
                position_counts.get(position_group, 0) + 1
            )

            for key, value in player.items():
                if key in NON_STAT_KEYS:
                    continue

                num = _to_number(value)
                if num is None:
                    continue

                totals[name][key] = totals[name].get(key, 0) + num

    # collect every stat key that appeared for at least one player
    stat_keys = set()
    for player_stats in totals.values():
        stat_keys.update(
            key
            for key in player_stats.keys()
            if not key.startswith("_")
        )

    # keep appearances first, then common football stats, then everything else
    preferred_order = [
        "appearances", "goals", "assists", "rating", "shots", "shotson",
        "passesmade", "passattempts", "dribblesmade", "tacklesmade",
        "interceptions", "blocks", "saves", "goalsconceded",
        "yellowcards", "redcards", "fouls", "foulssuffered",
        "cleansheets", "motm"
    ]

    ordered_stat_keys = [k for k in preferred_order if k in stat_keys]
    ordered_stat_keys += sorted(k for k in stat_keys if k not in ordered_stat_keys)

    return {
        "matches": last_5,
        "totals": dict(sorted(totals.items(), key=_sort_players_for_stats5)),
        "stat_keys": ordered_stat_keys,
    }

STATS5_PRIORITY_ORDER = [
    "appearances",
    "goals",
    "assists",
    "rating",
    "shots",
    "shotson",
    "passattempts",
    "passesmade",
    "dribblesmade",
    "tacklesmade",
    "tacklesuccessful",
    "interceptions",
    "blocks",
    "saves",
    "goalsconceded",
    "cleansheets",
    "yellowcards",
    "redcards",
    "fouls",
    "foulssuffered",
    "motm",
]

def _format_stat_value(key: str, val):
    if isinstance(val, float):
        if key == "rating":
            return f"{val:.1f}"
        if val.is_integer():
            return str(int(val))
        return f"{val:.2f}"
    return str(val)

STATS5_HIDE_KEYS = {
    "archetypeid",
    "balllivesaves",
    "cleansheetsany",
    "cleansheetsdef",
    "cleansheetsgk",
    "gooddirectionsaves",
    "namespace",
    "parrysaves",
    "punchsaves",
    "realtimegame",
    "realtimeidle",
    "reflexsaves",
    "score",
    "secondsplayed",
    "secondplayed",
    "tackleattempts",
    "userresult",
    "vprohackreason",
    "wins",
    "crosssaves",
    "mom",
}

STATS5_COMPACT_LABELS = {
    "appearances": "Apps",
    "goals": "Goals",
    "assists": "Ast",
    "rating": "Rating",
    "shots": "Shots",
    "shotson": "OnTgt",
    "passattempts": "PassAtt",
    "passesmade": "Passes",
    "dribblesmade": "Dribbles",
    "tacklesmade": "Tackles",
    "tacklesuccessful": "TklWon",
    "interceptions": "Int",
    "blocks": "Blocks",
    "saves": "Saves",
    "goalsconceded": "Conceded",
    "cleansheets": "CS",
    "yellowcards": "YC",
    "redcards": "RC",
    "fouls": "Fouls",
    "foulssuffered": "WonFld",
    "motm": "POTM",
    "possession": "Poss",
    "corners": "Corners",
    "offsides": "Offside",
}

def _format_stat_value(key: str, val):
    if isinstance(val, float):
        if key == "rating":
            return f"{val:.1f}"
        if val.is_integer():
            return str(int(val))
        return f"{val:.2f}"
    return str(val)

def _format_stats5_team_totals(
    totals: dict,
    matches: list[dict],
    club_id: str | int,
) -> str:
    club_id = str(club_id)
    matches_played = 0
    wins = 0
    draws = 0
    losses = 0
    goals_for = 0
    goals_against = 0
    clean_sheets = 0

    assists = 0
    shots = 0
    pass_attempts = 0
    pass_completed = 0
    tackle_attempts = 0
    tackles_won = 0
    yc = 0
    rc = 0
    saves = 0
    rating_sum = 0.0
    rating_count = 0

    # Match-level figures must come from the club score in each match.
    # Player goals-conceded values are repeated for multiple players and
    # cannot safely be added together.
    for match in matches:
        clubs = match.get("clubs") or {}
        our_id = next(
            (candidate_id for candidate_id in clubs if str(candidate_id) == club_id),
            None,
        )
        our_club = clubs.get(our_id) if our_id is not None else None
        opponent_id = next(
            (
                candidate_id
                for candidate_id in clubs
                if str(candidate_id) != club_id
            ),
            None,
        )
        opponent = clubs.get(opponent_id) if opponent_id else None

        if not our_club or not opponent:
            continue

        our_score = int(our_club.get("goals", 0) or 0)
        opponent_score = int(opponent.get("goals", 0) or 0)

        matches_played += 1
        goals_for += our_score
        goals_against += opponent_score

        if our_score > opponent_score:
            wins += 1
        elif our_score < opponent_score:
            losses += 1
        else:
            draws += 1

        if opponent_score == 0:
            clean_sheets += 1

    for _, stats in totals.items():
        assists += int(stats.get("assists", 0) or 0)
        shots += int(stats.get("shots", 0) or 0)

        pass_attempts += int(stats.get("passattempts", 0) or 0)
        pass_completed += int(stats.get("passesmade", 0) or 0)

        tackle_attempts += int(stats.get("tackleattempts", 0) or 0)
        tackles_won += int(stats.get("tacklesmade", 0) or 0)

        yc += int(stats.get("yellowcards", 0) or 0)
        rc += int(stats.get("redcards", 0) or 0)
        saves += int(stats.get("saves", 0) or 0)

        player_apps = int(stats.get("appearances", 0) or 0)
        player_rating_total = float(stats.get("rating", 0) or 0)
        if player_apps > 0:
            rating_sum += player_rating_total
            rating_count += player_apps

    pass_pct = round((pass_completed / pass_attempts) * 100) if pass_attempts else 0
    tackle_pct = round((tackles_won / tackle_attempts) * 100) if tackle_attempts else 0
    avg_rating = round(rating_sum / rating_count, 1) if rating_count else 0.0

    return (
        f"`Pl {matches_played} · W {wins} · D {draws} · L {losses}`\n"
        f"`GF {goals_for} · GA {goals_against} · CS {clean_sheets}`\n"
        f"`A {assists} · Sh {shots} · Sv {saves}`\n"
        f"`P {pass_completed}/{pass_attempts} · P% {pass_pct}`\n"
        f"`T {tackles_won}/{tackle_attempts} · T% {tackle_pct}`\n"
        f"`YC {yc} · RC {rc}`\n"
        f"`Rt {avg_rating:.1f}`"
    )


def _stats5_position_group(stats: dict) -> str:
    counts = stats.get("_position_counts") or {}
    if not counts:
        return "Players"

    # Dict order follows the newest matches first, so ties favour the
    # player's most recently recorded position group.
    return max(counts, key=counts.get)


def _format_player_stats_row(player_name: str, stats: dict):
    apps = int(stats.get("appearances", 0))
    goals = int(stats.get("goals", 0))
    assists = int(stats.get("assists", 0))
    shots = int(stats.get("shots", 0))

    pass_attempts = int(stats.get("passattempts", 0) or 0)
    pass_completed = int(stats.get("passesmade", 0) or 0)
    pass_pct = round((pass_completed / pass_attempts) * 100) if pass_attempts else 0

    tackle_attempts = int(stats.get("tackleattempts", 0) or 0)
    tackles_won = int(stats.get("tacklesmade", 0) or 0)
    tackle_pct = round((tackles_won / tackle_attempts) * 100) if tackle_attempts else 0

    yc = int(stats.get("yellowcards", 0) or 0)
    rc = int(stats.get("redcards", 0) or 0)

    rating_total = float(stats.get("rating", 0) or 0)
    rating = round(rating_total / apps, 1) if apps else 0

    name = escape_markdown(player_name)
    group = _stats5_position_group(stats)

    group_icons = {
        "Forwards": "⚽",
        "Midfielders": "🎯",
        "Defenders": "🛡️",
        "Goalkeepers": "🧤",
        "Players": "👤",
    }
    icon = group_icons.get(group, "👤")

    if group == "Goalkeepers":
        saves = int(stats.get("saves", 0) or 0)
        conceded = int(stats.get("goalsconceded", 0) or 0)
        clean_sheets = int(stats.get("cleansheetsgk", 0) or 0)
        return (
            f"**{icon} {name}**\n"
            f"`Pl {apps} · Sv {saves} · Con {conceded} · CS {clean_sheets}`\n"
            f"`YC {yc} · RC {rc}`\n"
            f"`Rt {rating:.1f}`"
        )

    if group in ("Midfielders", "Defenders"):
        return (
            f"**{icon} {name}**\n"
            f"`Pl {apps} · G {goals} · A {assists}`\n"
            f"`P {pass_completed}/{pass_attempts} · P% {pass_pct}`\n"
            f"`T {tackles_won}/{tackle_attempts} · T% {tackle_pct}`\n"
            f"`YC {yc} · RC {rc}`\n"
            f"`Rt {rating:.1f}`"
        )

    return (
        f"**{icon} {name}**\n"
        f"`Pl {apps} · G {goals} · A {assists} · Sh {shots}`\n"
        f"`P {pass_completed}/{pass_attempts} · P% {pass_pct}`\n"
        f"`YC {yc} · RC {rc}`\n"
        f"`Rt {rating:.1f}`"
    )

async def build_stats5_embeds(club_id: str, club_name: str | None):
    club_name = club_name or f"Club {club_id}"
    data = await get_last5_player_totals(club_id)

    matches = data["matches"]
    totals = data["totals"]

    if not matches:
        return []

    if not totals:
        return []

    crest_asset_id = await get_crest_asset_id_for_club(club_id)
    crest_url = build_crest_url(crest_asset_id) if crest_asset_id else None

    base_title = f"📊 {club_name.upper()} — LAST 5 PLAYER TOTALS"
    subtitle = f"Across League, Playoff and Friendly matches ({len(matches)} matches)"

    player_items = sorted(
        totals.items(),
        key=lambda item: (
            {
                "Forwards": 0,
                "Midfielders": 1,
                "Defenders": 2,
                "Goalkeepers": 3,
                "Players": 4,
            }.get(_stats5_position_group(item[1]), 4),
            -((float(item[1].get("rating", 0) or 0) / int(item[1].get("appearances", 1) or 1))
              if int(item[1].get("appearances", 0) or 0) > 0 else 0),
            -int(item[1].get("goals", 0) or 0),
            -int(item[1].get("assists", 0) or 0),
            item[0].lower()
        )
    )

    rows = [_format_player_stats_row(player_name, player_stats) for player_name, player_stats in player_items]
    if not rows:
        return []

    team_totals_row = _format_stats5_team_totals(totals, matches, club_id)
    leaders_text = _build_stats5_leaders_text(totals)

    player_chunks = []
    current_rows = []

    for row in rows:
        # Discord allows up to 1,024 characters in an embed field value.
        # Measure the finished value exactly so we do not create a
        # continuation field earlier than necessary.
        candidate_rows = [*current_rows, row]
        candidate_value = "\n\n".join(candidate_rows)

        if current_rows and len(candidate_value) > 1024:
            player_chunks.append(current_rows)
            current_rows = []

        current_rows.append(row)

    if current_rows:
        player_chunks.append(current_rows)

    if not player_chunks:
        return []

    embed = discord.Embed(
        title=base_title,
        description=f"{subtitle}\n\n{leaders_text}",
        color=0xB30000,
    )

    if crest_url:
        embed.set_thumbnail(url=crest_url)

    for index, chunk_rows in enumerate(player_chunks):
        embed.add_field(
            name=(
                "Player Totals"
                if index == 0
                else "Player Totals — continued"
            ),
            value="\n\n".join(chunk_rows),
            inline=False,
        )

    embed.add_field(
        name="Team Totals",
        value=team_totals_row,
        inline=False,
    )
    embed.set_footer(
        text=f"EAFC — Aggregated from the most recent {len(matches)} matches"
    )

    return [embed]

# Helpers + embed builder for /stats
ZWSP = "\u200b"

def _field(name: str, value: str, inline: bool = True) -> dict:
    return {"name": name, "value": value if value else "—", "inline": inline}

def _spacer(inline: bool = True) -> dict:
    return {"name": ZWSP, "value": ZWSP, "inline": inline}

def _format_squad_table(names: list[str]) -> str:
    """Inline-code name chips wrap cleanly at any Discord client width."""
    return " ".join(
        f"`{str(name).replace('`', "'")}`"
        for name in names
    )

def build_stats_embed(club_id: str, club_name: str | None, data: dict) -> discord.Embed:
    """
    Layout:
      Rank | Skill
      Matches Played (full width)
      W-D-L (full width, single line)
      Win Streak | Unbeaten Streak
      Last 5 Matches (full width)
      Recent Form (full width)
      Days Since Last | Club ID
    """
    title_name = (club_name or f"Club {club_id}").upper()
    s = data.get("stats", {})

    mp = s.get("matchesPlayed", "N/A")
    wins = s.get("wins", "N/A")
    draws = s.get("draws", "N/A")
    losses = s.get("losses", "N/A")
    sr = s.get("skillRating", "N/A")
    wstreak = s.get("winStreak", "0")
    ubstreak = s.get("unbeatenStreak", "0")

    rank_display = data.get("rank_display", "Unranked")
    days_display = data.get("days_display", "—")
    recent_form = data.get("recent_form", "No recent matches")
    last5 = data.get("last5", "No recent matches")

    # Use same color as your other embeds (red)
    embed = discord.Embed(
        title=f"{title_name}",
        description=None,
        color=data.get("accent_color", 0xB30000)
    )

    # ✅ Crest thumbnail
    crest_asset_id = data.get("crestAssetId")
    crest_url = build_crest_url(crest_asset_id) if crest_asset_id else None
    if crest_url:
        embed.set_thumbnail(url=crest_url)

    fields: list[dict] = []

    # Row 1 — two columns (+spacer for grid)
    fields += [
        _field("Leaderboard Rank", f"📈 {rank_display}", inline=True),
        _field("Skill Rating", f"🏅 {sr}", inline=True),
        _spacer(True),
    ]

    # Row 2 — two-column record summary
    fields += [
        _field("Matches Played", f"📊 {mp}", inline=True),
        _field("W-D-L", f"{wins} - {draws} - {losses}", inline=True),
        _spacer(True),
    ]

    # Row 4 — two columns
    fields += [
        _field("Win Streak", f"🔥 {wstreak}", inline=True),
        _field("Unbeaten Streak", f"🛡️ {ubstreak}", inline=True),
        _spacer(True),
    ]

    # Latest match summary and position-aware player statistics.
    last_match = data.get("last_match")
    if last_match:
        fields.append(_field("📅 LATEST MATCH", last_match["summary"], inline=False))

        group_titles = {
            "Forwards": "⚽ FORWARDS",
            "Midfielders": "🎯 MIDFIELDERS",
            "Defenders": "🛡️ DEFENDERS",
            "Goalkeepers": "🧤 GOALKEEPERS",
            "Players": "👤 OTHER PLAYERS",
        }
        for group in ("Forwards", "Midfielders", "Defenders", "Goalkeepers", "Players"):
            player_lines = last_match["players"].get(group)
            if player_lines:
                player_text = "\n\n".join(player_lines)
                fields.append(
                    _field(group_titles[group], player_text, inline=False)
                )

    # Recent results follow the detailed latest-match section.
    fields.append(_field("📋 RECENT RESULTS — LAST 5", last5, inline=False))

    # Row 6 — Current Squad (full width)
    squad_list = data.get("current_squad", []) or []
    if squad_list:
        squad_text = _format_squad_table(squad_list)
    else:
        squad_text = "—"
    
    fields.append(_field("👥 CURRENT SQUAD", squad_text, inline=False))

    # Row 6 — two columns
    fields += [
        _field("Last Active", f"🗓️ {days_display}", inline=True),
        _field("Club ID", f"`{club_id}`", inline=True),
        _spacer(True),
    ]

    for f in fields:
        embed.add_field(**f)

    embed.set_footer(text="EAFC — Pro Clubs Stats")
    return embed

def format_columns(names: list[str], cols: int = 2) -> str:
    """
    Return wrapping inline-code chips that work at any Discord client width.
    ``cols`` is retained for compatibility with older callers.
    """
    if not names:
        return "—"
    return " ".join(
        f"`{str(name).replace('`', "'")}`"
        for name in names
    )

def _leaderboard_rank_value(club: dict) -> int:
    """Return a sortable numeric rank, putting invalid ranks last."""
    try:
        return int(str(club.get("rank", "")).replace(",", "").strip())
    except (TypeError, ValueError):
        return 999999


async def get_top_ten_presence_clubs() -> list[tuple[int, str]]:
    """Fetch the current EA all-time leaderboard top ten."""
    data = await _ea_get_json(
        "https://proclubs.ea.com/api/fc/allTimeLeaderboard",
        {"platform": PLATFORM},
    )

    if not isinstance(data, list):
        raise ValueError(f"Unexpected leaderboard payload: {type(data).__name__}")

    clubs: list[tuple[int, str]] = []
    ordered = sorted(data, key=_leaderboard_rank_value)

    for fallback_rank, club in enumerate(ordered, start=1):
        if not isinstance(club, dict):
            continue

        name = (
            club.get("name")
            or (club.get("clubInfo") or {}).get("name")
            or ""
        )
        name = " ".join(str(name).split()).strip()
        if not name:
            continue

        rank = _leaderboard_rank_value(club)
        if rank == 999999:
            rank = fallback_rank

        clubs.append((rank, name))
        if len(clubs) == 10:
            break

    if not clubs:
        raise ValueError("EA leaderboard did not contain any named clubs")

    return clubs


async def rotate_presence():
    """Cycle through EA leaderboard positions #1 to #10."""
    await client.wait_until_ready()

    try:
        rotate_seconds = max(
            30,
            int(os.getenv("LEADERBOARD_PRESENCE_SECONDS", "60")),
        )
    except ValueError:
        rotate_seconds = 60

    cached_clubs: list[tuple[int, str]] = []

    while not client.is_closed():
        try:
            latest_clubs = await get_top_ten_presence_clubs()
            cached_clubs = latest_clubs
            print(
                f"[PRESENCE] Loaded EA leaderboard top {len(cached_clubs)}; "
                f"rotating every {rotate_seconds}s."
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"[PRESENCE] Could not refresh EA leaderboard: {error}")

        if not cached_clubs:
            try:
                await client.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.watching,
                        name="EA FC Club Leaderboard",
                    )
                )
            except Exception as error:
                print(f"[PRESENCE] Could not set fallback activity: {error}")

            await asyncio.sleep(rotate_seconds)
            continue

        for rank, club_name in cached_clubs:
            if client.is_closed():
                return

            try:
                await client.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.watching,
                        name=f"EA Top 10 | #{rank} {club_name}",
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"[PRESENCE] Could not show #{rank} {club_name}: {error}")

            await asyncio.sleep(rotate_seconds)


# =========================================================
# EA FC TOP 100 LEADERBOARD CHANNEL
# =========================================================

EA_TOP100_CHANNEL_ID = int(
    os.getenv("EA_TOP100_CHANNEL_ID", "1553322041124585573")
)
EA_TOP100_STATE_FILE = os.getenv(
    "EA_TOP100_STATE_FILE",
    "ea_top100_messages.json",
)
EA_TOP100_LAST_PLAYED_FILE = os.getenv(
    "EA_TOP100_LAST_PLAYED_FILE",
    "ea_top100_last_played.json",
)
EA_TOP100_CLUBS_PER_EMBED = 5

try:
    EA_TOP100_LAST_PLAYED_CACHE_HOURS = max(
        1,
        int(os.getenv("EA_TOP100_LAST_PLAYED_CACHE_HOURS", "6")),
    )
except ValueError:
    EA_TOP100_LAST_PLAYED_CACHE_HOURS = 6

try:
    EA_TOP100_UPDATE_MINUTES = max(
        10,
        int(os.getenv("EA_TOP100_UPDATE_MINUTES", "30")),
    )
except ValueError:
    EA_TOP100_UPDATE_MINUTES = 30

_ea_top100_refresh_lock = asyncio.Lock()
_ea_top100_last_played_lock = asyncio.Lock()
_ea_top100_clubs_cache: list[dict] = []


def _load_ea_top100_last_played_cache() -> dict:
    try:
        with open(
            EA_TOP100_LAST_PLAYED_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            cache = json.load(file)

        return cache if isinstance(cache, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as error:
        print(f"[TOP 100] Could not load last-played cache: {error}")
        return {}


def _save_ea_top100_last_played_cache(cache: dict) -> None:
    temporary_file = f"{EA_TOP100_LAST_PLAYED_FILE}.tmp"

    try:
        with open(temporary_file, "w", encoding="utf-8") as file:
            json.dump(cache, file, indent=2, ensure_ascii=False)

        os.replace(temporary_file, EA_TOP100_LAST_PLAYED_FILE)
    except Exception as error:
        print(f"[TOP 100] Could not save last-played cache: {error}")


def _load_ea_top100_state() -> dict:
    """Load the IDs of the persistent leaderboard messages."""
    try:
        with open(EA_TOP100_STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)

        if not isinstance(state, dict):
            raise ValueError("state is not a JSON object")

        state.setdefault("channel_id", EA_TOP100_CHANNEL_ID)
        state.setdefault("pages", {})
        return state
    except FileNotFoundError:
        return {
            "channel_id": EA_TOP100_CHANNEL_ID,
            "pages": {},
        }
    except Exception as error:
        print(f"[TOP 100] Could not load message state: {error}")
        return {
            "channel_id": EA_TOP100_CHANNEL_ID,
            "pages": {},
        }


def _save_ea_top100_state(state: dict) -> None:
    """Atomically save the persistent leaderboard message IDs."""
    temporary_file = f"{EA_TOP100_STATE_FILE}.tmp"

    try:
        with open(temporary_file, "w", encoding="utf-8") as file:
            json.dump(state, file, indent=2, ensure_ascii=False)

        os.replace(temporary_file, EA_TOP100_STATE_FILE)
    except Exception as error:
        print(f"[TOP 100] Could not save message state: {error}")


def _ea_top100_int(club: dict, key: str, default: int = 0) -> int:
    try:
        return int(float(str(club.get(key, default)).replace(",", "")))
    except (TypeError, ValueError):
        return default


def _ea_top100_club_name(club: dict) -> str:
    club_info = club.get("clubInfo") or {}
    name = (
        club.get("clubName")
        or club.get("name")
        or club_info.get("name")
        or "Unknown Club"
    )
    return " ".join(str(name).split()).strip()


def _ea_top100_club_id(club: dict) -> str:
    club_info = club.get("clubInfo") or {}
    club_id = club.get("clubId") or club_info.get("clubId") or "Unknown"
    return str(club_id)


async def fetch_ea_top100_clubs() -> list[dict]:
    """Fetch and validate the current EA all-time leaderboard top 100."""
    data = await _ea_get_json(
        "https://proclubs.ea.com/api/fc/allTimeLeaderboard",
        {"platform": PLATFORM},
        retries=5,
    )

    if not isinstance(data, list):
        raise RuntimeError(
            f"EA returned {type(data).__name__} instead of a leaderboard list"
        )

    clubs = [club for club in data if isinstance(club, dict)]
    clubs.sort(key=_leaderboard_rank_value)
    clubs = clubs[:100]

    if not clubs:
        raise RuntimeError("EA returned an empty leaderboard")

    return clubs


async def _enrich_ea_top100_last_played_unlocked(
    clubs: list[dict],
) -> None:
    """Attach each club's newest match time without refetching unchanged clubs."""
    cache = _load_ea_top100_last_played_cache()
    semaphore = asyncio.Semaphore(3)
    cache_changed = False
    now_timestamp = int(datetime.now(timezone.utc).timestamp())
    cache_ttl = EA_TOP100_LAST_PLAYED_CACHE_HOURS * 60 * 60

    async def enrich_club(club: dict) -> None:
        nonlocal cache_changed

        club_id = _ea_top100_club_id(club)
        games_played = _ea_top100_int(club, "gamesPlayed")
        cached = cache.get(club_id) or {}

        try:
            cached_games = int(cached.get("games_played", -1))
            cached_timestamp = int(cached.get("timestamp", 0))
            cached_checked_at = int(cached.get("checked_at", 0))
        except (TypeError, ValueError):
            cached_games = -1
            cached_timestamp = 0
            cached_checked_at = 0

        cache_is_fresh = (
            cached_checked_at > 0
            and now_timestamp - cached_checked_at < cache_ttl
        )

        # Recheck immediately when the match count changes, and periodically
        # in case EA has recorded a friendly without changing that total.
        if (
            cached_games == games_played
            and cached_timestamp > 0
            and cache_is_fresh
        ):
            club["_lastPlayedTimestamp"] = cached_timestamp
            return

        async with semaphore:
            last_played = await get_last_played_timestamp(club_id)

        if last_played is not None:
            timestamp = int(last_played.timestamp())
            club["_lastPlayedTimestamp"] = timestamp
            cache[club_id] = {
                "games_played": games_played,
                "timestamp": timestamp,
                "checked_at": now_timestamp,
            }
            cache_changed = True
            return

        # Keep the last known good value if EA temporarily fails.
        if cached_timestamp > 0:
            club["_lastPlayedTimestamp"] = cached_timestamp

    await asyncio.gather(*(enrich_club(club) for club in clubs))

    if cache_changed:
        _save_ea_top100_last_played_cache(cache)


async def enrich_ea_top100_last_played(clubs: list[dict]) -> None:
    async with _ea_top100_last_played_lock:
        await _enrich_ea_top100_last_played_unlocked(clubs)


def _ea_top100_last_played_line(club: dict) -> str:
    try:
        timestamp = int(club.get("_lastPlayedTimestamp", 0))
    except (TypeError, ValueError):
        timestamp = 0

    if timestamp <= 0:
        return "🕒 **Last played** —"

    # Discord renders this as a live relative value, such as "2 hours ago".
    return f"🕒 **Last played** <t:{timestamp}:R>"


def _ea_top100_medal(rank: int) -> str:
    return {
        1: "🥇",
        2: "🥈",
        3: "🥉",
    }.get(rank, "🏆")


def build_ea_top100_embeds(
    clubs: list[dict],
    updated_at: datetime,
) -> list[discord.Embed]:
    """Build mobile-friendly embeds containing five clubs each."""
    embeds: list[discord.Embed] = []
    total_pages = max(
        1,
        math.ceil(len(clubs) / EA_TOP100_CLUBS_PER_EMBED),
    )

    for page_index in range(total_pages):
        start_index = page_index * EA_TOP100_CLUBS_PER_EMBED
        page_clubs = clubs[
            start_index:start_index + EA_TOP100_CLUBS_PER_EMBED
        ]

        if not page_clubs:
            continue

        first_rank = _leaderboard_rank_value(page_clubs[0])
        last_rank = _leaderboard_rank_value(page_clubs[-1])

        embed = discord.Embed(
            title=(
                f"EA FC TOP 100 — RANKS "
                f"{first_rank}–{last_rank}"
            ),
            color=0x18AFE6,
            timestamp=updated_at,
        )

        club_sections: list[str] = []

        # Each page is descending so all messages form one continuous
        # #100-to-#1 list, with #1 at the very bottom of the channel.
        for reversed_index, club in enumerate(reversed(page_clubs)):
            rank = _leaderboard_rank_value(club)
            if rank == 999999:
                rank = start_index + len(page_clubs) - reversed_index

            club_name = escape_markdown(
                _ea_top100_club_name(club)
            ).upper()
            club_id = _ea_top100_club_id(club)
            skill_rating = _ea_top100_int(club, "skillRating")
            games_played = _ea_top100_int(club, "gamesPlayed")
            wins = _ea_top100_int(club, "wins")
            draws = _ea_top100_int(club, "ties")
            losses = _ea_top100_int(club, "losses")
            goals_for = _ea_top100_int(club, "goals")
            goals_against = _ea_top100_int(club, "goalsAgainst")
            clean_sheets = _ea_top100_int(club, "cleanSheets")
            current_division = _ea_top100_int(club, "currentDivision")
            best_division = _ea_top100_int(club, "bestDivision")
            reputation = _ea_top100_int(club, "reputationlevel")

            win_rate = (
                (wins / games_played) * 100
                if games_played > 0
                else 0.0
            )
            goal_difference = goals_for - goals_against

            division_text = (
                str(current_division)
                if current_division > 0
                else "—"
            )
            best_division_text = (
                str(best_division)
                if best_division > 0
                else "—"
            )

            medal = f"{_ea_top100_medal(rank)} " if rank <= 3 else ""
            club_sections.append(
                (
                    f"### {medal}#{rank} — {club_name}\n"
                    f"🏅 **SR** {skill_rating:,} · "
                    f"**D** {division_text} · "
                    f"**BD** {best_division_text} · "
                    f"**R** {reputation}\n"
                    f"🎮 **P** {games_played:,} · "
                    f"**W-D-L** {wins:,}-{draws:,}-{losses:,} · "
                    f"**W%** {win_rate:.1f}\n"
                    f"⚽ **GF** {goals_for:,} · "
                    f"**GA** {goals_against:,} · "
                    f"**GD** {goal_difference:+,} · "
                    f"**CS** {clean_sheets:,}\n"
                    f"{_ea_top100_last_played_line(club)}\n"
                    f"🆔 **Club ID** `{club_id}`"
                )
            )

        embed.description = "\n\n".join(club_sections)

        footer_icon = (
            client.user.display_avatar.url
            if client.user
            else None
        )
        embed.set_footer(
            text=(
                f"EA FC Club Leaderboard • Page "
                f"{page_index + 1}/{total_pages} • "
                f"Updates every {EA_TOP100_UPDATE_MINUTES} minutes"
            ),
            icon_url=footer_icon,
        )
        embeds.append(embed)

    return embeds


def build_ea_top100_search_embed(club: dict) -> discord.Embed:
    """Build a compact result card for a typed Top 100 search."""
    rank = _leaderboard_rank_value(club)
    club_name = escape_markdown(_ea_top100_club_name(club)).upper()
    club_id = _ea_top100_club_id(club)
    skill_rating = _ea_top100_int(club, "skillRating")
    games_played = _ea_top100_int(club, "gamesPlayed")
    wins = _ea_top100_int(club, "wins")
    draws = _ea_top100_int(club, "ties")
    losses = _ea_top100_int(club, "losses")
    goals_for = _ea_top100_int(club, "goals")
    goals_against = _ea_top100_int(club, "goalsAgainst")
    clean_sheets = _ea_top100_int(club, "cleanSheets")
    current_division = _ea_top100_int(club, "currentDivision")
    best_division = _ea_top100_int(club, "bestDivision")
    reputation = _ea_top100_int(club, "reputationlevel")
    goal_difference = goals_for - goals_against
    win_rate = (
        (wins / games_played) * 100
        if games_played > 0
        else 0.0
    )

    medal = f"{_ea_top100_medal(rank)} " if rank <= 3 else "🏆 "
    embed = discord.Embed(
        title=f"{medal}TOP 100 RESULT — #{rank}",
        description=(
            f"### {club_name}\n"
            f"🏅 **SR** {skill_rating:,} · "
            f"**D** {current_division or '—'} · "
            f"**BD** {best_division or '—'} · "
            f"**R** {reputation}\n"
            f"🎮 **P** {games_played:,} · "
            f"**W-D-L** {wins:,}-{draws:,}-{losses:,} · "
            f"**W%** {win_rate:.1f}\n"
            f"⚽ **GF** {goals_for:,} · "
            f"**GA** {goals_against:,} · "
            f"**GD** {goal_difference:+,} · "
            f"**CS** {clean_sheets:,}\n"
            f"{_ea_top100_last_played_line(club)}\n"
            f"🆔 **Club ID** `{club_id}`"
        ),
        color=0x18AFE6,
        timestamp=datetime.now(timezone.utc),
    )

    footer_icon = (
        client.user.display_avatar.url
        if client.user
        else None
    )
    embed.set_footer(
        text="EA FC Club Leaderboard • Top 100 search",
        icon_url=footer_icon,
    )
    return embed


def _normalise_ea_top100_search(value: str) -> str:
    return " ".join(value.casefold().split())


async def _log_ea_top100_search(
    request_message: discord.Message,
    embed: discord.Embed,
) -> None:
    """Mirror successful typed Top 100 searches to the search log."""
    try:
        log_channel = (
            request_message.guild.get_channel(LOG_CHANNEL_ID)
            or client.get_channel(LOG_CHANNEL_ID)
        )
        if not log_channel:
            print(f"[TOP 100 SEARCH] Log channel {LOG_CHANNEL_ID} not found")
            return

        await log_channel.send(
            content=(
                f"🏆 Top 100 search by "
                f"{request_message.author.mention} in "
                f"{request_message.channel.mention}:"
            ),
            embed=embed,
        )
    except Exception as error:
        print(f"[TOP 100 SEARCH] Could not write search log: {error}")


async def _record_ea_top100_search(
    request_message: discord.Message,
    embed: discord.Embed,
) -> None:
    record_club_search(
        request_message.guild,
        request_message.author,
    )
    await _log_ea_top100_search(request_message, embed)


class EATop100SearchDropdown(discord.ui.View):
    def __init__(
        self,
        matches: list[dict],
        request_message: discord.Message,
    ):
        super().__init__(timeout=90)
        self.matches = matches[:25]
        self.request_message = request_message

        options = []
        for club in self.matches:
            rank = _leaderboard_rank_value(club)
            name = _ea_top100_club_name(club)
            skill_rating = _ea_top100_int(club, "skillRating")
            wins = _ea_top100_int(club, "wins")
            draws = _ea_top100_int(club, "ties")
            losses = _ea_top100_int(club, "losses")

            options.append(
                discord.SelectOption(
                    label=f"#{rank} — {name}"[:100],
                    description=(
                        f"SR {skill_rating:,} • "
                        f"W-D-L {wins}-{draws}-{losses}"
                    )[:100],
                    value=_ea_top100_club_id(club),
                )
            )

        select = discord.ui.Select(
            placeholder="Choose a Top 100 club…",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id == self.request_message.author.id:
            return True

        await interaction.response.send_message(
            "This Top 100 search belongs to another user.",
            ephemeral=True,
        )
        return False

    async def _on_select(self, interaction: discord.Interaction):
        selected_club_id = self.children[0].values[0]
        chosen = next(
            (
                club
                for club in self.matches
                if _ea_top100_club_id(club) == selected_club_id
            ),
            None,
        )

        if chosen is None:
            await interaction.response.edit_message(
                content="That Top 100 club could not be found.",
                embed=None,
                view=None,
            )
            return

        if not chosen.get("_lastPlayedTimestamp"):
            await enrich_ea_top100_last_played([chosen])

        embed = build_ea_top100_search_embed(chosen)
        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=None,
        )
        self.stop()

        await _record_ea_top100_search(self.request_message, embed)
        asyncio.create_task(
            safe_delete(interaction.message, delay=60)
        )


async def handle_ea_top100_channel_search(
    message: discord.Message,
) -> None:
    """Handle typed club-name searches in the Top 100 channel."""
    global _ea_top100_clubs_cache

    content = (message.content or "").strip()
    valid_club_name = (
        2 <= len(content) <= 15
        and all(
            character.isalnum() or character == " "
            for character in content
        )
    )

    if not valid_club_name:
        await warn_search_channel(
            message,
            "That message is not a valid EA FC club name.",
        )
        return

    asyncio.create_task(safe_delete(message))

    try:
        async with message.channel.typing():
            clubs = _ea_top100_clubs_cache
            if not clubs:
                clubs = await fetch_ea_top100_clubs()
                _ea_top100_clubs_cache = clubs

            query = _normalise_ea_top100_search(content)
            exact_matches = [
                club
                for club in clubs
                if _normalise_ea_top100_search(
                    _ea_top100_club_name(club)
                ) == query
            ]

            if exact_matches:
                matches = exact_matches
            else:
                matches = [
                    club
                    for club in clubs
                    if query in _normalise_ea_top100_search(
                        _ea_top100_club_name(club)
                    )
                ]

            if not matches:
                response = await message.channel.send(
                    f"{message.author.mention} **{content}** does not "
                    f"currently appear in the EA FC Top 100."
                )
                asyncio.create_task(safe_delete(response, delay=15))
                return

            if len(matches) == 1:
                if not matches[0].get("_lastPlayedTimestamp"):
                    await enrich_ea_top100_last_played([matches[0]])

                embed = build_ea_top100_search_embed(matches[0])
                response = await message.channel.send(embed=embed)
                await _record_ea_top100_search(message, embed)
                asyncio.create_task(safe_delete(response, delay=60))
                return

            view = EATop100SearchDropdown(matches, message)
            result_count = len(matches)
            selector = await message.channel.send(
                (
                    f"Found **{result_count}** matching Top 100 clubs. "
                    f"Please select one:"
                    + (
                        " Showing the first 25 results."
                        if result_count > 25
                        else ""
                    )
                ),
                view=view,
            )
            asyncio.create_task(safe_delete(selector, delay=90))

    except Exception as error:
        print(f"[TOP 100 SEARCH] Typed search failed: {error}")
        response = await message.channel.send(
            "The EA Top 100 search is temporarily unavailable. "
            "Please try again shortly."
        )
        asyncio.create_task(safe_delete(response, delay=15))


async def _discover_ea_top100_messages(channel) -> dict[str, int]:
    """Recover existing page IDs if the local state file is ever lost."""
    pages: dict[str, int] = {}
    pattern = re.compile(r"EA FC TOP 100 — RANKS (\d+)–(\d+)")

    async for message in channel.history(limit=100):
        if not client.user or message.author.id != client.user.id:
            continue
        if not message.embeds:
            continue

        title = message.embeds[0].title or ""
        match = pattern.fullmatch(title)
        if not match:
            continue

        first_rank = int(match.group(1))
        page_number = (
            (first_rank - 1) // EA_TOP100_CLUBS_PER_EMBED
        ) + 1
        pages[str(page_number)] = message.id

    return pages


async def _delete_old_ea_top100_messages(channel) -> int:
    """Delete only this bot's old Top 100 embeds during layout migration."""
    deleted = 0
    pattern = re.compile(r"EA FC TOP 100 — RANKS \d+–\d+")

    async for message in channel.history(limit=100):
        if not client.user or message.author.id != client.user.id:
            continue
        if not message.embeds:
            continue

        title = message.embeds[0].title or ""
        if not pattern.fullmatch(title):
            continue

        try:
            await message.delete()
            deleted += 1
        except (discord.NotFound, discord.Forbidden):
            continue
        except discord.HTTPException as error:
            print(
                f"[TOP 100] Could not remove old message "
                f"{message.id}: {error}"
            )

    return deleted


async def refresh_ea_top100(reason: str = "scheduled") -> dict:
    """Fetch the leaderboard and edit the persistent channel messages."""
    global _ea_top100_clubs_cache

    if not EA_TOP100_CHANNEL_ID:
        raise RuntimeError("EA_TOP100_CHANNEL_ID is not configured")

    async with _ea_top100_refresh_lock:
        channel = client.get_channel(EA_TOP100_CHANNEL_ID)
        if channel is None:
            channel = await client.fetch_channel(EA_TOP100_CHANNEL_ID)

        clubs = await fetch_ea_top100_clubs()
        await enrich_ea_top100_last_played(clubs)
        _ea_top100_clubs_cache = clubs
        updated_at = datetime.now(timezone.utc)
        embeds = build_ea_top100_embeds(clubs, updated_at)

        state = _load_ea_top100_state()
        if int(state.get("channel_id", 0) or 0) != EA_TOP100_CHANNEL_ID:
            state = {
                "channel_id": EA_TOP100_CHANNEL_ID,
                "pages": {},
            }

        existing_pages = state.get("pages") or {}
        try:
            previous_clubs_per_embed = int(
                state.get(
                    "clubs_per_embed",
                    10 if existing_pages else EA_TOP100_CLUBS_PER_EMBED,
                )
            )
        except (TypeError, ValueError):
            previous_clubs_per_embed = 10

        if previous_clubs_per_embed != EA_TOP100_CLUBS_PER_EMBED:
            deleted = await _delete_old_ea_top100_messages(channel)
            print(
                f"[TOP 100] Migrating leaderboard layout from "
                f"{previous_clubs_per_embed} to "
                f"{EA_TOP100_CLUBS_PER_EMBED} clubs per embed; "
                f"removed {deleted} old messages."
            )
            state["pages"] = {}

        pages = state.get("pages") or {}
        if not pages:
            pages = await _discover_ea_top100_messages(channel)

        created = 0
        edited = 0

        # Sending new pages in reverse order leaves ranks 1–10 as the
        # newest message at the bottom of the channel on first setup.
        missing_page_indexes = [
            index
            for index in range(len(embeds))
            if not pages.get(str(index + 1))
        ]
        newly_created_pages: set[str] = set()

        for page_index in reversed(missing_page_indexes):
            message = await channel.send(embed=embeds[page_index])
            page_key = str(page_index + 1)
            pages[page_key] = message.id
            newly_created_pages.add(page_key)
            created += 1

        for page_index, embed in enumerate(embeds):
            page_key = str(page_index + 1)
            message_id = pages.get(page_key)

            if not message_id:
                continue

            if page_key in newly_created_pages:
                continue

            try:
                message = await channel.fetch_message(int(message_id))
            except (discord.NotFound, discord.Forbidden):
                message = await channel.send(embed=embed)
                pages[page_key] = message.id
                created += 1
                continue

            if client.user and message.author.id != client.user.id:
                message = await channel.send(embed=embed)
                pages[page_key] = message.id
                created += 1
                continue

            await message.edit(content=None, embed=embed)
            edited += 1

        state["channel_id"] = EA_TOP100_CHANNEL_ID
        state["clubs_per_embed"] = EA_TOP100_CLUBS_PER_EMBED
        state["pages"] = pages
        state["last_updated"] = updated_at.isoformat()
        _save_ea_top100_state(state)

        print(
            f"[TOP 100] Refreshed {len(clubs)} clubs "
            f"({created} created, {edited} edited; {reason})."
        )

        return {
            "clubs": len(clubs),
            "created": created,
            "edited": edited,
            "updated_at": updated_at,
        }


async def ea_top100_update_loop():
    """Continuously refresh the leaderboard while the bot is running."""
    await client.wait_until_ready()
    normal_delay = EA_TOP100_UPDATE_MINUTES * 60

    while not client.is_closed():
        delay = normal_delay

        try:
            await refresh_ea_top100(reason="scheduled")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"[TOP 100] Scheduled refresh failed: {error}")
            delay = min(300, normal_delay)

        await asyncio.sleep(delay)


@tree.command(
    name="refreshtop100",
    description="Immediately refresh the EA FC Top 100 leaderboard.",
)
async def refresh_top100_command(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command must be used in the server.",
            ephemeral=True,
        )
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "You must be an administrator to use this command.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        result = await refresh_ea_top100(
            reason=f"manual request by {interaction.user}",
        )
        await interaction.followup.send(
            f"✅ Refreshed **{result['clubs']} clubs** across the "
            f"Top 100 leaderboard embeds.",
            ephemeral=True,
        )
    except Exception as error:
        print(f"[TOP 100] Manual refresh failed: {error}")
        await interaction.followup.send(
            f"❌ The Top 100 refresh failed: `{error}`",
            ephemeral=True,
        )

# =========================================================
# STAR CITIZEN / UEX
# =========================================================

UEX_HEADERS = {
    "Accept": "application/json",
    "Authorization": f"Bearer {UEX_API_KEY}",
}

_client_uex = httpx.AsyncClient(
    timeout=20,
    headers=UEX_HEADERS,
    follow_redirects=True,
)

STARCITIZEN_API_KEY = os.getenv("STARCITIZEN_API_KEY", "").strip()
SCAPI_MODE = os.getenv("SCAPI_MODE", "cache").strip() or "cache"
SCAPI_BASE = "https://api.starcitizen-api.com"
SC_ORG_SID = os.getenv("SC_ORG_SID", "").strip()

_client_scapi = httpx.AsyncClient(
    timeout=25,
    follow_redirects=True,
    headers={
        "Accept": "application/json",
        "User-Agent": "Phonics Discord Bot"
    }
)

async def _uex_get(resource: str, params: dict | None = None, retries: int = 3):
    if not UEX_API_KEY:
        raise RuntimeError("UEX_API_KEY is missing.")

    url = f"{UEX_API_BASE}/{resource.strip('/')}/"

    for attempt in range(retries):
        try:
            r = await _client_uex.get(url, params=params or {})

            if r.status_code == 200:
                payload = r.json()
                if isinstance(payload, dict):
                    return payload.get("data", payload)
                return payload

            print(f"[UEX] {r.status_code} {url} try {attempt+1}/{retries} :: {r.text[:300]}")

            if r.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep(1.2 + attempt)
                continue

        except Exception as e:
            print(f"[UEX] exception {url} try {attempt+1}/{retries} :: {e}")

        await asyncio.sleep(0.8 + attempt)

    return None

def _normalize_sc_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())

async def get_trending_commodities(limit: int = 10) -> list[dict]:
    data = await _uex_get("commodities_ranking")
    if not isinstance(data, list):
        return []

    cleaned = []
    for item in data:
        try:
            buy_scu = float(item.get("scu_buy_avg") or 0)
            sell_scu = float(item.get("scu_sell_avg") or 0)
            total_volume = buy_scu + sell_scu

            item["_buy_scu_avg_month"] = buy_scu
            item["_sell_scu_avg_month"] = sell_scu
            item["_total_scu_avg_month"] = total_volume
            cleaned.append(item)
        except Exception:
            continue

    cleaned.sort(
        key=lambda x: float(x.get("cax_score") or 0),
        reverse=True
    )
    return cleaned[:limit]

async def search_commodity_uex(query: str) -> list[dict]:
    data = await _uex_get("commodities")
    if not isinstance(data, list):
        return []

    q = _normalize_sc_name(query)
    if not q:
        return []

    exact = []
    partial = []

    for item in data:
        name = str(item.get("name", ""))
        code = str(item.get("code", ""))
        hay = _normalize_sc_name(name)
        hay_code = _normalize_sc_name(code)

        if q == hay or q == hay_code:
            exact.append(item)
        elif q in hay or q in hay_code:
            partial.append(item)

    if exact:
        return exact

    return partial

async def commodity_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    if not current or not current.strip():
        return []

    try:
        matches = await search_commodity_uex(current)
    except Exception as e:
        print(f"[ERROR] commodity_autocomplete failed: {e}")
        return []

    choices = []
    seen = set()

    for item in matches[:25]:
        commodity_name = str(item.get("name") or "").strip()
        if not commodity_name:
            continue

        key = commodity_name.lower()
        if key in seen:
            continue
        seen.add(key)

        code = str(item.get("code") or "").strip()
        label = commodity_name
        if code:
            label = f"{commodity_name} ({code})"

        choices.append(
            app_commands.Choice(
                name=label[:100],
                value=commodity_name[:100]
            )
        )

    return choices

async def get_commodity_prices(commodity_id: str | int):
    return await _uex_get("commodities_prices", params={"id_commodity": commodity_id})

async def get_commodity_routes(commodity_id: str | int, max_rows: int = 10):
    data = await _uex_get(
        "commodities_routes",
        params={
            "id_commodity": commodity_id,
            "limit": max_rows,
        }
    )
    if not isinstance(data, list):
        return []
    return data

_terminal_cache = None

async def get_all_terminals():
    global _terminal_cache

    if _terminal_cache is not None:
        return _terminal_cache

    data = await _uex_get("terminals")
    if not isinstance(data, list):
        _terminal_cache = []
        return _terminal_cache

    _terminal_cache = data
    return _terminal_cache

def find_terminal_info(terminals: list[dict], terminal_name: str):
    norm = _normalize_sc_name(terminal_name)

    for t in terminals:
        name = t.get("name", "")
        if _normalize_sc_name(name) == norm:
            return t

    return None

def terminal_system_name(terminal_info: dict | None) -> str:
    if not terminal_info:
        return "Unknown"
    return (
        terminal_info.get("star_system_name")
        or terminal_info.get("system_name")
        or terminal_info.get("name_star_system")
        or "Unknown"
    )

SCWIKI_VEHICLES_URL = "https://api.star-citizen.wiki/api/shipmatrix/vehicles"

_ship_cache = None
_scapi_ship_cache = None

async def get_all_ships_scwiki():
    global _ship_cache

    if _ship_cache is not None:
        return _ship_cache

    try:
        all_ships = []
        page_number = 1
        last_page = 1

        while page_number <= last_page:
            r = await _client_uex.get(
                SCWIKI_VEHICLES_URL,
                params={"page[number]": page_number}
            )

            if r.status_code != 200:
                print(f"[SCWIKI] {r.status_code} {SCWIKI_VEHICLES_URL} page {page_number} :: {r.text[:300]}")
                break

            payload = r.json()

            if isinstance(payload, dict):
                page_data = payload.get("data", []) or []
                meta = payload.get("meta", {}) or {}
                last_page = int(meta.get("last_page", last_page) or last_page)
            elif isinstance(payload, list):
                page_data = payload
                meta = {}
                last_page = page_number
            else:
                page_data = []
                meta = {}
                last_page = page_number

            if not isinstance(page_data, list):
                page_data = []

            page_data = [s for s in page_data if isinstance(s, dict)]
            all_ships.extend(page_data)

            page_number += 1

        # de-dupe by id/uuid/slug
        deduped = []
        seen = set()

        for ship in all_ships:
            sid = str(
                ship.get("uuid")
                or ship.get("id")
                or ship.get("slug")
                or ship.get("name")
                or ""
            )
            if not sid or sid in seen:
                continue
            seen.add(sid)
            deduped.append(ship)

        _ship_cache = deduped

        print(f"[SCWIKI] loaded ships: {len(_ship_cache)}")
        if _ship_cache:
            print("[SCWIKI] sample ship keys:", list(_ship_cache[0].keys()))

        return _ship_cache

    except Exception as e:
        print(f"[SCWIKI] exception loading ships :: {e}")
        _ship_cache = []
        return _ship_cache

async def get_all_scapi_ships():
    global _scapi_ship_cache

    if _scapi_ship_cache is not None:
        return _scapi_ship_cache

    if not STARCITIZEN_API_KEY:
        print("[SCAPI] Missing API key")
        _scapi_ship_cache = []
        return _scapi_ship_cache

    url = f"{SCAPI_BASE}/{STARCITIZEN_API_KEY}/v1/{SCAPI_MODE}/ships"

    try:
        r = await _client_scapi.get(
            url,
            params={"page_max": 1000}
        )

        print(f"[SCAPI] full ship cache -> {r.status_code}")

        if r.status_code != 200:
            print(f"[SCAPI] body: {r.text[:500]}")
            _scapi_ship_cache = []
            return _scapi_ship_cache

        payload = r.json()

        data = payload.get("data", [])

        if not isinstance(data, list):
            print("[SCAPI] unexpected payload shape")
            _scapi_ship_cache = []
            return _scapi_ship_cache

        _scapi_ship_cache = data

        print(f"[SCAPI] cached {len(_scapi_ship_cache)} ships")

        return _scapi_ship_cache

    except Exception as e:
        print(f"[SCAPI] failed loading ship cache: {e}")
        _scapi_ship_cache = []
        return _scapi_ship_cache


def _ship_display_name(ship: dict) -> str:
    return (
        ship.get("game_name")
        or ship.get("name")
        or ship.get("shipmatrix_name")
        or ship.get("slug")
        or "Unknown Ship"
    )


def _ship_scu(ship: dict) -> int:
    try:
        # common direct fields
        for key in ("cargo_capacity", "cargo", "scu"):
            value = ship.get(key)
            if value not in (None, "", 0, "0"):
                return int(float(value))

        # nested cargo object
        cargo_obj = ship.get("cargo")
        if isinstance(cargo_obj, dict):
            for key in ("capacity", "scu", "cargo_capacity", "value"):
                value = cargo_obj.get(key)
                if value not in (None, "", 0, "0"):
                    return int(float(value))

        # fallback if physical / specs style nesting exists
        specs = ship.get("specs")
        if isinstance(specs, dict):
            for key in ("cargo_capacity", "cargo", "scu"):
                value = specs.get(key)
                if value not in (None, "", 0, "0"):
                    return int(float(value))

        return 0
    except Exception:
        return 0

def ship_text(value, default="—"):
    if value in (None, "", [], {}):
        return default

    if isinstance(value, dict):
        return (
            value.get("en_EN")
            or value.get("en")
            or value.get("en_US")
            or next((v for v in value.values() if isinstance(v, str) and v.strip()), default)
        )

    return str(value)

async def search_ships_scwiki(query: str, cargo_only: bool = False) -> list[dict]:
    ships = await get_all_ships_scwiki()
    if not isinstance(ships, list):
        return []

    raw_query = (query or "").strip()
    q_norm = _normalize_sc_name(raw_query)
    if not q_norm:
        return []

    def ship_names(ship: dict) -> list[str]:
        return [
            str(ship.get("name", "")).strip(),
            str(ship.get("game_name", "")).strip(),
            str(ship.get("slug", "")).strip(),
            str(ship.get("shipmatrix_name", "")).strip(),
        ]

    def ship_id(ship: dict) -> str:
        return str(ship.get("uuid") or ship.get("id") or ship.get("slug") or _ship_display_name(ship))

    def usable(ship: dict) -> bool:
        return True if not cargo_only else _ship_scu(ship) > 0

    def dedupe(ship_list: list[dict]) -> list[dict]:
        seen = set()
        out = []
        for ship in ship_list:
            if not usable(ship):
                continue
            sid = ship_id(ship)
            if sid in seen:
                continue
            seen.add(sid)
            out.append(ship)
        return out

    exact = []
    token_matches = []
    partial = []

    for ship in ships:
        names = ship_names(ship)
        lowered = [n.lower() for n in names if n]
        norm_names = [_normalize_sc_name(n) for n in names if n]

        if any(q_norm == n for n in norm_names):
            exact.append(ship)
            continue

        if any(n.startswith(q_norm) for n in norm_names):
            token_matches.append(ship)
            continue

        if any(raw_query.lower() in n.split() for n in lowered):
            token_matches.append(ship)
            continue

        if any(part.startswith(raw_query.lower()) for n in lowered for part in re.split(r"[\s\-_\/]+", n) if part):
            token_matches.append(ship)
            continue

        if any(q_norm in n for n in norm_names):
            partial.append(ship)

    exact = dedupe(exact)
    if exact:
        return exact[:25]

    token_matches = dedupe(token_matches)
    if token_matches:
        return token_matches[:25]

    partial = dedupe(partial)
    if partial:
        return partial[:25]

    choices = []
    choice_to_ship = {}

    for ship in ships:
        if not usable(ship):
            continue

        for name in ship_names(ship):
            if not name:
                continue
            choices.append(name)
            choice_to_ship[name] = ship

    fuzzy = process.extract(raw_query, choices, scorer=fuzz.token_sort_ratio, limit=25)

    results = []
    seen = set()

    for matched_name, score in fuzzy:
        if score < 78:
            continue

        ship = choice_to_ship[matched_name]
        sid = ship_id(ship)

        if sid in seen:
            continue

        seen.add(sid)
        results.append(ship)

    return results[:25]
async def ship_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    if not current or not current.strip():
        return []

    global _ship_cache
    ships = _ship_cache or []

    if not ships:
        return []

    raw_query = current.strip()
    q_norm = _normalize_sc_name(raw_query)

    def ship_names(ship: dict) -> list[str]:
        return [
            str(ship.get("name", "")).strip(),
            str(ship.get("game_name", "")).strip(),
            str(ship.get("slug", "")).strip(),
            str(ship.get("shipmatrix_name", "")).strip(),
        ]

    matches = []

    for ship in ships:
        names = ship_names(ship)
        norm_names = [_normalize_sc_name(n) for n in names if n]

        if any(q_norm == n for n in norm_names) or any(q_norm in n for n in norm_names):
            matches.append(ship)

    if not matches:
        choices_pool = []
        choice_to_ship = {}

        for ship in ships:
            for n in ship_names(ship):
                if not n:
                    continue
                choices_pool.append(n)
                choice_to_ship[n] = ship

        fuzzy_matches = process.extract(raw_query, choices_pool, scorer=fuzz.token_sort_ratio, limit=25)

        seen = set()
        for matched_name, score in fuzzy_matches:
            if score < 78:
                continue

            ship = choice_to_ship[matched_name]
            sid = str(ship.get("uuid") or ship.get("id") or ship.get("slug") or _ship_display_name(ship))

            if sid in seen:
                continue

            seen.add(sid)
            matches.append(ship)

    choices = []
    seen_names = set()

    for ship in matches[:25]:
        ship_name = _ship_display_name(ship)
        ship_scu = _ship_scu(ship)

        if not ship_name:
            continue

        key = ship_name.lower().strip()
        if key in seen_names:
            continue

        seen_names.add(key)

        label = f"{ship_name} ({ship_scu} SCU)" if ship_scu > 0 else ship_name

        choices.append(
            app_commands.Choice(
                name=label[:100],
                value=ship_name[:100]
            )
        )

    return choices

def _safe_ship_value(value, default="—"):
    if value in (None, "", [], {}):
        return default
    return str(value)


def _first_ship_image(ship: dict) -> str | None:
    """
    Try to find a usable ship image from StarCitizen-API's media field.
    """
    media = ship.get("media") or []

    if isinstance(media, list):
        for item in media:
            if not isinstance(item, dict):
                continue

            # Common possible shapes
            url = (
                item.get("source")
                or item.get("large")
                or item.get("thumbnail")
                or item.get("url")
            )

            # Sometimes URLs are nested
            urls = item.get("urls")
            if not url and isinstance(urls, dict):
                url = (
                    urls.get("source")
                    or urls.get("large")
                    or urls.get("rect")
                    or urls.get("square")
                )

            if url:
                url = str(url)
                if url.startswith("//"):
                    return "https:" + url
                if url.startswith("/"):
                    return "https://robertsspaceindustries.com" + url
                return url

    return None


def _manufacturer_name(ship: dict) -> str:
    manufacturer = ship.get("manufacturer")

    if isinstance(manufacturer, dict):
        return (
            manufacturer.get("name")
            or manufacturer.get("code")
            or str(ship.get("manufacturer_id") or "—")
        )

    return str(manufacturer or ship.get("manufacturer_id") or "—")


async def fetch_ship_from_scapi(ship_name: str) -> dict | None:
    ships = await get_all_scapi_ships()

    if not ships:
        return None

    q = _normalize_sc_name(ship_name)

    exact = []
    partial = []

    for ship in ships:
        names = [
            str(ship.get("name", "")).strip(),
            str(ship.get("name_full", "")).strip(),
            str(ship.get("slug", "")).strip(),
        ]

        norm_names = [
            _normalize_sc_name(n)
            for n in names
            if n
        ]

        # exact
        if any(q == n for n in norm_names):
            exact.append(ship)
            continue

        # partial
        if any(q in n for n in norm_names):
            partial.append(ship)

    if exact:
        print(f"[SCAPI] exact match for {ship_name}")
        return exact[0]

    if partial:
        print(f"[SCAPI] partial match for {ship_name}")
        return partial[0]

    # fuzzy fallback
    choices = {}
    choice_names = []

    for ship in ships:
        name = str(ship.get("name") or "").strip()

        if not name:
            continue

        choice_names.append(name)
        choices[name] = ship

    fuzzy = process.extractOne(
        ship_name,
        choice_names,
        scorer=fuzz.token_sort_ratio
    )

    if fuzzy:
        matched_name, score = fuzzy

        print(f"[SCAPI] fuzzy match {matched_name} ({score})")

        if score >= 70:
            return choices[matched_name]

    print(f"[SCAPI] no ship found for {ship_name}")

    return None
    
def build_ship_embed(ship: dict) -> discord.Embed:
    ship_name = ship_text(
        ship.get("name")
        or ship.get("game_name")
        or ship.get("shipmatrix_name")
        or ship.get("slug"),
        "Unknown Ship"
    )

    description = ship_text(
        ship.get("description")
        or ship.get("short_description")
        or ship.get("excerpt"),
        "No description available."
    )

    if len(description) > 350:
        description = description[:347] + "..."

    embed = discord.Embed(
        title=f"🚀 {ship_name}",
        description=description,
        color=0x5865F2
    )

    image_url = _first_ship_image(ship)
    if image_url:
        embed.set_thumbnail(url=image_url)

    manufacturer = ship.get("manufacturer")
    if isinstance(manufacturer, dict):
        manufacturer = ship_text(manufacturer.get("name") or manufacturer)
    else:
        manufacturer = ship_text(
            ship.get("manufacturer_name")
            or ship.get("manufacturer")
            or ship.get("manufacturer_code")
        )

    cargo = (
        ship.get("cargocapacity")
        or ship.get("cargo_capacity")
        or ship.get("scu")
        or _ship_scu(ship)
    )

    crew_data = ship.get("crew") or {}

    if isinstance(crew_data, dict):
        crew_min = ship_text(crew_data.get("min"))
        crew_max = ship_text(crew_data.get("max"))
    else:
        crew_min = ship_text(crew_data)
        crew_max = ship_text(crew_data)
    
    crew = crew_min if crew_min == crew_max else f"{crew_min} - {crew_max}"
    
    foci = ship.get("foci") or []
    
    if isinstance(foci, list) and foci:
        focus = ", ".join(ship_text(f) for f in foci)
    else:
        focus = ship_text(ship.get("focus") or ship.get("role"))
    
    embed.add_field(name="Manufacturer", value=manufacturer, inline=True)
    embed.add_field(name="Focus", value=focus, inline=True)
    embed.add_field(name="Type", value=ship_text(ship.get("type")), inline=True)
    
    embed.add_field(name="Size", value=ship_text(ship.get("size")).title(), inline=True)
    embed.add_field(name="Crew", value=crew, inline=True)
    embed.add_field(name="Cargo", value=f"{cargo} SCU" if cargo not in (None, "", "—") else "—", inline=True)
    
    dimension = ship.get("dimension") or {}
    
    if isinstance(dimension, dict):
        length = dimension.get("length")
    else:
        length = ship.get("length")
    
    embed.add_field(
        name="Length",
        value=f"{ship_text(length)} m",
        inline=True
    )
    
    mass = ship.get("mass")
    
    embed.add_field(
        name="Mass",
        value=f"{ship_text(mass)} kg",
        inline=True
    )
    embed.add_field(name="Status", value=ship_text(ship.get("production_status") or ship.get("status")).title(), inline=True)

    embed.set_footer(text="Star Citizen — Ship Data")
    return embed

async def fetch_org_members_scapi(org_sid: str, max_pages: int = 10) -> list[dict]:
    if not STARCITIZEN_API_KEY:
        raise RuntimeError("STARCITIZEN_API_KEY is missing from .env")

    if not org_sid:
        raise RuntimeError("SC_ORG_SID is missing from .env")

    members = []

    # Org members works best in live mode
    mode = "live"

    for page in range(1, max_pages + 1):
        url = f"{SCAPI_BASE}/{STARCITIZEN_API_KEY}/v1/{mode}/organization_members/{org_sid.upper()}"

        try:
            r = await _client_scapi.get(url, params={"page": page})

            print(f"[SCAPI] org members {org_sid.upper()} page {page} -> {r.status_code}")

            if r.status_code != 200:
                print(f"[SCAPI] body: {r.text[:500]}")
                break

            payload = r.json()
            data = payload.get("data") if isinstance(payload, dict) else None

            if not isinstance(data, list) or not data:
                print(f"[SCAPI] no members returned on page {page}")
                break

            members.extend(data)

            if len(data) < 32:
                break

        except Exception as e:
            print(f"[SCAPI] fetch_org_members_scapi failed: {e}")
            break

    return members

RANK_ORDER = {
    "founder": 1,
    "director": 2,
    "leader": 3,
    "officer": 4,
    "recruitment": 5,
    "member": 6,
    "regular": 7,
    "affiliate": 8,
    "recruit": 9,
}

def member_rank_priority(member: dict):
    rank = str(member.get("rank") or "").lower()

    roles = member.get("roles") or []
    roles_text = " ".join(str(r).lower() for r in roles)

    best = 999

    for key, value in RANK_ORDER.items():
        if key in rank:
            best = min(best, value)

        if key in roles_text:
            best = min(best, value)

    display = str(member.get("display") or member.get("handle") or "").lower()

    return (best, display)

async def fetch_org_info_scapi(org_sid: str) -> dict | None:
    if not STARCITIZEN_API_KEY or not org_sid:
        return None

    url = f"{SCAPI_BASE}/{STARCITIZEN_API_KEY}/v1/live/organization/{org_sid.upper()}"

    try:
        r = await _client_scapi.get(url)
        print(f"[SCAPI] org info {org_sid.upper()} -> {r.status_code}")

        if r.status_code != 200:
            print(f"[SCAPI] org info body: {r.text[:500]}")
            return None

        payload = r.json()
        data = payload.get("data") if isinstance(payload, dict) else None

        return data if isinstance(data, dict) else None

    except Exception as e:
        print(f"[SCAPI] fetch_org_info_scapi failed: {e}")
        return None

def build_members_embed(org_sid: str, members: list[dict], org_info: dict | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=f"👥 {org_sid.upper()} Members",
        description=f"Current organisation members found: **{len(members)}**",
        color=0x5865F2
    )

    # Org logo thumbnail
    if org_info:
        logo = (
            org_info.get("logo")
            or org_info.get("image")
            or org_info.get("thumbnail")
            or org_info.get("banner")
        )

        if isinstance(logo, dict):
            logo = logo.get("url") or logo.get("source") or logo.get("small")

        if logo:
            logo = str(logo)
            if logo.startswith("/"):
                logo = "https://robertsspaceindustries.com" + logo
            embed.set_thumbnail(url=logo)

    if not members:
        embed.add_field(name="Members", value="No members found.", inline=False)
        embed.set_footer(text="Star Citizen — Organisation Members")
        return embed

    groups = {
        "👑 Leadership": [],
        "🛡️ Staff": [],
        "👥 Members": [],
        "📦 Other": [],
    }

    for member in members:
        display = str(member.get("display") or member.get("handle") or "Unknown").strip()
        handle = str(member.get("handle") or "").strip()
        rank = str(member.get("rank") or "—").strip()

        roles = member.get("roles") or []
        cleaned_roles = [
            str(r).strip()
            for r in roles
            if str(r).strip()
        ] if isinstance(roles, list) else []

        roles_text = ", ".join(cleaned_roles)

        handle_part = ""
        if handle and handle.lower() != display.lower():
            handle_part = f" `@{handle}`"

        details = rank
        if roles_text:
            details += f" • {roles_text}"

        line = f"**{display}**{handle_part}\n*{details}*"

        rank_l = rank.lower()
        roles_l = roles_text.lower()

        if "founder" in roles_l or "master" in rank_l:
            groups["👑 Leadership"].append(line)
        elif "officer" in roles_l or "recruitment" in roles_l or "branding" in roles_l:
            groups["🛡️ Staff"].append(line)
        elif "regular" in rank_l or "member" in rank_l:
            groups["👥 Members"].append(line)
        else:
            groups["📦 Other"].append(line)

    for group_name, group_members in groups.items():
        if not group_members:
            continue

        embed.add_field(
            name=f"{group_name} · {len(group_members)}",
            value="\n\n".join(group_members[:15]),
            inline=False
        )

    embed.set_footer(text="Phonics — Organisation Members")
    return embed

async def build_commodity_embed(
    commodity: dict,
    auto_load_only: bool = False,
    system_filter: str | None = None
) -> discord.Embed:
    commodity_id = commodity.get("id") or commodity.get("id_commodity")
    commodity_name = commodity.get("name", "Unknown Commodity")

    prices = await get_commodity_prices(commodity_id)
    rows = prices if isinstance(prices, list) else []

    terminals = await get_all_terminals()
    wanted_system = (system_filter or "").strip().lower()

    def is_terminal_auto_load(terminal_name: str) -> bool | None:
        terminal_info = find_terminal_info(terminals, terminal_name)
        if not terminal_info:
            return None

        val = terminal_info.get("is_auto_load")
        if val in (1, "1", True):
            return True
        if val in (0, "0", False):
            return False
        return None

    def terminal_matches_system(terminal_name: str) -> bool:
        if not wanted_system:
            return True
        terminal_info = find_terminal_info(terminals, terminal_name)
        system_name = terminal_system_name(terminal_info).lower()
        return system_name == wanted_system

    embed = discord.Embed(
        title=f"🚚 {commodity_name}",
        description="Best buy/sell locations from UEX trade data.",
        color=0x5865F2
    )

    if not rows:
        embed.add_field(name="Prices", value="No price data found.", inline=False)
        embed.set_footer(text="Star Citizen — UEX")
        return embed

    buy_candidates = [r for r in rows if r.get("price_buy") not in (None, "", 0)]
    sell_candidates = [r for r in rows if r.get("price_sell") not in (None, "", 0)]

    if auto_load_only:
        buy_candidates = [
            r for r in buy_candidates
            if is_terminal_auto_load(r.get("terminal_name") or r.get("name_terminal") or "") is True
        ]
        sell_candidates = [
            r for r in sell_candidates
            if is_terminal_auto_load(r.get("terminal_name") or r.get("name_terminal") or "") is True
        ]

    if wanted_system:
        buy_candidates = [
            r for r in buy_candidates
            if terminal_matches_system(r.get("terminal_name") or r.get("name_terminal") or "")
        ]
        sell_candidates = [
            r for r in sell_candidates
            if terminal_matches_system(r.get("terminal_name") or r.get("name_terminal") or "")
        ]

    buy_rows = sorted(
        buy_candidates,
        key=lambda x: (
            terminal_system_name(find_terminal_info(terminals, x.get("terminal_name") or x.get("name_terminal") or "")),
            float(x.get("price_buy", 999999999))
        )
    )[:5]

    sell_rows = sorted(
        sell_candidates,
        key=lambda x: (
            terminal_system_name(find_terminal_info(terminals, x.get("terminal_name") or x.get("name_terminal") or "")),
            -float(x.get("price_sell", 0))
        )
    )[:5]

    if buy_rows:
        lines = []
        best_sell = max(
            [float(s.get("price_sell") or 0) for s in sell_rows],
            default=0
        )

        for r in buy_rows:
            terminal = r.get("terminal_name") or r.get("name_terminal") or "Unknown"
            terminal_info = find_terminal_info(terminals, terminal)
            system_name = terminal_system_name(terminal_info)
            buy_price = float(r.get("price_buy") or 0)
            profit = int(best_sell - buy_price)
            stock = r.get("scu_buy") or r.get("stock_buy") or "—"

            lines.append(
                f"**[{system_name}] {terminal}** — Buy: `{int(buy_price)}` aUEC/SCU • Profit: `+{profit}` • Stock: `{stock}`"
            )

        embed.add_field(name="Best Buy", value="\n".join(lines), inline=False)
    else:
        embed.add_field(
            name="Best Buy",
            value="No buy locations matched your current filters.",
            inline=False
        )

    if sell_rows:
        lines = []
        for r in sell_rows:
            terminal = r.get("terminal_name") or r.get("name_terminal") or "Unknown"
            terminal_info = find_terminal_info(terminals, terminal)
            system_name = terminal_system_name(terminal_info)
            price = r.get("price_sell", "—")
            demand = r.get("scu_sell") or r.get("stock_sell") or "—"

            lines.append(
                f"**[{system_name}] {terminal}** — Sell: `{price}` aUEC/SCU • Demand: `{demand}`"
            )

        embed.add_field(name="Best Sell", value="\n".join(lines), inline=False)
    else:
        embed.add_field(
            name="Best Sell",
            value="No sell locations matched your current filters.",
            inline=False
        )

    footer_bits = ["Star Citizen — UEX"]
    if auto_load_only:
        footer_bits.append("Auto-load only")
    if wanted_system:
        footer_bits.append(f"System: {system_filter}")
    embed.set_footer(text=" • ".join(footer_bits))

    return embed

async def build_route_embed(
    commodity: dict,
    auto_load_only: bool = False,
    system_filter: str | None = None,
    cargo_scu: int | None = None,
    ship_name: str | None = None
) -> discord.Embed:
    commodity_id = commodity.get("id") or commodity.get("id_commodity")
    commodity_name = commodity.get("name", "Unknown Commodity")

    routes = await get_commodity_routes(commodity_id, max_rows=25)
    terminals = await get_all_terminals()
    wanted_system = (system_filter or "").strip().lower()

    def is_terminal_auto_load(terminal_name: str) -> bool | None:
        terminal_info = find_terminal_info(terminals, terminal_name)
        if not terminal_info:
            return None
        val = terminal_info.get("is_auto_load")
        if val in (1, "1", True):
            return True
        if val in (0, "0", False):
            return False
        return None

    embed = discord.Embed(
        title=f"📈 Best Routes — {commodity_name}",
        description="Top trade routes by profit and margin.",
        color=0x2ECC71
    )

    if not routes:
        embed.add_field(name="Routes", value="No routes found.", inline=False)
        embed.set_footer(text="Star Citizen — UEX")
        return embed

    filtered_routes = []

    for r in routes:
        origin = (
            r.get("terminal_origin_name")
            or r.get("origin_terminal_name")
            or r.get("from_terminal_name")
            or "Unknown Origin"
        )
        destination = (
            r.get("terminal_destination_name")
            or r.get("destination_terminal_name")
            or r.get("to_terminal_name")
            or "Unknown Destination"
        )

        origin_info = find_terminal_info(terminals, origin)
        destination_info = find_terminal_info(terminals, destination)

        origin_system = terminal_system_name(origin_info)
        destination_system = terminal_system_name(destination_info)

        origin_auto = is_terminal_auto_load(origin)
        destination_auto = is_terminal_auto_load(destination)

        if auto_load_only and not (origin_auto is True and destination_auto is True):
            continue

        if wanted_system and not (
            origin_system.lower() == wanted_system or destination_system.lower() == wanted_system
        ):
            continue

        filtered_routes.append((
            r,
            origin,
            destination,
            origin_system,
            destination_system,
            origin_auto,
            destination_auto
        ))

    filtered_routes = sorted(
        filtered_routes,
        key=lambda x: (
            x[3],
            -(float(x[0].get("profit") or x[0].get("profit_total") or 0))
        )
    )

    if not filtered_routes:
        embed.add_field(name="Routes", value="No routes found matching that filter.", inline=False)
        embed.set_footer(text="Star Citizen — UEX")
        return embed

    def auto_icon(val):
        if val is True:
            return "✅"
        if val is False:
            return "❌"
        return "❓"

    lines = []

    for r, origin, destination, origin_system, destination_system, origin_auto, destination_auto in filtered_routes[:10]:
        margin = r.get("profit_margin") or r.get("margin") or "—"
        profit_per_scu = r.get("profit") or r.get("profit_total") or "—"

        line = (
            f"**[{origin_system}] {origin} {auto_icon(origin_auto)} → [{destination_system}] {destination} {auto_icon(destination_auto)}**\n"
            f"Profit: `{profit_per_scu}` aUEC/SCU • Margin: `{margin}`"
        )

        if cargo_scu:
            try:
                total_profit = float(profit_per_scu) * int(cargo_scu)
                line += f" • Full Load ({cargo_scu} SCU): `{int(total_profit):,}` aUEC"
            except Exception:
                pass

        lines.append(line)

    field_text = ""
    for line in lines:
        # +2 accounts for the "\n\n"
        if len(field_text) + len(line) + 2 > 1024:
            break
        field_text += line + "\n\n"
    
    embed.add_field(
        name="Top Routes",
        value=field_text.strip(),
        inline=False
    )

    if cargo_scu and ship_name:
        embed.set_author(name=f"Ship: {ship_name} • {cargo_scu} SCU")
    elif cargo_scu:
        embed.set_author(name=f"Cargo: {cargo_scu} SCU")

    footer_bits = [
        "Star Citizen — UEX",
        "✅ Auto-load",
        "❌ No auto-load",
        "❔ Unknown"
    ]
    
    if auto_load_only:
        footer_bits.append("Auto-load only")
    
    if wanted_system:
        footer_bits.append(f"System: {system_filter}")
    
    embed.set_footer(text=" • ".join(footer_bits))

    return embed

class CommodityDropdown(discord.ui.View):
    def __init__(
        self,
        results: list[dict],
        mode: str = "commodity",
        auto_load_only: bool = False,
        system_filter: str | None = None,
        cargo_scu: int | None = None,
        buy_price_override: float | None = None,
        ship_name: str | None = None
    ):
        super().__init__(timeout=90)
        self.results = results
        self.mode = mode
        self.auto_load_only = auto_load_only
        self.system_filter = system_filter
        self.cargo_scu = cargo_scu
        self.buy_price_override = buy_price_override
        self.ship_name = ship_name

        options = []
        for item in results[:25]:
            label = item.get("name", "Unknown Commodity")
            value = str(item.get("id") or item.get("id_commodity") or "")
            if not value:
                continue
            options.append(discord.SelectOption(label=label[:100], value=value))

        options.append(discord.SelectOption(label="None of these", value="none"))

        select = discord.ui.Select(
            placeholder="Choose a commodity…",
            options=options,
            min_values=1,
            max_values=1
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        value = self.children[0].values[0]

        if value == "none":
            await interaction.response.edit_message(content="Selection cancelled.", view=None)
            return

        chosen = next(
            (x for x in self.results if str(x.get("id") or x.get("id_commodity")) == value),
            None
        )
        if not chosen:
            await interaction.response.edit_message(content="Could not find that commodity.", view=None)
            return

        await interaction.response.defer()

        if self.mode == "commodity":
            embed = await build_commodity_embed(
                chosen,
                auto_load_only=self.auto_load_only,
                system_filter=self.system_filter
            )
            command_name = "commodity"

        elif self.mode == "route":
            embed = await build_route_embed(
                chosen,
                auto_load_only=self.auto_load_only,
                system_filter=self.system_filter,
                cargo_scu=self.cargo_scu,
                ship_name=self.ship_name
            )
            command_name = "route"

        elif self.mode == "cargo":
            embed = await build_cargo_embed(
                chosen,
                cargo_scu=self.cargo_scu,
                buy_price_override=self.buy_price_override,
                auto_load_only=self.auto_load_only,
                system_filter=self.system_filter
            )
            command_name = "cargo"

        else:
            await interaction.edit_original_response(
                content="Unknown dropdown mode.",
                view=None
            )
            return

        await interaction.edit_original_response(content=None, embed=embed, view=None)

        try:
            msg = await interaction.original_response()
            await log_star_command_usage(interaction, command_name, message=msg)
        except Exception as e:
            print(f"[ERROR] Failed to log final dropdown output ({command_name}): {e}")
        
class ShipDropdown(discord.ui.View):
    def __init__(
        self,
        ship_results: list[dict],
        commodity_query: str,
        mode: str = "cargo",
        buy_price_override: float | None = None,
        auto_load_only: bool = False,
        system_filter: str | None = None
    ):
        super().__init__(timeout=90)
        self.ship_results = ship_results
        self.commodity_query = commodity_query
        self.mode = mode
        self.buy_price_override = buy_price_override
        self.auto_load_only = auto_load_only
        self.system_filter = system_filter

        options = []
        for ship in ship_results[:25]:
            ship_name = _ship_display_name(ship)
            ship_scu = _ship_scu(ship)
            ship_id = str(ship.get("uuid") or ship.get("id") or ship.get("slug") or ship_name)

            options.append(
                discord.SelectOption(
                    label=ship_name[:100],
                    value=ship_id,
                    description=f"{ship_scu} SCU"[:100]
                )
            )

        options.append(discord.SelectOption(label="None of these", value="none"))

        select = discord.ui.Select(
            placeholder="Choose a ship…",
            options=options,
            min_values=1,
            max_values=1
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        value = self.children[0].values[0]

        if value == "none":
            await interaction.response.edit_message(content="Selection cancelled.", view=None)
            return

        chosen_ship = next(
            (
                s for s in self.ship_results
                if str(s.get("uuid") or s.get("id") or s.get("slug") or _ship_display_name(s)) == value
            ),
            None
        )

        if not chosen_ship:
            await interaction.response.edit_message(content="Could not find that ship.", view=None)
            return

        await interaction.response.defer()

        ship_scu = _ship_scu(chosen_ship)
        ship_name = _ship_display_name(chosen_ship)

        if ship_scu <= 0:
            await interaction.edit_original_response(
                content="That ship does not have a usable cargo capacity in the API.",
                view=None
            )
            return

        commodity_matches = await search_commodity_uex(self.commodity_query)

        if not commodity_matches:
            await interaction.edit_original_response(
                content="No matching commodities found.",
                view=None
            )
            return

        if len(commodity_matches) > 1:
            view = CommodityDropdown(
                commodity_matches,
                mode=self.mode,
                auto_load_only=self.auto_load_only,
                system_filter=self.system_filter,
                cargo_scu=ship_scu,
                buy_price_override=self.buy_price_override,
                ship_name=ship_name
            )
            await interaction.edit_original_response(
                content=f"Using **{ship_name}** (`{ship_scu}` SCU).\nMultiple commodities found. Please choose:",
                view=view,
                embed=None
            )
            return

        chosen_commodity = commodity_matches[0]

        if self.mode == "cargo":
            embed = await build_cargo_embed(
                chosen_commodity,
                cargo_scu=ship_scu,
                buy_price_override=self.buy_price_override,
                auto_load_only=self.auto_load_only,
                system_filter=self.system_filter
            )
            embed.set_author(name=f"Ship: {ship_name} • {ship_scu} SCU")
            command_name = "cargo"

        elif self.mode == "route":
            embed = await build_route_embed(
                chosen_commodity,
                auto_load_only=self.auto_load_only,
                system_filter=self.system_filter,
                cargo_scu=ship_scu,
                ship_name=ship_name
            )
            command_name = "route"

        else:
            await interaction.edit_original_response(
                content="Unknown ship dropdown mode.",
                view=None
            )
            return

        await interaction.edit_original_response(content=None, embed=embed, view=None)

        try:
            msg = await interaction.original_response()
            await log_star_command_usage(interaction, command_name, message=msg)
        except Exception as e:
            print(f"[ERROR] Failed to log final ship dropdown output ({command_name}): {e}")

class TerminalDropdown(discord.ui.View):
    def __init__(self, results: list[dict]):
        super().__init__(timeout=90)
        self.results = results

        options = []
        for item in results[:25]:
            label = str(item.get("name") or "Unknown Terminal").strip()

            # fall back to other stable identifiers if id is missing
            option_value = str(
                item.get("id")
                or item.get("slug")
                or item.get("code")
                or label
            ).strip()

            if not label or not option_value:
                continue

            system_name = (
                item.get("star_system_name")
                or item.get("system_name")
                or item.get("name_star_system")
                or "Unknown"
            )

            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=option_value[:100],
                    description=system_name[:100]
                )
            )

        # Discord select must have 1-25 options
        if not options:
            options.append(
                discord.SelectOption(
                    label="No valid terminal options found",
                    value="none"
                )
            )
        else:
            options.append(discord.SelectOption(label="None of these", value="none"))

        select = discord.ui.Select(
            placeholder="Choose a terminal…",
            options=options[:25],
            min_values=1,
            max_values=1
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        value = self.children[0].values[0]

        if value == "none":
            await interaction.response.edit_message(content="Selection cancelled.", view=None)
            return

        chosen = next(
            (
                x for x in self.results
                if str(
                    x.get("id")
                    or x.get("slug")
                    or x.get("code")
                    or x.get("name")
                    or ""
                ).strip() == value
            ),
            None
        )

        if not chosen:
            await interaction.response.edit_message(content="Could not find that terminal.", view=None)
            return

        await interaction.response.defer()

        terminal_id = chosen.get("id")
        if not terminal_id:
            await interaction.edit_original_response(
                content="That terminal does not expose a usable terminal ID in the API.",
                view=None
            )
            return

        data = await _uex_get("commodities_prices", params={"id_terminal": terminal_id})

        if not data:
            await interaction.edit_original_response(
                content="No trade data found for this terminal.",
                view=None
            )
            return

        buy = [c for c in data if c.get("price_buy")]
        sell = [c for c in data if c.get("price_sell")]

        system_name = (
            chosen.get("star_system_name")
            or chosen.get("system_name")
            or chosen.get("name_star_system")
            or "Unknown"
        )

        embed = discord.Embed(
            title=f"🏪 {chosen.get('name', 'Unknown Terminal')}",
            description=f"Available trading commodities • {system_name}",
            color=0x3498DB
        )

        if buy:
            lines = [f"{c.get('commodity_name', 'Unknown')} — `{c.get('price_buy', '—')}`" for c in buy[:10]]
            embed.add_field(name="Buys", value="\n".join(lines), inline=False)

        if sell:
            lines = [f"{c.get('commodity_name', 'Unknown')} — `{c.get('price_sell', '—')}`" for c in sell[:10]]
            embed.add_field(name="Sells", value="\n".join(lines), inline=False)

        await interaction.edit_original_response(content=None, embed=embed, view=None)

        try:
            msg = await interaction.original_response()
            await log_star_command_usage(interaction, "terminal", message=msg)
        except Exception as e:
            print(f"[ERROR] Failed to log final terminal dropdown output: {e}")

async def build_bestnow_embed(
    auto_load_only: bool = False,
    system_filter: str | None = None,
    cargo_scu: int | None = None,
    ship_name: str | None = None
) -> discord.Embed:
    ranked = await _uex_get("commodities_ranking")
    terminals = await get_all_terminals()
    wanted_system = (system_filter or "").strip().lower()

    def is_terminal_auto_load(terminal_name: str) -> bool | None:
        terminal_info = find_terminal_info(terminals, terminal_name)
        if not terminal_info:
            return None
        val = terminal_info.get("is_auto_load")
        if val in (1, "1", True):
            return True
        if val in (0, "0", False):
            return False
        return None

    def auto_icon(value):
        if value is True:
            return "✅"
        if value is False:
            return "❌"
        return "❔"

    embed = discord.Embed(
        title="💰 Best Trade Right Now",
        color=0x2ECC71
    )

    if not isinstance(ranked, list) or not ranked:
        embed.description = "No commodity ranking data found."
        embed.set_footer(text="Star Citizen — UEX")
        return embed

    # Try top ranked commodities first, then find their best route.
    ranked = sorted(
        ranked,
        key=lambda x: float(x.get("cax_score") or 0),
        reverse=True
    )[:40]

    best = None

    for commodity in ranked:
        commodity_id = commodity.get("id") or commodity.get("id_commodity")
        if not commodity_id:
            continue

        routes = await get_commodity_routes(commodity_id, max_rows=10)
        if not routes:
            continue

        for r in routes:
            origin = (
                r.get("terminal_origin_name")
                or r.get("origin_terminal_name")
                or r.get("from_terminal_name")
                or "Unknown Origin"
            )
            destination = (
                r.get("terminal_destination_name")
                or r.get("destination_terminal_name")
                or r.get("to_terminal_name")
                or "Unknown Destination"
            )

            origin_info = find_terminal_info(terminals, origin)
            destination_info = find_terminal_info(terminals, destination)

            origin_system = terminal_system_name(origin_info)
            destination_system = terminal_system_name(destination_info)

            origin_auto = is_terminal_auto_load(origin)
            destination_auto = is_terminal_auto_load(destination)

            if auto_load_only and not (origin_auto is True and destination_auto is True):
                continue

            if wanted_system and not (
                origin_system.lower() == wanted_system
                and destination_system.lower() == wanted_system
            ):
                continue

            profit = float(r.get("profit") or r.get("profit_total") or 0)
            if profit <= 0:
                continue

            candidate = {
                "commodity": (
                    commodity.get("name")
                    or r.get("commodity_name")
                    or r.get("name_commodity")
                    or "Unknown Commodity"
                ),
                "origin": origin,
                "destination": destination,
                "origin_system": origin_system,
                "destination_system": destination_system,
                "origin_auto": origin_auto,
                "destination_auto": destination_auto,
                "profit": profit,
            }

            if best is None or candidate["profit"] > best["profit"]:
                best = candidate

    if not best:
        embed.description = "No profitable trade found matching your filters."
        footer_bits = ["Star Citizen — UEX", "✅ Auto-load", "❌ No auto-load", "❔ Unknown"]
        if auto_load_only:
            footer_bits.append("Auto-load only")
        if system_filter:
            footer_bits.append(f"System: {system_filter}")
        embed.set_footer(text=" • ".join(footer_bits))
        return embed

    line = (
        f"**{best['commodity']}** — Buy at **[{best['origin_system']}] {best['origin']} {auto_icon(best['origin_auto'])}** "
        f"→ Sell at **[{best['destination_system']}] {best['destination']} {auto_icon(best['destination_auto'])}** "
        f"for **{int(best['profit']):,} aUEC/SCU profit**"
    )

    if cargo_scu:
        full_profit = int(best["profit"] * int(cargo_scu))
        line += f" • **Full load:** `{full_profit:,}` aUEC"

    embed.description = line

    if cargo_scu and ship_name:
        embed.set_author(name=f"Ship: {ship_name} • {cargo_scu} SCU")
    elif cargo_scu:
        embed.set_author(name=f"Cargo: {cargo_scu} SCU")

    footer_bits = ["Star Citizen — UEX", "✅ Auto-load", "❌ No auto-load", "❔ Unknown"]
    if auto_load_only:
        footer_bits.append("Auto-load only")
    if system_filter:
        footer_bits.append(f"System: {system_filter}")

    embed.set_footer(text=" • ".join(footer_bits))
    return embed
        
async def search_terminal_uex(query: str) -> list[dict]:
    terminals = await get_all_terminals()
    if not isinstance(terminals, list):
        return []

    raw_query = (query or "").strip()
    q_norm = _normalize_sc_name(raw_query)
    if not q_norm:
        return []

    exact = []
    partial = []

    for terminal in terminals:
        name = str(terminal.get("name", "")).strip()
        code = str(terminal.get("code", "")).strip()

        system_name = (
            terminal.get("star_system_name")
            or terminal.get("system_name")
            or terminal.get("name_star_system")
            or ""
        )

        names = [name, code, system_name]
        norm_names = [_normalize_sc_name(n) for n in names if n]

        if any(q_norm == n for n in norm_names):
            exact.append(terminal)
        elif any(q_norm in n for n in norm_names):
            partial.append(terminal)

    def dedupe(items: list[dict]) -> list[dict]:
        seen = set()
        out = []
        for t in items:
            tid = str(t.get("id") or t.get("name") or "")
            if tid in seen:
                continue
            seen.add(tid)
            out.append(t)
        return out

    exact = dedupe(exact)
    if exact:
        return exact[:25]

    partial = dedupe(partial)
    if partial:
        return partial[:25]

    # fuzzy fallback
    choices = []
    choice_to_terminal = {}

    for terminal in terminals:
        possible_names = [
            str(terminal.get("name", "")).strip(),
            str(terminal.get("code", "")).strip(),
            str(terminal.get("star_system_name", "")).strip(),
            str(terminal.get("system_name", "")).strip(),
            str(terminal.get("name_star_system", "")).strip(),
        ]

        for n in possible_names:
            if not n:
                continue
            choices.append(n)
            choice_to_terminal[n] = terminal

    fuzzy_matches = process.extract(raw_query, choices, scorer=fuzz.token_sort_ratio, limit=25)

    results = []
    seen = set()

    for matched_name, score in fuzzy_matches:
        if score < 75:
            continue

        terminal = choice_to_terminal[matched_name]
        tid = str(terminal.get("id") or terminal.get("name") or "")
        if tid in seen:
            continue

        seen.add(tid)
        results.append(terminal)

    return results[:25]

async def terminal_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    if not current or not current.strip():
        return []

    try:
        matches = await search_terminal_uex(current)
    except Exception as e:
        print(f"[ERROR] terminal_autocomplete failed: {e}")
        return []

    choices = []
    seen = set()

    for terminal in matches[:25]:
        name = str(terminal.get("name") or "").strip()
        if not name:
            continue

        key = name.lower()
        if key in seen:
            continue
        seen.add(key)

        system_name = (
            terminal.get("star_system_name")
            or terminal.get("system_name")
            or terminal.get("name_star_system")
            or ""
        )

        label = name
        if system_name:
            label = f"{name} ({system_name})"

        choices.append(
            app_commands.Choice(
                name=label[:100],
                value=name[:100]
            )
        )

    return choices

def _to_float(value, default=0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _best_sell_row(rows: list[dict]) -> dict | None:
    sell_rows = [r for r in rows if _to_float(r.get("price_sell")) > 0]
    if not sell_rows:
        return None
    return max(sell_rows, key=lambda r: _to_float(r.get("price_sell")))

async def build_trending_embed(limit: int = 10) -> discord.Embed:
    items = await get_trending_commodities(limit=limit)

    embed = discord.Embed(
        title="📈 Trending Commodities",
        description="Most traded commodities by average monthly SCU volume.",
        color=0xF1C40F
    )

    if not items:
        embed.add_field(
            name="No Data",
            value="No trending commodity data was returned by UEX.",
            inline=False
        )
        embed.set_footer(text="Star Citizen — UEX")
        return embed

    lines = []
    for idx, item in enumerate(items, start=1):
        name = item.get("name", "Unknown")
        code = item.get("code", "—")
        buy_scu = int(item.get("_buy_scu_avg_month", 0))
        sell_scu = int(item.get("_sell_scu_avg_month", 0))
        total_scu = int(item.get("_total_scu_avg_month", 0))
        cax_score = item.get("cax_score", "—")

        lines.append(
            f"**{idx}. {name}** (`{code}`)\n"
            f"Buy: `{buy_scu:,}` SCU/mo • Sell: `{sell_scu:,}` SCU/mo • Total: `{total_scu:,}` SCU/mo • CAX: `{cax_score}`"
        )

    field_text = ""
    for line in lines:
        if len(field_text) + len(line) + 2 > 1024:
            break
        field_text += line + "\n\n"

    embed.add_field(name="Top Commodities", value=field_text.strip(), inline=False)
    embed.set_footer(text="Star Citizen — UEX")
    return embed


async def build_cargo_embed(
    commodity: dict,
    cargo_scu: int,
    buy_price_override: float | None = None,
    auto_load_only: bool = False,
    system_filter: str | None = None
) -> discord.Embed:
    commodity_id = commodity.get("id") or commodity.get("id_commodity")
    commodity_name = commodity.get("name", "Unknown Commodity")

    prices = await get_commodity_prices(commodity_id)
    rows = prices if isinstance(prices, list) else []
    terminals = await get_all_terminals()
    wanted_system = (system_filter or "").strip().lower()

    def is_terminal_auto_load(terminal_name: str) -> bool | None:
        terminal_info = find_terminal_info(terminals, terminal_name)
        if not terminal_info:
            return None
        val = terminal_info.get("is_auto_load")
        if val in (1, "1", True):
            return True
        if val in (0, "0", False):
            return False
        return None

    def terminal_matches_system(terminal_name: str) -> bool:
        if not wanted_system:
            return True
        terminal_info = find_terminal_info(terminals, terminal_name)
        system_name = terminal_system_name(terminal_info).lower()
        return system_name == wanted_system

    embed = discord.Embed(
        title=f"📦 Cargo Calculator — {commodity_name}",
        color=0xF1C40F
    )

    if not rows:
        embed.description = "No price data found for this commodity."
        embed.set_footer(text="Star Citizen — UEX")
        return embed

    if auto_load_only:
        rows = [
            r for r in rows
            if is_terminal_auto_load(r.get("terminal_name") or r.get("name_terminal") or "") is True
        ]
    
    if wanted_system:
        rows = [
            r for r in rows
            if terminal_matches_system(r.get("terminal_name") or r.get("name_terminal") or "")
        ]
    
    min_required_scu = cargo_scu * 0.75
    
    sell_candidates = [
        r for r in rows
        if _to_float(r.get("price_sell")) > 0
        and _to_float(r.get("scu_sell")) >= min_required_scu
    ]
    
    buy_rows = [
        r for r in rows
        if _to_float(r.get("price_buy")) > 0
        and _to_float(r.get("scu_buy")) >= min_required_scu
    ]
    
    best_sell = _best_sell_row(sell_candidates)

    if not best_sell or not buy_rows:
        embed.description = (
            f"Not enough buy/sell data to calculate cargo profit.\n"
            f"Locations must support at least `{min_required_scu:,.0f}` SCU "
            f"(75% of your `{cargo_scu}` SCU ship)."
        )
        footer_bits = ["Star Citizen — UEX"]
        if auto_load_only:
            footer_bits.append("Auto-load only")
        if wanted_system:
            footer_bits.append(f"System: {system_filter}")
        embed.set_footer(text=" • ".join(footer_bits))
        return embed

    sell_terminal = (
        best_sell.get("terminal_name")
        or best_sell.get("name_terminal")
        or "Unknown"
    )
    sell_info = find_terminal_info(terminals, sell_terminal)
    sell_system = terminal_system_name(sell_info)
    sell_price = _to_float(best_sell.get("price_sell"))

    if buy_price_override is not None:
        buy_price = float(buy_price_override)
        buy_terminal = "Manual price"
        buy_system = system_filter if system_filter else "N/A"
    else:
        best_buy = min(buy_rows, key=lambda r: _to_float(r.get("price_buy"), 999999999))
        buy_terminal = (
            best_buy.get("terminal_name")
            or best_buy.get("name_terminal")
            or "Unknown"
        )
        buy_info = find_terminal_info(terminals, buy_terminal)
        buy_system = terminal_system_name(buy_info)
        buy_price = _to_float(best_buy.get("price_buy"))

    profit_per_scu = sell_price - buy_price
    total_cost = buy_price * cargo_scu
    total_sale = sell_price * cargo_scu
    total_profit = profit_per_scu * cargo_scu

    embed.add_field(name="Cargo Size", value=f"`{cargo_scu}` SCU", inline=True)
    embed.add_field(name="Buy Price", value=f"`{buy_price:,.2f}` aUEC/SCU", inline=True)
    embed.add_field(name="Sell Price", value=f"`{sell_price:,.2f}` aUEC/SCU", inline=True)

    if buy_price_override is not None:
        buy_location_text = f"[{buy_system}] {buy_terminal}"
    else:
        buy_available = _to_float(best_buy.get("scu_buy"))
        buy_location_text = (
            f"[{buy_system}] {buy_terminal}\n"
            f"Available to buy: `{buy_available:,.0f}` SCU"
        )
    
    sell_capacity = _to_float(best_sell.get("scu_sell"))
    sell_location_text = (
        f"[{sell_system}] {sell_terminal}\n"
        f"Sell capacity: `{sell_capacity:,.0f}` SCU"
    )
    
    embed.add_field(
        name="Buy Location",
        value=buy_location_text,
        inline=False
    )
    embed.add_field(
        name="Best Sell Location",
        value=sell_location_text,
        inline=False
    )

    embed.add_field(name="Profit / SCU", value=f"`{profit_per_scu:,.2f}` aUEC", inline=True)
    embed.add_field(name="Total Cost", value=f"`{total_cost:,.2f}` aUEC", inline=True)
    embed.add_field(name="Total Profit", value=f"`{total_profit:,.2f}` aUEC", inline=True)

    footer_bits = ["Star Citizen — UEX"]
    if auto_load_only:
        footer_bits.append("Auto-load only")
    if wanted_system:
        footer_bits.append(f"System: {system_filter}")
    embed.set_footer(text=" • ".join(footer_bits))

    return embed

# Safe interaction helpers
async def safe_interaction_edit(interaction, embed, view):
    try:
        if interaction.response.is_done():
            return await interaction.edit_original_response(embed=embed, view=view)
        else:
            return await interaction.response.edit_message(embed=embed, view=view)
    except Exception as e:
        print(f"[ERROR] Failed to safely edit interaction: {e}")
        return None

async def safe_interaction_respond(interaction: discord.Interaction, **kwargs):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)
            return await interaction.original_response()
    except Exception as e:
        print(f"[ERROR] Failed to respond to interaction: {e}")
        return None

async def send_temporary_message(destination, content=None, embed=None, view=None, delay=60):
    try:
        # Ask Discord to return the actual message object
        if view:
            message = await destination.send(content=content, embed=embed, view=view, wait=True)
        else:
            message = await destination.send(content=content, embed=embed, wait=True)

        # Auto-delete after X seconds
        await asyncio.sleep(delay)
        await message.delete()
    except Exception as e:
        print(f"[ERROR] Failed to auto-delete message: {e}")

async def log_command_output(
    interaction: discord.Interaction,
    command_name: str,
    message: discord.Message = None,
    extra_text: str = None
):
    archive_channel = client.get_channel(LOG_CHANNEL_ID)
    if not archive_channel:
        print(f"[WARN] Archive channel not found for ID {LOG_CHANNEL_ID}")
        return

    embed = discord.Embed(
        title=f"📦 Command Archive: /{command_name}",
        color=discord.Color.dark_grey()
    )
    embed.add_field(name="User", value=f"{interaction.user.name}", inline=False)
    embed.add_field(name="Used In", value=f"{interaction.channel.mention}", inline=False)
    embed.add_field(name="Timestamp", value=discord.utils.format_dt(interaction.created_at, style='F'), inline=False)

    if message:
        if message.embeds:
            for em in message.embeds:
                await archive_channel.send(
                    content=f"📥 /{command_name} by {interaction.user.name} in {interaction.channel.mention}:",
                    embed=em
                )
        elif message.content:
            embed.add_field(name="Output", value=message.content[:1000], inline=False)
            await archive_channel.send(embed=embed)
    elif extra_text:
        embed.add_field(name="Output", value=extra_text[:1000], inline=False)
        await archive_channel.send(embed=embed)

class ClubDropdownView(discord.ui.View):
    def __init__(self, interaction, options, club_data):
        super().__init__()
        self.add_item(ClubDropdown(interaction, options, club_data))

class StatsDropdown(discord.ui.View):
    def __init__(self, results: list[dict]):
        super().__init__(timeout=90)
        self.results = results
        options = [
            discord.SelectOption(label=r["clubInfo"]["name"], value=str(r["clubInfo"]["clubId"]))
            for r in results[:25]
        ]
        options.append(discord.SelectOption(label="None of these", value="none"))

        select = discord.ui.Select(placeholder="Choose a club…", options=options, min_values=1, max_values=1)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        value = self.children[0].values[0]
        if value == "none":
            msg = await interaction.response.edit_message(content="Selection cancelled.", view=None)
            asyncio.create_task(delete_after_delay(msg, 60))
            return

        chosen = next((c for c in self.results if str(c["clubInfo"]["clubId"]) == str(value)), None)
        if not chosen:
            msg = await interaction.response.edit_message(content="Could not find that club.", view=None)
            asyncio.create_task(delete_after_delay(msg, 60))
            return

        club_id = str(chosen["clubInfo"]["clubId"])
        club_name = chosen["clubInfo"]["name"]

        await interaction.response.defer()

        # turn the dropdown message → loading text
        loading_msg = await interaction.edit_original_response(content="⏳ Fetching club stats…", view=None)

        # fetch + render
        data = await fetch_all_stats_for_club(club_id)
        embed = build_stats_embed(club_id, club_name, data)

        view = PrintRecordButton(
            {
                "matchesPlayed": data["stats"].get("matchesPlayed"),
                "wins": data["stats"].get("wins"),
                "draws": data["stats"].get("draws"),
                "losses": data["stats"].get("losses"),
                "skillRating": data["stats"].get("skillRating"),
            },
            (club_name or f"Club {club_id}").upper(),
        )
        final_msg = await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=view,
        )

        record_club_search(
            interaction.guild,
            interaction.user,
        )

        await log_command_output(interaction, "stats", final_msg)

        # 🔔 auto-delete the final embed after N seconds
        asyncio.create_task(delete_after_delay(final_msg, 60))

class FreeStatsDropdown(discord.ui.View):
    def __init__(self, results: list[dict], original_query: str, request_message: discord.Message):
        super().__init__(timeout=90)
        self.results = results
        self.original_query = original_query
        self.request_message = request_message

        options = [
            discord.SelectOption(label=r["clubInfo"]["name"], value=str(r["clubInfo"]["clubId"]))
            for r in results[:25]
        ]
        options.append(discord.SelectOption(label="None of these", value="none"))

        select = discord.ui.Select(placeholder="Choose a club…", options=options, min_values=1, max_values=1)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        value = self.children[0].values[0]
        if value == "none":
            # 🔵 LOG: user cancelled the selection
            #await log_free_stats(interaction.message, query=self.original_query, resolved="cancelled")

            msg = await interaction.response.edit_message(content="Selection cancelled.", view=None)
            asyncio.create_task(delete_after_delay(msg, 60))
            return

        chosen = next((c for c in self.results if str(c["clubInfo"]["clubId"]) == str(value)), None)
        if not chosen:
            # 🔵 LOG: selection not found (edge case)
            #await log_free_stats(interaction.message, query=self.original_query, resolved="selection not found")

            msg = await interaction.response.edit_message(content="Could not find that club.", view=None)
            asyncio.create_task(delete_after_delay(msg, 60))
            return

        club_id = str(chosen["clubInfo"]["clubId"])
        club_name = chosen["clubInfo"]["name"]

        # 🔵 LOG: final selection resolved
        #await log_free_stats(interaction.message, query=self.original_query, resolved=f"{club_name} (ID {club_id})")

        await interaction.response.defer()

        # turn the dropdown message → loading text
        loading_msg = await interaction.edit_original_response(content="⏳ Fetching club stats…", view=None)

       # fetch + render
        data = await fetch_all_stats_for_club(club_id)
        embed = build_stats_embed(club_id, club_name, data)
        
        # 🔵 NEW: mirror the card to your log channel with a header like "/stats by ... in #..."
        await log_stats_embed_for_request(
            guild=self.request_message.guild,
            author=self.request_message.author,
            origin_channel=self.request_message.channel,
            embed=embed
        )
        
        view = PrintRecordButton(
            {
                "matchesPlayed": data["stats"].get("matchesPlayed"),
                "wins": data["stats"].get("wins"),
                "draws": data["stats"].get("draws"),
                "losses": data["stats"].get("losses"),
                "skillRating": data["stats"].get("skillRating"),
            },
            (club_name or f"Club {club_id}").upper()
        )
        final_msg = await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=view,
        )

        record_club_search(
            self.request_message.guild,
            self.request_message.author,
        )

        asyncio.create_task(delete_after_delay(final_msg, 60))

class Stats5Dropdown(discord.ui.View):
    def __init__(self, results: list[dict]):
        super().__init__(timeout=90)
        self.results = results

        options = [
            discord.SelectOption(
                label=r["clubInfo"]["name"],
                value=str(r["clubInfo"]["clubId"])
            )
            for r in results[:25]
        ]
        options.append(discord.SelectOption(label="None of these", value="none"))

        select = discord.ui.Select(
            placeholder="Choose a club…",
            options=options,
            min_values=1,
            max_values=1
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        value = self.children[0].values[0]

        if value == "none":
            msg = await interaction.response.edit_message(content="Selection cancelled.", view=None)
            asyncio.create_task(delete_after_delay(msg, 60))
            return

        chosen = next((c for c in self.results if str(c["clubInfo"]["clubId"]) == str(value)), None)
        if not chosen:
            msg = await interaction.response.edit_message(content="Could not find that club.", view=None)
            asyncio.create_task(delete_after_delay(msg, 60))
            return

        club_id = str(chosen["clubInfo"]["clubId"])
        club_name = chosen["clubInfo"]["name"]

        await interaction.response.defer()
        await interaction.edit_original_response(content="⏳ Fetching last 5 player totals…", view=None)

        embeds = await build_stats5_embeds(club_id, club_name)
        if not embeds:
            final_msg = await interaction.edit_original_response(
                content="No recent matches found for this club.",
                embed=None,
                view=None
            )
            asyncio.create_task(delete_after_delay(final_msg, 60))
            return

        # first page edits the original message
        final_msg = await interaction.edit_original_response(content=None, embed=embeds[0], view=None)
        await log_command_output(interaction, "stats5", final_msg)
        asyncio.create_task(delete_after_delay(final_msg, 60))

        # extra pages are sent as followups
        for extra_embed in embeds[1:]:
            extra_msg = await interaction.followup.send(embed=extra_embed)
            asyncio.create_task(delete_after_delay(extra_msg, 60))
        
class LastMatchDropdown(discord.ui.Select):
    def __init__(self, interaction, options, club_data):
        self.interaction = interaction
        self.club_data = club_data
        super().__init__(
            placeholder="Select the correct club...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if self.values[0] == "none":
            await interaction.message.edit(content="Okay, request cancelled.", view=None)
            async def delete_after_cancel():
                await asyncio.sleep(60)
                try:
                    await interaction.message.delete()
                except Exception as e:
                    print(f"[ERROR] Failed to auto-delete cancel message: {e}")
            asyncio.create_task(delete_after_cancel())
            return

        chosen = self.values[0]
        selected = next((c for c in self.club_data if str(c['clubInfo']['clubId']) == chosen), None)
        if not selected:
            await interaction.message.edit(content="Club data could not be found.", view=None)
            return

        await handle_lastmatch(interaction, chosen, from_dropdown=True, original_message=interaction.message)

class LastMatchDropdownView(discord.ui.View):
    def __init__(self, interaction, options, club_data):
        super().__init__()
        self.add_item(LastMatchDropdown(interaction, options, club_data))

class Last5Dropdown(discord.ui.Select):
    def __init__(self, options, club_data):
        self.club_data = club_data
        super().__init__(
            placeholder="Select the correct club...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

        chosen = self.values[0]

        if self.values[0] == "none":
            await interaction.message.edit(content="Okay, request cancelled.", view=None)
            async def delete_after_cancel():
                await asyncio.sleep(60)
                try:
                    await interaction.message.delete()
                except Exception as e:
                    print(f"[ERROR] Failed to auto-delete cancel message: {e}")
            asyncio.create_task(delete_after_cancel())
            return

        club_name = next((c["clubInfo"]["name"] for c in self.club_data if str(c["clubInfo"]["clubId"]) == chosen), "Club")
        await fetch_and_display_last5(interaction, chosen, club_name, original_message=interaction.message)

class Last5DropdownView(discord.ui.View):
    def __init__(self, options, club_data):
        super().__init__(timeout=180)
        self.add_item(Last5Dropdown(options, club_data))


async def fetch_and_display_last5(interaction, club_id, club_name="Club", original_message=None):
    club_id = str(club_id)

    match_types = ["leagueMatch", "playoffMatch", "friendlyMatch"]
    matches = []

    for match_type in match_types:
        data = await _ea_get_json(
            "https://proclubs.ea.com/api/fc/clubs/matches",
            {"matchType": match_type, "platform": PLATFORM, "clubIds": club_id},
        ) or []
        for m in data:
            m["_matchType"] = match_type  # keep track of type
        matches.extend(data)

    matches.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    last_5 = matches[:5]

    if not last_5:
        await interaction.followup.send("No recent matches found.")
        return

    # Build embed once
    embed = discord.Embed(
        title=f"📅 {club_name.upper()}'s Last 5",
        color=discord.Color.blue()
    )

    # ✅ Crest thumbnail (proper indentation, no duplicate embed)
    crest_asset_id = await get_crest_asset_id_for_club(club_id)
    crest_url = build_crest_url(crest_asset_id) if crest_asset_id else None
    if crest_url:
        embed.set_thumbnail(url=crest_url)

    for idx, match in enumerate(last_5, 1):
        clubs = match.get("clubs", {}) or {}
        club_data = clubs.get(str(club_id)) or {}
        opponent_id = next((cid for cid in clubs if cid != str(club_id)), None)
        opponent_data = clubs.get(opponent_id) if opponent_id else {}

        opponent_name = (
            (opponent_data.get("details") or {}).get("name")
            or opponent_data.get("name")
            or "Unknown"
        )

        our_score = int(club_data.get("goals", 0))
        opponent_score = int(opponent_data.get("goals", 0)) if opponent_data else 0

        result = "✅" if our_score > opponent_score else "❌" if our_score < opponent_score else "➖"

        raw_type = match.get("_matchType") or match.get("matchType")
        label = MATCH_TYPE_LABELS.get(raw_type, raw_type or "Unknown")

        # (Optional) put emoji first for alignment:
        # name=f"{idx}⃣ {result} {label} — vs {opponent_name}",
        embed.add_field(
            name=f"{idx}⃣ {result} [{label}] vs {opponent_name}",
            value=f"Score: {our_score}-{opponent_score}",
            inline=False
        )

    if original_message:
        await original_message.edit(content=None, embed=embed, view=None)
        asyncio.create_task(delete_after_delay(original_message))
        await log_command_output(interaction, "last5", original_message)
    else:
        message = await interaction.followup.send(embed=embed)
        await log_command_output(interaction, "last5", message)
        asyncio.create_task(delete_after_delay(message))


async def delete_after_delay(message, delay=60):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception as e:
        print(f"[ERROR] Failed to auto-delete message: {e}")

def _position_options_from_lp(lp: dict, selected_index: int | None = None) -> list[discord.SelectOption]:
    opts: list[discord.SelectOption] = []
    for idx, pos in enumerate(lp.get("positions", [])):
        status = "Assigned" if pos.get("user_id") else "Unassigned"
        opts.append(discord.SelectOption(
            label=pos["code"],
            description=status,
            value=str(idx),
            default=(selected_index is not None and idx == selected_index)
        ))
    return opts

class PositionSelect(discord.ui.Select):
    def __init__(self, lp: dict):
        self.lp = lp
        super().__init__(
            placeholder="Choose a position to assign...",
            options=_position_options_from_lp(lp),
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        view: "LineupAssignView" = self.view  # type: ignore
    
        if getattr(view, "_formation_change_mode", False):
            await interaction.response.send_message("Pick a new formation first.", ephemeral=True)
            return
    
        view.current_index = int(self.values[0])
    
        # Keep the picked position visible + selected
        pos_code = self.lp["positions"][view.current_index]["code"]
        self.placeholder = f"Position: {pos_code}"
        self.options = _position_options_from_lp(self.lp, selected_index=view.current_index)
    
        await interaction.response.edit_message(view=view)

class PlayerSelect(discord.ui.UserSelect):
    def __init__(self, lp: dict):
        self.lp = lp
        super().__init__(placeholder="Pick a player for the selected position", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view: "LineupAssignView" = self.view  # type: ignore
        position_index = view.current_index
        positions = self.lp.get("positions", [])
        if not (0 <= position_index < len(positions)):
            await interaction.response.send_message("No position selected. Pick a position first.", ephemeral=True)
            return

        picked: discord.Member = self.values[0]  # type: ignore

        # Role enforcement (if lineup has role_id)
        role_id = self.lp.get("role_id")
        if role_id:
            has_role = any(r.id == role_id for r in picked.roles)
            if not has_role:
                await interaction.response.send_message(
                    f"❌ {picked.mention} doesn't have the required role <@&{role_id}>.",
                    ephemeral=True
                )
                return

class FormationSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=f, value=f) for f in FORMATIONS.keys()]
        super().__init__(
            placeholder="Select a new formation…",
            options=options,
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        view: "LineupAssignView" = self.view  # type: ignore
        new_formation = self.values[0]

        # FormationSelect should ONLY apply the formation.
        # apply_new_formation() should rebuild positions, clear assignments, save, and refresh the embed/view.
        await view.apply_new_formation(interaction, new_formation)

class RoleMemberSelect(discord.ui.Select):
    def __init__(self, lp: dict, members: list[discord.Member], page: int = 0, per_page: int = 25):
        self.lp = lp
        self.members = members
        self.page = page
        self.per_page = per_page

        start = page * per_page
        chunk = members[start:start + per_page]

        options = [
            discord.SelectOption(
                label=m.display_name[:100],
                value=str(m.id),
                description=(m.top_role.name if m.top_role else "Member"),
            )
            for m in chunk
        ] or [discord.SelectOption(label="No eligible members", value="none", description=" ")]
        
        super().__init__(
            placeholder="Pick a player with the required role",
            min_values=1,
            max_values=1,
            options=options,
            disabled=(options[0].value == "none"),
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("No eligible members to select.", ephemeral=True)
            return

        view: "LineupAssignView" = self.view  # type: ignore
        position_index = view.current_index
        positions = self.lp.get("positions", [])
        if not (0 <= position_index < len(positions)):
            await interaction.response.send_message("No position selected. Pick a position first.", ephemeral=True)
            return

        picked_id = int(self.values[0])
        self.lp["positions"][position_index]["user_id"] = picked_id
        self.lp["updated_at"] = datetime.now(timezone.utc).isoformat()
        lineups_store["lineups"][str(self.lp["id"])] = self.lp
        save_lineups_store()

        # Reflect assignment then reset both dropdowns to defaults
        view.refresh_position_options(keep_selected=True)
        view.current_index = None
        view.refresh_position_options(keep_selected=False)
        view._reset_player_placeholder()
        
        embed = make_lineup_embed(self.lp)
        await safe_interaction_edit(interaction, embed=embed, view=view)

async def send_stats_message_to_channel(
    channel: discord.TextChannel, club_id: str, club_name: str, *, origin_message: discord.Message | None = None
):
    data = await fetch_all_stats_for_club(club_id)
    embed = build_stats_embed(club_id, club_name, data)
    view = PrintRecordButton(
        {
            "matchesPlayed": data["stats"].get("matchesPlayed"),
            "wins": data["stats"].get("wins"),
            "draws": data["stats"].get("draws"),
            "losses": data["stats"].get("losses"),
            "skillRating": data["stats"].get("skillRating"),
        },
        (club_name or f"Club {club_id}").upper(),
    )
    msg = await channel.send(embed=embed, view=view)
    asyncio.create_task(delete_after_delay(msg, 60))

    if origin_message:
        record_club_search(
            origin_message.guild,
            origin_message.author,
        )

    # Mirror to the log channel with a header that looks like the slash command
    if origin_message:
        await log_stats_embed_for_request(
            guild=origin_message.guild,
            author=origin_message.author,
            origin_channel=origin_message.channel,
            embed=embed,
        )

async def auto_post_lineup_in_thread(ev: dict, thread: discord.Thread, formation: str):
    """
    Create a lineup inside the provided event thread, save it, and pin the message.
    Formation is REQUIRED (no default).
    """
    try:
        formation_str = (formation or "").strip()
        if formation_str not in FORMATIONS:
            raise ValueError("Formation is required and must be a valid option.")

        # Allocate lineup id
        lid = lineups_store.get("next_id", 1)

        lp = {
            "id": lid,
            "title": f"{ev.get('name')} Lineup",
            "formation": formation_str,
            "positions": _build_positions_for_formation(formation_str),
            "role_id": ev.get("role_id"),
            "channel_id": thread.id,
            "message_id": None,
            "creator_id": ev.get("creator_id"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": None,
            "finished_once": False,
            "pinged_user_ids": [],
            "kickoff_at": ev.get("datetime"),
        }

        embed = make_lineup_embed(lp)
        view = LineupAssignView(lp, editor_id=lp["creator_id"] or 0)

        sent = await thread.send(embed=embed, view=view)
        view.message = sent

        lp["message_id"] = sent.id
        lineups_store.setdefault("lineups", {})[str(lid)] = lp
        lineups_store["next_id"] = lid + 1
        save_lineups_store()

        try:
            await sent.pin(reason="Auto-pinned lineup for event thread")
        except Exception as pe:
            print(f"[WARN] Could not pin lineup message: {pe}")

    except Exception as e:
        print(f"[ERROR] auto_post_lineup_in_thread failed: {e}")

        # Allocate lineup id
        lid = lineups_store.get("next_id", 1)

        lp = {
            "id": lid,
            "title": f"{ev.get('name')} Lineup",
            "formation": formation_str,
            "positions": _build_positions_for_formation(formation_str),
            "role_id": ev.get("role_id"),
            "channel_id": thread.id,
            "message_id": None,
            "creator_id": ev.get("creator_id"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": None,
            "finished_once": False,
            "pinged_user_ids": [],
            "kickoff_at": ev.get("datetime"),  # <— use the event time if present
        }

        embed = make_lineup_embed(lp)
        view = LineupAssignView(lp, editor_id=lp["creator_id"] or 0)

        # Send lineup to the thread
        sent = await thread.send(embed=embed, view=view)
        view.message = sent

        # Persist lineup
        lp["message_id"] = sent.id
        lineups_store.setdefault("lineups", {})[str(lid)] = lp
        lineups_store["next_id"] = lid + 1
        save_lineups_store()

        # Pin it
        try:
            await sent.pin(reason="Auto-pinned lineup for event thread")
        except Exception as pe:
            print(f"[WARN] Could not pin lineup message: {pe}")

    except Exception as e:
        print(f"[ERROR] auto_post_lineup_in_thread failed: {e}")

class LineupAssignView(discord.ui.View):
    def __init__(self, lp: dict, editor_id: int, timeout: int = 600):
        super().__init__(timeout=timeout)
        self.lp = lp
        self.editor_id = editor_id
        self.current_index: int | None = None
        self.message: discord.Message | None = None

        # For role-paged select
        self._role_page = 0
        self._role_members: list[discord.Member] = []
        self._role_picker_active = False

        # Always include the position picker
        self.add_item(PositionSelect(lp))

        # Formation Changer
        self._formation_change_mode: bool = False
        self._formation_select: FormationSelect | None = None

        # Decide which player picker to use
        role_id = lp.get("role_id")
        ch = client.get_channel(lp.get("channel_id"))
        guild = ch.guild if isinstance(ch, (discord.TextChannel, discord.Thread)) else None

        if role_id and guild:
            role = guild.get_role(role_id)
            if role:
                # NOTE: Requires Server Members Intent ON and the cache to be reasonably warm.
                self._role_members = sorted(
                    [m for m in role.members if not m.bot],
                    key=lambda m: m.display_name.lower()
                )
                self._role_picker_active = True
                # Add the first page of the role-filtered select
                self.add_item(RoleMemberSelect(self.lp, self._role_members, page=self._role_page))
                # Add pager buttons
                self.add_item(self._PrevButton())
                self.add_item(self._NextButton())
            else:
                # Role not found – fallback to generic searchable picker
                self.add_item(PlayerSelect(lp))
        else:
            # No role set – fallback to generic searchable picker
            self.add_item(PlayerSelect(lp))

    # ---------- Pager helpers ----------

    def _refresh_role_select(self):
        # Remove the old RoleMemberSelect (if any) and re-add with new page
        for item in list(self.children):
            if isinstance(item, RoleMemberSelect):
                self.remove_item(item)
        self.add_item(RoleMemberSelect(self.lp, self._role_members, page=self._role_page))

    class _PrevButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label="◀️ Prev", style=discord.ButtonStyle.secondary)

        async def callback(self, interaction: discord.Interaction):
            view: "LineupAssignView" = self.view  # type: ignore
            if not view._role_picker_active:
                await interaction.response.defer()
                return
            if view._role_page > 0:
                view._role_page -= 1
                view._refresh_role_select()
            await interaction.response.edit_message(view=view)

    class _NextButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label="Next ▶️", style=discord.ButtonStyle.secondary)

        async def callback(self, interaction: discord.Interaction):
            view: "LineupAssignView" = self.view  # type: ignore
            if not view._role_picker_active:
                await interaction.response.defer()
                return
            max_page = (max(len(view._role_members) - 1, 0)) // 25
            if view._role_page < max_page:
                view._role_page += 1
                view._refresh_role_select()
            await interaction.response.edit_message(view=view)

    def _reset_player_placeholder(self):
        for child in self.children:
            if isinstance(child, PlayerSelect):
                child.placeholder = "Pick a player for the selected position"
            elif isinstance(child, RoleMemberSelect):
                child.placeholder = "Pick a player with the required role"

    def refresh_position_options(self, keep_selected: bool = False):
        """Rebuild top select; optionally keep the current selection highlighted."""
        selected = self.current_index if keep_selected else None
        for child in self.children:
            if isinstance(child, PositionSelect):
                if selected is not None:
                    pos_code = self.lp["positions"][selected]["code"]
                    child.placeholder = f"Position: {pos_code}"
                else:
                    child.placeholder = "Choose a position to assign..."
                child.options = _position_options_from_lp(self.lp, selected_index=selected)
                break

    def _set_assignment_controls_enabled(self, enabled: bool):
        """Enable/disable position/player picking (and related buttons) as a group."""
        for child in self.children:
            # Leave formation dropdown alone (handled separately)
            if isinstance(child, FormationSelect):
                continue
    
            # Disable the assignment UI while waiting for formation selection
            if isinstance(child, (PositionSelect, PlayerSelect, RoleMemberSelect, self._PrevButton, self._NextButton, discord.ui.Button)):
                # But keep the "Change Formation" button enabled so they can re-open it if needed
                if isinstance(child, discord.ui.Button) and getattr(child, "custom_id", None) == "change_formation_btn":
                    child.disabled = False
                else:
                    child.disabled = not enabled
    
    async def enter_change_formation_mode(self, interaction: discord.Interaction):
        """Clear current assignments and force selecting a new formation before assigning again."""
        # Clear all assigned players
        for p in self.lp.get("positions", []):
            p["user_id"] = None
        self.current_index = None
    
        self.lp["updated_at"] = datetime.now(timezone.utc).isoformat()
        lineups_store["lineups"][str(self.lp["id"])] = self.lp
        save_lineups_store()
    
        # Add dropdown if missing
        if not any(isinstance(c, FormationSelect) for c in self.children):
            self._formation_select = FormationSelect()
            # Put it at the top-ish so it’s obvious
            self.add_item(self._formation_select)
    
        self._formation_change_mode = True
        self._set_assignment_controls_enabled(False)
    
        embed = make_lineup_embed(self.lp)
        # Optional: add a hint line
        embed.description = (embed.description or "") + "\n\n⚠️ **Pick a new formation to continue.**"
        await safe_interaction_edit(interaction, embed=embed, view=self)
    
    async def apply_new_formation(self, interaction: discord.Interaction, formation: str):
        """Apply a formation, rebuild positions, remove formation dropdown, re-enable assignments."""
        formation = (formation or "").strip()
        if formation not in FORMATIONS:
            await interaction.response.send_message("❌ Invalid formation.", ephemeral=True)
            return
    
        # Set new formation + rebuild positions (all unassigned)
        self.lp["formation"] = formation
        self.lp["positions"] = _build_positions_for_formation(formation)
        self.lp["updated_at"] = datetime.now(timezone.utc).isoformat()
    
        lineups_store["lineups"][str(self.lp["id"])] = self.lp
        save_lineups_store()
    
        # Exit formation-change mode
        self._formation_change_mode = False
        self.current_index = None
    
        # Remove the formation dropdown from the view
        for child in list(self.children):
            if isinstance(child, FormationSelect):
                self.remove_item(child)
    
        # Refresh position dropdown options for the new positions list
        self.refresh_position_options(keep_selected=False)
        self._reset_player_placeholder()
    
        # Re-enable assignment controls
        self._set_assignment_controls_enabled(True)
    
        embed = make_lineup_embed(self.lp)
        await safe_interaction_edit(interaction, embed=embed, view=self)
        
                
    # ---------- Permissions + your existing buttons ----------

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        member = interaction.user if isinstance(interaction.user, discord.Member) else (guild.get_member(interaction.user.id) if guild else None)
        ok = has_admin_role(member) if member else False
        if not ok:
            await interaction.response.send_message("❌ Only **Administrators** can use the lineup controls.", ephemeral=True)
        return ok

    @discord.ui.button(label="Clear Selected", style=discord.ButtonStyle.secondary)
    async def clear_selected(self, interaction: discord.Interaction, button: discord.ui.Button):
        idx = self.current_index
        if idx is None:
            await interaction.response.send_message("Pick a position first.", ephemeral=True)
            return
        if 0 <= idx < len(self.lp.get("positions", [])):
            self.lp["positions"][idx]["user_id"] = None
            self.lp["updated_at"] = datetime.now(timezone.utc).isoformat()
            lineups_store["lineups"][str(self.lp["id"])] = self.lp
            save_lineups_store()
    
            self.refresh_position_options()
    
        embed = make_lineup_embed(self.lp)
        await safe_interaction_edit(interaction, embed=embed, view=self)

    @discord.ui.button(label="Clear All", style=discord.ButtonStyle.danger)
    async def clear_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        # If nothing is assigned, tell the user and bail
        if not any(p.get("user_id") for p in self.lp.get("positions", [])):
            await interaction.response.send_message("Nothing to clear — all positions are already unassigned.", ephemeral=True)
            return
    
        # Clear every assignment
        for p in self.lp.get("positions", []):
            p["user_id"] = None
    
        # Persist + timestamp
        self.lp["updated_at"] = datetime.now(timezone.utc).isoformat()
        lineups_store["lineups"][str(self.lp["id"])] = self.lp
        save_lineups_store()
    
        # Reset picker state and refresh the position menu so descriptions show "Unassigned"
        self.current_index = None
        self.refresh_position_options(False)
        self._reset_player_placeholder()
        
        # Update the embed in-place
        embed = make_lineup_embed(self.lp)
        await safe_interaction_edit(interaction, embed=embed, view=self)

    @discord.ui.button(label="Change Formation", style=discord.ButtonStyle.primary, custom_id="change_formation_btn")
    async def change_formation(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.enter_change_formation_mode(interaction)

    @discord.ui.button(label="Finish", style=discord.ButtonStyle.success)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Backfill for older lineups
        self.lp.setdefault("pinged_user_ids", [])
        first_time = not self.lp.get("finished_once", False)

        # 1) Update embed & remove controls
        embed = make_lineup_embed(self.lp)
        await safe_interaction_edit(interaction, embed=embed, view=None)

        # 2) Ensure ✅ reaction is present
        try:
            msg = interaction.message or self.message
            if msg:
                try:
                    await msg.add_reaction("✅")
                except Exception:
                    pass
        except Exception:
            pass

        # 3) Build assigned list (deduped in order)
        assigned_ids: list[int] = []
        for p in self.lp.get("positions", []):
            uid = p.get("user_id")
            if uid and uid not in assigned_ids:
                assigned_ids.append(uid)
        
        already_pinged = set(self.lp.get("pinged_user_ids", []))
        to_ping = assigned_ids if first_time else [u for u in assigned_ids if u not in already_pinged]
        
        # 4) Send finalize/update message with pings (if there’s anyone to ping)
        if to_ping:
            title = self.lp.get("title") or f"{self.lp.get('formation')} Lineup"
            header = "finalized" if first_time else "updated"
            content = (
                f"📣 **{title}** {header}. Please confirm with ✅\n"
                + " ".join(f"<@{u}>" for u in to_ping)
            )
        
            allowed = discord.AllowedMentions(
                users=[discord.Object(id=u) for u in to_ping],
                roles=False, everyone=False, replied_user=False
            )
        
            try:
                ch = (
                    interaction.message.channel if getattr(interaction, "message", None)
                    else self.message.channel if self.message
                    else interaction.channel
                )
                await ch.send(content=content, allowed_mentions=allowed)
            except Exception:
                # If sending fails (missing perms, etc.), just skip gracefully
                pass
        
        # 5) Persist state regardless (so second press becomes "updated")
        self.lp["finished_once"] = True
        if to_ping:
            self.lp["pinged_user_ids"] = list(already_pinged.union(to_ping))
        lineups_store["lineups"][str(self.lp["id"])] = self.lp
        save_lineups_store()

# -------------------------
# Twitch API helpers
# -------------------------
async def _twitch_fetch_app_token() -> dict:
    """
    Client Credentials flow -> {"access_token", "expires_at"}.
    """
    global _twitch_token

    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        raise RuntimeError("TWITCH_CLIENT_ID/SECRET not set (check Railway env vars)")

    token_url = "https://id.twitch.tv/oauth2/token"
    form = {
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials",
    }

    headers = {
        "User-Agent": "omitS-DiscordBot/1.0",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=15, headers=headers) as c:
        r = await c.post(token_url, data=form)

        # 👇 THIS is the important change
        if r.status_code != 200:
            raise RuntimeError(f"Twitch token failed {r.status_code}: {r.text}")

        data = r.json()
        expires_in = int(data.get("expires_in", 3600))

        _twitch_token = {
            "access_token": data["access_token"],
            # refresh 60s early
            "expires_at": datetime.now(timezone.utc)
            + timedelta(seconds=max(expires_in - 60, 0)),
        }

        return _twitch_token

async def _twitch_get_app_token_str() -> str:
    global _twitch_token
    if _twitch_token is None or datetime.now(timezone.utc) >= _twitch_token["expires_at"]:
        await _twitch_fetch_app_token()
    return _twitch_token["access_token"]

async def _twitch_api_get(path: str, params: dict) -> dict:
    token = await _twitch_get_app_token_str()
    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}",
        "User-Agent": "omitS-DiscordBot/1.0",
    }
    url = f"https://api.twitch.tv/helix{path}"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url, headers=headers, params=params)
        if r.status_code == 401:
            await _twitch_fetch_app_token()
            headers["Authorization"] = f"Bearer {_twitch_token['access_token']}"
            r = await c.get(url, headers=headers, params=params)
        r.raise_for_status()
        return r.json()

async def twitch_get_stream_by_login(login: str) -> dict | None:
    data = await _twitch_api_get("/streams", {"user_login": login})
    arr = data.get("data", [])
    return arr[0] if arr else None

async def twitch_get_game_box_art_url(game_id: str | None) -> str | None:
    if not game_id:
        return None
    data = await _twitch_api_get("/games", {"id": game_id})
    arr = data.get("data", [])
    if not arr:
        return None
    raw = arr[0].get("box_art_url")
    return raw.replace("{width}", "285").replace("{height}", "380") if raw else None

# - /lastmatch & alias
async def handle_lastmatch(interaction: discord.Interaction, club: str, from_dropdown: bool = False, original_message=None):
    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
    except Exception as e:
        print(f"[WARN] Could not defer interaction: {e}")

    try:
        # Resolve club ID
        if club.isdigit():
            club_id = club
        else:
            valid_clubs = await search_clubs_ea(club)
            if not valid_clubs:
                await send_temporary_message(interaction.followup, content="No matching clubs found.", delay=15)
                return
            if len(valid_clubs) > 1 and not from_dropdown:
                options = [
                    discord.SelectOption(label=c["clubInfo"]["name"], value=str(c["clubInfo"]["clubId"]))
                    for c in valid_clubs[:25]
                ]
                options.append(discord.SelectOption(label="None of these", value="none"))
                view = LastMatchDropdownView(interaction, options, valid_clubs)
                await interaction.followup.send("Multiple clubs found. Please select:", view=view)
                return
            club_id = str(valid_clubs[0]["clubInfo"]["clubId"]) if valid_clubs else club

        # Pull matches
        match_types = ["leagueMatch", "playoffMatch", "friendlyMatch"]
        matches = []
        for match_type in match_types:
            data = await _ea_get_json(
                "https://proclubs.ea.com/api/fc/clubs/matches",
                {"matchType": match_type, "platform": PLATFORM, "clubIds": club_id},
            ) or []
            for m in data:
                m["_matchType"] = match_type
            matches.extend(data)

        if not matches:
            await interaction.followup.send("No matches found for this club.")
            return

        matches.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        last_match = matches[0]

        raw_type = last_match.get("_matchType") or last_match.get("matchType")
        label = MATCH_TYPE_LABELS.get(raw_type, raw_type or "Unknown")

        clubs = last_match.get("clubs", {}) or {}
        our_id = next(
            (cid for cid in clubs if str(cid) == str(club_id)),
            None,
        )
        club_data = clubs.get(our_id) if our_id is not None else None
        opponent_id = next(
            (cid for cid in clubs if str(cid) != str(club_id)),
            None,
        )
        opponent_data = clubs.get(opponent_id) if opponent_id else {}

        our_name = club_data.get("details", {}).get("name", club_data.get("name", "Unknown")) if club_data else "Unknown"
        opponent_name = opponent_data.get("details", {}).get("name", opponent_data.get("name", "Unknown")) if opponent_data else "Unknown"
        our_score = int(club_data.get("goals", 0)) if club_data else 0
        opponent_score = int(opponent_data.get("goals", 0)) if opponent_data else 0

        result_emoji = "✅" if our_score > opponent_score else "❌" if our_score < opponent_score else "➖"
        result_text = "Win" if our_score > opponent_score else "Loss" if our_score < opponent_score else "Draw"

        embed = discord.Embed(
            title=f"📅 {our_name.upper()} — LAST MATCH",
            description=(
                f"**{label}** · vs **{escape_markdown(opponent_name)}**\n"
                f"{result_emoji} **{result_text.upper()}** · "
                f"**{our_score}–{opponent_score}**"
            ),
            color=discord.Color.green() if our_score > opponent_score else discord.Color.red() if our_score < opponent_score else discord.Color.gold()
        )

        # ✅ ADD THIS BLOCK (right here, same indent level)
        crest_asset_id = await get_crest_asset_id_for_club(str(club_id))
        crest_url = build_crest_url(crest_asset_id) if crest_asset_id else None
        if crest_url:
            embed.set_thumbnail(url=crest_url)

        # Position-aware player sections. Each statistic group gets its own
        # line so the layout remains readable on narrow mobile screens.
        players_by_club = last_match.get("players", {}) or {}
        player_club_id = next(
            (cid for cid in players_by_club if str(cid) == str(club_id)),
            None,
        )
        club_players = (
            players_by_club.get(player_club_id, {})
            if player_club_id is not None
            else {}
        )
        players_data = [
            player
            for player in club_players.values()
            if isinstance(player, dict)
        ]

        grouped_players = {
            "Forwards": [],
            "Midfielders": [],
            "Defenders": [],
            "Goalkeepers": [],
            "Players": [],
        }
        for player in players_data:
            grouped_players[_player_position_group(player)].append(player)

        section_icons = {
            "Forwards": "⚽",
            "Midfielders": "🎯",
            "Defenders": "🛡️",
            "Goalkeepers": "🧤",
            "Players": "👤",
        }

        for group_name, group_players in grouped_players.items():
            if not group_players:
                continue

            group_players.sort(
                key=lambda player: float(_to_number(player.get("rating")) or 0),
                reverse=True,
            )
            rows = [
                _format_last_match_player(player, group_name)
                for player in group_players
            ]

            chunks = []
            current_rows = []
            for row in rows:
                candidate_rows = [*current_rows, row]
                if current_rows and len("\n\n".join(candidate_rows)) > 1024:
                    chunks.append(current_rows)
                    current_rows = []
                current_rows.append(row)
            if current_rows:
                chunks.append(current_rows)

            for chunk_index, chunk_rows in enumerate(chunks):
                continuation = " — continued" if chunk_index else ""
                embed.add_field(
                    name=(
                        f"{section_icons[group_name]} "
                        f"{group_name}{continuation}"
                    ),
                    value="\n\n".join(chunk_rows),
                    inline=False,
                )

        embed.add_field(
            name="📊 Team Totals",
            value=_format_last_match_team_totals(
                players_data,
                our_score,
                opponent_score,
            ),
            inline=False,
        )
        embed.set_footer(text="EAFC — Latest recorded club match")

        if from_dropdown and original_message:
            await original_message.edit(content=None, embed=embed, view=None)
            await log_command_output(interaction, "lastmatch", original_message)
            async def delete_after_timeout():
                await asyncio.sleep(60)
                try:
                    await original_message.delete()
                except Exception as e:
                    print(f"[ERROR] Failed to auto-delete dropdown message: {e}")
            asyncio.create_task(delete_after_timeout())
        else:
            message = await interaction.followup.send(embed=embed)
            await log_command_output(interaction, "lastmatch", message)
            async def delete_after_timeout():
                await asyncio.sleep(60)
                try:
                    await message.delete()
                except Exception as e:
                    print(f"[ERROR] Failed to auto-delete lastmatch message: {e}")
            asyncio.create_task(delete_after_timeout())

    except Exception as e:
        print(f"[ERROR] Failed to fetch last match: {e}")
        await send_temporary_message(interaction.followup, content="An error occurred while fetching opponent stats.")

@tree.command(name="lastmatch", description="Show the last match stats for a club.")
@app_commands.describe(club="Club name or club ID")
async def lastmatch_command(interaction: discord.Interaction, club: str):
    await handle_lastmatch(interaction, club, from_dropdown=False, original_message=None)

@tree.command(name="lm", description="Alias for /lastmatch")
@app_commands.describe(club="Club name or club ID")
async def lm_command(interaction: discord.Interaction, club: str):
    await handle_lastmatch(interaction, club, from_dropdown=False, original_message=None)


# - /roast
def _funstat_minutes(player: dict) -> int:
    seconds = int(
        _to_number(
            player.get("secondsPlayed", player.get("gameTime", 0))
        )
        or 0
    )
    return round(seconds / 60) if seconds > 0 else 0


def _format_funstat_evidence(player: dict) -> str:
    """Show the fullest useful set of documented match-performance data."""
    group = _player_position_group(player)
    name = escape_markdown(_player_display_name(player))
    rating = _match_rating(player)
    minutes = _funstat_minutes(player)
    goals = int(_to_number(player.get("goals")) or 0)
    assists = int(_to_number(player.get("assists")) or 0)
    shots = int(_to_number(player.get("shots")) or 0)
    passes_made = int(_to_number(player.get("passesmade")) or 0)
    pass_attempts = int(_to_number(player.get("passattempts")) or 0)
    pass_pct = round((passes_made / pass_attempts) * 100) if pass_attempts else 0
    tackles_made = int(_to_number(player.get("tacklesmade")) or 0)
    tackle_attempts = int(_to_number(player.get("tackleattempts")) or 0)
    tackle_pct = round((tackles_made / tackle_attempts) * 100) if tackle_attempts else 0
    red_cards = int(_to_number(player.get("redcards")) or 0)
    yellow_cards = int(_to_number(player.get("yellowcards")) or 0)
    player_of_match = int(_to_number(player.get("mom")) or 0)
    potm_text = "Yes" if player_of_match else "No"

    lines = [f"**{name}**"]
    if minutes:
        lines.append(f"`Minutes {minutes} · Rating {rating}`")
    else:
        lines.append(f"`Rating {rating}`")
    lines.append(f"`Player of Match: {potm_text}`")

    if group == "Goalkeepers":
        saves = int(_to_number(player.get("saves")) or 0)
        conceded = int(_to_number(player.get("goalsconceded")) or 0)
        clean_sheets = int(_to_number(player.get("cleansheetsgk")) or 0)
        dive_saves = int(_to_number(player.get("ballDiveSaves")) or 0)
        reflex_saves = int(_to_number(player.get("reflexSaves")) or 0)
        parry_saves = int(_to_number(player.get("parrySaves")) or 0)
        cross_saves = int(_to_number(player.get("crossSaves")) or 0)
        punch_saves = int(_to_number(player.get("punchSaves")) or 0)
        direction_saves = int(_to_number(player.get("goodDirectionSaves")) or 0)
        lines.extend([
            f"`Saves {saves} · Conceded {conceded}`",
            f"`Clean sheets {clean_sheets}`",
            f"`Dive {dive_saves} · Reflex {reflex_saves} · Parry {parry_saves}`",
            f"`Cross {cross_saves} · Punch {punch_saves} · Direction {direction_saves}`",
            f"`Yellow cards {yellow_cards} · Red cards {red_cards}`",
        ])
        return "\n".join(lines)

    lines.append(f"`Goals {goals} · Assists {assists} · Shots {shots}`")
    lines.append(
        f"`Passes {passes_made}/{pass_attempts} · Success {pass_pct}%`"
    )
    if group in ("Midfielders", "Defenders"):
        lines.append(
            f"`Tackles {tackles_made}/{tackle_attempts} · Success {tackle_pct}%`"
        )

    clean_sheets = int(
        _to_number(
            player.get(
                "cleansheetsdef" if group == "Defenders" else "cleansheetsany"
            )
        )
        or 0
    )
    if clean_sheets:
        lines.append(f"`Clean sheets {clean_sheets}`")
    lines.append(
        f"`Yellow cards {yellow_cards} · Red cards {red_cards}`"
    )
    return "\n".join(lines)


def _funstat_badness(player: dict) -> int:
    """Return a simple football-performance roast score."""
    score = 0
    rating = float(_to_number(player.get("rating")) or 0)
    goals = int(_to_number(player.get("goals")) or 0)
    assists = int(_to_number(player.get("assists")) or 0)
    shots = int(_to_number(player.get("shots")) or 0)
    red_cards = int(_to_number(player.get("redcards")) or 0)
    yellow_cards = int(_to_number(player.get("yellowcards")) or 0)
    passes_made = int(_to_number(player.get("passesmade")) or 0)
    pass_attempts = int(_to_number(player.get("passattempts")) or 0)
    tackles_made = int(_to_number(player.get("tacklesmade")) or 0)
    tackle_attempts = int(_to_number(player.get("tackleattempts")) or 0)
    conceded = int(_to_number(player.get("goalsconceded")) or 0)
    saves = int(_to_number(player.get("saves")) or 0)
    player_of_match = int(_to_number(player.get("mom")) or 0)
    minutes = _funstat_minutes(player)
    clean_sheet = max(
        int(_to_number(player.get("cleansheetsany")) or 0),
        int(_to_number(player.get("cleansheetsdef")) or 0),
        int(_to_number(player.get("cleansheetsgk")) or 0),
    )
    group = _player_position_group(player)

    rating_thresholds = {
        # (very poor, poor, below par). Defensive positions naturally receive
        # fewer attacking rating boosts, so their thresholds are lower.
        "Forwards": (6.2, 6.7, 7.1),
        "Midfielders": (6.0, 6.5, 7.0),
        "Defenders": (5.8, 6.3, 6.8),
        "Goalkeepers": (5.8, 6.3, 6.8),
        "Players": (6.0, 6.5, 7.0),
    }
    very_poor, poor, below_par = rating_thresholds.get(
        group,
        rating_thresholds["Players"],
    )

    if rating and rating < very_poor:
        score += 5
    elif rating and rating < poor:
        score += 4
    elif rating and rating < below_par:
        score += 2

    if red_cards:
        score += 5
    elif yellow_cards:
        score += 1

    if pass_attempts >= 5:
        pass_pct = (passes_made / pass_attempts) * 100
        if pass_pct < 60:
            score += 4
        elif pass_pct < 70:
            score += 2

    if tackle_attempts >= 2:
        tackle_pct = (tackles_made / tackle_attempts) * 100
        if tackle_pct < 40:
            score += 3
        elif tackle_pct < 60:
            score += 2

    if group == "Forwards" and goals == 0 and assists == 0:
        score += 1
    if shots >= 3 and goals == 0:
        score += 2
    if group == "Goalkeepers":
        if conceded >= 3:
            score += 3
        if conceded > 0 and saves == 0:
            score += 2

        shots_faced = saves + conceded
        save_pct = (saves / shots_faced) * 100 if shots_faced else 100
        if shots_faced >= 3 and save_pct < 50:
            score += 2

    # Positive contributions protect a player from being labelled poor solely
    # because of one weaker metric.
    score -= min(goals * 2, 4)
    score -= min(assists, 2)
    score -= min(clean_sheet * (2 if group in ("Defenders", "Goalkeepers") else 1), 2)

    if group == "Defenders" and tackle_attempts >= 2:
        tackle_pct = (tackles_made / tackle_attempts) * 100
        if tackle_pct >= 85:
            score -= 2
        elif tackle_pct >= 70:
            score -= 1

    if minutes and minutes < 30:
        score -= 2
    elif minutes and minutes < 60:
        score -= 1

    if player_of_match:
        score -= 5

    return max(score, 0)


def _funstat_roast_lines_legacy(
    player: dict,
    teammates: list[dict],
) -> list[str]:
    name = escape_markdown(_player_display_name(player))
    rating = float(_to_number(player.get("rating")) or 0)
    goals = int(_to_number(player.get("goals")) or 0)
    assists = int(_to_number(player.get("assists")) or 0)
    shots = int(_to_number(player.get("shots")) or 0)
    red_cards = int(_to_number(player.get("redcards")) or 0)
    passes_made = int(_to_number(player.get("passesmade")) or 0)
    pass_attempts = int(_to_number(player.get("passattempts")) or 0)
    tackles_made = int(_to_number(player.get("tacklesmade")) or 0)
    tackle_attempts = int(_to_number(player.get("tackleattempts")) or 0)
    conceded = int(_to_number(player.get("goalsconceded")) or 0)
    saves = int(_to_number(player.get("saves")) or 0)
    minutes = _funstat_minutes(player)
    player_of_match = int(_to_number(player.get("mom")) or 0)
    group = _player_position_group(player)

    lines = []

    if rating and rating < 6.5:
        lines.extend([
            f"A **{rating:.1f}** rating — the match engine considered issuing a missing-person report for **{name}**.",
            f"**{name}** earned a **{rating:.1f}**. Technically present, statistically questionable.",
            f"That **{rating:.1f}** rating is less ‘player of the match’ and more ‘person near the match’.",
        ])
    elif rating and rating < 7.0:
        lines.extend([
            f"A **{rating:.1f}** rating: not a disaster, but nobody is framing the match report.",
            f"**{name}** finished on **{rating:.1f}** — aggressively average with a hint of danger.",
        ])

    if pass_attempts >= 5:
        pass_pct = round((passes_made / pass_attempts) * 100)
        if pass_pct < 70:
            lines.extend([
                f"Passing finished at **{pass_pct}%**. Several teammates are still looking for the ball.",
                f"With **{passes_made}/{pass_attempts}** passes completed, possession was treated as a temporary arrangement.",
                f"At **{pass_pct}%** passing, the opposition received excellent service.",
            ])

    if tackle_attempts >= 2:
        tackle_pct = round((tackles_made / tackle_attempts) * 100)
        if tackle_pct < 60:
            lines.extend([
                f"Only **{tackles_made}/{tackle_attempts}** tackles landed. The attackers mostly experienced a guided tour.",
                f"A **{tackle_pct}%** tackle rate — more social distancing than defending.",
            ])

    if shots >= 3 and goals == 0:
        lines.extend([
            f"**{shots}** shots and no goals. The corner flags were under more threat than the goalkeeper.",
            f"After **{shots}** attempts without scoring, the goal may need to be made wider next time.",
        ])

    if group == "Forwards" and goals == 0 and assists == 0:
        lines.append(
            "A forward with no goal or assist — an impressively convincing spectator role."
        )

    if red_cards:
        lines.extend([
            "The red card was a bold tactical decision to give everyone else more space.",
            "Leaving early was efficient, although the manager probably meant after full-time.",
        ])

    if group == "Goalkeepers" and conceded >= 3:
        lines.append(
            f"**{conceded}** conceded — the goal spent the match operating an open-door policy."
        )
    if group == "Goalkeepers" and conceded > 0 and saves == 0:
        lines.append("Zero saves. At least the net got plenty of touches.")
    if group == "Goalkeepers":
        shots_faced = saves + conceded
        save_pct = round((saves / shots_faced) * 100) if shots_faced else 100
        if shots_faced >= 3 and save_pct < 50:
            lines.extend([
                f"A **{save_pct}%** save rate — the gloves appear to have been mainly decorative.",
                f"Only **{saves}** of **{shots_faced}** shots were stopped. The net had the busier afternoon.",
            ])

    if minutes >= 85 and goals == 0 and assists == 0 and rating < 6.5:
        lines.extend([
            f"After **{minutes} minutes**, the main contribution was helping the clock reach full-time.",
            f"They had **{minutes} minutes** to change the match and chose consistency instead.",
        ])

    # This normally prevents a roast through the badness score, but retaining
    # the check keeps manually selected POTM performances fair.
    if player_of_match:
        return [
            f"**{name}** was Player of the Match. VAR has cancelled the roast for lack of evidence."
        ]

    other_players = [
        teammate
        for teammate in teammates
        if teammate is not player
    ]
    if other_players:
        best_teammate = max(
            other_players,
            key=lambda teammate: float(_to_number(teammate.get("rating")) or 0),
        )
        best_name = escape_markdown(_player_display_name(best_teammate))
        best_rating = float(_to_number(best_teammate.get("rating")) or 0)
        if best_rating >= rating + 1.0:
            lines.extend([
                f"Meanwhile, **{best_name}** posted **{best_rating:.1f}** and may request separate changing facilities.",
                f"For comparison, **{best_name}** managed **{best_rating:.1f}** in the very same match.",
                f"**{best_name}** reached **{best_rating:.1f}**, proving it was not the pitch, weather or controller batteries.",
            ])

        selected_contributions = goals + assists
        best_contributor = max(
            other_players,
            key=lambda teammate: (
                int(_to_number(teammate.get("goals")) or 0)
                + int(_to_number(teammate.get("assists")) or 0)
            ),
        )
        best_contributions = (
            int(_to_number(best_contributor.get("goals")) or 0)
            + int(_to_number(best_contributor.get("assists")) or 0)
        )
        if selected_contributions == 0 and best_contributions > 0:
            contributor_name = escape_markdown(
                _player_display_name(best_contributor)
            )
            lines.append(
                f"**{contributor_name}** supplied **{best_contributions}** goal contribution(s); "
                f"**{name}** supplied moral support."
            )

        passing_teammates = []
        for teammate in other_players:
            teammate_attempts = int(
                _to_number(teammate.get("passattempts")) or 0
            )
            teammate_made = int(_to_number(teammate.get("passesmade")) or 0)
            if teammate_attempts >= 5:
                passing_teammates.append(
                    (
                        teammate,
                        round((teammate_made / teammate_attempts) * 100),
                        teammate_attempts,
                    )
                )
        if pass_attempts >= 5 and passing_teammates:
            selected_pass_pct = round((passes_made / pass_attempts) * 100)
            best_passing_teammate, best_pass_pct, _ = max(
                passing_teammates,
                key=lambda item: (item[1], item[2]),
            )
            if best_pass_pct >= selected_pass_pct + 15:
                passer_name = escape_markdown(
                    _player_display_name(best_passing_teammate)
                )
                lines.append(
                    f"**{passer_name}** passed at **{best_pass_pct}%** while "
                    f"**{name}** managed **{selected_pass_pct}%** — same match, different sport."
                )

        tackling_teammates = []
        for teammate in other_players:
            teammate_attempts = int(
                _to_number(teammate.get("tackleattempts")) or 0
            )
            teammate_made = int(_to_number(teammate.get("tacklesmade")) or 0)
            if teammate_attempts >= 2:
                tackling_teammates.append(
                    (
                        teammate,
                        round((teammate_made / teammate_attempts) * 100),
                        teammate_attempts,
                    )
                )
        if tackle_attempts >= 2 and tackling_teammates:
            selected_tackle_pct = round(
                (tackles_made / tackle_attempts) * 100
            )
            best_tackling_teammate, best_tackle_pct, _ = max(
                tackling_teammates,
                key=lambda item: (item[1], item[2]),
            )
            if best_tackle_pct >= selected_tackle_pct + 20:
                tackler_name = escape_markdown(
                    _player_display_name(best_tackling_teammate)
                )
                lines.append(
                    f"**{tackler_name}** won **{best_tackle_pct}%** of their tackles; "
                    f"**{name}** answered with **{selected_tackle_pct}%** and optimism."
                )

    if not lines:
        lines.append(
            f"The numbers refuse to cooperate: **{name}** did not provide enough evidence for a proper roasting."
        )

    # Avoid repeating the same type of joke while keeping every invocation random.
    return random.sample(lines, k=min(3, len(lines)))


# Remember recently used rendered lines for each player. This prevents the
# same small group of jokes appearing every time the command is used.
FUNSTAT_RECENT_LINES: dict[str, list[str]] = {}
FUNSTAT_RECENT_PLAYERS: dict[str, list[str]] = {}


def _funstat_choose_player(
    club_id: str | int,
    candidates: list[dict],
) -> dict:
    """Rotate eligible players so repeated calls do not target one person."""
    club_key = str(club_id)
    recent = FUNSTAT_RECENT_PLAYERS.setdefault(club_key, [])

    fresh_candidates = [
        candidate
        for candidate in candidates
        if _player_display_name(candidate).casefold() not in recent
    ]

    if not fresh_candidates:
        recent.clear()
        fresh_candidates = candidates[:]

    chosen = random.choice(fresh_candidates)
    chosen_key = _player_display_name(chosen).casefold()
    recent.append(chosen_key)

    # Remember enough names to rotate a normal Pro Clubs starting eleven,
    # while allowing the pool to recover when the eligible squad changes.
    del recent[:-10]
    return chosen


def _funstat_pick_fresh_lines(
    player_key: str,
    categories: list[list[str]],
    count: int = 3,
) -> list[str]:
    recent = FUNSTAT_RECENT_LINES.setdefault(player_key.casefold(), [])
    shuffled_categories = [category[:] for category in categories if category]
    random.shuffle(shuffled_categories)

    selected = []
    for category in shuffled_categories:
        fresh = [line for line in category if line not in recent]
        if fresh:
            selected.append(random.choice(fresh))
        if len(selected) >= count:
            break

    # If every applicable line has recently appeared, progressively release
    # the oldest history rather than repeating the latest response.
    if len(selected) < count:
        all_lines = [line for category in shuffled_categories for line in category]
        while len(selected) < count and recent:
            recent.pop(0)
            available = [
                line
                for line in all_lines
                if line not in recent and line not in selected
            ]
            if available:
                selected.append(random.choice(available))

    if len(selected) < count:
        remaining = [
            line
            for category in shuffled_categories
            for line in category
            if line not in selected
        ]
        random.shuffle(remaining)
        selected.extend(remaining[: count - len(selected)])

    recent.extend(selected)
    del recent[:-30]
    return selected


def _funstat_roast_lines(
    player: dict,
    teammates: list[dict],
    our_score: int,
    opponent_score: int,
) -> list[str]:
    name = escape_markdown(_player_display_name(player))
    player_key = _player_display_name(player)
    rating = float(_to_number(player.get("rating")) or 0)
    goals = int(_to_number(player.get("goals")) or 0)
    assists = int(_to_number(player.get("assists")) or 0)
    shots = int(_to_number(player.get("shots")) or 0)
    red_cards = int(_to_number(player.get("redcards")) or 0)
    passes_made = int(_to_number(player.get("passesmade")) or 0)
    pass_attempts = int(_to_number(player.get("passattempts")) or 0)
    tackles_made = int(_to_number(player.get("tacklesmade")) or 0)
    tackle_attempts = int(_to_number(player.get("tackleattempts")) or 0)
    conceded = int(_to_number(player.get("goalsconceded")) or 0)
    saves = int(_to_number(player.get("saves")) or 0)
    minutes = _funstat_minutes(player)
    player_of_match = int(_to_number(player.get("mom")) or 0)
    group = _player_position_group(player)

    if player_of_match:
        return _funstat_pick_fresh_lines(player_key, [[
            f"**{name}** won Player of the Match. The roast writer has been sent home early.",
            f"Player of the Match belongs to **{name}**. Even sarcasm has to respect the evidence.",
            f"The match awarded **{name}** POTM, so the complaint desk is currently closed.",
            f"**{name}** has the POTM award. Find another suspect; this one has an alibi.",
            f"A Player of the Match roast was attempted, but the statistics filed an appeal and won.",
        ]], count=1)

    categories: list[list[str]] = []

    if rating and rating < 6.5:
        categories.append([
            f"A **{rating:.1f}** rating: **{name}** had the impact of a ‘skip intro’ button that did not work.",
            f"**{name}** finished on **{rating:.1f}** — roughly the football equivalent of a supermarket trolley with one bad wheel.",
            f"At **{rating:.1f}**, **{name}** was less a match participant and more background scenery.",
            f"That **{rating:.1f}** rating has all the ambition of a phone battery stuck on one percent.",
            f"**{name}** posted **{rating:.1f}**. A cardboard cut-out would have offered similar positional discipline.",
            f"A **{rating:.1f}** performance: not quite invisible, but close enough to trigger the motion sensor twice.",
            f"The match gave **{name}** a **{rating:.1f}**; the local traffic cone has asked for a trial.",
            f"**{rating:.1f}** is the sort of rating normally found next to a one-star delivery review.",
            f"**{name}** earned **{rating:.1f}**, bringing all the urgency of somebody browsing the reduced aisle.",
            f"The **{rating:.1f}** suggests **{name}** attended the match mainly for the group photo.",
            f"On **{rating:.1f}**, **{name}** was football’s answer to an unplugged Wi-Fi extender.",
            f"That **{rating:.1f}** display had less influence than the fourth official’s spare pen.",
        ])
    elif rating and rating < 7.0:
        categories.append([
            f"A **{rating:.1f}** rating: perfectly adequate if the objective was to avoid being remembered.",
            f"**{name}** reached **{rating:.1f}** — the statistical equivalent of plain toast.",
            f"At **{rating:.1f}**, **{name}** delivered a performance with all the excitement of a software update.",
            f"That **{rating:.1f}** will not cause a crisis, but it will not make the highlights either.",
            f"**{name}** scored **{rating:.1f}**: safely parked between useful and suspicious.",
            f"A **{rating:.1f}** performance — football happened nearby and **{name}** occasionally acknowledged it.",
            f"The rating says **{rating:.1f}**; the eye test says ‘maybe next match’.",
            f"**{name}** was rated **{rating:.1f}**, which is fine in the same way airport coffee is fine.",
        ])

    # A general real-world comparison category ensures variety even when only
    # one specific weakness qualifies.
    categories.append([
        f"**{name}** moved with the urgency of someone waiting for a kettle to boil.",
        f"The performance had the reliability of a weather app during a British bank holiday.",
        f"A sat-nav saying ‘recalculating’ contributed more clear direction than **{name}**.",
        f"**{name}** offered the cutting edge of a plastic picnic knife.",
        f"The display was as reassuring as an unexpected noise from the washing machine.",
        f"A self-checkout machine provided more assistance and asked fewer questions.",
        f"**{name}** had the presence of a parcel marked ‘delivery attempted’. Nobody saw it happen.",
        f"The performance was less Premier League and more Sunday league after a heavy Saturday.",
        f"A scarecrow covers more ground, although admittedly with less controller input.",
        f"**{name}** brought the energy of the final meeting before a long weekend.",
        f"The tactical contribution was comparable to putting an umbrella up indoors.",
        f"A deck chair would have held its position and offered somewhere useful to sit.",
        f"**{name}** operated like public Wi-Fi: visible, available and rarely connected.",
        f"The performance had fewer useful features than a chocolate teapot.",
        f"A revolving door successfully completes more transitions than that.",
        f"**{name}** looked like the ‘before’ picture in a football coaching manual.",
    ])

    if pass_attempts >= 5:
        pass_pct = round((passes_made / pass_attempts) * 100)
        if pass_pct < 70:
            categories.append([
                f"Pass completion ended at **{pass_pct}%**; Royal Mail would reject that delivery rate.",
                f"With **{pass_pct}% pass completion**, **{name}** distributed possession like free samples to the opposition.",
                f"Only **{passes_made}/{pass_attempts}** passes arrived. Even budget couriers provide better tracking.",
                f"**{name}** completed **{passes_made}/{pass_attempts}** passes, apparently using a sat-nav set to the wrong postcode.",
                f"A **{pass_pct}% pass completion rate** suggests the controller’s X button was working on commission for the other team.",
                f"With **{passes_made}/{pass_attempts}** completed, every pass became a small community raffle.",
                f"The passing map probably resembles dropped spaghetti: plenty of lines, very little direction.",
                f"At **{pass_pct}% pass completion**, teammates required binoculars and a collection point.",
                f"**{name}** treated accurate passing as optional downloadable content.",
                f"The ball left **{name}** more reliably than it reached a teammate.",
                f"With **{pass_pct}% pass completion**, possession came with a generous returns policy.",
                f"Those passes had the destination accuracy of luggage during a cancelled flight.",
            ])

    if tackle_attempts >= 2:
        tackle_pct = round((tackles_made / tackle_attempts) * 100)
        if tackle_pct < 60:
            categories.append([
                f"A **{tackle_pct}% tackle success rate**: attackers received less resistance than an automatic door.",
                f"Only **{tackles_made}/{tackle_attempts}** tackles landed; the opponents were shown around like estate viewers.",
                f"**{name}** recorded **{tackle_pct}% tackle success**, roughly the defensive strength of wet cardboard.",
                f"The tackling approach was mostly a polite suggestion to stop.",
                f"With **{tackles_made}/{tackle_attempts}** won, **{name}** defended like a password hint.",
                f"The opposition passed **{name}** with the confidence of commuters through an open ticket barrier.",
                f"A training cone would not win the ball either, but at least it keeps the correct shape.",
                f"**{name}** attempted **{tackle_attempts}** tackles and completed **{tackles_made}** — excellent customer service for attackers.",
                f"That **{tackle_pct}% tackle success rate** turned defending into a non-contact activity.",
                f"The tackles had all the stopping power of a strongly worded email.",
                f"Attackers saw **{name}** and selected ‘continue without interruption’.",
                f"The defensive plan appeared to be asking the opponent where they were going next.",
            ])

    if shots >= 3 and goals == 0:
        categories.append([
            f"**{shots}** shots without scoring; nearby advertising boards have requested protective equipment.",
            f"After **{shots}** attempts, the goal remains an unsolved geographical mystery.",
            f"**{name}** took **{shots}** shots and found everything except the net.",
            f"The shooting accuracy had the precision of throwing socks at a laundry basket from another room.",
            f"With **{shots}** empty attempts, the corner flags experienced genuine danger.",
            f"The goalkeeper faced **{shots}** shots and may still qualify for an unused-item refund.",
            f"**{name}** approached finishing like a stormtrooper on a company training day.",
            f"Those **{shots}** attempts had more destinations than a replacement bus service.",
            f"The goal is eight yards wide, but **{name}** apparently selected expert difficulty.",
            f"**{shots}** shots, zero goals and several spectators checking their car windscreens.",
            f"The finishing was sponsored by GPS: repeatedly recalculating, never arriving.",
            f"At this rate the match ball needs travel insurance, not goal-line technology.",
        ])

    if group == "Forwards" and goals == 0 and assists == 0:
        categories.append([
            f"A forward with no goal or assist: **{name}** completed the premium spectator package.",
            f"No goal and no assist; the striker’s union has requested clarification of **{name}**’s duties.",
            f"The final-third contribution matched a closed café: promising sign, nothing being served.",
            f"**{name}** returned zero goal contributions, but did occupy a shirt successfully.",
            f"The attacking output was quieter than a library’s silent-reading section.",
            f"No goals, no assists and no danger of the highlight editor working overtime.",
            f"**{name}** played forward in the geographical sense only.",
            f"The opposition defence has nominated **{name}** for employee of the month.",
        ])

    if red_cards:
        categories.append([
            "The red card turned the performance into an early-access departure.",
            f"**{name}** left before full-time like someone avoiding the car-park traffic.",
            "The tactical masterplan apparently involved creating extra space for everyone else.",
            "A red card: the fastest route from player statistics to audience statistics.",
            "The referee produced red and the team’s difficulty setting immediately increased.",
            f"**{name}** clocked out early without completing the handover.",
            "The changing room gained a new occupant while the match lost one.",
            "Leaving the pitch early was decisive, which was more than could be said for the football.",
        ])

    if group == "Goalkeepers":
        shots_faced = saves + conceded
        save_pct = round((saves / shots_faced) * 100) if shots_faced else 100
        if conceded >= 3 or (shots_faced >= 3 and save_pct < 50):
            categories.append([
                f"A **{save_pct}% save success rate** gave the net more touches than **{name}**.",
                f"**{conceded}** conceded; the goal operated with the opening hours of a 24-hour supermarket.",
                f"Only **{saves}/{shots_faced}** shots were stopped. The gloves may still be eligible for a refund.",
                "The goalkeeper’s union has classified that as ‘mostly ball retrieval’.",
                "The net enjoyed a busier shift than the person standing in front of it.",
                f"At **{save_pct}% save success**, the goal had less protection than a free antivirus trial.",
                "Opposition shots arrived like parcels and were accepted without a signature.",
                "The goal required a goalkeeper but received an enthusiastic tour guide.",
                "The clean-sheet bonus left the stadium before half-time.",
                f"**{name}** made **{saves}** saves; the scoreboard kept the more impressive total.",
            ])

    if minutes >= 85 and goals == 0 and assists == 0 and rating < 6.5:
        categories.append([
            f"**{minutes} minutes** produced the output of a five-minute substitute warming up.",
            f"After **{minutes} minutes**, **{name}** mainly helped demonstrate that time is linear.",
            f"They had **{minutes} minutes** to influence the game and spent most of them gathering evidence against it.",
            f"**{minutes} minutes** on the pitch and the highlights department still finished early.",
            f"The clock recorded **{minutes} minutes**; the statistics remain unconvinced.",
            f"A full shift from **{name}**, if the job description was ‘remain within camera range’.",
            f"In **{minutes} minutes**, a microwave could have prepared several more useful contributions.",
            f"**{name}** stayed for **{minutes} minutes**, showing admirable commitment to the experiment.",
        ])

    other_players = [teammate for teammate in teammates if teammate is not player]
    if other_players:
        best_teammate = max(
            other_players,
            key=lambda teammate: float(_to_number(teammate.get("rating")) or 0),
        )
        best_name = escape_markdown(_player_display_name(best_teammate))
        best_rating = float(_to_number(best_teammate.get("rating")) or 0)
        if best_rating >= rating + 1.0:
            categories.append([
                f"**{best_name}** reached **{best_rating:.1f}** while **{name}** managed **{rating:.1f}** — same pitch, different subscription tier.",
                f"At **{best_rating:.1f}**, **{best_name}** looked like the player; at **{rating:.1f}**, **{name}** looked like the tutorial assistant.",
                f"**{best_name}** posted **{best_rating:.1f}**, removing the pitch, weather and controller from **{name}**’s list of excuses.",
                f"The rating gap between **{best_name}** and **{name}** was large enough to require public transport.",
                f"**{best_name}** brought a **{best_rating:.1f}**; **{name}** brought a **{rating:.1f}** and presumably snacks.",
                f"Watching **{best_name}** at **{best_rating:.1f}** next to **{name}** at **{rating:.1f}** was a live before-and-after demonstration.",
                f"**{best_name}** delivered **{best_rating:.1f}**. **{name}** delivered the contrast.",
                f"The teammates shared a kit, but **{best_name}**’s **{best_rating:.1f}** suggests they did not share the same game plan.",
                f"**{best_name}** made **{best_rating:.1f}** look achievable; **{name}** made it look exclusive.",
                f"One squad contained **{best_name}** on **{best_rating:.1f}** and **{name}** on **{rating:.1f}**. Football contains multitudes.",
            ])

        best_contributor = max(
            other_players,
            key=lambda teammate: (
                int(_to_number(teammate.get("goals")) or 0)
                + int(_to_number(teammate.get("assists")) or 0)
            ),
        )
        best_contributions = (
            int(_to_number(best_contributor.get("goals")) or 0)
            + int(_to_number(best_contributor.get("assists")) or 0)
        )
        if goals + assists == 0 and best_contributions > 0:
            contributor_name = escape_markdown(_player_display_name(best_contributor))
            categories.append([
                f"**{contributor_name}** produced **{best_contributions}** goal contribution(s); **{name}** produced a convincing attendance record.",
                f"While **{contributor_name}** affected the score, **{name}** concentrated on maintaining team numbers.",
                f"**{contributor_name}** found the decisive action; **{name}** found several excellent viewing positions.",
                f"The scoreboard remembers **{contributor_name}**. The team sheet confirms **{name}** was also there.",
                f"**{contributor_name}** supplied the end product; **{name}** supplied emotional availability.",
                f"Goal contributions: **{contributor_name} {best_contributions}**, **{name} 0**. At least the shirts matched.",
            ])

        passing_teammates = []
        tackling_teammates = []
        for teammate in other_players:
            teammate_pass_attempts = int(_to_number(teammate.get("passattempts")) or 0)
            teammate_passes = int(_to_number(teammate.get("passesmade")) or 0)
            if teammate_pass_attempts >= 5:
                passing_teammates.append((
                    teammate,
                    round((teammate_passes / teammate_pass_attempts) * 100),
                    teammate_pass_attempts,
                ))
            teammate_tackle_attempts = int(_to_number(teammate.get("tackleattempts")) or 0)
            teammate_tackles = int(_to_number(teammate.get("tacklesmade")) or 0)
            if teammate_tackle_attempts >= 2:
                tackling_teammates.append((
                    teammate,
                    round((teammate_tackles / teammate_tackle_attempts) * 100),
                    teammate_tackle_attempts,
                ))

        if pass_attempts >= 5 and passing_teammates:
            selected_pct = round((passes_made / pass_attempts) * 100)
            best_player, best_pct, _ = max(passing_teammates, key=lambda item: (item[1], item[2]))
            if best_pct >= selected_pct + 15:
                passer_name = escape_markdown(_player_display_name(best_player))
                categories.append([
                    f"**{passer_name}** completed **{best_pct}% of their passes**; **{name}** answered with **{selected_pct}% pass completion** and a tracking number.",
                    f"Pass completion: **{passer_name} {best_pct}%**, **{name} {selected_pct}%**. One delivered; one left a card through the door.",
                    f"**{passer_name}** found teammates with **{best_pct}% pass completion**. **{name}** managed **{selected_pct}% pass completion**, apparently without directions.",
                    f"The same ball produced **{best_pct}% pass completion** for **{passer_name}** and **{selected_pct}% pass completion** for **{name}**. Equipment excuse denied.",
                    f"**{passer_name}** used passing lanes; **{name}** appeared to use postcode lottery results.",
                    f"At **{best_pct}% pass completion**, **{passer_name}** ran a delivery service. At **{selected_pct}% pass completion**, **{name}** ran lost property.",
                ])

        if tackle_attempts >= 2 and tackling_teammates:
            selected_pct = round((tackles_made / tackle_attempts) * 100)
            best_player, best_pct, _ = max(tackling_teammates, key=lambda item: (item[1], item[2]))
            if best_pct >= selected_pct + 20:
                tackler_name = escape_markdown(_player_display_name(best_player))
                categories.append([
                    f"**{tackler_name}** achieved **{best_pct}% tackle success**; **{name}** managed **{selected_pct}% tackle success** and several polite introductions.",
                    f"Tackle success: **{tackler_name} {best_pct}%**, **{name} {selected_pct}%**. One stopped attacks; one observed them.",
                    f"**{tackler_name}** achieved **{best_pct}% tackle success**. **{name}** offered opponents a **{100 - selected_pct}%** escape rate.",
                    f"The tackle gap between **{tackler_name}** and **{name}** could fit another midfielder.",
                    f"**{tackler_name}** defended the area; **{name}** provided directions through it.",
                    f"At **{best_pct}% tackle success**, **{tackler_name}** was a barrier. At **{selected_pct}% tackle success**, **{name}** was a suggestion.",
                ])

    if our_score > opponent_score:
        categories.append([
            f"The team still won **{our_score}–{opponent_score}**, proving group projects can survive uneven contributions.",
            f"A **{our_score}–{opponent_score}** win means the teammates successfully carried both the result and this review.",
            f"The victory arrived despite **{name}** treating the match as a supervised work-experience placement.",
            f"Fortunately, football is a team game and somebody else remembered the assignment.",
            f"The win survived, although **{name}** appeared determined to add a difficulty modifier.",
            f"Three points secured; individual accountability remains under investigation.",
        ])
    elif our_score < opponent_score:
        categories.append([
            f"In a **{our_score}–{opponent_score}** loss, **{name}** blended seamlessly into the evidence.",
            f"The scoreboard said **{our_score}–{opponent_score}** and this performance declined to offer a counterargument.",
            f"The team needed a response; **{name}** supplied an out-of-office message.",
            f"A defeat required heroes, but **{name}** had apparently booked annual leave.",
            f"The comeback plan arrived without the section containing **{name}**’s contribution.",
            f"At **{our_score}–{opponent_score}**, every useful action mattered. That made the silence louder.",
        ])
    else:
        categories.append([
            f"The match ended **{our_score}–{opponent_score}**, and **{name}** also finished perfectly balanced between impact and absence.",
            "The result was a draw; unfortunately the performance did not win any arguments either.",
            f"Nobody won the match, and **{name}** made sure nobody won this statistical debate.",
            "A draw was recorded, along with several unanswered questions about the individual contribution.",
            f"The scoreboard stayed level while **{name}** kept expectations safely below it.",
            "Honours ended even; the workload distribution may require a separate inquiry.",
        ])

    return _funstat_pick_fresh_lines(player_key, categories, count=3)


@tree.command(
    name="roast",
    description="Give a poor latest-match performance a random football roasting.",
)
@app_commands.describe(
    club="Club name or club ID",
    player="Optional player gamertag; leave blank for a random poor performer",
)
async def roast_command(
    interaction: discord.Interaction,
    club: str,
    player: str | None = None,
):
    try:
        await interaction.response.defer()

        if club.isdigit():
            club_id = club
        else:
            matches = await search_clubs_ea(club)
            if not matches:
                await send_temporary_message(
                    interaction.followup,
                    content="No matching clubs found.",
                    delay=15,
                )
                return

            exact_matches = [
                match
                for match in matches
                if str((match.get("clubInfo") or {}).get("name", "")).casefold()
                == club.strip().casefold()
            ]
            chosen_club = exact_matches[0] if exact_matches else matches[0]
            club_id = str(chosen_club["clubInfo"]["clubId"])

        all_matches = []
        for match_type in ("leagueMatch", "playoffMatch", "friendlyMatch"):
            match_data = await _ea_get_json(
                "https://proclubs.ea.com/api/fc/clubs/matches",
                {
                    "matchType": match_type,
                    "platform": PLATFORM,
                    "clubIds": club_id,
                },
            ) or []
            for match in match_data:
                match["_matchType"] = match_type
            all_matches.extend(match_data)

        if not all_matches:
            await send_temporary_message(
                interaction.followup,
                content="No matches found for this club.",
                delay=15,
            )
            return

        all_matches.sort(key=lambda match: match.get("timestamp", 0), reverse=True)
        latest_match = all_matches[0]

        clubs = latest_match.get("clubs") or {}
        our_id = next(
            (candidate_id for candidate_id in clubs if str(candidate_id) == str(club_id)),
            None,
        )
        opponent_id = next(
            (candidate_id for candidate_id in clubs if str(candidate_id) != str(club_id)),
            None,
        )
        our_club = clubs.get(our_id) if our_id is not None else {}
        opponent = clubs.get(opponent_id) if opponent_id is not None else {}

        club_name = (
            (our_club.get("details") or {}).get("name")
            or our_club.get("name")
            or f"Club {club_id}"
        )
        opponent_name = (
            (opponent.get("details") or {}).get("name")
            or opponent.get("name")
            or "Unknown"
        )
        our_score = int(our_club.get("goals", 0) or 0)
        opponent_score = int(opponent.get("goals", 0) or 0)

        players_by_club = latest_match.get("players") or {}
        player_club_id = next(
            (
                candidate_id
                for candidate_id in players_by_club
                if str(candidate_id) == str(club_id)
            ),
            None,
        )
        club_players = (
            players_by_club.get(player_club_id, {})
            if player_club_id is not None
            else {}
        )
        players = [
            candidate
            for candidate in club_players.values()
            if isinstance(candidate, dict)
        ]

        if not players:
            await send_temporary_message(
                interaction.followup,
                content="No player statistics were found for the latest match.",
                delay=15,
            )
            return

        if player:
            requested = player.strip().casefold()
            exact_players = [
                candidate
                for candidate in players
                if _player_display_name(candidate).casefold() == requested
            ]
            partial_players = [
                candidate
                for candidate in players
                if requested in _player_display_name(candidate).casefold()
            ]
            selected_player = (
                exact_players[0]
                if exact_players
                else partial_players[0] if len(partial_players) == 1 else None
            )
            if selected_player is None:
                available_names = ", ".join(
                    escape_markdown(_player_display_name(candidate))
                    for candidate in players
                )
                await send_temporary_message(
                    interaction.followup,
                    content=(
                        f"That player was not found in the latest match. "
                        f"Available players: {available_names}"
                    )[:1900],
                    delay=30,
                )
                return
        else:
            bad_candidates = [
                candidate
                for candidate in players
                if _funstat_badness(candidate) >= 2
            ]
            selected_player = _funstat_choose_player(
                club_id,
                bad_candidates or players,
            )

        badness = _funstat_badness(selected_player)
        selected_name = escape_markdown(_player_display_name(selected_player))
        selected_player_key = _player_display_name(selected_player)
        raw_type = latest_match.get("_matchType") or latest_match.get("matchType")
        match_label = MATCH_TYPE_LABELS.get(raw_type, raw_type or "Match")

        if our_score > opponent_score:
            result_emoji, result_text = "✅", "WIN"
        elif our_score < opponent_score:
            result_emoji, result_text = "❌", "LOSS"
        else:
            result_emoji, result_text = "➖", "DRAW"

        if badness < 2:
            verdict_lines = _funstat_pick_fresh_lines(
                selected_player_key,
                [[
                    f"**{selected_name}** escaped the roast: the numbers are inconveniently respectable.",
                    f"The statistics refuse to cooperate. **{selected_name}** actually did their job.",
                    f"A roast was ordered, but **{selected_name}** supplied no usable evidence.",
                    f"**{selected_name}** has been released without charge due to competent football.",
                    f"The complaint form was opened, reviewed and quietly closed again.",
                    f"No roast today. **{selected_name}** appears to have read the job description.",
                    f"The joke writer checked twice; **{selected_name}** was annoyingly effective.",
                    f"This performance is too solid to roast and too sensible to become a meme.",
                    f"**{selected_name}** survives. The numbers have provided a complete alibi.",
                    f"The sarcasm department has marked this case ‘insufficient incompetence’.",
                    f"Nothing to see here: **{selected_name}** completed a professional shift.",
                    f"The roast has been postponed until **{selected_name}** provides worse material.",
                ]],
                count=2,
            )
            embed_colour = discord.Color.green()
        else:
            verdict_lines = _funstat_roast_lines(
                selected_player,
                players,
                our_score,
                opponent_score,
            )
            embed_colour = (
                discord.Color.red()
                if badness >= 6
                else discord.Color.orange()
            )

        embed = discord.Embed(
            title=f"🔥 PLAYER ROAST — {selected_name}",
            description=(
                f"**{club_name} · {match_label}**\n"
                f"{result_emoji} **{result_text}** vs "
                f"**{escape_markdown(opponent_name)}** · "
                f"**{our_score}–{opponent_score}**"
            ),
            color=embed_colour,
        )
        embed.add_field(
            name="📋 The Evidence",
            value=_format_funstat_evidence(selected_player),
            inline=False,
        )
        embed.add_field(
            name=random.choice([
                "🔥 The Post-Match Roast",
                "😂 The Reality Check",
                "🗞️ Tomorrow’s Back Page",
                "🎤 The Dressing-Room Review",
                "📉 Performance Appraisal",
                "🧾 The Statistical Receipt",
                "🪑 From the Pundit’s Chair",
                "🥶 Cold, Hard Numbers",
            ]),
            value="\n\n".join(verdict_lines),
            inline=False,
        )

        crest_asset_id = await get_crest_asset_id_for_club(club_id)
        crest_url = build_crest_url(crest_asset_id) if crest_asset_id else None
        if crest_url:
            embed.set_thumbnail(url=crest_url)

        embed.set_footer(text="All in good fun — blame the statistics")
        message = await interaction.followup.send(embed=embed)
        await log_command_output(interaction, "roast", message)
        asyncio.create_task(delete_after_delay(message, 60))

    except Exception as e:
        print(f"[ERROR] /roast failed: {e}")
        await send_temporary_message(
            interaction.followup,
            content="An error occurred while building the fun stat.",
            delay=15,
        )

# - Top 100
class Top100View(discord.ui.View):
    def __init__(self, data, per_page=10):
        super().__init__(timeout=60)
        self.data = data
        self.per_page = per_page
        self.page = 0
        self.message = None
        self.last_played_cache: dict[str, datetime | None] = {}
        self._busy = False


    # ---------- helpers ----------
    def _set_buttons_enabled(self, enabled: bool):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = not enabled

    def _loading_embed(self) -> discord.Embed:
        page_count = (len(self.data) + self.per_page - 1) // self.per_page
        title = f"🏆 Top 100 Clubs (Page {self.page + 1}/{page_count})"
        body = "⏳ Fetching latest data…"
        subtitle = "_Navigate using the buttons below._\n\n"
        embed = discord.Embed(
            title=title,
            description=f"{subtitle}{body}",
            color=discord.Color.gold()
        )
        embed.set_footer(text="EA Pro Clubs All-Time Leaderboard")
        return embed

    def get_page_slice(self):
        start = self.page * self.per_page
        end = start + self.per_page
        return self.data[start:end]

    async def _ensure_last_played_for_page(self):
        """Fetch last-played for visible clubs if not already cached."""
        page_rows = self.get_page_slice()
        ids_needed = [
            str(club.get("clubId"))
            for club in page_rows
            if str(club.get("clubId")) not in self.last_played_cache
        ]
        if not ids_needed:
            return

        sem = asyncio.Semaphore(5)

        async def _job(cid: str):
            async with sem:
                dt = await get_last_played_timestamp(cid)
                self.last_played_cache[cid] = dt

        await asyncio.gather(*[_job(cid) for cid in ids_needed])

    def _format_row(self, club: dict) -> str:
        # data extraction
        name = club.get("name") or (club.get("clubInfo", {}) or {}).get("name") or "Unknown"
        name = md_escape(name)
        rank = club.get("rank", "—")
        sr = club.get("skillRating", club.get("skill", "—"))
        cid = str(club.get("clubId", ""))

        # optional last played
        lp = format_last_played(self.last_played_cache.get(cid))
        last_str = f" • Last Played: {lp}" if lp and lp != "—" else ""

        # two-line entry
        line1 = f"**#{rank} – {name}**"
        line2 = f"⭐ Skill Rating: {sr}{last_str}"

        return f"{line1}\n{line2}"

    async def get_embed(self):
        await self._ensure_last_played_for_page()

        page_rows = self.get_page_slice()
        description_lines = [self._format_row(c) for c in page_rows]
        body = "\n\n".join(description_lines) if description_lines else "No data."

        page_count = (len(self.data) + self.per_page - 1) // self.per_page
        title = f"🏆 Top 100 Clubs (Page {self.page + 1}/{page_count})"
        subtitle = "_Navigate using the buttons below._\n\n"

        embed = discord.Embed(
            title=title,
            description=f"{subtitle}{body}",
            color=discord.Color.gold()
        )
        embed.set_footer(text="EA Pro Clubs All-Time Leaderboard")
        return embed

    # ---------- buttons (INSIDE the class) ----------
    @discord.ui.button(label="⏮️ First", style=discord.ButtonStyle.secondary)
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._busy:
            await interaction.response.defer()
            return
        self._busy = True
        await interaction.response.defer()
        try:
            self.page = 0
            self._set_buttons_enabled(False)
            await interaction.edit_original_response(embed=self._loading_embed(), view=self)
            embed = await self.get_embed()
            self._set_buttons_enabled(True)
            await interaction.edit_original_response(embed=embed, view=self)
        finally:
            self._busy = False
    
    @discord.ui.button(label="⬅️ Prev", style=discord.ButtonStyle.primary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._busy:
            await interaction.response.defer()
            return
        self._busy = True
        await interaction.response.defer()
        try:
            if self.page > 0:
                self.page -= 1
            self._set_buttons_enabled(False)
            await interaction.edit_original_response(embed=self._loading_embed(), view=self)
            embed = await self.get_embed()
            self._set_buttons_enabled(True)
            await interaction.edit_original_response(embed=embed, view=self)
        finally:
            self._busy = False
    
    @discord.ui.button(label="➡️ Next", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._busy:
            await interaction.response.defer()
            return
        self._busy = True
        await interaction.response.defer()
        try:
            if (self.page + 1) * self.per_page < len(self.data):
                self.page += 1
            self._set_buttons_enabled(False)
            await interaction.edit_original_response(embed=self._loading_embed(), view=self)
            embed = await self.get_embed()
            self._set_buttons_enabled(True)
            await interaction.edit_original_response(embed=embed, view=self)
        finally:
            self._busy = False
    
    @discord.ui.button(label="⏭️ Last", style=discord.ButtonStyle.secondary)
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._busy:
            await interaction.response.defer()
            return
        self._busy = True
        await interaction.response.defer()
        try:
            self.page = (len(self.data) - 1) // self.per_page
            self._set_buttons_enabled(False)
            await interaction.edit_original_response(embed=self._loading_embed(), view=self)
            embed = await self.get_embed()
            self._set_buttons_enabled(True)
            await interaction.edit_original_response(embed=embed, view=self)
        finally:
            self._busy = False

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.delete()
            except Exception as e:
                print(f"[ERROR] Failed to auto-delete /t100 message: {e}")

@tree.command(name="t100", description="Show the Top 100 Clubs from EA Pro Clubs Leaderboard.")
async def top100_command(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        data = await _ea_get_json(
            "https://proclubs.ea.com/api/fc/allTimeLeaderboard",
            {"platform": PLATFORM},
        )
        if not isinstance(data, list):
            await interaction.followup.send("⚠️ No leaderboard data found.")
            return

        top_100 = sorted(data, key=lambda c: c.get("rank", 9999))[:100]

        view = Top100View(top_100, per_page=10)
        embed = await view.get_embed()   # CHANGED: await
        message = await interaction.followup.send(embed=embed, view=view)
        view.message = message
        await log_command_output(interaction, "t100", message)
    except Exception as e:
        print(f"[ERROR] Failed to fetch Top 100: {e}")
        await send_temporary_message(interaction.followup, content="❌ An error occurred while fetching the Top 100 clubs.")

@tree.command(name="last5", description="Show the last 5 matches for a club.")
@app_commands.describe(club="Club name or club ID")
async def last5_command(interaction: discord.Interaction, club: str):
    await interaction.response.defer()

    try:
        if club.isdigit():
            await fetch_and_display_last5(interaction, club, "Club")
            return

        valid_clubs = await search_clubs_ea(club)
        if not valid_clubs:
            await send_temporary_message(interaction.followup, content="No matching clubs found.", delay=15)
            return

        if len(valid_clubs) == 1:
            club_id = str(valid_clubs[0]["clubInfo"]["clubId"])
            club_name = valid_clubs[0]["clubInfo"]["name"]
            await fetch_and_display_last5(interaction, club_id, club_name)
        else:
            options = [
                discord.SelectOption(label=c["clubInfo"]["name"], value=str(c["clubInfo"]["clubId"]))
                for c in valid_clubs[:25]
            ]
            options.append(discord.SelectOption(label="None of these", value="none"))
            view = Last5DropdownView(options, valid_clubs)
            await interaction.followup.send("Multiple clubs found. Please select:", view=view)

    except Exception as e:
        print(f"[ERROR] /last5 failed: {e}")
        await interaction.followup.send("An error occurred while fetching last 5 matches.")

@tree.command(name="l5", description="Alias for /last5")
@app_commands.describe(club="Club name or club ID")
async def l5_command(interaction: discord.Interaction, club: str):
    await last5_command.callback(interaction, club)

@tree.command(name="stats5", description="Show total player stats from a club's last 5 matches across all match types.")
@app_commands.describe(club="Club name or club ID")
async def stats5_command(interaction: discord.Interaction, club: str):
    await interaction.response.defer()

    try:
        if club.isdigit():
            club_id = club
            club_name = None
        else:
            hits = await search_clubs_ea(club)
            if not hits:
                await interaction.followup.send("No matching clubs found.", ephemeral=True)
                return

            if len(hits) > 1:
                view = Stats5Dropdown(hits)
                msg = await interaction.followup.send(
                    "Multiple clubs found. Please choose the correct one:",
                    view=view
                )
                asyncio.create_task(delete_after_delay(msg, 60))
                return

            club_id = str(hits[0]["clubInfo"]["clubId"])
            club_name = hits[0]["clubInfo"]["name"]

        msg = await interaction.followup.send("⏳ Fetching last 5 player totals…")

        embeds = await build_stats5_embeds(club_id, club_name)
        if not embeds:
            await msg.edit(content="No recent matches found for this club.", embed=None, view=None)
            asyncio.create_task(delete_after_delay(msg, 60))
            return

        await msg.edit(content=None, embed=embeds[0], view=None)
        refreshed = await interaction.channel.fetch_message(msg.id)
        await log_command_output(interaction, "stats5", refreshed)
        asyncio.create_task(delete_after_delay(refreshed, 60))

        for extra_embed in embeds[1:]:
            extra_msg = await interaction.followup.send(embed=extra_embed)
            asyncio.create_task(delete_after_delay(extra_msg, 60))

    except Exception as e:
        print(f"[ERROR] /stats5 failed: {e}")
        await interaction.followup.send(
            "❌ An unexpected error occurred while fetching last 5 player totals.",
            ephemeral=True
        )

@tree.command(name="s5", description="Alias for /stats5")
@app_commands.describe(club="Club name or club ID")
async def s5_command(interaction: discord.Interaction, club: str):
    await stats5_command.callback(interaction, club)

@tree.command(name="stats", description="All-in-one club stats: rank, rating, record, form, last 5 matches, activity.")
@app_commands.describe(club="Club name or club ID")
async def stats_command(interaction: discord.Interaction, club: str):
    await interaction.response.defer()

    try:
        # Resolve club
        if club.isdigit():
            club_id = club
            club_name = None
        else:
            hits = await search_clubs_ea(club)
            if not hits:
                await interaction.followup.send("No matching clubs found.", ephemeral=True)
                return
            if len(hits) > 1:
                view = StatsDropdown(hits)  # this view will handle its own auto-delete (see step 3)
                msg = await interaction.followup.send("Multiple clubs found. Please choose the correct one:", view=view)
                # optional timeout cleanup for an unselected dropdown:
                asyncio.create_task(delete_after_delay(msg, 60))
                return
            club_id = str(hits[0]["clubInfo"]["clubId"])
            club_name = hits[0]["clubInfo"]["name"]

        # One placeholder → edit in-place
        msg = await interaction.followup.send("⏳ Fetching club stats…")

        try:
            data = await fetch_all_stats_for_club(club_id)
            embed = build_stats_embed(club_id, club_name, data)
        except Exception as e:
            print(f"[ERROR] fetch_all_stats_for_club failed: {e}")
            embed = discord.Embed(title="❌ Error", description="Could not fetch all stats for this club.", color=discord.Color.red())

        view = PrintRecordButton(data["stats"], (club_name or f"Club {club_id}").upper())
        
        await msg.edit(content=None, embed=embed, view=view)

        record_club_search(
            interaction.guild,
            interaction.user,
        )

        msg = await interaction.channel.fetch_message(msg.id)
        await log_command_output(interaction, "stats", msg)
        asyncio.create_task(delete_after_delay(msg, 60))

    except Exception as e:
        print(f"[ERROR] /stats failed: {e}")
        await interaction.followup.send("❌ An unexpected error occurred while fetching club stats.", ephemeral=True)

@tree.command(
    name="leaderboard",
    description="Show who has researched the most EA FC clubs.",
)
async def leaderboard_command(
    interaction: discord.Interaction,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    guild_id = str(interaction.guild.id)

    guild_data = search_leaderboard_data.get(
        "guilds",
        {},
    ).get(
        guild_id,
        {},
    )

    users = guild_data.get("users", {})

    if not users:
        await interaction.response.send_message(
            "No successful club searches have been recorded yet.",
            ephemeral=True,
        )
        return

    ranked_users = sorted(
        users.items(),
        key=lambda item: int(item[1].get("count", 0)),
        reverse=True,
    )[:10]

    medals = {
        1: "🥇",
        2: "🥈",
        3: "🥉",
    }

    leaderboard_lines = []

    for position, (user_id, entry) in enumerate(
        ranked_users,
        start=1,
    ):
        count = int(entry.get("count", 0))
        marker = medals.get(position, f"`{position}.`")
        search_word = "search" if count == 1 else "searches"

        member = interaction.guild.get_member(int(user_id))

        if member:
            user_display = member.mention
        else:
            stored_name = entry.get(
                "display_name",
                f"User {user_id}",
            )
            user_display = discord.utils.escape_markdown(stored_name)

        leaderboard_lines.append(
            f"{marker} {user_display} — **{count} {search_word}**"
        )

    total_searches = sum(
        int(entry.get("count", 0))
        for entry in users.values()
    )

    embed = discord.Embed(
        title="🔍 Club Research Leaderboard",
        description="\n".join(leaderboard_lines),
        color=discord.Color.gold(),
    )

    embed.set_footer(
        text=f"{total_searches} successful club searches recorded"
    )

    if interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)

    await interaction.response.send_message(embed=embed)

@tree.command(name="lineup", description="Create an interactive lineup from a formation.")
@app_commands.describe(
    formation="Choose a soccer formation",
    title="Optional custom title for the lineup",
    role="Optional role restriction: only members with this role can be assigned",
    channel="Channel to post the lineup (defaults to current channel)",
    kickoff="Kickoff date/time (DD-MM-YYYY HH:MM) in Europe/London"
)
@app_commands.choices(formation=[app_commands.Choice(name=f, value=f) for f in FORMATIONS.keys()])
async def lineup_command(
    interaction: discord.Interaction,
    formation: app_commands.Choice[str],
    title: str | None = None,
    role: discord.Role | None = None,
    channel: discord.TextChannel | None = None,
    kickoff: str | None = None,
):
    await interaction.response.defer(ephemeral=True)
    target_channel = channel or interaction.channel
    if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
        await safe_interaction_respond(interaction, content="❌ Please specify a valid text channel.", ephemeral=True)
        return

        # Parse optional kickoff (Europe/London -> UTC ISO)
        kickoff_iso = None
        if kickoff:
            try:
                dt_local_naive = datetime.strptime(kickoff, "%d-%m-%Y %H:%M")
                dt_local = dt_local_naive.replace(tzinfo=DEFAULT_TZ)
                dt_utc = dt_local.astimezone(timezone.utc)
                kickoff_iso = dt_utc.isoformat()
            except Exception:
                await safe_interaction_respond(
                    interaction,
                    content="❌ Invalid kickoff format. Use `DD-MM-YYYY HH:MM` (24-hour), Europe/London.",
                    ephemeral=True
                )
                return
    
        # Build lineup object
        lid = lineups_store.get("next_id", 1)
        lp = {
            "id": lid,
            "title": (title or "").strip() or None,
            "formation": formation.value,
            "positions": _build_positions_for_formation(formation.value),
            "role_id": (role.id if role else None),
            "channel_id": target_channel.id,
            "message_id": None,
            "creator_id": interaction.user.id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": None,
            "finished_once": False,
            "pinged_user_ids": [],
            "kickoff_at": kickoff_iso,
        }

    embed = make_lineup_embed(lp)

    # Post with interactive view
    view = LineupAssignView(lp, editor_id=interaction.user.id)
    try:
        sent = await target_channel.send(embed=embed, view=view)
        view.message = sent
        lp["message_id"] = sent.id
        lineups_store.setdefault("lineups", {})[str(lid)] = lp
        lineups_store["next_id"] = lid + 1
        save_lineups_store()
    except Exception as e:
        await safe_interaction_respond(interaction, content=f"❌ Failed to post lineup: {e}", ephemeral=True)
        return

    await safe_interaction_respond(interaction, content=f"✅ Lineup created (ID `{lid}`) in {target_channel.mention}.", ephemeral=True)
    #await log_command_output(interaction, "lineup", sent)


@tree.command(name="editlineup", description="Edit an existing lineup by ID.")
@app_commands.describe(
    lineup_id="The lineup ID to edit"
)
async def editlineup_command(interaction: discord.Interaction, lineup_id: int):
    await interaction.response.defer(ephemeral=True)

    lp = lineups_store.get("lineups", {}).get(str(lineup_id))
    if not lp:
        await safe_interaction_respond(interaction, content="❌ Lineup ID not found.", ephemeral=True)
        return

    member = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
    if not user_can_edit_lineup(member, lp):
        await safe_interaction_respond(interaction, content="❌ You don't have permission to edit this lineup.", ephemeral=True)
        return

    try:
        ch = client.get_channel(lp["channel_id"]) or await client.fetch_channel(lp["channel_id"])
        msg = await ch.fetch_message(lp["message_id"])
    except Exception as e:
        await safe_interaction_respond(interaction, content=f"❌ Couldn't access the lineup message: {e}", ephemeral=True)
        return

    # Re-attach an active view
    view = LineupAssignView(lp, editor_id=interaction.user.id)
    view.message = msg
    try:
        await msg.edit(embed=make_lineup_embed(lp), view=view)
    except Exception as e:
        await safe_interaction_respond(interaction, content=f"❌ Failed to attach editor: {e}", ephemeral=True)
        return

    await safe_interaction_respond(interaction, content=f"✏️ Editing lineup `{lineup_id}`.", ephemeral=True)
    #await log_command_output(interaction, "editlineup", msg)

@tree.command(name="deletelineup", description="Delete a lineup by ID.")
@app_commands.describe(lineup_id="The lineup ID to delete")
async def deletelineup_command(interaction: discord.Interaction, lineup_id: int):
    await interaction.response.defer(ephemeral=True)

    # Find lineup
    lp = lineups_store.get("lineups", {}).get(str(lineup_id))
    if not lp:
        await safe_interaction_respond(interaction, content="❌ Lineup ID not found.", ephemeral=True)
        return

    # Permission: creator or Moderator (same as edit)
    member = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
    if not user_can_edit_lineup(member, lp):
        await safe_interaction_respond(interaction, content="❌ You don't have permission to delete this lineup.", ephemeral=True)
        return

    # Try to delete the original lineup message
    try:
        ch = client.get_channel(lp["channel_id"]) or await client.fetch_channel(lp["channel_id"])
        msg = await ch.fetch_message(lp["message_id"])
        await msg.delete()
    except Exception as e:
        # It's okay if the message is gone; we'll still remove the record
        print(f"[WARN] Could not delete lineup message {lineup_id}: {e}")

    # Remove from store and persist
    try:
        lineups_store["lineups"].pop(str(lineup_id), None)
        save_lineups_store()
    except Exception as e:
        await safe_interaction_respond(interaction, content=f"⚠️ Deleted message but failed to update storage: {e}", ephemeral=True)
        return

    await safe_interaction_respond(interaction, content=f"🗑️ Lineup `{lineup_id}` deleted.", ephemeral=True)
    # (Optional) log to your archive channel:
    # await log_command_output(interaction, "deletelineup", extra_text=f"Deleted lineup {lineup_id}.")

# -------------------------
# Event & Template persistence
# -------------------------
def load_json_file(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERROR] Failed to load {path}: {e}")
        return default

def save_json_file(path, data):
    """
    Keep the same call sites, but persist to Postgres asynchronously.
    'path' is our logical key (e.g., 'events.json', 'templates.json', 'lineups.json').
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(db_save_json(path, data))
    except RuntimeError:
        # No running loop (very early import) — ignore
        pass

events_store = {"next_id": 1, "events": {}}
templates_store = {}
lineups_store = {"next_id": 1, "lineups": {}}

def make_event_embed(ev: dict) -> discord.Embed:
    """
    Build the embed for an event from the stored event dict.
    Adds a bold "Event Info" heading above the event description,
    and uses the server icon for both thumbnail and footer.
    """
    color = discord.Color(int(EVENT_EMBED_COLOR_HEX.strip().lstrip("#"), 16))

    desc_text = ev.get("description", "\u200b")
    embed_description = f"**Event Info**\n{desc_text}"

    embed = discord.Embed(
        title=f"📅 {ev.get('name')}",
        description=embed_description,
        color=color
    )

    # When
    dt_iso = ev.get("datetime")
    try:
        dt = datetime.fromisoformat(dt_iso)
        dt_utc = dt.astimezone(timezone.utc)
        embed.add_field(name="When", value=discord.utils.format_dt(dt_utc, style='F'), inline=False)
    except Exception:
        embed.add_field(name="When", value="Unknown", inline=False)

    # Stream Link (optional)
    stream_url = ev.get("twitch_url")
    if stream_url:
        username = stream_url.rsplit("/", 1)[-1]
        embed.add_field(name="Stream Link", value=f"[{username}]({stream_url})", inline=False)

    # Thread field if created
    if ev.get("thread_id"):
        embed.add_field(name="Thread", value=f"<#{ev['thread_id']}>", inline=False)

    # Columns: Attend / Absent / Maybe
    def users_to_text(user_ids):
        if not user_ids:
            return "—"
        return "\n".join(f"<@{uid}>" for uid in user_ids)

    def late_to_text(ev: dict) -> str:
        late_map = ev.get("attend_later_times") or {}
        if not late_map:
            return ""
        lines = []
        for uid_str, iso in late_map.items():
            try:
                uid = int(uid_str)
            except Exception:
                continue
            try:
                dt = datetime.fromisoformat(iso).astimezone(timezone.utc)
                lines.append(f"<@{uid}> — {LATE_EMOJI} {discord.utils.format_dt(dt, style='t')}")
            except Exception:
                lines.append(f"<@{uid}> — {LATE_EMOJI} (time set)")
        return "\n".join(lines)
    
    attend_txt = users_to_text(ev.get("attend", []))
    late_txt = late_to_text(ev)
    if late_txt:
        attend_txt = attend_txt if attend_txt != "—" else ""
        attend_txt = (attend_txt + ("\n" if attend_txt else "") + late_txt).strip() or "—"
    
    embed.add_field(name=f"{ATTEND_EMOJI} Attend", value=attend_txt, inline=True)
    embed.add_field(name=f"{ABSENT_EMOJI} Absent", value=users_to_text(ev.get("absent", [])), inline=True)
    embed.add_field(name=f"{MAYBE_EMOJI} Maybe", value=users_to_text(ev.get("maybe", [])), inline=True)


    # Server assets (thumbnail + footer icon)
    guild = None
    try:
        ch = client.get_channel(ev.get("channel_id"))
        guild = ch.guild if ch is not None else None
    except Exception:
        pass

    try:
        if guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
    except Exception:
        pass

    footer_icon = None
    try:
        ch = client.get_channel(ev.get("channel_id"))
        guild = ch.guild if ch else None
        if guild and guild.icon:
            footer_icon = guild.icon.url
    except Exception:
        pass

    embed.set_footer(text=f"Phonics Bot • Event ID: {ev.get('id')}", icon_url=footer_icon)
    return embed

def user_can_create_events(member: discord.Member) -> bool:
    if not member:
        return False
    if EVENT_CREATOR_ROLE_ID:
        return any(r.id == EVENT_CREATOR_ROLE_ID for r in member.roles)
    else:
        return any(r.name == EVENT_CREATOR_ROLE_NAME for r in member.roles)

def emoji_to_key(emoji: str):
    if emoji == ATTEND_EMOJI:
        return "attend"
    if emoji == ABSENT_EMOJI:
        return "absent"
    if emoji == MAYBE_EMOJI:
        return "maybe"
    if emoji == LATE_EMOJI:
        return "attend_later"
    return None

def save_events_store():
    save_json_file(EVENTS_FILE, events_store)

def save_templates_store():
    save_json_file(TEMPLATES_FILE, templates_store)

def build_late_time_options(ev: dict) -> list[discord.SelectOption]:
    """
    Options: every 15 minutes after kickoff, for 2 hours.
    Stored/used as UTC ISO string values.
    """
    dt_iso = ev.get("datetime")
    if not dt_iso:
        return []

    kickoff_utc = datetime.fromisoformat(dt_iso).astimezone(timezone.utc)

    opts: list[discord.SelectOption] = []
    # 15..120 minutes inclusive (8 options)
    for mins in range(15, 121, 15):
        arr = kickoff_utc + timedelta(minutes=mins)
        label = arr.astimezone(DEFAULT_TZ).strftime("%H:%M")  # display in London time
        value = arr.isoformat()
        desc = f"{mins} mins late"
        opts.append(discord.SelectOption(label=label, value=value, description=desc))
    return opts

class AttendLaterTimeSelect(discord.ui.Select):
    def __init__(self, ev: dict, user_id: int):
        self.ev = ev
        self.user_id = user_id

        options = build_late_time_options(ev)
        super().__init__(
            placeholder="Select your arrival time…",
            options=options[:25],  # (we only have 8)
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        # Only the reacting user can use it
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This dropdown isn’t for you 🙂", ephemeral=True)
            return

        arrival_iso = self.values[0]

        # Save
        self.ev.setdefault("attend_later_times", {})
        self.ev["attend_later_times"][str(self.user_id)] = arrival_iso

        # Ensure they’re not in absent/maybe/attend (optional: you can also remove from attend)
        for k in ("absent", "maybe", "attend"):
            if self.user_id in (self.ev.get(k) or []):
                self.ev[k].remove(self.user_id)

        # Persist + update embed
        events_store["events"][str(self.ev["id"])] = self.ev
        save_events_store()

        try:
            ch = client.get_channel(self.ev["channel_id"]) or await client.fetch_channel(self.ev["channel_id"])
            msg = await ch.fetch_message(self.ev["message_id"])
            await msg.edit(embed=make_event_embed(self.ev))
        except Exception as e:
            print(f"[WARN] Could not edit event embed after attend_later time pick: {e}")

        # Thread membership (treat like attend/maybe)
        asyncio.create_task(add_user_to_event_thread(self.ev, self.user_id))
        
        # Acknowledge the interaction without posting/editing visible text,
        # then delete the dropdown prompt message.
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except Exception:
            # Fallback: at least remove the UI and clear the message
            try:
                await interaction.edit_original_response(content="", view=None)
            except Exception:
                pass

class AttendLaterTimeView(discord.ui.View):
    def __init__(self, ev: dict, user_id: int):
        super().__init__(timeout=120)
        self.add_item(AttendLaterTimeSelect(ev, user_id))

    async def on_timeout(self):
        # Optional: you can clean up if you stored the message reference elsewhere
        return

def make_lineup_embed(lp: dict) -> discord.Embed:
    """
    Build an embed for a lineup. Single column: `Lineup` with all positions.
    Footer is standardized to 'Phonics Bot' with the server icon.
    """
    color = discord.Color(int(EVENT_EMBED_COLOR_HEX.strip().lstrip("#"), 16))
    ch = client.get_channel(lp.get("channel_id"))
    guild = ch.guild if ch else None

    title = lp.get("title") or f"{lp.get('formation')} Lineup"
    formation = lp.get("formation")
    role_id = lp.get("role_id")

    # Build the details section
    details: list[str] = [f"**Formation:** `{formation}`"]
    if role_id:
        details.append(f"**Eligible Role:** <@&{role_id}>")

    # Kickoff (optional)
    ko_iso = lp.get("kickoff_at")
    if ko_iso:
        try:
            dt = datetime.fromisoformat(ko_iso).astimezone(timezone.utc)
            # Absolute + relative time
            details.append(f"**Kickoff:** {discord.utils.format_dt(dt, style='F')} ({discord.utils.format_dt(dt, style='R')})")
        except Exception:
            pass

    embed = discord.Embed(
        title=f"🧩 {title}",
        description="\n".join(details),
        color=color,
    )

    # Build one column list of positions
    positions: list[dict] = lp.get("positions", [])
    lines = []
    for pos in positions:
        mention = f"<@{pos['user_id']}>" if pos.get("user_id") else "—"
        lines.append(f"**{pos['code']}** — {mention}")

    embed.add_field(name="Lineup", value="\n".join(lines) or "—", inline=False)

    # Server icon as thumbnail (optional) + footer icon
    try:
        if guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
    except Exception:
        pass

    footer_icon = guild.icon.url if (guild and guild.icon) else None
    embed.set_footer(text=f"Phonics Bot • Lineup ID: {lp.get('id')}", icon_url=footer_icon)
    return embed

# -------------------------
# Twitch live embed + button
# -------------------------
def make_twitch_live_embed(stream: dict, game_box_url: str | None) -> discord.Embed:
    color = discord.Color(int(EVENT_EMBED_COLOR_HEX.strip().lstrip("#"), 16))

    streamer = stream.get("user_name") or "Streamer"
    title = stream.get("title") or "Live now!"
    game = stream.get("game_name") or "Just Chatting"
    login = (stream.get("user_login") or TWITCH_CHANNEL_LOGIN or streamer).lower()
    twitch_url = f"https://twitch.tv/{login}"

    # Base embed
    embed = discord.Embed(
        title=f"🔴 LIVE: {streamer}",
        description=f"**{title}**",
        color=color,
        url=twitch_url,  # make title clickable
        timestamp=datetime.now(timezone.utc),
    )

    # Core fields
    embed.add_field(name="Streamer", value=streamer, inline=True)
    embed.add_field(name="Game", value=game, inline=True)

    # Viewer count (if available)
    viewers = stream.get("viewer_count")
    if isinstance(viewers, int):
        embed.add_field(name="Viewers", value=f"{viewers:,}", inline=True)

    # Uptime (from started_at)
    started_at = stream.get("started_at")
    if started_at:
        try:
            started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            delta = datetime.now(timezone.utc) - started_dt
            total_mins = int(delta.total_seconds() // 60)
            hours, mins = divmod(total_mins, 60)
            uptime = f"{hours}h {mins}m" if hours else f"{mins}m"
            embed.add_field(name="Uptime", value=uptime, inline=True)
        except Exception:
            pass

    # Thumbnail: game box art
    if game_box_url:
        try:
            embed.set_thumbnail(url=game_box_url)
        except Exception:
            pass

    # Main image: live preview (updates periodically on Twitch side)
    preview = stream.get("thumbnail_url")
    if preview:
        # Use a decent size and add a cache-buster so Discord refreshes it
        preview = preview.replace("{width}", "1280").replace("{height}", "720")
        cache_bust = int(datetime.now(timezone.utc).timestamp())
        embed.set_image(url=f"{preview}?v={cache_bust}")

    embed.set_footer(text="Phonics Bot • Twitch Live")
    return embed

class WatchButtonView(discord.ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=None)  # link button doesn't need a timeout
        self.add_item(discord.ui.Button(label="Watch", style=discord.ButtonStyle.link, url=url))

# -------------------------
# Template commands
# -------------------------
@tree.command(name="createtemplate", description="Create an event template (Moderator role required).")
@app_commands.describe(
    template_name="Unique template name",
    event_name="Event display name",
    description="Event description",
    channel="Optional channel to save with the template",
    role="Optional role to ping when this template is used",
    stream="Optional Twitch channel or URL (e.g. ninja or https://twitch.tv/ninja)"  # NEW
)
async def createtemplate_command(
    interaction: discord.Interaction,
    template_name: str,
    event_name: str,
    description: str,
    channel: discord.TextChannel = None,
    role: discord.Role = None,
    stream: str = None
):
    member = interaction.user
    if not user_can_create_events(member):
        await safe_interaction_respond(interaction, content="❌ You do not have permission to create templates.", ephemeral=True)
        return

    key = template_name.strip()
    if not key:
        await safe_interaction_respond(interaction, content="❌ Template name cannot be empty.", ephemeral=True)
        return

    if key in templates_store:
        await safe_interaction_respond(interaction, content="❌ A template with that name already exists. Delete it first or choose another name.", ephemeral=True)
        return

    templates_store[key] = {
        "name": event_name,
        "description": description,
        "channel_id": channel.id if channel else None,
        "role_id": role.id if role else None,
        "twitch_url": _twitch_url_from_input(stream),
        "creator_id": interaction.user.id,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    save_templates_store()
    await safe_interaction_respond(interaction, content=f"✅ Template `{key}` created.", ephemeral=True)

@tree.command(name="listtemplates", description="List saved event templates.")
async def listtemplates_command(interaction: discord.Interaction):
    if not templates_store:
        await safe_interaction_respond(interaction, content="No templates saved.", ephemeral=True)
        return

    lines = []
    for k, t in templates_store.items():
        channel_part = f" • Channel: <#{t['channel_id']}>" if t.get("channel_id") else ""
        stream_part = ""
        if t.get("twitch_url"):
            stream_part = f" • Stream: {t['twitch_url'].rsplit('/', 1)[-1]}"
        lines.append(f"**{k}** — {t.get('name')} {channel_part}{stream_part}\n{t.get('description')[:150]}")
    text = "\n\n".join(lines)
    await safe_interaction_respond(
        interaction,
        embed=discord.Embed(title="Saved Templates", description=text, color=discord.Color.blue()),
        ephemeral=True
    )

@tree.command(name="deletetemplate", description="Delete a saved template (Moderator role required).")
@app_commands.describe(template_name="Name of template to delete")
async def deletetemplate_command(interaction: discord.Interaction, template_name: str):
    member = interaction.user
    if not user_can_create_events(member):
        await safe_interaction_respond(interaction, content="❌ You do not have permission to delete templates.", ephemeral=True)
        return

    key = template_name.strip()
    if key not in templates_store:
        await safe_interaction_respond(interaction, content="❌ Template not found.", ephemeral=True)
        return

    templates_store.pop(key, None)
    save_templates_store()
    await safe_interaction_respond(interaction, content=f"✅ Template `{key}` deleted.", ephemeral=True)

# -------------------------
# Event creation from template
# -------------------------
@tree.command(name="createfromtemplate", description="Create an event from a saved template (Moderator role required).")
@app_commands.describe(
    template_name="Template to use",
    date="Date (DD-MM-YYYY) — local to Europe/London",
    time="Time (HH:MM 24-hour) — local to Europe/London",
    formation="Formation (required) for the lineup in the event thread",
    channel="Optional channel to post the event in (defaults to template channel or current channel)",
    role="Optional role to ping (overrides template's saved role)",
    stream="Optional Twitch channel or URL (overrides template stream)"
)
@app_commands.choices(formation=[app_commands.Choice(name=f, value=f) for f in FORMATIONS.keys()])
async def createfromtemplate_command(
    interaction: discord.Interaction,
    template_name: str,
    date: str,
    time: str,
    formation: app_commands.Choice[str],  # ✅ REQUIRED
    channel: discord.TextChannel = None,
    role: discord.Role = None,
    stream: str = None
):
    await interaction.response.defer(ephemeral=True)
    member = interaction.user
    if not user_can_create_events(member):
        await safe_interaction_respond(interaction, content="❌ You do not have permission to create events.", ephemeral=True)
        return

    key = template_name.strip()
    tpl = templates_store.get(key)
    if not tpl:
        await safe_interaction_respond(interaction, content="❌ Template not found.", ephemeral=True)
        return

    # Resolve role
    chosen_role = role
    if chosen_role is None:
        rid = tpl.get("role_id")
        if rid:
            chosen_role = interaction.guild.get_role(rid)

    # Resolve stream (override if provided)
    chosen_stream_url = _twitch_url_from_input(stream) if stream else tpl.get("twitch_url")

    # parse date/time
    try:
        dt_local_naive = datetime.strptime(f"{date} {time}", "%d-%m-%Y %H:%M")
        dt_local = dt_local_naive.replace(tzinfo=DEFAULT_TZ)
        dt_utc = dt_local.astimezone(timezone.utc)
    except Exception:
        await safe_interaction_respond(interaction, content="❌ Invalid date/time format. Please use `DD-MM-YYYY` and `HH:MM` (24-hour).", ephemeral=True)
        return

    target_channel = None
    if channel:
        target_channel = channel
    elif tpl.get("channel_id"):
        try:
            target_channel = client.get_channel(tpl["channel_id"]) or await client.fetch_channel(tpl["channel_id"])
        except Exception:
            target_channel = None
    target_channel = target_channel or interaction.channel

    if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
        await safe_interaction_respond(interaction, content="❌ Please specify a valid text channel.", ephemeral=True)
        return

    eid = events_store.get("next_id", 1)
    ev = {
        "id": eid,
        "name": tpl.get("name"),
        "description": tpl.get("description"),
        "channel_id": target_channel.id,
        "message_id": None,
        "thread_id": None,
        "creator_id": interaction.user.id,
        "datetime": dt_utc.isoformat(),
        "closed": False,
        "attend": [],
        "absent": [],
        "maybe": [],
        "attend_later_times": {},
        "role_id": (chosen_role.id if chosen_role else None),
        "twitch_url": chosen_stream_url
    }

    embed = make_event_embed(ev)

    content = f"||{chosen_role.mention}||" if chosen_role else None
    allowed_mentions = discord.AllowedMentions(roles=[chosen_role]) if chosen_role else None

    try:
        sent = await target_channel.send(content=content, embed=embed, allowed_mentions=allowed_mentions)
        await sent.add_reaction(ATTEND_EMOJI)
        await sent.add_reaction(ABSENT_EMOJI)
        await sent.add_reaction(MAYBE_EMOJI)
        await sent.add_reaction(LATE_EMOJI)

        try:
            thread = await sent.create_thread(name=ev["name"], auto_archive_duration=10080)
            ev["thread_id"] = thread.id
            try:
                await thread.add_user(interaction.user)
            except Exception:
                pass
            try:
                await sent.edit(embed=make_event_embed(ev))
            except Exception:
                pass

            # 🚀 Auto-create + pin a lineup inside the new event thread
            try:
                await auto_post_lineup_in_thread(ev, thread, formation.value)  # uses DEFAULT_LINEUP_FORMATION
            except Exception as le:
                print(f"[WARN] Failed to auto-create lineup in thread: {le}")

        except Exception as te:
            print(f"[WARN] Could not create thread for event {eid}: {te}")

    except Exception as e:
        await safe_interaction_respond(interaction, content=f"❌ Failed to post event: {e}", ephemeral=True)
        return

    ev["message_id"] = sent.id
    events_store.setdefault("events", {})[str(eid)] = ev
    events_store["next_id"] = eid + 1
    save_events_store()

    await safe_interaction_respond(
        interaction,
        content=f"✅ Event created from template `{key}` with ID `{eid}` and posted in {target_channel.mention}.",
        ephemeral=True
    )

# -------------------------
# Event slash commands
# -------------------------
@tree.command(name="createevent", description="Create an event (Moderator role required).")
@app_commands.describe(
    name="Event name",
    description="Event description",
    date="Date (DD-MM-YYYY) — local to Europe/London",
    time="Time (HH:MM 24-hour) — local to Europe/London",
    formation="Formation (required) for the lineup in the event thread",
    channel="Channel to post the event in (optional, defaults to current channel)",
    role="Optional role to ping (will be spoilered)",
    stream="Optional Twitch channel or URL (e.g. ninja or https://twitch.tv/ninja)"
)
@app_commands.choices(formation=[app_commands.Choice(name=f, value=f) for f in FORMATIONS.keys()])
async def createevent_command(
    interaction: discord.Interaction,
    name: str,
    description: str,
    date: str,
    time: str,
    formation: app_commands.Choice[str],  # ✅ REQUIRED
    channel: discord.TextChannel = None,
    role: discord.Role = None,
    stream: str = None
):
    
    await interaction.response.defer(ephemeral=True)
    member = interaction.user
    if not isinstance(member, discord.Member):
        member = interaction.guild.get_member(interaction.user.id)

    if not user_can_create_events(member):
        await safe_interaction_respond(interaction, content="❌ You do not have permission to create events (Moderator role required).", ephemeral=True)
        return

    # parse DD-MM-YYYY
    try:
        dt_local_naive = datetime.strptime(f"{date} {time}", "%d-%m-%Y %H:%M")
        dt_local = dt_local_naive.replace(tzinfo=DEFAULT_TZ)
        dt_utc = dt_local.astimezone(timezone.utc)
    except Exception:
        await safe_interaction_respond(interaction, content="❌ Invalid date/time format. Please use `DD-MM-YYYY` for date and `HH:MM` (24-hour) for time.", ephemeral=True)
        return

    target_channel = channel or interaction.channel
    if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
        await safe_interaction_respond(interaction, content="❌ Please specify a valid text channel.", ephemeral=True)
        return

    eid = events_store.get("next_id", 1)
    ev = {
        "id": eid,
        "name": name,
        "description": description,
        "channel_id": target_channel.id,
        "message_id": None,
        "thread_id": None,
        "creator_id": interaction.user.id,
        "datetime": dt_utc.isoformat(),
        "closed": False,
        "attend": [],
        "absent": [],
        "maybe": [],
        "attend_later_times": {},
        "role_id": (role.id if role else None),
        "twitch_url": _twitch_url_from_input(stream),
    }
    embed = make_event_embed(ev)

    # Prepare spoilered mention outside the embed (so it pings)
    content = f"||{role.mention}||" if role else None
    allowed_mentions = discord.AllowedMentions(roles=[role]) if role else None

    try:
        sent = await target_channel.send(
            content=content,
            embed=embed,
            allowed_mentions=allowed_mentions
        )
        await sent.add_reaction(ATTEND_EMOJI)
        await sent.add_reaction(ABSENT_EMOJI)
        await sent.add_reaction(MAYBE_EMOJI)
        await sent.add_reaction(LATE_EMOJI)

        # Create a thread tied to the event message (same name as event)
        try:
            thread = await sent.create_thread(name=ev["name"], auto_archive_duration=10080)
            ev["thread_id"] = thread.id
            try:
                await thread.add_user(interaction.user)
            except Exception:
                pass
            try:
                await sent.edit(embed=make_event_embed(ev))
            except Exception:
                pass

            # 🚀 Auto-create + pin a lineup inside the new event thread
            try:
                await auto_post_lineup_in_thread(ev, thread, formation.value)  # uses DEFAULT_LINEUP_FORMATION
            except Exception as le:
                print(f"[WARN] Failed to auto-create lineup in thread: {le}")

        except Exception as te:
            print(f"[WARN] Could not create thread for event {eid}: {te}")

    except Exception as e:
        await safe_interaction_respond(interaction, content=f"❌ Failed to post event: {e}", ephemeral=True)
        return

    ev["message_id"] = sent.id
    events_store.setdefault("events", {})[str(eid)] = ev
    events_store["next_id"] = eid + 1
    save_events_store()

    await safe_interaction_respond(interaction, content=f"✅ Event created with ID `{eid}` and posted in {target_channel.mention}.", ephemeral=True)

@tree.command(name="cancelevent", description="Cancel (delete) an event by ID (Moderator role required).")
@app_commands.describe(event_id="Event ID")
async def cancelevent_command(interaction: discord.Interaction, event_id: int):
    member = interaction.user
    if not user_can_create_events(member):
        await safe_interaction_respond(interaction, content="❌ You do not have permission to cancel events.", ephemeral=True)
        return

    ev = events_store.get("events", {}).get(str(event_id))
    if not ev:
        await safe_interaction_respond(interaction, content="❌ Event ID not found.", ephemeral=True)
        return

    # Delete message; archive/lock thread if present
    try:
        ch = client.get_channel(ev["channel_id"]) or await client.fetch_channel(ev["channel_id"])
        msg = await ch.fetch_message(ev["message_id"])
        await msg.delete()
    except Exception as e:
        print(f"[WARN] Could not delete event message: {e}")

    try:
        if ev.get("thread_id"):
            thread = client.get_channel(ev["thread_id"])
            if isinstance(thread, discord.Thread):
                await thread.edit(archived=True, locked=True)
    except Exception as e:
        print(f"[WARN] Could not archive/lock thread for event {event_id}: {e}")

    events_store["events"].pop(str(event_id), None)
    save_events_store()
    await safe_interaction_respond(interaction, content=f"✅ Event `{event_id}` cancelled and removed.", ephemeral=True)

@tree.command(name="closeevent", description="Close signups for an event (Moderator role required).")
@app_commands.describe(event_id="Event ID")
async def closeevent_command(interaction: discord.Interaction, event_id: int):
    member = interaction.user
    if not user_can_create_events(member):
        await safe_interaction_respond(interaction, content="❌ You do not have permission to close events.", ephemeral=True)
        return

    ev = events_store.get("events", {}).get(str(event_id))
    if not ev:
        await safe_interaction_respond(interaction, content="❌ Event ID not found.", ephemeral=True)
        return

    ev["closed"] = True
    save_events_store()

    try:
        ch = client.get_channel(ev["channel_id"]) or await client.fetch_channel(ev["channel_id"])
        msg = await ch.fetch_message(ev["message_id"])
        embed = make_event_embed(ev)
        embed.color = discord.Color.dark_grey()
        
        ft = (embed.footer.text or f"Phonics Bot • Event ID: {ev.get('id')}") + " • CLOSED"
        embed.set_footer(text=ft, icon_url=embed.footer.icon_url)
        
        await msg.edit(embed=embed)
    except Exception as e:
        print(f"[WARN] Could not edit event message when closing: {e}")

    await safe_interaction_respond(interaction, content=f"✅ Event `{event_id}` is now closed for signups.", ephemeral=True)

@tree.command(name="openevent", description="Open signups for an event (Moderator role required).")
@app_commands.describe(event_id="Event ID")
async def openevent_command(interaction: discord.Interaction, event_id: int):
    member = interaction.user
    if not user_can_create_events(member):
        await safe_interaction_respond(interaction, content="❌ You do not have permission to open events.", ephemeral=True)
        return

    ev = events_store.get("events", {}).get(str(event_id))
    if not ev:
        await interaction.response.send_message("❌ Event ID not found.", ephemeral=True)
        return

    if not ev.get("closed", False):
        await interaction.response.send_message("ℹ️ Event is already open for signups.", ephemeral=True)
        return

    ev["closed"] = False
    save_events_store()

    try:
        ch = client.get_channel(ev["channel_id"]) or await client.fetch_channel(ev["channel_id"])
        msg = await ch.fetch_message(ev["message_id"])
        embed = make_event_embed(ev)
        embed.color = discord.Color(int(EVENT_EMBED_COLOR_HEX.strip().lstrip("#"), 16))
        
        await msg.edit(embed=embed)
    except Exception as e:
        print(f"[WARN] Could not edit event message when opening: {e}")

    await interaction.response.send_message(f"✅ Event `{event_id}` is now open for signups.", ephemeral=True)

@tree.command(name="eventinfo", description="Show event info by ID.")
@app_commands.describe(event_id="Event ID")
async def eventinfo_command(interaction: discord.Interaction, event_id: int):
    ev = events_store.get("events", {}).get(str(event_id))
    if not ev:
        await safe_interaction_respond(interaction, content="❌ Event ID not found.", ephemeral=True)
        return
    embed = make_event_embed(ev)
    await safe_interaction_respond(interaction, embed=embed, ephemeral=True)

# ---------- AUTOCOMPLETE: Event IDs ----------
def _event_choices(prefix: str, limit: int = 25):
    items = []
    for eid_str, ev in events_store.get("events", {}).items():
        try:
            eid = int(eid_str)
        except Exception:
            continue
        name = ev.get("name", "Event")
        when_txt = ""
        try:
            dt = datetime.fromisoformat(ev.get("datetime", "")).astimezone(timezone.utc)
            when_txt = discord.utils.format_dt(dt, style="F")
        except Exception:
            pass
        display = f"{eid} — {name}" + (f" — {when_txt}" if when_txt else "")
        items.append((display, eid))

    prefix_l = (prefix or "").lower()
    if prefix_l:
        items = [x for x in items if prefix_l in str(x[1]).lower() or prefix_l in x[0].lower()]
    return items[:limit]

@cancelevent_command.autocomplete("event_id")
async def cancelevent_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=disp, value=val) for disp, val in _event_choices(current)]

@closeevent_command.autocomplete("event_id")
async def closeevent_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=disp, value=val) for disp, val in _event_choices(current)]

@openevent_command.autocomplete("event_id")
async def openevent_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=disp, value=val) for disp, val in _event_choices(current)]

@eventinfo_command.autocomplete("event_id")
async def eventinfo_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=disp, value=val) for disp, val in _event_choices(current)]

# ---------- AUTOCOMPLETE: Template names ----------
def _template_choices(prefix: str, limit: int = 25):
    keys = list(templates_store.keys())
    prefix_l = (prefix or "").lower()
    if prefix_l:
        keys = [k for k in keys if prefix_l in k.lower()]
    keys = keys[:limit]
    return [app_commands.Choice(name=k, value=k) for k in keys]

@createfromtemplate_command.autocomplete("template_name")
async def createfromtemplate_autocomplete(interaction: discord.Interaction, current: str):
    return _template_choices(current)

@deletetemplate_command.autocomplete("template_name")
async def deletetemplate_autocomplete(interaction: discord.Interaction, current: str):
    return _template_choices(current)

def _lineup_choices(prefix: str, limit: int = 25):
    items = []
    for lid_str, lp in lineups_store.get("lineups", {}).items():
        try:
            lid = int(lid_str)
        except Exception:
            continue
        name = lp.get("title") or lp.get("formation")
        display = f"{lid} — {name}"
        items.append((display, lid))
    prefix_l = (prefix or "").lower()
    if prefix_l:
        items = [x for x in items if prefix_l in str(x[1]).lower() or prefix_l in x[0].lower()]
    return items[:limit]

# ---------- AUTOCOMPLETE: Lineups (open only) ----------
async def _lineup_open_choices(prefix: str, limit: int = 25):
    """Return Choice(name, id) for lineups whose message still exists."""
    prefix_l = (prefix or "").lower()
    choices: list[app_commands.Choice[int]] = []

    for lid_str, lp in lineups_store.get("lineups", {}).items():
        # id parse
        try:
            lid = int(lid_str)
        except Exception:
            continue

        # label text
        name = lp.get("title") or lp.get("formation") or "Lineup"
        display = f"{lid} — {name}"

        # text filter (by id or label)
        if prefix_l and (prefix_l not in str(lid) and prefix_l not in display.lower()):
            continue

        # only suggest if the original message still exists
        ch = client.get_channel(lp.get("channel_id"))
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            continue
        try:
            await ch.fetch_message(lp.get("message_id"))
        except Exception:
            # message gone -> treat as closed, skip
            continue

        choices.append(app_commands.Choice(name=display, value=lid))
        if len(choices) >= limit:
            break

    return choices

@editlineup_command.autocomplete("lineup_id")
async def editlineup_autocomplete(interaction: discord.Interaction, current: str):
    return await _lineup_open_choices(current)

@deletelineup_command.autocomplete("lineup_id")
async def deletelineup_autocomplete(interaction: discord.Interaction, current: str):
    return await _lineup_open_choices(current)

@tree.command(name="offside", description="Increment and show the offside counter.")
async def offside_command(interaction: discord.Interaction):
    # Increment in DB
    try:
        count = await db_incr_offside()
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to update counter: {e}", ephemeral=True)
        return

    # Build an embed with your standard color, but NO thumbnail
    color = discord.Color(int(EVENT_EMBED_COLOR_HEX.strip().lstrip("#"), 16))
    desc = f"🏃‍♂️‍➡️MistrCraven has been caught offside **{count}** times. 🏃‍♂️"

    embed = discord.Embed(
        title="🚩 Offside",
        description=desc,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    # (Deliberately NOT setting a thumbnail)

    embed.set_footer(text=f"Triggered by {interaction.user.display_name}")

    await interaction.response.send_message(embed=embed)

@tree.command(name="resetoffside", description="Admin: reset the offside counter to 0.")
async def resetoffside_command(interaction: discord.Interaction):
    # Only allow admins (uses your existing role helper)
    member = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
    if not has_admin_role(member):
        await interaction.response.send_message("❌ Only **Administrators** can use /resetoffside.", ephemeral=True)
        return

    # Make the reply ephemeral so it doesn't spam the channel
    await interaction.response.defer(ephemeral=True)

    # Ensure DB is ready (it is once on_ready ran)
    try:
        # Load current value (create if missing)
        data = await db_load_json(OFFSIDE_KEY, {"count": 0})
        before = int(data.get("count", 0))

        # Reset to zero
        data["count"] = 0
        await db_save_json(OFFSIDE_KEY, data)

        await interaction.followup.send(f"✅ Offside counter reset (was **{before}**, now **0**).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Failed to reset counter: {e}", ephemeral=True)

@tree.command(name="commodity", description="Show Star Citizen commodity buy/sell data.")
@app_commands.describe(
    name="Commodity name, e.g. Gold, Agricium, Quantanium",
    auto_load_only="Only show terminals that support auto loading",
    system_filter="Only use terminals in this star system"
)
@app_commands.choices(system_filter=[
    app_commands.Choice(name="Stanton", value="Stanton"),
    app_commands.Choice(name="Pyro", value="Pyro"),
    app_commands.Choice(name="Nyx", value="Nyx"),
])
async def commodity_command(
    interaction: discord.Interaction,
    name: str,
    auto_load_only: bool = False,
    system_filter: app_commands.Choice[str] = None
):
    await interaction.response.defer()

    try:
        selected_system = system_filter.value if system_filter else None

        matches = await search_commodity_uex(name)

        if not matches:
            await send_temp_followup(
                interaction,
                content="No matching commodities found.",
                ephemeral=True
            )
            return

        if len(matches) > 1:
            view = CommodityDropdown(
                matches,
                mode="commodity",
                auto_load_only=auto_load_only,
                system_filter=selected_system
            )
            msg = await send_temp_followup(
                interaction,
                content="Multiple commodities found. Please choose:",
                view=view,
                delete_after=90
            )
            return

        embed = await build_commodity_embed(
            matches[0],
            auto_load_only=auto_load_only,
            system_filter=selected_system
        )
        msg = await send_temp_followup(interaction, embed=embed)
        await log_star_command_usage(interaction, "commodity", message=msg)

    except Exception as e:
        print(f"[ERROR] /commodity failed: {e}")
        await send_temp_followup(
            interaction,
            content="❌ An unexpected error occurred while fetching commodity data.",
            ephemeral=True
        )

@commodity_command.autocomplete("name")
async def commodity_name_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    return await commodity_autocomplete(interaction, current)
        
@tree.command(name="route", description="Show best Star Citizen trade routes for a commodity.")
@app_commands.describe(
    name="Commodity name, e.g. Gold, Agricium, Quantanium",
    ship="Optional ship name to auto-use its SCU capacity",
    scu="Manual ship cargo size in SCU if no ship is provided",
    auto_load_only="Only show routes where both terminals support auto loading",
    system_filter="Only use routes in this star system"
)
@app_commands.choices(system_filter=[
    app_commands.Choice(name="Stanton", value="Stanton"),
    app_commands.Choice(name="Pyro", value="Pyro"),
    app_commands.Choice(name="Nyx", value="Nyx"),
])
async def route_command(
    interaction: discord.Interaction,
    name: str,
    ship: str = None,
    scu: app_commands.Range[int, 1, 100000] = None,
    auto_load_only: bool = False,
    system_filter: app_commands.Choice[str] = None
):
    await interaction.response.defer()

    try:
        selected_system = system_filter.value if system_filter else None

        if ship:
            ship_matches = await search_ships_scwiki(ship, cargo_only=True)

            if not ship_matches:
                await send_temp_followup(interaction, content="No matching ships found.", ephemeral=True)
                return

            if len(ship_matches) > 1:
                view = ShipDropdown(
                    ship_matches,
                    commodity_query=name,
                    mode="route",
                    buy_price_override=None,
                    auto_load_only=auto_load_only,
                    system_filter=selected_system
                )
                await send_temp_followup(
                    interaction,
                    content="Multiple ships found. Please choose:",
                    view=view,
                    delete_after=90
                )
                return

            chosen_ship = ship_matches[0]
            resolved_scu = _ship_scu(chosen_ship)

            if resolved_scu <= 0:
                await send_temp_followup(
                    interaction,
                    content="That ship does not have a usable cargo capacity in the API.",
                    ephemeral=True
                )
                return

            chosen_ship_name = _ship_display_name(chosen_ship)

        else:
            chosen_ship_name = None
            resolved_scu = int(scu) if scu is not None else None

        matches = await search_commodity_uex(name)

        if not matches:
            await send_temp_followup(interaction, content="No matching commodities found.", ephemeral=True)
            return

        if len(matches) > 1:
            view = CommodityDropdown(
                matches,
                mode="route",
                auto_load_only=auto_load_only,
                system_filter=selected_system,
                cargo_scu=resolved_scu,
                ship_name=chosen_ship_name
            )
            await send_temp_followup(
                interaction,
                content="Multiple commodities found. Please choose:",
                view=view,
                delete_after=90
            )
            return

        embed = await build_route_embed(
            matches[0],
            auto_load_only=auto_load_only,
            system_filter=selected_system,
            cargo_scu=resolved_scu,
            ship_name=chosen_ship_name
        )
        msg = await send_temp_followup(interaction, embed=embed)
        await log_star_command_usage(interaction, "route", message=msg)

    except Exception as e:
        print(f"[ERROR] /route failed: {e}")
        await send_temp_followup(
            interaction,
            content="❌ An unexpected error occurred while fetching route data.",
            ephemeral=True
        )

@route_command.autocomplete("name")
async def route_name_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    return await commodity_autocomplete(interaction, current)

@route_command.autocomplete("ship")
async def route_ship_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    return await ship_autocomplete(interaction, current)

@tree.command(name="terminal", description="Show what a terminal buys/sells")
@app_commands.describe(name="Terminal name (e.g. Area18, Lorville)")
async def terminal_command(interaction: discord.Interaction, name: str):
    await interaction.response.defer()

    try:
        matches = await search_terminal_uex(name)

        if not matches:
            await send_temp_followup(
                interaction,
                content="No matching terminals found.",
                ephemeral=True
            )
            return

        if len(matches) > 1:
            view = TerminalDropdown(matches)
            await send_temp_followup(
                interaction,
                content="Multiple terminals found. Please choose:",
                view=view,
                delete_after=90
            )
            return

        terminal = matches[0]
        terminal_id = terminal.get("id")

        data = await _uex_get("commodities_prices", params={"id_terminal": terminal_id})

        if not data:
            await send_temp_followup(
                interaction,
                content="No trade data found for this terminal."
            )
            return

        buy = [c for c in data if c.get("price_buy")]
        sell = [c for c in data if c.get("price_sell")]

        system_name = (
            terminal.get("star_system_name")
            or terminal.get("system_name")
            or terminal.get("name_star_system")
            or "Unknown"
        )

        embed = discord.Embed(
            title=f"🏪 {terminal.get('name', 'Unknown Terminal')}",
            description=f"Available trading commodities • {system_name}",
            color=0x3498DB
        )

        if buy:
            lines = [f"{c['commodity_name']} — `{c['price_buy']}`" for c in buy[:10]]
            embed.add_field(name="Buys", value="\n".join(lines), inline=False)

        if sell:
            lines = [f"{c['commodity_name']} — `{c['price_sell']}`" for c in sell[:10]]
            embed.add_field(name="Sells", value="\n".join(lines), inline=False)

        msg = await send_temp_followup(interaction, embed=embed)
        await log_star_command_usage(interaction, "terminal", message=msg)

    except Exception as e:
        print(f"[ERROR] /terminal failed: {e}")
        await send_temp_followup(
            interaction,
            content="Error fetching terminal data.",
            ephemeral=True
        )

@terminal_command.autocomplete("name")
async def terminal_name_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    return await terminal_autocomplete(interaction, current)

@tree.command(name="besttrade", description="Find most profitable trade routes")
async def besttrade_command(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        routes = await _uex_get("commodities_routes", params={"limit": 20})

        if not routes:
            msg = await send_temp_followup(
                interaction,
                content="No trade routes found."
            )
            await log_star_command_usage(interaction, "besttrade", message=msg)
            return

        routes = sorted(routes, key=lambda x: float(x.get("profit", 0)), reverse=True)

        embed = discord.Embed(
            title="💰 Best Trade Routes",
            description="Top profitable routes right now",
            color=0x2ECC71
        )

        lines = []
        for r in routes[:10]:
            origin = r.get("origin_terminal_name", "Unknown")
            dest = r.get("destination_terminal_name", "Unknown")
            commodity = r.get("commodity_name", "Unknown")
            profit = r.get("profit", "—")

            lines.append(f"**{commodity}**\n{origin} → {dest}\nProfit: `{profit}` aUEC")

        embed.add_field(name="Top Routes", value="\n\n".join(lines), inline=False)

        msg = await send_temp_followup(interaction, embed=embed)
        await log_star_command_usage(interaction, "besttrade", message=msg)

    except Exception as e:
        print(f"[ERROR] /besttrade failed: {e}")
        await send_temp_followup(
            interaction,
            content="Error fetching trade routes.",
            ephemeral=True
        )

@tree.command(name="ship", description="Show Star Citizen ship information.")
@app_commands.describe(
    name="Ship name, e.g. C2 Hercules, Vulture, Prospector, Perseus"
)
async def ship_command(
    interaction: discord.Interaction,
    name: str
):
    await interaction.response.defer()

    try:
        ship_matches = await search_ships_scwiki(name)

        if not ship_matches:
            await send_temp_followup(
                interaction,
                content="No matching ships found.",
                ephemeral=True
            )
            return

        chosen_ship = ship_matches[0]
        resolved_name = _ship_display_name(chosen_ship)

        # Try StarCitizen-API first
        ship_data = await fetch_ship_from_scapi(resolved_name)

        # If StarCitizen-API returns nothing, use your existing SCWiki ship data
        if not ship_data:
            print(f"[SHIP] SCAPI had no data for {resolved_name}; using SCWIKI fallback")
            ship_data = chosen_ship

        embed = build_ship_embed(ship_data)

        msg = await send_temp_followup(interaction, embed=embed)
        await log_star_command_usage(interaction, "ship", message=msg)

    except Exception as e:
        print(f"[ERROR] /ship failed: {e}")
        await send_temp_followup(
            interaction,
            content="❌ An unexpected error occurred while fetching ship data.",
            ephemeral=True
        )


@ship_command.autocomplete("name")
async def ship_name_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    return await ship_autocomplete(interaction, current)

@tree.command(name="cargo", description="Calculate cargo run profit for a Star Citizen commodity.")
@app_commands.describe(
    name="Commodity name, e.g. Gold, Agricium, Quantanium",
    ship="Optional ship name to auto-use its SCU capacity",
    scu="Manual ship cargo size in SCU if no ship is provided",
    buy_price="Optional manual buy price per SCU",
    auto_load_only="Only use terminals that support auto loading",
    system_filter="Only use terminals in this star system"
)
@app_commands.choices(system_filter=[
    app_commands.Choice(name="Stanton", value="Stanton"),
    app_commands.Choice(name="Pyro", value="Pyro"),
    app_commands.Choice(name="Nyx", value="Nyx"),
])
async def cargo_command(
    interaction: discord.Interaction,
    name: str,
    ship: str = None,
    scu: app_commands.Range[int, 1, 100000] = None,
    buy_price: app_commands.Range[float, 0, 1000000] = None,
    auto_load_only: bool = False,
    system_filter: app_commands.Choice[str] = None
):
    await interaction.response.defer()

    try:
        selected_system = system_filter.value if system_filter else None

        if ship:
            ship_matches = await search_ships_scwiki(ship, cargo_only=True)

            if not ship_matches:
                await send_temp_followup(
                    interaction,
                    content="No matching ships found.",
                    ephemeral=True
                )
                return

            if len(ship_matches) > 1:
                view = ShipDropdown(
                    ship_matches,
                    commodity_query=name,
                    mode="cargo",
                    buy_price_override=buy_price,
                    auto_load_only=auto_load_only,
                    system_filter=selected_system
                )
                await send_temp_followup(
                    interaction,
                    content="Multiple ships found. Please choose:",
                    view=view,
                    delete_after=90
                )
                return

            chosen_ship = ship_matches[0]
            resolved_scu = _ship_scu(chosen_ship)

            if resolved_scu <= 0:
                await send_temp_followup(
                    interaction,
                    content="That ship does not have a usable cargo capacity in the API.",
                    ephemeral=True
                )
                return
        else:
            if scu is None:
                await send_temp_followup(
                    interaction,
                    content="Please provide either a ship name or a manual SCU value.",
                    ephemeral=True
                )
                return

            chosen_ship = None
            resolved_scu = int(scu)

        matches = await search_commodity_uex(name)

        if not matches:
            await send_temp_followup(
                interaction,
                content="No matching commodities found.",
                ephemeral=True
            )
            return

        if len(matches) > 1:
            view = CommodityDropdown(
                matches,
                mode="cargo",
                auto_load_only=auto_load_only,
                system_filter=selected_system,
                cargo_scu=resolved_scu,
                buy_price_override=buy_price,
                ship_name=_ship_display_name(chosen_ship) if chosen_ship else None
            )
            prefix = ""
            if chosen_ship:
                prefix = f"Using **{_ship_display_name(chosen_ship)}** (`{resolved_scu}` SCU).\n"

            await send_temp_followup(
                interaction,
                content=prefix + "Multiple commodities found. Please choose:",
                view=view,
                delete_after=90
            )
            return

        embed = await build_cargo_embed(
            matches[0],
            cargo_scu=resolved_scu,
            buy_price_override=buy_price,
            auto_load_only=auto_load_only,
            system_filter=selected_system
        )

        if chosen_ship:
            embed.set_author(
                name=f"Ship: {_ship_display_name(chosen_ship)} • {resolved_scu} SCU"
            )

        msg = await send_temp_followup(interaction, embed=embed)
        await log_star_command_usage(interaction, "cargo", message=msg)

    except Exception as e:
        print(f"[ERROR] /cargo failed: {e}")
        await send_temp_followup(
            interaction,
            content="❌ An unexpected error occurred while calculating cargo profit.",
            ephemeral=True
        )

@cargo_command.autocomplete("name")
async def cargo_name_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    return await commodity_autocomplete(interaction, current)

@cargo_command.autocomplete("ship")
async def cargo_ship_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    return await ship_autocomplete(interaction, current)

@tree.command(name="trending", description="Show the most traded Star Citizen commodities right now.")
@app_commands.describe(limit="How many commodities to show (default 10)")
async def trending_command(
    interaction: discord.Interaction,
    limit: app_commands.Range[int, 1, 15] = 10
):
    await interaction.response.defer()

    try:
        embed = await build_trending_embed(limit=limit)
        msg = await send_temp_followup(interaction, embed=embed)
        await log_star_command_usage(interaction, "trending", message=msg)

    except Exception as e:
        print(f"[ERROR] /trending failed: {e}")
        await send_temp_followup(
            interaction,
            content="❌ An unexpected error occurred while fetching trending commodity data.",
            ephemeral=True
        )

@tree.command(name="bestnow", description="Show the single best Star Citizen trade right now.")
@app_commands.describe(
    ship="Optional ship name to calculate full-load profit",
    scu="Manual cargo size in SCU if no ship is provided",
    auto_load_only="Only show a trade where both terminals support auto loading",
    system_filter="Only show a trade where both terminals are in this star system"
)
@app_commands.choices(system_filter=[
    app_commands.Choice(name="Stanton", value="Stanton"),
    app_commands.Choice(name="Pyro", value="Pyro"),
    app_commands.Choice(name="Nyx", value="Nyx"),
])
async def bestnow_command(
    interaction: discord.Interaction,
    ship: str = None,
    scu: app_commands.Range[int, 1, 100000] = None,
    auto_load_only: bool = False,
    system_filter: app_commands.Choice[str] = None
):
    await interaction.response.defer()

    try:
        selected_system = system_filter.value if system_filter else None
        chosen_ship_name = None
        resolved_scu = int(scu) if scu is not None else None

        if ship:
            ship_matches = await search_ships_scwiki(ship, cargo_only=True)

            if not ship_matches:
                await send_temp_followup(
                    interaction,
                    content="No matching ships found.",
                    ephemeral=True
                )
                return

            if len(ship_matches) > 1:
                await send_temp_followup(
                    interaction,
                    content="Multiple ships found. Please choose the exact ship from autocomplete or type a more specific name.",
                    ephemeral=True
                )
                return

            chosen_ship = ship_matches[0]
            resolved_scu = _ship_scu(chosen_ship)
            chosen_ship_name = _ship_display_name(chosen_ship)

            if resolved_scu <= 0:
                await send_temp_followup(
                    interaction,
                    content="That ship does not have a usable cargo capacity in the API.",
                    ephemeral=True
                )
                return

        embed = await build_bestnow_embed(
            auto_load_only=auto_load_only,
            system_filter=selected_system,
            cargo_scu=resolved_scu,
            ship_name=chosen_ship_name
        )

        msg = await send_temp_followup(interaction, embed=embed)
        await log_star_command_usage(interaction, "bestnow", message=msg)

    except Exception as e:
        print(f"[ERROR] /bestnow failed: {e}")
        await send_temp_followup(
            interaction,
            content="❌ An unexpected error occurred while fetching the best trade.",
            ephemeral=True
        )

@bestnow_command.autocomplete("ship")
async def bestnow_ship_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    return await ship_autocomplete(interaction, current)

@tree.command(name="members", description="Show current Star Citizen organisation members.")
async def members_command(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        members = await fetch_org_members_scapi(SC_ORG_SID)

        if not members:
            msg = await send_temp_followup(
                interaction,
                content=f"No organisation members found for `{SC_ORG_SID}`."
            )
            await log_star_command_usage(interaction, "members", message=msg)
            return

        org_info = await fetch_org_info_scapi(SC_ORG_SID)
        embed = build_members_embed(SC_ORG_SID, members, org_info)

        msg = await send_temp_followup(interaction, embed=embed)
        await log_star_command_usage(interaction, "members", message=msg)

    except RuntimeError as e:
        await send_temp_followup(
            interaction,
            content=f"❌ {e}",
            ephemeral=True
        )

    except Exception as e:
        print(f"[ERROR] /members failed: {e}")
        await send_temp_followup(
            interaction,
            content="❌ An unexpected error occurred while fetching organisation members.",
            ephemeral=True
        )
# ---------------------------------------------------
# Reaction removal suppression so bot-initiated removals don't unregister users
# ---------------------------------------------------
pending_reaction_removals: set[tuple[int, int, str]] = set()

async def mark_suppressed_reaction(message_id: int, user_id: int, emoji_str: str, ttl: int = 10):
    key = (message_id, user_id, emoji_str)
    pending_reaction_removals.add(key)
    async def _clear():
        await asyncio.sleep(ttl)
        pending_reaction_removals.discard(key)
    asyncio.create_task(_clear())
# -------------------------
# Helpers for lineups
# -------------------------
def user_can_edit_lineup(member: discord.Member, lp: dict) -> bool:
    """Allow editors if they created it or they can create events (Moderator)."""
    return (member and (member.id == lp.get("creator_id"))) or user_can_create_events(member)

def _build_positions_for_formation(formation: str) -> list[dict]:
    codes = FORMATIONS.get(formation, [])
    return [{"code": c, "user_id": None} for c in codes]

async def _resolve_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    if not guild:
        return None
    m = guild.get_member(user_id)
    if m:
        return m
    try:
        return await guild.fetch_member(user_id)
    except Exception:
        return None
# -------------------------
# Helpers to manage thread membership
# -------------------------
async def add_user_to_event_thread(ev: dict, user_id: int):
    try:
        if not ev.get("thread_id"):
            return
        thread = client.get_channel(ev["thread_id"])
        if not isinstance(thread, discord.Thread):
            return
        guild = thread.guild
        member = guild.get_member(user_id) if guild else None
        if member is None and guild:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                member = None
        if member:
            try:
                await thread.add_user(member)
            except Exception:
                pass
    except Exception as e:
        print(f"[WARN] add_user_to_event_thread failed: {e}")

async def remove_user_from_event_thread_if_needed(ev: dict, user_id: int):
    try:
        if not ev.get("thread_id"):
            return
        still_should_be_in = (
            (user_id in ev.get("attend", [])) or
            (user_id in ev.get("maybe", [])) or
            (str(user_id) in (ev.get("attend_later_times") or {}))
        )
        if still_should_be_in:
            return
        thread = client.get_channel(ev["thread_id"])
        if not isinstance(thread, discord.Thread):
            return
        guild = thread.guild
        member = guild.get_member(user_id) if guild else None
        if member is None and guild:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                member = None
        if member:
            try:
                await thread.remove_user(member)
            except Exception:
                pass
    except Exception as e:
        print(f"[WARN] remove_user_from_event_thread_if_needed failed: {e}")

# Reaction add/remove handling (raw events to support uncached messages)
@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == client.user.id:
        return

    # =========================================================
    # SELF-SELECT ROLES (PHONICS)
    # =========================================================
    if payload.channel_id == SELF_ROLE_CHANNEL_ID and payload.message_id == SELF_ROLE_MESSAGE_ID:
        emoji = str(payload.emoji)
        role_id = SELF_SELECT_ROLES.get(emoji)

        if not role_id:
            return

        guild = client.get_guild(payload.guild_id)
        if guild is None:
            return

        member = guild.get_member(payload.user_id)
        if member is None:
            member = await guild.fetch_member(payload.user_id)

        role = guild.get_role(role_id)
        if role is None:
            return

        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Self-role toggle off")
                print(f"[SELF ROLES] Removed {role.name} from {member.display_name}")
            else:
                await member.add_roles(role, reason="Self-role toggle on")
                print(f"[SELF ROLES] Added {role.name} to {member.display_name}")

            channel = client.get_channel(payload.channel_id)
            if channel is None:
                channel = await client.fetch_channel(payload.channel_id)

            message = await channel.fetch_message(payload.message_id)

            await message.remove_reaction(payload.emoji, member)

        except Exception as e:
            print(f"[SELF ROLE ERROR] {e}")

        return

    ev = None
    for eid, e in events_store.get("events", {}).items():
        if e.get("message_id") == payload.message_id:
            ev = e
            break

    if not ev or ev.get("closed"):
        return

    emoji_str = str(payload.emoji)
    key = emoji_to_key(emoji_str)

    # 🚫 Block invalid reactions
    if not key:
        try:
            ch = client.get_channel(ev["channel_id"]) or await client.fetch_channel(ev["channel_id"])
            msg = await ch.fetch_message(ev["message_id"])
            guild = client.get_guild(payload.guild_id)
            user_obj = (guild.get_member(payload.user_id) if guild else None) or await client.fetch_user(payload.user_id)
            await mark_suppressed_reaction(ev["message_id"], payload.user_id, emoji_str)
            await msg.remove_reaction(payload.emoji, user_obj)
        except Exception:
            pass
        return

    # Fetch message (optional)
    try:
        ch = client.get_channel(ev["channel_id"]) or await client.fetch_channel(ev["channel_id"])
        msg = await ch.fetch_message(ev["message_id"])
    except Exception:
        ch = None
        msg = None

    uid = payload.user_id
    changed = False

    # 🕒 Attend Later — prompt for a time and stop here
    if key == "attend_later":
        # Remove from attend/absent/maybe
        for k in ("attend", "absent", "maybe"):
            if uid in ev.get(k, []):
                ev[k].remove(uid)

        # Clear any previous late time (forces selecting again)
        ev.setdefault("attend_later_times", {}).pop(str(uid), None)

        events_store["events"][str(ev["id"])] = ev
        save_events_store()

        # Update embed immediately
        if msg:
            try:
                await msg.edit(embed=make_event_embed(ev))
            except Exception:
                pass

            # Remove their 🕒 reaction so reactions don't pile up
            try:
                guild = client.get_guild(payload.guild_id)
                user_obj = (guild.get_member(uid) if guild else None) or await client.fetch_user(uid)
                await mark_suppressed_reaction(ev["message_id"], uid, emoji_str)
                await mark_suppressed_reaction(ev["message_id"], payload.user_id, emoji_str)
                await msg.remove_reaction(payload.emoji, user_obj)
            except Exception:
                pass

        # Prompt dropdown
        try:
            if ch is None:
                ch = client.get_channel(ev["channel_id"]) or await client.fetch_channel(ev["channel_id"])
            await ch.send(
                f"<@{uid}> You selected **Attend Later**. What time will you arrive?",
                view=AttendLaterTimeView(ev, uid)
            )
        except Exception:
            pass

        return

    # ✅ NORMAL reactions (attend / absent / maybe)

    # Remove from other lists when switching
    for k in ("attend", "absent", "maybe"):
        if k != key and uid in ev.get(k, []):
            ev[k].remove(uid)
            changed = True

    # Remove late time if switching away from Attend Later
    if str(uid) in ev.get("attend_later_times", {}):
        ev["attend_later_times"].pop(str(uid), None)
        changed = True

    # Add to chosen list
    if uid not in ev.get(key, []):
        ev.setdefault(key, []).append(uid)
        changed = True

    # Thread membership (Attend + Maybe stay in thread)
    if key in ("attend", "maybe"):
        asyncio.create_task(add_user_to_event_thread(ev, uid))
    else:
        asyncio.create_task(remove_user_from_event_thread_if_needed(ev, uid))

    # Save + update embed + remove the reaction (so reactions don’t accumulate)
    # Save + update embed + remove the reaction (so reactions don’t accumulate)
    if changed:
        events_store["events"][str(ev["id"])] = ev
        save_events_store()
    
        if msg:
            try:
                await msg.edit(embed=make_event_embed(ev))
    
                guild = client.get_guild(payload.guild_id)
                user_obj = (guild.get_member(uid) if guild else None) or await client.fetch_user(uid)
    
                await mark_suppressed_reaction(ev["message_id"], uid, emoji_str)
                await msg.remove_reaction(payload.emoji, user_obj)
    
            except Exception as e:
                print(f"[ERROR] Failed to update event embed or remove reaction: {e}")

@client.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.user_id == client.user.id:
        return

    emoji_str = str(payload.emoji)

    # ✅ Ignore bot-initiated removals (matches your mark_suppressed_reaction flow)
    key_tuple = (payload.message_id, payload.user_id, emoji_str)
    if key_tuple in pending_reaction_removals:
        pending_reaction_removals.discard(key_tuple)
        return

    ev = None
    for e in events_store.get("events", {}).values():
        if e.get("message_id") == payload.message_id:
            ev = e
            break
    if not ev:
        return

    key = emoji_to_key(emoji_str)
    if not key:
        return

    uid = payload.user_id

    # 🕒 Attend Later removal (mapping)
    if key == "attend_later":
        late_map = ev.get("attend_later_times") or {}
        if str(uid) in late_map:
            late_map.pop(str(uid), None)
            ev["attend_later_times"] = late_map
    else:
        # ✅ Normal lists
        if uid in ev.get(key, []):
            ev[key].remove(uid)

    events_store["events"][str(ev["id"])] = ev
    save_events_store()

    # Removal might mean they should leave the thread
    asyncio.create_task(remove_user_from_event_thread_if_needed(ev, uid))

    # Update embed
    try:
        ch = client.get_channel(ev["channel_id"]) or await client.fetch_channel(ev["channel_id"])
        msg = await ch.fetch_message(ev["message_id"])
        await msg.edit(embed=make_event_embed(ev))
    except Exception:
        pass

DB_POOL: asyncpg.pool.Pool | None = None
DATABASE_URL = os.getenv("DATABASE_URL")

async def init_db():
    """Create pool + table, and migrate data column to JSONB if needed."""
    global DB_POOL

    if DB_POOL:
        return

    if not DATABASE_URL:
        print("[INFO] DATABASE_URL not set — using local JSON storage only.")
        return

    DB_POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with DB_POOL.acquire() as con:
        # 1) Ensure table exists
        await con.execute("""
            CREATE TABLE IF NOT EXISTS app_store (
                name        TEXT PRIMARY KEY,
                data        JSONB NOT NULL,
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)

        # 2) If the existing `data` column is TEXT (from an earlier version), migrate it to JSONB.
        col_type = await con.fetchval("""
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'app_store'
              AND column_name = 'data';
        """)

        if (col_type or "").lower() != "jsonb":
            await con.execute("""
            ALTER TABLE app_store
            ALTER COLUMN data TYPE JSONB USING
              CASE
                WHEN data IS NULL THEN '{}'::jsonb
                WHEN data ~ '^[\\s]*[{\\[]' THEN data::jsonb
                ELSE to_jsonb(data)
              END;
            """)

            logging.info("Migrated app_store.data to JSONB")

async def db_load_json(name: str, default_obj):
    """Load a JSON object by logical file name; insert default if missing."""
    assert DB_POOL, "DB not initialized"
    async with DB_POOL.acquire() as con:
        row = await con.fetchrow("SELECT data FROM app_store WHERE name=$1", name)
        if row and row["data"] is not None:
            val = row["data"]
            # Handle both cases: jsonb (dict) or text (str)
            if isinstance(val, str):
                try:
                    return json.loads(val)
                except Exception:
                    return default_obj
            return dict(val)
        
        # seed with default if not present (send JSON text, cast to jsonb)
        await con.execute(
            "INSERT INTO app_store (name, data) VALUES ($1, $2::jsonb)",
            name,
            json.dumps(default_obj),
        )
        return default_obj

async def db_save_json(name: str, obj):
    """Upsert JSON by name."""
    assert DB_POOL, "DB not initialized"
    async with DB_POOL.acquire() as con:
        await con.execute("""
            INSERT INTO app_store (name, data, updated_at)
            VALUES ($1, $2::jsonb, now())
            ON CONFLICT (name) DO UPDATE
              SET data = EXCLUDED.data,
                  updated_at = now();
        """, name, json.dumps(obj))

async def db_incr_offside() -> int:
    """
    Atomically increment and return the offside counter.
    Stored under name 'offside.json' in app_store (JSONB).
    """
    assert DB_POOL, "DB not initialized"
    async with DB_POOL.acquire() as con:
        row = await con.fetchrow("""
            INSERT INTO app_store (name, data, updated_at)
            VALUES ($1, '{"count":1}', now())
            ON CONFLICT (name) DO UPDATE
              SET data = jsonb_set(app_store.data, '{count}',
                                   to_jsonb(COALESCE((app_store.data->>'count')::int, 0) + 1)),
                  updated_at = now()
            RETURNING (data->>'count')::int AS count;
        """, "offside.json")
        return int(row["count"])

# -------------------------
# Twitch live monitor (with live updates)
# -------------------------
async def monitor_twitch_live():
    """
    Announce Twitch live status in multiple Discord channels.
    """

    state = {
        "live_stream_id": None,
        "messages": {}
    }

    try:
        state = await db_load_json(TWITCH_STATE_KEY, state)
        if "messages" not in state:
            state["messages"] = {}
    except Exception:
        pass

    def _twitch_url() -> str:
        return f"https://twitch.tv/{TWITCH_CHANNEL_LOGIN}"

    async def save_state():
        try:
            await db_save_json(TWITCH_STATE_KEY, state)
        except Exception:
            pass

    async def send_live_message(channel_id: int, stream: dict):
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)

            game_box = await twitch_get_game_box_art_url(stream.get("game_id"))
            embed = make_twitch_live_embed(stream, game_box)

            role_id = TWITCH_LIVE_ROLE_IDS.get(channel_id)

            content = f"||<@&{role_id}>||" if role_id else None
            allowed = (
                discord.AllowedMentions(roles=[discord.Object(id=role_id)])
                if role_id
                else None
            )

            msg = await channel.send(
                content=content,
                embed=embed,
                view=WatchButtonView(_twitch_url()),
                allowed_mentions=allowed
            )

            state["messages"][str(channel_id)] = {
                "channel_id": channel.id,
                "message_id": msg.id
            }

            print(f"[TWITCH] Posted live message in channel {channel_id}")

        except Exception as e:
            print(f"[ERROR] Could not send Twitch live message to channel {channel_id}: {e}")

    async def delete_live_messages():
        for channel_id, saved in list(state.get("messages", {}).items()):
            try:
                ch_id = int(saved.get("channel_id"))
                msg_id = int(saved.get("message_id"))

                channel = client.get_channel(ch_id) or await client.fetch_channel(ch_id)
                msg = await channel.fetch_message(msg_id)
                await msg.delete()

                print(f"[TWITCH] Deleted live message in channel {ch_id}")

            except Exception as e:
                print(f"[WARN] Could not delete Twitch live message for channel {channel_id}: {e}")

        state["messages"] = {}

    async def update_live_messages(stream: dict):
        for channel_id in TWITCH_ANNOUNCE_CHANNEL_IDS:
            saved = state.get("messages", {}).get(str(channel_id))

            if not saved:
                await send_live_message(channel_id, stream)
                continue

            try:
                ch_id = int(saved.get("channel_id"))
                msg_id = int(saved.get("message_id"))

                channel = client.get_channel(ch_id) or await client.fetch_channel(ch_id)
                msg = await channel.fetch_message(msg_id)

                game_box = await twitch_get_game_box_art_url(stream.get("game_id"))
                embed = make_twitch_live_embed(stream, game_box)

                await msg.edit(
                    embed=embed,
                    view=WatchButtonView(_twitch_url())
                )

            except Exception as e:
                print(f"[WARN] Could not update Twitch live message in channel {channel_id}: {e}")
                await send_live_message(channel_id, stream)

    while not client.is_closed():
        try:
            if not (
                TWITCH_CLIENT_ID
                and TWITCH_CLIENT_SECRET
                and TWITCH_CHANNEL_LOGIN
                and TWITCH_ANNOUNCE_CHANNEL_IDS
            ):
                await asyncio.sleep(max(TWITCH_POLL_INTERVAL, 30))
                continue

            stream = await twitch_get_stream_by_login(TWITCH_CHANNEL_LOGIN)
            is_live_now = stream is not None
            was_live = bool(state.get("live_stream_id"))

            if is_live_now and not was_live:
                for channel_id in TWITCH_ANNOUNCE_CHANNEL_IDS:
                    await send_live_message(channel_id, stream)

                state["live_stream_id"] = stream.get("id")
                await save_state()

            elif not is_live_now and was_live:
                await delete_live_messages()

                state["live_stream_id"] = None
                await save_state()

            elif is_live_now:
                await update_live_messages(stream)
                state["live_stream_id"] = stream.get("id")
                await save_state()

        except Exception as e:
            print(f"[ERROR] monitor_twitch_live tick failed: {e}")

        await asyncio.sleep(TWITCH_POLL_INTERVAL if TWITCH_POLL_INTERVAL > 0 else 60)

# -------------------------
# Command sync (global + optional guild)
# -------------------------
@client.event
async def on_ready():
    # --- DB bootstrap + load persistent state ---
    global events_store, templates_store, lineups_store

    await init_db()

    try:
        await honeypot_startup()
    except Exception:
        logging.exception("[HONEYPOT] Startup failed; check state and permissions")

    if DB_POOL:
        try:
            # Pull latest snapshots for each store from Postgres
            events_store = await db_load_json(EVENTS_FILE, {"next_id": 1, "events": {}})
            templates_store = await db_load_json(TEMPLATES_FILE, {})
            lineups_store = await db_load_json(LINEUPS_FILE, {"next_id": 1, "lineups": {}})
            print("🗄️ Loaded stores from Postgres.")
        except Exception as e:
            print(f"[ERROR] Postgres load failed: {e}")
    else:
        print("🗄️ Postgres skipped — using local JSON storage only.")
        # (Optional) raise here if persistence is required
        # raise
        # --- command sync for multiple guilds ---
    try:
        guild_ids = [
            int(x.strip())
            for x in os.getenv("GUILD_IDS", "").split(",")
            if x.strip()
        ]

        if not guild_ids:
            print("[WARN] GUILD_IDS not set or empty")
        else:
            for gid in guild_ids:
                try:
                    guild = client.get_guild(gid) or await client.fetch_guild(gid)

                    tree.clear_commands(guild=guild)
                    tree.copy_global_to(guild=guild)

                    cmds = await tree.sync(guild=guild)
                    print(f"✅ Synced {len(cmds)} commands to guild {gid}")

                except Exception as e:
                    print(f"[ERROR] Failed to sync commands to guild {gid}: {e}")

            tree.clear_commands(guild=None)
            await tree.sync()
            print("🧹 Cleared global commands")

    except Exception as e:
        print(f"[ERROR] Command sync failed: {e}")

    print(f"Bot is ready as {client.user}")
    await warm_ea_session()
    asyncio.create_task(warm_scwiki_ship_cache())
    
    # Run background tasks once (avoid duplicates on reconnect)
    if not getattr(client, "background_started", False):
        try:
            client.loop.create_task(rotate_presence())
            print("🌀 Presence rotation started.")
        except Exception as e:
            print(f"[ERROR] Could not start presence rotation: {e}")

        try:
            client.loop.create_task(ea_top100_update_loop())
            print("🏆 EA Top 100 updater started.")
        except Exception as e:
            print(f"[ERROR] Could not start EA Top 100 updater: {e}")
    
        try:
            client.loop.create_task(monitor_twitch_live())
            print("📡 Twitch live monitor started.")
        except Exception as e:
            print(f"[ERROR] Could not start Twitch monitor: {e}")
    
        client.background_started = True

    announce_channel_ids = [
        int(x.strip())
        for x in os.getenv("ANNOUNCE_CHANNEL_IDS", "").split(",")
        if x.strip()
    ]
    
    for channel_id in announce_channel_ids:
        channel = client.get_channel(channel_id)
    
        if channel:
            message = await channel.send("✅ - Phonics Bot is now online and ready for commands!")
    
            async def delete_after_announcement(msg):
                await asyncio.sleep(60)
                try:
                    await msg.delete()
                except Exception as e:
                    print(f"[ERROR] Failed to auto-delete announcement message: {e}")
    
            asyncio.create_task(delete_after_announcement(message))
        else:
            print(f"[WARN] Could not find announce channel with ID {channel_id}")

    # =========================================================
    # ADD SELF-ROLE REACTIONS
    # =========================================================

    try:
        channel = client.get_channel(SELF_ROLE_CHANNEL_ID)

        if channel is None:
            channel = await client.fetch_channel(SELF_ROLE_CHANNEL_ID)

        message = await channel.fetch_message(SELF_ROLE_MESSAGE_ID)

        for emoji in SELF_SELECT_ROLES.keys():
            await message.add_reaction(emoji)

        print("[SELF ROLES] Reactions added successfully.")

    except Exception as e:
        print(f"[SELF ROLES] Failed to add reactions: {e}")

client.run(TOKEN)
