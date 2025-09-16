import discord
from discord import app_commands
import httpx
import json
from fuzzywuzzy import process, fuzz
import os
import random
import asyncio
from dotenv import load_dotenv
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import re
import asyncpg
import logging

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CLUB_ID = os.getenv("CLUB_ID", "167054")  # fallback/default
PLATFORM = os.getenv("PLATFORM", "common-gen5")
OFFSIDE_KEY = "offside.json"

# Event & template config
EVENT_CREATOR_ROLE_ID = int(os.getenv("EVENT_CREATOR_ROLE_ID", "0")) if os.getenv("EVENT_CREATOR_ROLE_ID") else 0
EVENT_CREATOR_ROLE_NAME = "Moderator"
EVENTS_FILE = os.getenv("EVENTS_FILE", "events.json")
TEMPLATES_FILE = os.getenv("TEMPLATES_FILE", "templates.json")
ATTEND_EMOJI = "✅"
ABSENT_EMOJI = "❌"
MAYBE_EMOJI = "🤷"
EVENT_EMBED_COLOR_HEX = os.getenv("EVENT_EMBED_COLOR_HEX", "#3498DB")
DEFAULT_TZ = ZoneInfo("Europe/London")

# --- Intents ---
intents = discord.Intents.default()
intents.members = True  # ✅ REQUIRED for on_member_join and member lookups
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

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
    "3-5-2": ["GK", "RCB", "CB", "LCB", "RM", "RCM", "CDM", "LCM", "LM", "RST", "LST"],
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
    channel_id = welcome_config.get("channel_id", 0)
    if not channel_id:
        return

    try:
        channel = member.guild.get_channel(channel_id) or await member.guild.fetch_channel(channel_id)
    except Exception as e:
        print(f"[ERROR] Welcome channel fetch failed: {e}")
        return

    if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.ForumChannel)):
        return

    embed = discord.Embed(
        title="Welcome aboard! 👋",
        description=(
            f"{member.mention}, you've reached **Stimo's** Discord server!\n\n"
            "• **Read the rules:** <#1362311374293958856>\n"
            "• **Grab roles:** <#1361921570104283186>\n"
            "• **Say hi!:** <#1361690632392933527> 👋"
        ),
        color=_color_from_hex(welcome_config.get("color_hex")),
        timestamp=datetime.now(timezone.utc)
    )

    embed.set_author(
        name=f"{member.display_name} has arrived!",
        icon_url=member.display_avatar.url
    )

    if member.guild.icon:
        embed.set_thumbnail(url=member.guild.icon.url)
    else:
        embed.set_thumbnail(url=member.display_avatar.url)

    embed.set_footer(
        text="omitS Bot",
        icon_url="https://i.imgur.com/Uy3fdb1.png"
    )

    try:
        message = await channel.send(content=member.mention, embed=embed)
        emoji = discord.utils.get(member.guild.emojis, name="Wave")
        if emoji:
            await message.add_reaction(emoji)
    except Exception as e:
        print(f"[ERROR] Failed to send welcome embed or add reaction: {e}")

@tree.command(name="setwelcomechannel", description="Set the channel for welcome messages")
@app_commands.checks.has_permissions(manage_guild=True)
async def setwelcomechannel(interaction: discord.Interaction, channel: discord.TextChannel):
    welcome_config["channel_id"] = channel.id
    await safe_interaction_respond(interaction, content=f"✅ Welcome channel set to {channel.mention}", ephemeral=True)

@tree.command(name="setwelcomecolor", description="Set the welcome embed color (hex like #5865F2)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setwelcomecolor(interaction: discord.Interaction, hex_color: str):
    try:
        _ = _color_from_hex(hex_color)
        welcome_config["color_hex"] = hex_color
        await safe_interaction_respond(interaction, content=f"✅ Welcome color set to `{hex_color}`", ephemeral=True)
    except Exception:
        await safe_interaction_respond(interaction, content="❌ Please provide a valid hex color like `#2ecc71`.", ephemeral=True)
        
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

# --- Web helpers for EA endpoints ---
async def get_club_stats(club_id):
    url = f"https://proclubs.ea.com/api/fc/clubs/overallStats?platform={PLATFORM}&clubIds={club_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(timeout=10) as client_http:
            response = await client_http.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list) and len(data) > 0:
                    club = data[0]
                    return {
                        "matchesPlayed": club.get("gamesPlayed", "N/A"),
                        "wins": club.get("wins", "N/A"),
                        "draws": club.get("ties", "N/A"),
                        "losses": club.get("losses", "N/A"),
                        "winStreak": club.get("wstreak", "0"),
                        "unbeatenStreak": club.get("unbeatenstreak", "0"),
                        "skillRating": club.get("skillRating", "N/A")
                    }
    except Exception as e:
        print(f"Error fetching club stats: {e}")
    return None

async def get_recent_form(club_id):
    base_url = "https://proclubs.ea.com/api/fc/clubs/matches"
    headers = {"User-Agent": "Mozilla/5.0"}
    match_types = ["leagueMatch", "playoffMatch"]
    all_matches = []

    try:
        async with httpx.AsyncClient(timeout=10) as client_http:
            for match_type in match_types:
                url = f"{base_url}?matchType={match_type}&platform={PLATFORM}&clubIds={club_id}"
                response = await client_http.get(url, headers=headers)
                if response.status_code == 200:
                    matches = response.json()
                    all_matches.extend(matches)

        all_matches.sort(key=lambda x: x.get("timestamp", 0), reverse=True)

        results = []
        for match in all_matches[:5]:
            clubs_data = match.get("clubs", {})
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

async def get_last_match(club_id):
    base_url = "https://proclubs.ea.com/api/fc/clubs/matches"
    headers = {"User-Agent": "Mozilla/5.0"}
    match_types = ["leagueMatch", "playoffMatch"]
    all_matches = []

    try:
        async with httpx.AsyncClient(timeout=10) as client_http:
            for match_type in match_types:
                url = f"{base_url}?matchType={match_type}&platform={PLATFORM}&clubIds={club_id}"
                response = await client_http.get(url, headers=headers)
                if response.status_code == 200:
                    matches = response.json()
                    all_matches.extend(matches)

        all_matches.sort(key=lambda x: x.get("timestamp", 0), reverse=True)

        if not all_matches:
            return "Last match data not available."

        match = all_matches[0]
        clubs_data = match.get("clubs", {})
        club_data = clubs_data.get(str(club_id))
        opponent_id = next((cid for cid in clubs_data if cid != str(club_id)), None)
        opponent_data = clubs_data.get(opponent_id) if opponent_id else None

        if not club_data or not opponent_data:
            return "Last match data not available."

        opponent_name = (
            opponent_data.get("name")
            or opponent_data.get("details", {}).get("name")
            or match.get("opponentClub", {}).get("name", "Unknown")
        )

        our_score = int(club_data.get("goals", 0))
        opponent_score = int(opponent_data.get("goals", 0))

        result = "✅" if our_score > opponent_score else "❌" if our_score < opponent_score else "➖"
        return f"{result} - {opponent_name} ({our_score}-{opponent_score})"

    except Exception as e:
        print(f"[ERROR] Failed to fetch last match: {e}")
        return "Last match data not available."

async def get_club_rank(club_id):
    url = f"https://proclubs.ea.com/api/fc/allTimeLeaderboard?platform={PLATFORM}"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        async with httpx.AsyncClient(timeout=15) as client_http:
            response = await client_http.get(url, headers=headers)
            if response.status_code == 200:
                leaderboard = response.json()
                for club in leaderboard:
                    if str(club.get("clubId")) == str(club_id):
                        return club.get("rank", "Unranked")
            else:
                print(f"[ERROR] Failed to fetch leaderboard, status code {response.status_code}")
    except Exception as e:
        print(f"[ERROR] Exception in get_club_rank: {e}")
    
    return "Unranked"

async def get_days_since_last_match(club_id):
    base_url = "https://proclubs.ea.com/api/fc/clubs/matches"
    headers = {"User-Agent": "Mozilla/5.0"}
    match_types = ["leagueMatch", "playoffMatch"]
    all_matches = []

    try:
        async with httpx.AsyncClient(timeout=10) as client_http:
            for match_type in match_types:
                url = f"{base_url}?matchType={match_type}&platform={PLATFORM}&clubIds={club_id}"
                response = await client_http.get(url, headers=headers)
                if response.status_code == 200:
                    matches = response.json()
                    all_matches.extend(matches)

        all_matches.sort(key=lambda x: x.get("timestamp", 0), reverse=True)

        if not all_matches:
            return None

        last_timestamp = all_matches[0].get("timestamp", 0)
        last_datetime = datetime.fromtimestamp(last_timestamp, tz=timezone.utc)
        now = datetime.now(timezone.utc)

        delta_days = (now - last_datetime).days
        return delta_days

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

async def rotate_presence():
    await client.wait_until_ready()

    guild_id = int(os.getenv("GUILD_ID", "0"))
    role_id = int(os.getenv("WATCH_ROLE_ID", "0"))
    role_name = os.getenv("WATCH_ROLE_NAME")

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
        if view:
            message = await destination.send(content=content, embed=embed, view=view)
        else:
            message = await destination.send(content=content, embed=embed)
        await asyncio.sleep(delay)
        await message.delete()
    except Exception as e:
        print(f"[ERROR] Failed to auto-delete message: {e}")

async def log_command_output(interaction: discord.Interaction, command_name: str, message: discord.Message = None, extra_text: str = None):
    archive_channel_id = int(os.getenv("ARCHIVE_CHANNEL_ID", "0"))
    archive_channel = client.get_channel(archive_channel_id)

    if not archive_channel:
        print(f"[WARN] Archive channel not found for ID {archive_channel_id}")
        return

    embed = discord.Embed(
        title=f"📦 Command Archive: /{command_name}",
        color=discord.Color.dark_grey()
    )
    embed.add_field(name="User", value=f"{interaction.user.mention}", inline=False)
    embed.add_field(name="Used In", value=f"{interaction.channel.mention}", inline=False)
    embed.add_field(name="Timestamp", value=discord.utils.format_dt(interaction.created_at, style='F'), inline=False)

    if message:
        if message.embeds:
            for em in message.embeds:
                await archive_channel.send(content=f"📥 `/`{command_name} by {interaction.user.mention} in {interaction.channel.mention}:", embed=em)
        elif message.content:
            embed.add_field(name="Output", value=message.content[:1000], inline=False)
            await archive_channel.send(embed=embed)
    elif extra_text:
        embed.add_field(name="Output", value=extra_text[:1000], inline=False)
        await archive_channel.send(embed=embed)

# --- Dropdowns and views (versus/lastmatch/last5) remain the same as previously supplied ---
class ClubDropdown(discord.ui.Select):
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

        stats = await get_club_stats(chosen)
        recent_form = await get_recent_form(chosen)
        last_match = await get_last_match(chosen)
        rank = await get_club_rank(chosen)
        rank_display = f"#{rank}" if isinstance(rank, int) else "Unranked"
        form_string = ' '.join(recent_form) if recent_form else "No recent matches found."
        days_since_last = await get_days_since_last_match(chosen)
        days_display = f"🗓️ {days_since_last} day(s) ago" if days_since_last is not None else "🗓️ Unavailable"

        embed = discord.Embed(
            title=f"📋 {selected['clubInfo']['name'].upper()} Club Stats",
            color=0xB30000
        )
        embed.add_field(name="Leaderboard Rank", value=f"📈 {rank_display}", inline=False)
        embed.add_field(name="Skill Rating", value=f"🏅 {stats['skillRating']}", inline=False)
        embed.add_field(name="Matches Played", value=f"📊 {stats['matchesPlayed']}", inline=False)
        embed.add_field(name="Wins", value=f"✅ {stats['wins']}", inline=False)
        embed.add_field(name="Draws", value=f"➖ {stats['draws']}", inline=False)
        embed.add_field(name="Losses", value=f"❌ {stats['losses']}", inline=False)
        embed.add_field(name="Win Streak", value=f"{stats['winStreak']} {streak_emoji(stats['winStreak'])}", inline=False)
        embed.add_field(name="Unbeaten Streak", value=f"{stats['unbeatenStreak']} {streak_emoji(stats['unbeatenStreak'])}", inline=False)
        embed.add_field(name="Last Match", value=last_match, inline=False)
        embed.add_field(name="Recent Form", value=form_string, inline=False)
        embed.add_field(name="Days Since Last Match", value=days_display, inline=False)

        view = PrintRecordButton(stats, selected['clubInfo']['name'].upper())
        view.message = await interaction.message.edit(content=None, embed=embed, view=view)

        async def delete_after_timeout():
            try:
                await asyncio.sleep(180)
                await view.message.delete()
            except Exception as e:
                print(f"[ERROR] Failed to delete message after timeout: {e}")
        asyncio.create_task(delete_after_timeout())

        await log_command_output(interaction, "versus", view.message)

class ClubDropdownView(discord.ui.View):
    def __init__(self, interaction, options, club_data):
        super().__init__()
        self.add_item(ClubDropdown(interaction, options, club_data))

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
    base_url = "https://proclubs.ea.com/api/fc/clubs/matches"
    headers = {"User-Agent": "Mozilla/5.0"}
    match_types = ["leagueMatch", "playoffMatch"]
    matches = []

    async with httpx.AsyncClient(timeout=10) as client_http:
        for match_type in match_types:
            url = f"{base_url}?matchType={match_type}&platform={PLATFORM}&clubIds={club_id}"
            response = await client_http.get(url, headers=headers)
            if response.status_code == 200:
                matches.extend(response.json())

    matches.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    last_5 = matches[:5]

    if not last_5:
        await interaction.followup.send("No recent matches found.")
        return

    embed = discord.Embed(
        title=f"📅 {club_name.upper()}'s Last 5",
        color=discord.Color.blue()
    )

    for idx, match in enumerate(last_5, 1):
        clubs = match.get("clubs", {})
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

        embed.add_field(
            name=f"{idx}⃣ {result} vs {opponent_name}",
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

async def delete_after_delay(message, delay=180):
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

        # Assign and update
        self.lp["positions"][position_index]["user_id"] = picked.id
        self.lp["updated_at"] = datetime.now(timezone.utc).isoformat()
        lineups_store["lineups"][str(self.lp["id"])] = self.lp
        save_lineups_store()
        
        # Reflect the assignment, then reset both dropdowns to defaults
        view.refresh_position_options(keep_selected=True)  # briefly keep the highlight
        view.current_index = None
        view.refresh_position_options(keep_selected=False)
        view._reset_player_placeholder()
        
        # Refresh embed
        embed = make_lineup_embed(self.lp)
        await safe_interaction_edit(interaction, embed=embed, view=view)

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

    @discord.ui.button(label="Finish", style=discord.ButtonStyle.success)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 0) Backfill for old lineups
        self.lp.setdefault("pinged_user_ids", [])
    
        # 1) Update embed & remove controls
        embed = make_lineup_embed(self.lp)
        await safe_interaction_edit(interaction, embed=embed, view=None)
    
        # 2) Ensure ✅ is present
        try:
            msg = interaction.message or self.message
            if msg:
                try:
                    await msg.add_reaction("✅")
                except Exception:
                    pass
        except Exception:
            pass
    
        # 3) Compute newly-added users (assigned but never pinged before)
        assigned_ids: list[int] = []
        for p in self.lp.get("positions", []):
            uid = p.get("user_id")
            if uid and uid not in assigned_ids:
                assigned_ids.append(uid)
    
        already_pinged = set(self.lp.get("pinged_user_ids", []))
        new_to_ping = [u for u in assigned_ids if u not in already_pinged]
    
        if new_to_ping:
            title = self.lp.get("title") or f"{self.lp.get('formation')} Lineup"
            content = f"📣 **{title}** updated. Please confirm with ✅\n" + " ".join(f"<@{u}>" for u in new_to_ping)
    
            allowed = discord.AllowedMentions(
                users=[discord.Object(id=u) for u in new_to_ping],
                roles=False, everyone=False, replied_user=False
            )
    
            try:
                ch = (interaction.message.channel if interaction.message
                      else self.message.channel if self.message
                      else interaction.channel)
                await ch.send(content=content, allowed_mentions=allowed)
            except Exception:
                pass
    
            # 4) Persist that we’ve pinged these users
            self.lp["pinged_user_ids"] = list(already_pinged.union(new_to_ping))
            lineups_store["lineups"][str(self.lp["id"])] = self.lp
            save_lineups_store()
            except Exception:
                # If something odd happens (e.g., missing perms), we just skip the ping gracefully
                pass

# - /versus & aliases
@tree.command(name="versus", description="Check another club's stats by name or ID.")
@app_commands.describe(club="Club name or club ID")
async def versus_command(interaction: discord.Interaction, club: str):
    await interaction.response.defer(ephemeral=False)
    headers = {"User-Agent": "Mozilla/5.0"}

    async with httpx.AsyncClient(timeout=10) as client_http:
        try:
            encoded_name = club.replace(" ", "%20")
            search_url = f"https://proclubs.ea.com/api/fc/allTimeLeaderboard/search?platform={PLATFORM}&clubName={encoded_name}"
            search_response = await client_http.get(search_url, headers=headers)

            if search_response.status_code != 200:
                await send_temporary_message(interaction.followup, content="Club not found or EA API failed.")
                return

            search_data = search_response.json()
            if not search_data or not isinstance(search_data, list):
                await send_temporary_message(interaction.followup, content="No matching clubs found.")
                return

            valid_clubs = [
                c for c in search_data
                if c.get("clubInfo", {}).get("name", "").strip().lower() != "none of these"
            ]

            if len(valid_clubs) == 1:
                selected = valid_clubs[0]
                opponent_id = str(selected["clubInfo"]["clubId"])
                stats = await get_club_stats(opponent_id)
                recent_form = await get_recent_form(opponent_id)
                last_match = await get_last_match(opponent_id)
                days_since_last = await get_days_since_last_match(opponent_id)
                days_display = f"🗓️ {days_since_last} day(s) ago" if days_since_last is not None else "🗓️ Unavailable"
                rank = await get_club_rank(opponent_id)
                rank_display = f"#{rank}" if isinstance(rank, int) else "Unranked"
                form_string = ' '.join(recent_form) if recent_form else "No recent matches found."
            
                embed = discord.Embed(
                    title=f"📋 {selected['clubInfo']['name'].upper()} Club Stats",
                    color=0xB30000
                )
                embed.add_field(name="Leaderboard Rank", value=f"📈 {rank_display}", inline=False)
                embed.add_field(name="Skill Rating", value=f"🏅 {stats['skillRating']}", inline=False)
                embed.add_field(name="Matches Played", value=f"📊 {stats['matchesPlayed']}", inline=False)
                embed.add_field(name="Wins", value=f"✅ {stats['wins']}", inline=False)
                embed.add_field(name="Draws", value=f"➖ {stats['draws']}", inline=False)
                embed.add_field(name="Losses", value=f"❌ {stats['losses']}", inline=False)
                embed.add_field(name="Win Streak", value=f"{stats['winStreak']} {streak_emoji(stats['winStreak'])}", inline=False)
                embed.add_field(name="Unbeaten Streak", value=f"{stats['unbeatenStreak']} {streak_emoji(stats['unbeatenStreak'])}", inline=False)
                embed.add_field(name="Last Match", value=last_match, inline=False)
                embed.add_field(name="Recent Form", value=form_string, inline=False)
                embed.add_field(name="Days Since Last Match", value=days_display, inline=False)
            
                view = PrintRecordButton(stats, selected['clubInfo']['name'].upper())
                message = await interaction.followup.send(embed=embed, view=view)
                await log_command_output(interaction, "versus", message)

                async def delete_after_timeout():
                    await asyncio.sleep(180)
                    try:
                        await message.delete()
                    except Exception as e:
                        print(f"[ERROR] Failed to delete message after timeout: {e}")
                asyncio.create_task(delete_after_timeout())
                return

            options = [
                discord.SelectOption(label=c['clubInfo']['name'], value=str(c['clubInfo']['clubId']))
                for c in valid_clubs[:25]
            ]
            options.append(discord.SelectOption(label="None of these", value="none"))

            view = ClubDropdownView(interaction, options, valid_clubs)
            await interaction.followup.send("Multiple clubs found. Please choose the correct one:", view=view)

        except Exception as e:
            print(f"Error in /versus: {e}")
            await send_temporary_message(interaction.followup, content="An error occurred while fetching opponent stats.")

@tree.command(name="vs", description="Alias for /versus")
@app_commands.describe(club="Club name or club ID")
async def vs_command(interaction: discord.Interaction, club: str):
    await versus_command.callback(interaction, club)

# - /lastmatch & alias
async def handle_lastmatch(interaction: discord.Interaction, club: str, from_dropdown: bool = False, original_message=None):
    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
    except Exception as e:
        print(f"[WARN] Could not defer interaction: {e}")
    
    headers = {"User-Agent": "Mozilla/5.0"}
   
    async with httpx.AsyncClient(timeout=10) as client_http:
        try:
            if club.isdigit():
                club_id = club
            else:
                search_url = f"https://proclubs.ea.com/api/fc/allTimeLeaderboard/search?platform={PLATFORM}&clubName={club.replace(' ', '%20')}"
                search_response = await client_http.get(search_url, headers=headers)

                if search_response.status_code != 200:
                    await send_temporary_message(interaction.followup, content="Club not found or EA API failed.")
                    return

                search_data = search_response.json()
                if not search_data or not isinstance(search_data, list):
                    await send_temporary_message(interaction.followup, content="No matching clubs found.")
                    return

                valid_clubs = [
                    c for c in search_data
                    if c.get("clubInfo", {}).get("name", "").strip().lower() != "none of these"
                ]

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

            base_url = "https://proclubs.ea.com/api/fc/clubs/matches"
            match_types = ["leagueMatch", "playoffMatch"]
            matches = []

            for match_type in match_types:
                url = f"{base_url}?matchType={match_type}&platform={PLATFORM}&clubIds={club_id}"
                response = await client_http.get(url, headers=headers)
                if response.status_code == 200:
                    matches.extend(response.json())

            if not matches:
                await interaction.followup.send("No matches found for this club.")
                return

            matches.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
            last_match = matches[0]

            clubs = last_match.get("clubs", {})
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
                title=f"📅 Last Match: {our_name} vs {opponent_name}",
                description=f"{result_emoji} {result_text} ({our_score}-{opponent_score})",
                color=discord.Color.green() if our_score > opponent_score else discord.Color.red() if our_score < opponent_score else discord.Color.gold()
            )

            players_data = list(last_match.get("players", {}).get(club_id, {}).values())
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

            embed.add_field(name="\u200b", value="\u200b", inline=False)
            embed.set_footer(text="📘 Stat Key: ⚽ Goals | 🎯 Assists | 🟥 Red Cards | 🛡️ Tackles | 🧤 Saves | ⭐ Rating")

            if from_dropdown and original_message:
                await original_message.edit(content=None, embed=embed, view=None)
                await log_command_output(interaction, "lastmatch", original_message)
                async def delete_after_timeout():
                    await asyncio.sleep(180)
                    try:
                        await original_message.delete()
                    except Exception as e:
                        print(f"[ERROR] Failed to auto-delete dropdown message: {e}")
                asyncio.create_task(delete_after_timeout())
            else:
                message = await interaction.followup.send(embed=embed)
                await log_command_output(interaction, "lastmatch", message)
                async def delete_after_timeout():
                    await asyncio.sleep(180)
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
    def __init__(self, leaderboard, per_page=10):
        super().__init__(timeout=180)
        self.leaderboard = leaderboard
        self.per_page = per_page
        self.page = 0
        self.message = None

    def get_embed(self):
        start = self.page * self.per_page
        end = start + self.per_page
        current_page_clubs = self.leaderboard[start:end]

        embed = discord.Embed(
            title=f"🏆 Top 100 Clubs (Page {self.page + 1}/{(len(self.leaderboard) - 1) // self.per_page + 1})",
            description="Navigate using the buttons below.",
            color=0xFFD700
        )

        for club in current_page_clubs:
            name = club.get("clubName", "Unknown Club")
            rank = club.get("rank", "N/A")
            skill = club.get("skillRating", "N/A")
            embed.add_field(
                name=f"#{rank} - {name}",
                value=f"⭐ Skill Rating: {skill}",
                inline=False
            )

        embed.set_footer(text="EA Pro Clubs All-Time Leaderboard")
        return embed

    @discord.ui.button(label="⏮️ First", style=discord.ButtonStyle.secondary)
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 0
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="⬅️ Prev", style=discord.ButtonStyle.primary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="➡️ Next", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if (self.page + 1) * self.per_page < len(self.leaderboard):
            self.page += 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="⏭️ Last", style=discord.ButtonStyle.secondary)
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = (len(self.leaderboard) - 1) // self.per_page
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.delete()
            except Exception as e:
                print(f"[ERROR] Failed to auto-delete /t100 message: {e}")

@tree.command(name="t100", description="Show the Top 100 Clubs from EA Pro Clubs Leaderboard.")
async def top100_command(interaction: discord.Interaction):
    await interaction.response.defer()

    url = f"https://proclubs.ea.com/api/fc/allTimeLeaderboard?platform={PLATFORM}"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        async with httpx.AsyncClient(timeout=15) as client_http:
            response = await client_http.get(url, headers=headers)
            if response.status_code != 200:
                await interaction.followup.send("⚠️ Failed to fetch the leaderboard from EA.")
                return

            leaderboard = response.json()

            if not leaderboard or not isinstance(leaderboard, list):
                await interaction.followup.send("⚠️ No leaderboard data found.")
                return

            top_100 = sorted(leaderboard, key=lambda c: c.get("rank", 9999))[:100]

            view = Top100View(top_100, per_page=10)
            embed = view.get_embed()

            message = await interaction.followup.send(embed=embed, view=view)
            view.message = message

            await log_command_output(interaction, "t100", view.message)

    except Exception as e:
        print(f"[ERROR] Failed to fetch Top 100: {e}")
        await send_temporary_message(interaction.followup, content="❌ An error occurred while fetching the Top 100 clubs.")

@tree.command(name="last5", description="Show the last 5 matches for a club.")
@app_commands.describe(club="Club name or club ID")
async def last5_command(interaction: discord.Interaction, club: str):
    await interaction.response.defer()
    headers = {"User-Agent": "Mozilla/5.0"}

    async with httpx.AsyncClient(timeout=10) as client_http:
        if club.isdigit():
            await fetch_and_display_last5(interaction, club, "Club")
            return

        search_url = f"https://proclubs.ea.com/api/fc/allTimeLeaderboard/search?platform={PLATFORM}&clubName={club.replace(' ', '%20')}"
        response = await client_http.get(search_url, headers=headers)

        if response.status_code != 200:
            await interaction.followup.send("Club not found or EA API failed.")
            return

        data = response.json()

        if not data or not isinstance(data, list):
            await send_temporary_message(interaction.followup, content="No matching clubs found.", delay=60)
            return
        
        valid_clubs = [
            c for c in data
            if c.get("clubInfo", {}).get("name", "").strip().lower() != "none of these"
        ]
        
        if len(valid_clubs) == 0:
            await send_temporary_message(interaction.followup, content="No matching clubs found.", delay=60)
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

@tree.command(name="l5", description="Alias for /last5")
@app_commands.describe(club="Club name or club ID")
async def l5_command(interaction: discord.Interaction, club: str):
    await last5_command.callback(interaction, club)

@tree.command(name="lineup", description="Create an interactive lineup from a formation.")
@app_commands.describe(
    formation="Choose a soccer formation",
    title="Optional custom title for the lineup",
    role="Optional role restriction: only members with this role can be assigned",
    channel="Channel to post the lineup (defaults to current channel)"
)
@app_commands.choices(formation=[app_commands.Choice(name=f, value=f) for f in FORMATIONS.keys()])
async def lineup_command(
    interaction: discord.Interaction,
    formation: app_commands.Choice[str],
    title: str | None = None,
    role: discord.Role | None = None,
    channel: discord.TextChannel | None = None
):
    await interaction.response.defer(ephemeral=True)
    target_channel = channel or interaction.channel
    if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
        await safe_interaction_respond(interaction, content="❌ Please specify a valid text channel.", ephemeral=True)
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
        # NEW: track who we've pinged already for this lineup
        "pinged_user_ids": []
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
    uses guild icon as thumbnail, and shows a Thread jump link if present.
    """
    color = discord.Color(int(EVENT_EMBED_COLOR_HEX.strip().lstrip("#"), 16))

    desc_text = ev.get("description", "\u200b")
    embed_description = f"**Event Info**\n{desc_text}"

    embed = discord.Embed(
        title=f"📅 {ev.get('name')}",
        description=embed_description,
        color=color
    )

    dt_iso = ev.get("datetime")
    try:
        dt = datetime.fromisoformat(dt_iso)
        dt_utc = dt.astimezone(timezone.utc)
        embed.add_field(name="When", value=discord.utils.format_dt(dt_utc, style='F'), inline=False)
    except Exception:
        embed.add_field(name="When", value="Unknown", inline=False)

    # ✅ Stream Link (optional)
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

    embed.add_field(name=f"{ATTEND_EMOJI} Attend", value=users_to_text(ev.get("attend", [])), inline=True)
    embed.add_field(name=f"{ABSENT_EMOJI} Absent", value=users_to_text(ev.get("absent", [])), inline=True)
    embed.add_field(name=f"{MAYBE_EMOJI} Maybe", value=users_to_text(ev.get("maybe", [])), inline=True)

    # Try to set guild icon as thumbnail (use channel -> guild)
    try:
        ch = client.get_channel(ev.get("channel_id"))
        guild = ch.guild if ch is not None else None
        if guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
    except Exception:
        pass

    embed.set_footer(text=f"Event ID: {ev.get('id')} • Created by: {ev.get('creator_id')}")
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
    return None

def save_events_store():
    save_json_file(EVENTS_FILE, events_store)

def save_templates_store():
    save_json_file(TEMPLATES_FILE, templates_store)

def make_lineup_embed(lp: dict) -> discord.Embed:
    """
    Build an embed for a lineup. Single column: `Lineup` with all positions.
    """
    color = discord.Color(int(EVENT_EMBED_COLOR_HEX.strip().lstrip("#"), 16))
    ch = client.get_channel(lp.get("channel_id"))
    guild = ch.guild if ch else None

    title = lp.get("title") or f"{lp.get('formation')} Lineup"
    formation = lp.get("formation")
    role_id = lp.get("role_id")

    embed = discord.Embed(
        title=f"🧩 {title}",
        description=(
            f"**Formation:** `{formation}`"
            + (f"\n**Eligible Role:** <@&{role_id}>" if role_id else "")
        ),
        color=color,
    )

    # Build one column list of positions
    positions: list[dict] = lp.get("positions", [])
    lines = []
    for pos in positions:
        mention = f"<@{pos['user_id']}>" if pos.get("user_id") else "—"
        lines.append(f"**{pos['code']}** — {mention}")

    embed.add_field(name="Lineup", value="\n".join(lines) or "—", inline=False)

    # Thumbnail: server icon if available
    try:
        if guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
    except Exception:
        pass

    embed.set_footer(text=f"Lineup ID: {lp.get('id')} • Created by: {lp.get('creator_id')}")
    return embed

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
    channel="Optional channel to post the event in (defaults to template channel or current channel)",
    role="Optional role to ping (overrides template's saved role)",
    stream="Optional Twitch channel or URL (overrides template stream)"
)
async def createfromtemplate_command(
    interaction: discord.Interaction,
    template_name: str,
    date: str,
    time: str,
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
    channel="Channel to post the event in (optional, defaults to current channel)",
    role="Optional role to ping (will be spoilered)",
    stream="Optional Twitch channel or URL (e.g. ninja or https://twitch.tv/ninja)"  # NEW
)
async def createevent_command(
    interaction: discord.Interaction,
    name: str,
    description: str,
    date: str,
    time: str,
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

        # Create a thread tied to the event message (same name as event)
        try:
            thread = await sent.create_thread(name=ev["name"], auto_archive_duration=10080)
            ev["thread_id"] = thread.id
            # Add the creator to the thread
            try:
                await thread.add_user(interaction.user)
            except Exception:
                pass
            # Update the embed now that thread exists
            try:
                await sent.edit(embed=make_event_embed(ev))
            except Exception:
                pass
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
        embed.set_footer(text=f"Event ID: {ev.get('id')} • CLOSED • Created by: {ev.get('creator_id')}")
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
        embed.set_footer(text=f"Event ID: {ev.get('id')} • Created by: {ev.get('creator_id')}")
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
        still_should_be_in = (user_id in ev.get("attend", [])) or (user_id in ev.get("maybe", []))
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

    if not ev:
        return

    if ev.get("closed"):
        return

    emoji_str = str(payload.emoji)
    key = emoji_to_key(emoji_str)
    
    # 🚫 Block any reaction that is not one of the 3 allowed (✅ ❌ 🤷)
    if not key:
        try:
            ch = client.get_channel(ev["channel_id"]) or await client.fetch_channel(ev["channel_id"])
            msg = await ch.fetch_message(ev["message_id"])
            # remove the unapproved reaction from the user
            guild = client.get_guild(payload.guild_id)
            user_obj = (guild.get_member(payload.user_id) if guild else None) or await client.fetch_user(payload.user_id)
            await msg.remove_reaction(payload.emoji, user_obj)
        except Exception as e:
            print(f"[WARN] Failed to remove unapproved reaction: {e}")
        return

    try:
        ch = client.get_channel(ev["channel_id"]) or await client.fetch_channel(ev["channel_id"])
        msg = await ch.fetch_message(ev["message_id"])
    except Exception:
        msg = None

    uid = payload.user_id
    changed = False

    # Remove user from other lists and remove their old reactions (with suppression)
    for k in ("attend", "absent", "maybe"):
        if uid in ev.get(k, []) and k != key:
            ev[k].remove(uid)
            changed = True
            if msg:
                try:
                    guild = client.get_guild(payload.guild_id)
                    member_obj = None
                    if guild:
                        member_obj = guild.get_member(uid)
                        if member_obj is None:
                            try:
                                member_obj = await guild.fetch_member(uid)
                            except Exception:
                                member_obj = None
                    user_obj = member_obj if member_obj else await client.fetch_user(uid)
                    old_emoji = ATTEND_EMOJI if k == "attend" else ABSENT_EMOJI if k == "absent" else MAYBE_EMOJI
                    try:
                        await mark_suppressed_reaction(ev["message_id"], uid, str(old_emoji))
                        await msg.remove_reaction(old_emoji, user_obj)
                    except Exception:
                        pass
                except Exception:
                    pass

    # Add user to chosen list
    if uid not in ev.get(key, []):
        ev.setdefault(key, []).append(uid)
        changed = True

    # Thread membership updates
    if key in ("attend", "maybe"):
        asyncio.create_task(add_user_to_event_thread(ev, uid))
    else:
        asyncio.create_task(remove_user_from_event_thread_if_needed(ev, uid))

    if changed:
        events_store["events"][str(ev["id"])] = ev
        save_events_store()
        if msg:
            try:
                embed = make_event_embed(ev)
                await msg.edit(embed=embed)

                # Remove the current reaction too, while keeping the signup (suppress removal)
                guild = client.get_guild(payload.guild_id)
                user_obj = (guild.get_member(uid) if guild else None) or await client.fetch_user(uid)
                await mark_suppressed_reaction(ev["message_id"], uid, str(payload.emoji))
                await msg.remove_reaction(payload.emoji, user_obj)

            except Exception as e:
                print(f"[ERROR] Failed to update event embed or remove reaction: {e}")

@client.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.user_id == client.user.id:
        return

    emoji_str = str(payload.emoji)
    key_tuple = (payload.message_id, payload.user_id, emoji_str)
    if key_tuple in pending_reaction_removals:
        pending_reaction_removals.discard(key_tuple)
        return  # ignore bot-initiated removals

    ev = None
    for eid, e in events_store.get("events", {}).items():
        if e.get("message_id") == payload.message_id:
            ev = e
            break

    if not ev:
        return

    key = emoji_to_key(emoji_str)
    if not key:
        return

    uid = payload.user_id
    if uid in ev.get(key, []):
        ev[key].remove(uid)
        events_store["events"][str(ev["id"])] = ev
        save_events_store()

        # If they removed Attend/Maybe and are no longer in either, remove from thread
        if key in ("attend", "maybe"):
            asyncio.create_task(remove_user_from_event_thread_if_needed(ev, uid))

        try:
            ch = client.get_channel(ev["channel_id"]) or await client.fetch_channel(ev["channel_id"])
            msg = await ch.fetch_message(ev["message_id"])
            embed = make_event_embed(ev)
            await msg.edit(embed=embed)
        except Exception as e:
            print(f"[ERROR] Failed to update event embed after reaction remove: {e}")

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

    # --- keep the rest unchanged ---
    client.loop.create_task(rotate_presence())

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
