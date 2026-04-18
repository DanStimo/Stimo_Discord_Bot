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

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CLUB_ID = os.getenv("CLUB_ID", "167054")
PLATFORM = os.getenv("PLATFORM", "gen5")
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
TWITCH_ANNOUNCE_CHANNEL_ID = int(os.getenv("TWITCH_ANNOUNCE_CHANNEL_ID", "0"))
TWITCH_LIVE_ROLE_ID = int(os.getenv("TWITCH_LIVE_ROLE_ID", "0"))
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
FREE_STATS_CHANNEL_ID = 1362795404185305129
# Channel where we log free-typed stats lookups
LOG_CHANNEL_ID = 1383731281577246810

CREST_URL_TEMPLATE = os.getenv("CREST_URL_TEMPLATE", "").strip()

def build_crest_url(team_id: str | int) -> str | None:
    """Return a crest URL from teamId using your template, or None if not set."""
    if not team_id or not CREST_URL_TEMPLATE:
        return None
    return CREST_URL_TEMPLATE.format(teamId=str(team_id))

async def get_team_id_for_club(club_id: str | int) -> str | None:
    """
    Try to find teamId for a club:
      1) overallStats (fast, single call)
      2) fall back to the newest matches and read clubs[club_id].details.teamId
    """
    club_id = str(club_id)

    # 1) overallStats
    try:
        r = await _client_ea.get(
            "https://proclubs.ea.com/api/fc/clubs/overallStats",
            params={"platform": PLATFORM, "clubIds": club_id},
        )
        if r.status_code == 200:
            data = r.json() or []
            if isinstance(data, list) and data:
                tid = data[0].get("teamId")
                if tid:
                    return str(tid)
    except Exception as e:
        print(f"[crest] overallStats lookup failed: {e}")

    # 2) matches fallback (check newest first among common types)
    try:
        match_types = ["leagueMatch", "playoffMatch", "friendlyMatch"]
        newest = []
        for mt in match_types:
            mres = await _client_ea.get(
                "https://proclubs.ea.com/api/fc/clubs/matches",
                params={"matchType": mt, "platform": PLATFORM, "clubIds": club_id},
            )
            if mres.status_code == 404:
                continue
            mres.raise_for_status()
            arr = mres.json() or []
            newest.extend(arr)
        newest.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        for m in newest:
            clubs = m.get("clubs", {}) or {}
            mine = clubs.get(club_id) or {}
            details = mine.get("details") or {}
            tid = details.get("teamId")
            if tid:
                return str(tid)
    except Exception as e:
        print(f"[crest] matches lookup failed: {e}")

    return None

# === Welcome Feature ===
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0"))
WELCOME_COLOR_HEX = os.getenv("WELCOME_COLOR_HEX", "#2ecc71")

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
    h = (h or "#2ecc71").strip().lstrip("#")
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

@client.event
async def on_member_join(member: discord.Member):
    print(f"[JOIN] on_member_join fired for {member} (id={member.id})")

    # --- Hardcoded config ---
    WELCOME_CHANNEL_ID = 1361690632392933527        # 👈 replace with your welcome channel ID
    WELCOME_COLOR = 0x3498DB                       # 👈 green color, hex without '#'
    MEMBER_ROLE_ID = 1361661691590606929            # 👈 replace with your Member role ID

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
            f"{member.mention}, you've reached **Stimo's** Discord server!\n\n"
            "• **Read the rules:** <#1362311374293958856>\n"
            "• **Grab roles:** <#1361921570104283186>\n"
            "• **Say hi!:** <#1361690632392933527> 👋"
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
    embed.set_footer(text="omitS Bot", icon_url="https://i.imgur.com/Uy3fdb1.png")

    # --- Send and react ---
    try:
        perms = channel.permissions_for(channel.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links and perms.add_reactions):
            print("[ERROR] Missing one of: View Channel / Send Messages / Embed Links / Add Reactions in welcome channel.")
            return

        message = await channel.send(content=member.mention, embed=embed)

        # react with custom emoji named "Wave"
        emoji = discord.utils.get(member.guild.emojis, name="Wave")
        if emoji:
            await message.add_reaction(emoji)
        else:
            print("[WARN] Could not find custom emoji 'Wave' in this server; skipping reaction.")

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

@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    if message.channel.id != FREE_STATS_CHANNEL_ID:
        return

    content = (message.content or "").strip()
    if not content or content.startswith(("/", "!", ".", "?")):
        return
    if len(content) < 2 or len(content) > 64:
        return

    try:
        async with message.channel.typing():
            # If they typed a clubId directly
            if content.isdigit():
                club_id = content
                found = await search_clubs_ea(content)
                club_name = str(found[0]["clubInfo"]["name"]) if found else f"Club {club_id}"

                # 🔵 LOG: numeric path
                #await log_free_stats(message, query=content, resolved=f"{club_name} (ID {club_id})")

                # delete the user's post so our response "replaces" it
                asyncio.create_task(safe_delete(message))
                await send_stats_message_to_channel(message.channel, club_id, club_name, origin_message=message)
                return

            # Search by name
            matches = await search_clubs_ea(content)
            if not matches:
                # 🔵 LOG: no matches
                #await log_free_stats(message, query=content, resolved="no matches")

                # delete the user’s message and show a short-lived note
                asyncio.create_task(safe_delete(message))
                m = await message.channel.send("No matching clubs found.")
                asyncio.create_task(delete_after_delay(m, 15))
                return

            if len(matches) == 1:
                c = matches[0]["clubInfo"]

                # 🔵 LOG: single match resolved
                #await log_free_stats(message, query=content, resolved=f"{c['name']} (ID {c['clubId']})")

                asyncio.create_task(safe_delete(message))
                await send_stats_message_to_channel(message.channel, str(c["clubId"]), c["name"], origin_message=message)
                return

            # 🔵 LOG: multiple matches, unresolved selection
            #await log_free_stats(message, query=content, resolved="multiple matches")

            # Multiple matches → present selector; delete the original user message
            asyncio.create_task(safe_delete(message))
            view = FreeStatsDropdown(matches, original_query=content, request_message=message)
            m = await message.channel.send("Multiple clubs found. Please select:", view=view)
            asyncio.create_task(delete_after_delay(m, 90))

    except Exception as e:
        print(f"[ERROR] free-typed stats failed: {e}")
        
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
        {"platform": PLATFORM, "clubName": query.strip()},
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
    last5_task = asyncio.create_task(get_last5_matches_summary(club_id))
    teamid_task = asyncio.create_task(get_team_id_for_club(club_id))
    squad_task = asyncio.create_task(get_current_squad(club_id))

    stats = await stats_task
    recent_form = await form_task
    days_since = await days_task
    rank = await rank_task
    last5 = await last5_task
    team_id = await teamid_task
    current_squad = await squad_task

    rank_display = f"#{rank}" if (isinstance(rank, int) or (isinstance(rank, str) and str(rank).isdigit())) else "Unranked"
    days_display = f"{days_since} day(s) ago" if days_since is not None else "—"
    form_string = " ".join(recent_form) if recent_form else "No recent matches"

    return {
        "stats": stats or {},
        "rank_display": rank_display,
        "recent_form": form_string,
        "last5": last5 or "No recent matches",
        "days_display": days_display,
        "teamId": team_id,
        "current_squad": current_squad,
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

    return (
        f"⚽ Top scorer: {join_names(top_scorers)} ({best_goals})\n"
        f"🅰️ Top assister: {join_names(top_assisters)} ({best_assists})\n"
        f"⭐ Best avg rating: {join_names(best_rated_players)} ({best_rating:.1f})"
    )

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
                totals[name] = {"appearances": 0}

            totals[name]["appearances"] += 1

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
        stat_keys.update(player_stats.keys())

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

def _format_stats5_team_totals(totals: dict) -> str:
    apps = 0
    goals = 0
    assists = 0
    shots = 0
    pass_attempts = 0
    pass_completed = 0
    tackle_attempts = 0
    tackles_won = 0
    yc = 0
    rc = 0
    rating_sum = 0.0
    rating_count = 0

    for _, stats in totals.items():
        apps += int(stats.get("appearances", 0) or 0)
        goals += int(stats.get("goals", 0) or 0)
        assists += int(stats.get("assists", 0) or 0)
        shots += int(stats.get("shots", 0) or 0)

        pass_attempts += int(stats.get("passattempts", 0) or 0)
        pass_completed += int(stats.get("passesmade", 0) or 0)

        tackle_attempts += int(stats.get("tackleattempts", 0) or 0)
        tackles_won += int(stats.get("tacklesmade", 0) or 0)

        rc += int(stats.get("redcards", 0) or 0)

        player_apps = int(stats.get("appearances", 0) or 0)
        player_rating_total = float(stats.get("rating", 0) or 0)
        if player_apps > 0:
            rating_sum += player_rating_total / player_apps
            rating_count += 1

    pass_pct = round((pass_completed / pass_attempts) * 100) if pass_attempts else 0
    tackle_pct = round((tackles_won / tackle_attempts) * 100) if tackle_attempts else 0
    avg_rating = round(rating_sum / rating_count, 1) if rating_count else 0.0

    return (
        f"{'TEAM':<12}"
        f"{goals:>3}"
        f"{assists:>3}"
        f"{shots:>4}"
        f"{pass_pct:>4}%"
        f"{tackle_pct:>4}%"
        f"{avg_rating:>5.1f}"
        f"{rc:>3}"
    )

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

    yc = int(stats.get("yellowcards", 0))
    rc = int(stats.get("redcards", 0))

    rating_total = float(stats.get("rating", 0) or 0)
    rating = round(rating_total / apps, 1) if apps else 0

    name = player_name[:12]

    return (
        f"{name:<12}"
        f"{goals:>3}"
        f"{assists:>3}"
        f"{shots:>4}"
        f"{pass_pct:>4}%"
        f"{tackle_pct:>4}%"
        f"{rating:>5.1f}"
        f"{rc:>3}"
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

    team_id = await get_team_id_for_club(club_id)
    crest_url = build_crest_url(team_id) if team_id else None

    base_title = f"📊 {club_name.upper()} — LAST 5 PLAYER TOTALS"
    subtitle = f"Across League, Playoff and Friendly matches ({len(matches)} matches)"

    player_items = sorted(
        totals.items(),
        key=lambda item: (
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

    team_totals_row = _format_stats5_team_totals(totals)
    leaders_text = _build_stats5_leaders_text(totals)

    header = (
        f"{'Player':<12}"
        f"{'G':>3}"
        f"{'A':>3}"
        f"{'Sh':>4}"
        f"{'PA%':>5}"
        f"{'TK%':>5}"
        f"{'Rt':>5}"
        f"{'RC':>3}"
    )
    divider = "-" * len(header)

    pages = []
    current_rows = []
    current_len = len(header) + len(divider) + 20

    for row in rows:
        extra_len = len(row) + 1
        if len(current_rows) >= 20 or current_len + extra_len > 3500:
            pages.append(current_rows)
            current_rows = []
            current_len = len(header) + len(divider) + 20

        current_rows.append(row)
        current_len += extra_len

    if current_rows:
        pages.append(current_rows)

    if not pages:
        return []

    embeds = []

    for idx, page_rows in enumerate(pages, start=1):
        table_body = "\n".join(page_rows)

        if idx == len(pages):
            table = (
                "```text\n"
                + header + "\n"
                + divider + "\n"
                + table_body + "\n"
                + divider + "\n"
                + team_totals_row + "\n```"
            )
        else:
            table = (
                "```text\n"
                + header + "\n"
                + divider + "\n"
                + table_body + "\n```"
            )

        embed = discord.Embed(
            title=base_title,
            description=f"{subtitle}\nPage {idx}/{len(pages)}\n\n{leaders_text}",
            color=0xB30000
        )

        if crest_url:
            embed.set_thumbnail(url=crest_url)

        embed.add_field(name="Totals", value=table, inline=False)
        embed.set_footer(text=f"EAFC — Aggregated from the most recent {len(matches)} matches")
        embeds.append(embed)

    return embeds

# Helpers + embed builder for /stats
ZWSP = "\u200b"

def _field(name: str, value: str, inline: bool = True) -> dict:
    return {"name": name, "value": value if value else "—", "inline": inline}

def _spacer(inline: bool = True) -> dict:
    return {"name": ZWSP, "value": ZWSP, "inline": inline}

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
        color=0xB30000
    )

    # ✅ Crest thumbnail
    team_id = data.get("teamId")
    crest_url = build_crest_url(team_id) if team_id else None
    if crest_url:
        embed.set_thumbnail(url=crest_url)

    fields: list[dict] = []

    # Row 1 — two columns (+spacer for grid)
    fields += [
        _field("Leaderboard Rank", f"📈 {rank_display}", inline=True),
        _field("Skill Rating", f"🏅 {sr}", inline=True),
        _spacer(True),
    ]

    # Row 2 — full width
    fields.append(_field("Matches Played", f"📊 {mp}", inline=False))

    # Row 3 — full width (single line W-D-L, no emojis)
    fields.append(_field("W-D-L", f"{wins} - {draws} - {losses}", inline=False))

    # Row 4 — two columns
    fields += [
        _field("Win Streak", f"🔥 {wstreak}", inline=True),
        _field("Unbeaten Streak", f"🛡️ {ubstreak}", inline=True),
        _spacer(True),
    ]

    # Row 5 — full width (Last 5)
    fields.append(_field("Last 5 Matches", last5, inline=False))

    # Row 6 — Current Squad (full width)
    squad_list = data.get("current_squad", []) or []
    if squad_list:
        # Escape markdown so underscores or asterisks don’t format names
        squad_text = ", ".join(escape_markdown(n) for n in squad_list)
        if len(squad_text) > 1000:
            allowed = 980
            truncated = squad_text[:allowed].rsplit(",", 1)[0]
            omitted = len(squad_list) - len(truncated.split(","))
            squad_text = f"{truncated} … (+{omitted} more)"
    else:
        squad_text = "—"
    
    fields.append(_field("Current Squad", squad_text, inline=False))

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
    Return a string with names displayed in `cols` columns, balanced top-to-bottom.
    Uses simple spacing; NOT a code block so markdown is escaped beforehand.
    """
    if not names:
        return "—"
    escaped = [escape_markdown(n) for n in names]
    rows = math.ceil(len(escaped) / cols)
    # build columns as lists
    columns = []
    for c in range(cols):
        start = c * rows
        columns.append(escaped[start:start + rows])
    # pad columns to equal length for zipping
    for col in columns:
        while len(col) < rows:
            col.append("")  # empty filler
    # compute column widths (for nicer alignment inside a code block)
    col_widths = [max((len(x) for x in col), default=0) for col in columns]
    # build lines
    lines = []
    for r in range(rows):
        parts = []
        for c in range(cols):
            name = columns[c][r]
            if not name:
                parts.append(" " * col_widths[c])
            else:
                parts.append(name.ljust(col_widths[c]))
        lines.append("  ".join(parts).rstrip())
    # Return as a code block (monospace) so spacing lines up
    return "```\n" + "\n".join(lines) + "\n```"

async def rotate_presence():
    await client.wait_until_ready()

    guild_id = int(os.getenv("GUILD_ID", "0"))
    role_id = int(os.getenv("WATCH_ROLE_ID", "1361661691590606929"))
    role_name = os.getenv("WATCH_ROLE_NAME", "Member")

    if not guild_id:
        print("[WARN] GUILD_ID not set – cannot rotate presence by role.")
        return

    guild = client.get_guild(guild_id)
    if guild is None:
        try:
            guild = await client.fetch_guild(guild_id)
        except Exception as e:
            print(f"[ERROR] Could not fetch guild {guild_id}: {e}")
            return

    try:
        # attempt to populate members cache
        await guild.fetch_members(limit=None).flatten()
    except AttributeError:
        try:
            async for _ in guild.fetch_members(limit=None):
                pass
        except Exception as e:
            print(f"[WARN] Could not fully fetch members: {e}")
    except Exception as e:
        print(f"[WARN] Could not fully fetch members: {e}")

    def get_candidates() -> list[discord.Member]:
        role = None
        if role_id:
            role = guild.get_role(role_id)
        if role is None and role_name:
            role = discord.utils.get(guild.roles, name=role_name)

        if role is None:
            print("[WARN] Target role not found; presence rotation will skip.")
            return []

        members = [m for m in role.members if not m.bot]
        return members

    while not client.is_closed():
        try:
            candidates = get_candidates()

            if candidates:
                pick = random.choice(candidates)
                watching_text = f"{pick.display_name} 👀"
            else:
                watching_text = "the club 👀"

            activity = discord.Activity(
                type=discord.ActivityType.watching,
                name=watching_text
            )
            await client.change_presence(activity=activity)

        except Exception as e:
            print(f"[ERROR] Failed to rotate presence: {e}")

        await asyncio.sleep(300)

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

    return exact + partial

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

SCWIKI_VEHICLES_URL = "https://api.star-citizen.wiki/api/vehicles"

_ship_cache = None

async def get_all_ships_scwiki():
    global _ship_cache

    if _ship_cache is not None:
        return _ship_cache

    try:
        r = await _client_uex.get(SCWIKI_VEHICLES_URL)
        if r.status_code != 200:
            print(f"[SCWIKI] {r.status_code} {SCWIKI_VEHICLES_URL} :: {r.text[:300]}")
            _ship_cache = []
            return _ship_cache

        payload = r.json()
        if isinstance(payload, dict):
            data = payload.get("data", [])
        elif isinstance(payload, list):
            data = payload
        else:
            data = []

        _ship_cache = data if isinstance(data, list) else []
        return _ship_cache

    except Exception as e:
        print(f"[SCWIKI] exception loading ships :: {e}")
        _ship_cache = []
        return _ship_cache


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

async def search_ships_scwiki(query: str) -> list[dict]:
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

    def dedupe(ship_list: list[dict]) -> list[dict]:
        seen = set()
        out = []
        for ship in ship_list:
            sid = ship_id(ship)
            if sid in seen:
                continue
            seen.add(sid)
            out.append(ship)
        return out

    # match first, filter usable SCU later
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

    exact = [s for s in dedupe(exact) if _ship_scu(s) > 0]
    if exact:
        return exact[:25]

    token_matches = [s for s in dedupe(token_matches) if _ship_scu(s) > 0]
    if token_matches:
        return token_matches[:25]

    partial = [s for s in dedupe(partial) if _ship_scu(s) > 0]
    if partial:
        return partial[:25]

    # fuzzy fallback only if direct matching found nothing
    choices = []
    choice_to_ship = {}

    for ship in ships:
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
        if _ship_scu(ship) <= 0:
            continue

        sid = ship_id(ship)
        if sid in seen:
            continue

        seen.add(sid)
        results.append(ship)

    return results[:25]

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
                f"**[{system_name}] {terminal}** — Buy: `{int(buy_price)}` • Profit: `+{profit}` • Stock: `{stock}`"
            )

        embed.add_field(name="Best Buy", value="\n".join(lines), inline=False)

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
    system_filter: str | None = None
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
            x[3],  # origin system
            float(x[0].get("profit") or x[0].get("profit_total") or 0)
        ),
        reverse=False
    )

    # Within each system grouping, highest profit first
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
        profit = r.get("profit") or r.get("profit_total") or "—"

        lines.append(
            f"**[{origin_system}] {origin} {auto_icon(origin_auto)} → [{destination_system}] {destination} {auto_icon(destination_auto)}**\n"
            f"Profit: `{profit}` aUEC • Margin: `{margin}`"
        )

    embed.add_field(name="Top Routes", value="\n\n".join(lines)[:1024], inline=False)

    footer_bits = ["Star Citizen — UEX"]
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
        buy_price_override: float | None = None
    ):
        super().__init__(timeout=90)
        self.results = results
        self.mode = mode
        self.auto_load_only = auto_load_only
        self.system_filter = system_filter
        self.cargo_scu = cargo_scu
        self.buy_price_override = buy_price_override

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
        elif self.mode == "route":
            embed = await build_route_embed(
                chosen,
                auto_load_only=self.auto_load_only,
                system_filter=self.system_filter
            )
        elif self.mode == "cargo":
            embed = await build_cargo_embed(
                chosen,
                cargo_scu=self.cargo_scu,
                buy_price_override=self.buy_price_override,
                auto_load_only=self.auto_load_only,
                system_filter=self.system_filter
            )
        else:
            await interaction.edit_original_response(
                content="Unknown dropdown mode.",
                view=None
            )
            return

        await interaction.edit_original_response(content=None, embed=embed, view=None)

class ShipDropdown(discord.ui.View):
    def __init__(
        self,
        ship_results: list[dict],
        commodity_query: str,
        buy_price_override: float | None = None,
        auto_load_only: bool = False,
        system_filter: str | None = None
    ):
        super().__init__(timeout=90)
        self.ship_results = ship_results
        self.commodity_query = commodity_query
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
                mode="cargo",
                auto_load_only=self.auto_load_only,
                system_filter=self.system_filter,
                cargo_scu=ship_scu,
                buy_price_override=self.buy_price_override
            )
            await interaction.edit_original_response(
                content=f"Using **{_ship_display_name(chosen_ship)}** (`{ship_scu}` SCU).\nMultiple commodities found. Please choose:",
                view=view,
                embed=None
            )
            return

        embed = await build_cargo_embed(
            commodity_matches[0],
            cargo_scu=ship_scu,
            buy_price_override=self.buy_price_override,
            auto_load_only=self.auto_load_only,
            system_filter=self.system_filter
        )

        embed.set_author(
            name=f"Ship: {_ship_display_name(chosen_ship)} • {ship_scu} SCU"
        )

        await interaction.edit_original_response(content=None, embed=embed, view=None)
        
async def search_terminal_uex(query: str) -> list[dict]:
    data = await _uex_get("terminals")
    if not isinstance(data, list):
        return []

    q = _normalize_sc_name(query)
    return [t for t in data if q in _normalize_sc_name(t.get("name", ""))]

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

        view = PrintRecordButton({"matchesPlayed": data["stats"].get("matchesPlayed"),
                          "wins": data["stats"].get("wins"),
                          "draws": data["stats"].get("draws"),
                          "losses": data["stats"].get("losses"),
                          "skillRating": data["stats"].get("skillRating")},
                         (club_name or f"Club {club_id}").upper())
        final_msg = await interaction.edit_original_response(content=None, embed=embed, view=view)

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
        final_msg = await interaction.edit_original_response(content=None, embed=embed, view=view)
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
    team_id = await get_team_id_for_club(club_id)
    crest_url = build_crest_url(team_id) if team_id else None
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
        club_data = clubs.get(club_id)
        opponent_id = next((cid for cid in clubs if cid != club_id), None)
        opponent_data = clubs.get(opponent_id) if opponent_id else {}

        our_name = club_data.get("details", {}).get("name", club_data.get("name", "Unknown")) if club_data else "Unknown"
        opponent_name = opponent_data.get("details", {}).get("name", opponent_data.get("name", "Unknown")) if opponent_data else "Unknown"
        our_score = int(club_data.get("goals", 0)) if club_data else 0
        opponent_score = int(opponent_data.get("goals", 0)) if opponent_data else 0

        result_emoji = "✅" if our_score > opponent_score else "❌" if our_score < opponent_score else "➖"
        result_text = "Win" if our_score > opponent_score else "Loss" if our_score < opponent_score else "Draw"

        embed = discord.Embed(
            title=f"📅 Last Match: [{label}] {our_name} vs {opponent_name}",
            description=f"{result_emoji} {result_text} ({our_score}-{opponent_score})",
            color=discord.Color.green() if our_score > opponent_score else discord.Color.red() if our_score < opponent_score else discord.Color.gold()
        )

        # ✅ ADD THIS BLOCK (right here, same indent level)
        team_id = await get_team_id_for_club(str(club_id))
        crest_url = build_crest_url(team_id) if team_id else None
        if crest_url:
            embed.set_thumbnail(url=crest_url)

        # Players
        players_data = list((last_match.get("players", {}) or {}).get(club_id, {}).values())
        sorted_players = sorted(players_data, key=lambda p: float(p.get("rating", 0)), reverse=True)
        for player in sorted_players:
            name = player.get("playername", "Unknown")
            goals = player.get("goals", 0)
            assists = player.get("assists", 0)
            red = player.get("redcards", 0)
            rating = player.get("rating", "N/A")
            tackles = player.get("tacklesmade", 0)
            saves = player.get("saves", 0)
            embed.add_field(
                name=f"{name}",
                value=(f"⚽ {goals} | 🎯 {assists} | 🟥 {red} | 🛡️ {tackles} | 🧤 {saves} | ⭐ {rating}"),
                inline=False
            )

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
        msg = await interaction.channel.fetch_message(msg.id)
        await log_command_output(interaction, "stats", msg)
        asyncio.create_task(delete_after_delay(msg, 60))

    except Exception as e:
        print(f"[ERROR] /stats failed: {e}")
        await interaction.followup.send("❌ An unexpected error occurred while fetching club stats.", ephemeral=True)

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

    embed.set_footer(text=f"omitS Bot • Event ID: {ev.get('id')}", icon_url=footer_icon)
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
    Footer is standardized to 'omitS Bot' with the server icon.
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
    embed.set_footer(text=f"omitS Bot • Lineup ID: {lp.get('id')}", icon_url=footer_icon)
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

    embed.set_footer(text="omitS Bot • Twitch Live")
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
        
        ft = (embed.footer.text or f"omitS Bot • Event ID: {ev.get('id')}") + " • CLOSED"
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
    system_filter="Optional system filter, e.g. Stanton, Pyro, Nyx"
)
async def commodity_command(
    interaction: discord.Interaction,
    name: str,
    auto_load_only: bool = False,
    system_filter: str = None
):
    await interaction.response.defer()

    try:
        matches = await search_commodity_uex(name)

        if not matches:
            await interaction.followup.send("No matching commodities found.", ephemeral=True)
            return

        if len(matches) > 1:
            view = CommodityDropdown(
                matches,
                mode="commodity",
                auto_load_only=auto_load_only,
                system_filter=system_filter
            )
            await interaction.followup.send(
                "Multiple commodities found. Please choose:",
                view=view
            )
            return

        embed = await build_commodity_embed(
            matches[0],
            auto_load_only=auto_load_only,
            system_filter=system_filter
        )
        await interaction.followup.send(embed=embed)

    except Exception as e:
        print(f"[ERROR] /commodity failed: {e}")
        await interaction.followup.send(
            "❌ An unexpected error occurred while fetching commodity data.",
            ephemeral=True
        )

@tree.command(name="route", description="Show best Star Citizen trade routes for a commodity.")
@app_commands.describe(
    name="Commodity name, e.g. Gold, Agricium, Quantanium",
    auto_load_only="Only show routes where both terminals support auto loading",
    system_filter="Optional system filter, e.g. Stanton, Pyro, Nyx"
)
async def route_command(
    interaction: discord.Interaction,
    name: str,
    auto_load_only: bool = False,
    system_filter: str = None
):
    await interaction.response.defer()

    try:
        matches = await search_commodity_uex(name)

        if not matches:
            await interaction.followup.send("No matching commodities found.", ephemeral=True)
            return

        if len(matches) > 1:
            view = CommodityDropdown(
                matches,
                mode="route",
                auto_load_only=auto_load_only,
                system_filter=system_filter
            )
            await interaction.followup.send(
                "Multiple commodities found. Please choose:",
                view=view
            )
            return

        embed = await build_route_embed(
            matches[0],
            auto_load_only=auto_load_only,
            system_filter=system_filter
        )
        await interaction.followup.send(embed=embed)

    except Exception as e:
        print(f"[ERROR] /route failed: {e}")
        await interaction.followup.send(
            "❌ An unexpected error occurred while fetching route data.",
            ephemeral=True
        )

@tree.command(name="terminal", description="Show what a terminal buys/sells")
@app_commands.describe(name="Terminal name (e.g. Area18, Lorville)")
async def terminal_command(interaction: discord.Interaction, name: str):
    await interaction.response.defer()

    try:
        matches = await search_terminal_uex(name)

        if not matches:
            await interaction.followup.send("No matching terminals found.", ephemeral=True)
            return

        terminal = matches[0]  # keep simple for now
        terminal_id = terminal.get("id")

        data = await _uex_get("commodities_prices", params={"id_terminal": terminal_id})

        if not data:
            await interaction.followup.send("No trade data found for this terminal.")
            return

        buy = [c for c in data if c.get("price_buy")]
        sell = [c for c in data if c.get("price_sell")]

        embed = discord.Embed(
            title=f"🏪 {terminal.get('name')}",
            description="Available trading commodities",
            color=0x3498DB
        )

        if buy:
            lines = [f"{c['commodity_name']} — `{c['price_buy']}`" for c in buy[:10]]
            embed.add_field(name="Buys", value="\n".join(lines), inline=False)

        if sell:
            lines = [f"{c['commodity_name']} — `{c['price_sell']}`" for c in sell[:10]]
            embed.add_field(name="Sells", value="\n".join(lines), inline=False)

        await interaction.followup.send(embed=embed)

    except Exception as e:
        print(f"[ERROR] /terminal failed: {e}")
        await interaction.followup.send("Error fetching terminal data.", ephemeral=True)

@tree.command(name="besttrade", description="Find most profitable trade routes")
async def besttrade_command(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        routes = await _uex_get("commodities_routes", params={"limit": 20})

        if not routes:
            await interaction.followup.send("No trade routes found.")
            return

        # Sort by profit
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

        await interaction.followup.send(embed=embed)

    except Exception as e:
        print(f"[ERROR] /besttrade failed: {e}")
        await interaction.followup.send("Error fetching trade routes.", ephemeral=True)

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

        # Ship mode takes priority over manual SCU
        if ship:
            ship_matches = await search_ships_scwiki(ship)

            if not ship_matches:
                await interaction.followup.send("No matching ships found.", ephemeral=True)
                return

            if len(ship_matches) > 1:
                view = ShipDropdown(
                    ship_matches,
                    commodity_query=name,
                    buy_price_override=buy_price,
                    auto_load_only=auto_load_only,
                    system_filter=selected_system
                )
                await interaction.followup.send(
                    "Multiple ships found. Please choose:",
                    view=view
                )
                return

            chosen_ship = ship_matches[0]
            resolved_scu = _ship_scu(chosen_ship)

            if resolved_scu <= 0:
                await interaction.followup.send(
                    "That ship does not have a usable cargo capacity in the API.",
                    ephemeral=True
                )
                return
        else:
            if scu is None:
                await interaction.followup.send(
                    "Please provide either a ship name or a manual SCU value.",
                    ephemeral=True
                )
                return

            chosen_ship = None
            resolved_scu = int(scu)

        matches = await search_commodity_uex(name)

        if not matches:
            await interaction.followup.send("No matching commodities found.", ephemeral=True)
            return

        if len(matches) > 1:
            view = CommodityDropdown(
                matches,
                mode="cargo",
                auto_load_only=auto_load_only,
                system_filter=selected_system,
                cargo_scu=resolved_scu,
                buy_price_override=buy_price
            )
            prefix = ""
            if chosen_ship:
                prefix = f"Using **{_ship_display_name(chosen_ship)}** (`{resolved_scu}` SCU).\n"
            await interaction.followup.send(
                prefix + "Multiple commodities found. Please choose:",
                view=view
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

        await interaction.followup.send(embed=embed)

    except Exception as e:
        print(f"[ERROR] /cargo failed: {e}")
        await interaction.followup.send(
            "❌ An unexpected error occurred while calculating cargo profit.",
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
        raise RuntimeError("DATABASE_URL is not set")

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
            # Attempt safe conversion:
            # - If it looks like JSON already, cast it
            # - Otherwise wrap the old text as a JSON string
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
    Announce when OFFLINE -> LIVE; update embed while LIVE; delete when LIVE -> OFFLINE.
    Persists minimal state (message_id/channel_id) if db_* helpers exist; otherwise falls back to memory.
    """
    # Load persisted state if available
    state = {"live_stream_id": None, "message_id": None, "channel_id": None}
    try:
        state = await db_load_json(TWITCH_STATE_KEY, state)  # type: ignore[name-defined]
    except Exception:
        pass  # no DB? that's fine — we keep it in memory

    def _twitch_url() -> str:
        return f"https://twitch.tv/{TWITCH_CHANNEL_LOGIN}"

    while not client.is_closed():
        try:
            # Ensure required config exists
            if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET and TWITCH_CHANNEL_LOGIN and TWITCH_ANNOUNCE_CHANNEL_ID):
                await asyncio.sleep(max(TWITCH_POLL_INTERVAL, 30))
                continue

            # Query Twitch
            stream = await twitch_get_stream_by_login(TWITCH_CHANNEL_LOGIN)
            is_live_now = stream is not None
            was_live = bool(state.get("live_stream_id"))

            # ------------------------------------------------------------------
            # Transition: OFFLINE -> LIVE  (post + save state)
            # ------------------------------------------------------------------
            if is_live_now and not was_live:
                game_box = await twitch_get_game_box_art_url(stream.get("game_id"))
                embed = make_twitch_live_embed(stream, game_box)

                channel = client.get_channel(TWITCH_ANNOUNCE_CHANNEL_ID) or await client.fetch_channel(TWITCH_ANNOUNCE_CHANNEL_ID)
                content = f"||<@&{TWITCH_LIVE_ROLE_ID}>||" if TWITCH_LIVE_ROLE_ID else None
                allowed = discord.AllowedMentions(roles=[discord.Object(id=TWITCH_LIVE_ROLE_ID)]) if TWITCH_LIVE_ROLE_ID else None

                msg = await channel.send(content=content, embed=embed, view=WatchButtonView(_twitch_url()), allowed_mentions=allowed)

                state.update({"live_stream_id": stream.get("id"), "message_id": msg.id, "channel_id": channel.id})
                try:
                    await db_save_json(TWITCH_STATE_KEY, state)  # type: ignore[name-defined]
                except Exception:
                    pass

            # ------------------------------------------------------------------
            # Transition: LIVE -> OFFLINE  (delete + clear state)
            # ------------------------------------------------------------------
            elif not is_live_now and was_live:
                ch_id = state.get("channel_id")
                msg_id = state.get("message_id")
                if ch_id and msg_id:
                    try:
                        ch = client.get_channel(ch_id) or await client.fetch_channel(ch_id)
                        m = await ch.fetch_message(msg_id)
                        await m.delete()
                    except Exception as e:
                        print(f"[WARN] Could not delete Twitch live message: {e}")

                state.update({"live_stream_id": None, "message_id": None, "channel_id": None})
                try:
                    await db_save_json(TWITCH_STATE_KEY, state)  # type: ignore[name-defined]
                except Exception:
                    pass

            # ------------------------------------------------------------------
            # Still LIVE: update (uptime/viewers/title/preview), with self-heal
            # ------------------------------------------------------------------
            elif is_live_now:
                ch_id = state.get("channel_id")
                msg_id = state.get("message_id")
                channel = None
                message = None

                # Resolve channel/message if we have ids
                if ch_id:
                    try:
                        channel = client.get_channel(ch_id) or await client.fetch_channel(ch_id)
                        if msg_id:
                            message = await channel.fetch_message(msg_id)
                    except Exception:
                        message = None

                # If message vanished (restart/manual delete), re-post once
                if not message:
                    game_box = await twitch_get_game_box_art_url(stream.get("game_id"))
                    embed = make_twitch_live_embed(stream, game_box)
                    if not channel:
                        channel = client.get_channel(TWITCH_ANNOUNCE_CHANNEL_ID) or await client.fetch_channel(TWITCH_ANNOUNCE_CHANNEL_ID)
                    msg = await channel.send(embed=embed, view=WatchButtonView(_twitch_url()))
                    state.update({"live_stream_id": stream.get("id"), "message_id": msg.id, "channel_id": channel.id})
                    try:
                        await db_save_json(TWITCH_STATE_KEY, state)  # type: ignore[name-defined]
                    except Exception:
                        pass
                else:
                    # Refresh embed every poll so uptime/viewers/preview update
                    try:
                        game_box = await twitch_get_game_box_art_url(stream.get("game_id"))
                        await message.edit(embed=make_twitch_live_embed(stream, game_box), view=WatchButtonView(_twitch_url()))
                    except Exception as e:
                        print(f"[WARN] Could not update live embed: {e}")

            # else: still OFFLINE — do nothing

        except Exception as e:
            print(f"[ERROR] monitor_twitch_live tick failed: {e}")

        await asyncio.sleep(TWITCH_POLL_INTERVAL if TWITCH_POLL_INTERVAL > 0 else 60)

# -------------------------
# Command sync (global + optional guild)
# -------------------------
@client.event
async def on_ready():
    # --- DB bootstrap + load persistent state ---
    try:
        await init_db()
        # Pull latest snapshots for each store from Postgres
        global events_store, templates_store, lineups_store
        events_store   = await db_load_json(EVENTS_FILE,   {"next_id": 1, "events": {}})
        templates_store = await db_load_json(TEMPLATES_FILE, {})
        lineups_store  = await db_load_json(LINEUPS_FILE,  {"next_id": 1, "lineups": {}})
        print("🗄️ Loaded stores from Postgres.")
    except Exception as e:
        print(f"[ERROR] Postgres init/load failed: {e}")
        # (Optional) raise here if persistence is required
        # raise

    # --- your existing command sync logic (unchanged) ---
    try:
        gid = int(os.getenv("GUILD_ID", "0"))
        guild = client.get_guild(gid) or (await client.fetch_guild(gid) if gid else None)

        if guild:
            # 1) Start clean: remove any existing guild-scoped registrations
            tree.clear_commands(guild=guild)

            # 2) Copy your global command definitions into the guild scope
            tree.copy_global_to(guild=guild)

            # 3) Publish guild-only commands (fast propagation)
            cmds = await tree.sync(guild=guild)
            print(f"✅ Synced {len(cmds)} commands to guild {gid}")

            # 4) Remove GLOBAL registrations so you don't see duplicates
            tree.clear_commands(guild=None)   # clears global
            await tree.sync()                  # push the deletion
            print("🧹 Cleared global commands")
        else:
            print("[WARN] GUILD_ID not set or guild not found")

    except Exception as e:
        print(f"[ERROR] Command sync failed: {e}")

    print(f"Bot is ready as {client.user}")
    await warm_ea_session()
    
    # Run background tasks once (avoid duplicates on reconnect)
    if not getattr(client, "background_started", False):
        try:
            client.loop.create_task(rotate_presence())
            print("🌀 Presence rotation started.")
        except Exception as e:
            print(f"[ERROR] Could not start presence rotation: {e}")
    
        try:
            client.loop.create_task(monitor_twitch_live())
            print("📡 Twitch live monitor started.")
        except Exception as e:
            print(f"[ERROR] Could not start Twitch monitor: {e}")
    
        client.background_started = True

    channel_id = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))
    channel = client.get_channel(channel_id)
    
    if channel:
        message = await channel.send("✅ - omitS Bot (<:discord:1363127822209646612>) is now online and ready for commands!")
        async def delete_after_announcement():
            await asyncio.sleep(60)
            try:
                await message.delete()
            except Exception as e:
                print(f"[ERROR] Failed to auto-delete announcement message: {e}")
        asyncio.create_task(delete_after_announcement())
    else:
        print(f"[WARN] Could not find channel with ID {channel_id}")

client.run(TOKEN)
