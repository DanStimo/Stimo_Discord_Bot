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
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CLUB_ID = os.getenv("CLUB_ID", "167054")  # fallback/default
PLATFORM = os.getenv("PLATFORM", "common-gen5")

# --- Intents ---
intents = discord.Intents.default()
intents.members = True  # ✅ REQUIRED for on_member_join
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# === Welcome Feature ===
# Config (env support + runtime updates via slash commands)
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0"))   # e.g. 123456789012345678
WELCOME_COLOR_HEX = os.getenv("WELCOME_COLOR_HEX", "#2ecc71")    # hex color

welcome_config = {
    "channel_id": WELCOME_CHANNEL_ID,
    "color_hex": WELCOME_COLOR_HEX,
}

def _color_from_hex(h: str) -> discord.Color:
    h = (h or "#2ecc71").strip().lstrip("#")
    return discord.Color(int(h, 16))

@client.event
async def on_member_join(member: discord.Member):
    """Send a tagged, colored welcome embed into the configured channel."""
    channel_id = welcome_config.get("channel_id", 0)
    if not channel_id:
        return  # not configured yet

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
        timestamp=datetime.now(timezone.utc)  # adds timestamp at bottom
    )

    # Thumbnail (right side): guild icon if available; otherwise the new member’s avatar
    if member.guild.icon:
        embed.set_thumbnail(url=member.guild.icon.url)
    else:
        embed.set_thumbnail(url=member.display_avatar.url)

    # Footer (bottom)
    embed.set_footer(
        text="omitS Bot",
        icon_url="https://i.imgur.com/Uy3fdb1.png"
    )

    try:
        # send the welcome message (still tags outside embed)
        message = await channel.send(content=member.mention, embed=embed)
    
        # ✅ react with a custom emoji from the same server
        emoji = discord.utils.get(member.guild.emojis, name=":Wave:")
        if emoji:
            await message.add_reaction(emoji)
        else:
            print("[WARN] Could not find the emoji by name in this server")
    
    except Exception as e:
        print(f"[ERROR] Failed to send welcome embed or add reaction: {e}")


@tree.command(name="setwelcomechannel", description="Set the channel for welcome messages")
@app_commands.checks.has_permissions(manage_guild=True)
async def setwelcomechannel(interaction: discord.Interaction, channel: discord.TextChannel):
    welcome_config["channel_id"] = channel.id
    await interaction.response.send_message(f"✅ Welcome channel set to {channel.mention}", ephemeral=True)

@tree.command(name="setwelcomecolor", description="Set the welcome embed color (hex like #5865F2)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setwelcomecolor(interaction: discord.Interaction, hex_color: str):
    try:
        _ = _color_from_hex(hex_color)
        welcome_config["color_hex"] = hex_color
        await interaction.response.send_message(f"✅ Welcome color set to `{hex_color}`", ephemeral=True)
    except Exception:
        await interaction.response.send_message("❌ Please provide a valid hex color like `#2ecc71`.", ephemeral=True)
# === End Welcome Feature ===

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
        super().__init__(timeout=900)  # 15 minutes
        self.stats = stats
        self.club_name = club_name
        self.message = None  # store the original message

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
    github_url = "https://raw.githubusercontent.com/DanStimo/Stimo_Discord_Bot/master/omitS_Squad.txt"

    while not client.is_closed():
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                response = await http.get(github_url)
                if response.status_code == 200:
                    lines = response.text.strip().splitlines()
                    valid_names = [line.strip() for line in lines if line.strip()]

                    if valid_names:
                        gamertag = random.choice(valid_names)
                        activity = discord.Activity(
                            type=discord.ActivityType.watching,
                            name=f"{gamertag} 👀"
                        )
                        await client.change_presence(activity=activity)
                    else:
                        print("[ERROR] Squad file is empty or badly formatted.")
                else:
                    print(f"[ERROR] GitHub file fetch failed: {response.status_code}")
        except Exception as e:
            print(f"[ERROR] Failed to rotate presence: {e}")

        await asyncio.sleep(300)  # 5 minutes

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

# - THIS IS FOR THE DROPDOWN IF MULTIPLE CLUBS ARE FOUND USING THE VERSUS COMMAND.
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
                print(f("[ERROR] Failed to delete message after timeout: {e}"))
        asyncio.create_task(delete_after_timeout())

        await log_command_output(interaction, "versus", view.message)

class ClubDropdownView(discord.ui.View):
    def __init__(self, interaction, options, club_data):
        super().__init__()
        self.add_item(ClubDropdown(interaction, options, club_data))

# - THIS IS FOR THE DROPDOWN IF MULTIPLE CLUBS ARE FOUND USING THE LASTMATCH COMMAND.
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

# - THIS IS FOR THE LAST5 DROPDOWN
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

# - THIS IS FOR THE /VERSUS COMMAND.
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

# - THIS IS FOR THE VS ALIAS OF VERSUS.
@tree.command(name="vs", description="Alias for /versus")
@app_commands.describe(club="Club name or club ID")
async def vs_command(interaction: discord.Interaction, club: str):
    await versus_command.callback(interaction, club)

# - THIS IS FOR THE /LASTMATCH COMMAND.
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

# - THIS IS FOR THE LM ALIAS OF LASTMATCH.
@tree.command(name="lm", description="Alias for /lastmatch")
@app_commands.describe(club="Club name or club ID")
async def lm_command(interaction: discord.Interaction, club: str):
    await handle_lastmatch(interaction, club, from_dropdown=False, original_message=None)

# - THIS IS FOR TOP 100.
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

@client.event
async def on_ready():
    await tree.sync()
    print(f"Bot is ready as {client.user}")

    client.loop.create_task(rotate_presence())

    # Optional announcement
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
