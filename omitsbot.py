import discord
from discord import app_commands
import httpx
import json
from fuzzywuzzy import process, fuzz
import os
import random
import asyncio
from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CLUB_ID = os.getenv("CLUB_ID", "167054")  # fallback/default
PLATFORM = os.getenv("PLATFORM", "common-gen5")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

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
        # After timeout, remove the view (i.e., the button)
        if self.message:
            try:
                await self.message.edit(view=None)
            except Exception as e:
                print(f"[ERROR] Failed to remove view after timeout: {e}")

async def get_club_stats(club_id):
    url = f"https://proclubs.ea.com/api/fc/clubs/overallStats?platform={PLATFORM}&clubIds={club_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=headers)
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
        async with httpx.AsyncClient(timeout=10) as client:
            for match_type in match_types:
                url = f"{base_url}?matchType={match_type}&platform={PLATFORM}&clubIds={club_id}"
                response = await client.get(url, headers=headers)
                if response.status_code == 200:
                    matches = response.json()
                    all_matches.extend(matches)

        # Sort by match timestamp (most recent first)
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
        async with httpx.AsyncClient(timeout=10) as client:
            for match_type in match_types:
                url = f"{base_url}?matchType={match_type}&platform={PLATFORM}&clubIds={club_id}"
                response = await client.get(url, headers=headers)
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

        # ✅ Safely pull opponent name from multiple possible sources
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
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, headers=headers)
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

async def get_squad_names(club_id):
    url = f"https://proclubs.ea.com/api/fc/club/members?platform={PLATFORM}&clubId={club_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=headers)
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
    while not client.is_closed():
        try:
            fixed_club_id = "304203"
            url = f"https://proclubs.ea.com/api/fc/members/stats?platform={PLATFORM}&clubId={fixed_club_id}"
            headers = {"User-Agent": "Mozilla/5.0"}

            async with httpx.AsyncClient(timeout=10) as http:
                response = await http.get(url, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    all_members = data.get("members", [])

                    # Filter out members with less than 10 games played
                    active_members = [
                        m for m in all_members
                        if int(m.get("gamesPlayed", 0)) >= 10
                    ]

                    if active_members:
                        random_member = random.choice(active_members)
                        gamertag = random_member.get("name", "someone")

                        activity = discord.Activity(
                            type=discord.ActivityType.watching,
                            name=f"{gamertag} 👀"
                        )
                        await client.change_presence(activity=activity)
                    else:
                        print("[ERROR] No active members with 10+ games found.")
                else:
                    print(f"[ERROR] API returned status {response.status_code}")
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

@tree.command(name="record", description="Show xNever Enoughx's current record.")
async def record_command(interaction: discord.Interaction):
    await interaction.response.defer()  # Prevent timeout

    stats = await get_club_stats(CLUB_ID)
    recent_form = await get_recent_form(CLUB_ID)
    rank = await get_club_rank(CLUB_ID)
    last_match = await get_last_match(CLUB_ID)
    form_string = ' '.join(recent_form) if recent_form else "No recent matches found."

    if stats:
        embed = discord.Embed(title="📊 xNever Enoughx Club Stats", color=0xB30000)
        embed.add_field(name="Leaderboard Rank", value=f"📈 #{rank}", inline=False)
        embed.add_field(name="Skill Rating", value=f"🏅 {stats['skillRating']}", inline=False)
        embed.add_field(name="Matches Played", value=f"📊 {stats['matchesPlayed']}", inline=False)
        embed.add_field(name="Wins", value=f"✅ {stats['wins']}", inline=False)
        embed.add_field(name="Draws", value=f"➖ {stats['draws']}", inline=False)
        embed.add_field(name="Losses", value=f"❌ {stats['losses']}", inline=False)
        embed.add_field(name="Win Streak", value=f"{stats['winStreak']} {streak_emoji(stats['winStreak'])}", inline=False)
        embed.add_field(name="Unbeaten Streak", value=f"{stats['unbeatenStreak']} {streak_emoji(stats['unbeatenStreak'])}", inline=False)
        embed.add_field(name="Last Match", value=last_match, inline=False)
        embed.add_field(name="Recent Form", value=form_string, inline=False)
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()

        async def delete_after_timeout():
            await asyncio.sleep(60)
            try:
                await message.delete()
            except Exception as e:
                print(f"[ERROR] Failed to delete /record embed after timeout: {e}")

        asyncio.create_task(delete_after_timeout())
    else:
        await interaction.response.send_message("Could not fetch club stats.")

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
        # ✅ Prevents "This interaction failed"
        await interaction.response.defer()

        if self.values[0] == "none":
            await interaction.message.edit(content="Okay, request canceled.", view=None)
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

        # Use PrintRecordButton with timeout
        view = PrintRecordButton(stats, selected['clubInfo']['name'].upper())
        view.message = await interaction.message.edit(content=None, embed=embed, view=view)

        # 🧹 Auto-delete the message after 60 seconds
        async def delete_after_timeout():
            try:
                await asyncio.sleep(60)
                await view.message.delete()
            except Exception as e:
                print(f"[ERROR] Failed to delete message after timeout: {e}")

        asyncio.create_task(delete_after_timeout())


class ClubDropdownView(discord.ui.View):
    def __init__(self, interaction, options, club_data):
        super().__init__()
        self.add_item(ClubDropdown(interaction, options, club_data))

@tree.command(name="versus", description="Check another club's stats by name or ID.")
@app_commands.describe(club="Club name or club ID")
async def versus_command(interaction: discord.Interaction, club: str):
    await interaction.response.defer(ephemeral=False)
    headers = {"User-Agent": "Mozilla/5.0"}

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            encoded_name = club.replace(" ", "%20")
            search_url = f"https://proclubs.ea.com/api/fc/allTimeLeaderboard/search?platform={PLATFORM}&clubName={encoded_name}"
            search_response = await client.get(search_url, headers=headers)

            if search_response.status_code != 200:
                await interaction.followup.send("Club not found or EA API failed.")
                return

            search_data = search_response.json()
            if not search_data or not isinstance(search_data, list):
                await interaction.followup.send("No matching clubs found.")
                return

            # Filter out bad names
            valid_clubs = [
                c for c in search_data
                if c.get("clubInfo", {}).get("name", "").strip().lower() != "none of these"
            ]

            # Auto-select if exactly one valid club found
            if len(valid_clubs) == 1:
                selected = valid_clubs[0]
                opponent_id = str(selected["clubInfo"]["clubId"])
                stats = await get_club_stats(opponent_id)
                recent_form = await get_recent_form(opponent_id)
                last_match = await get_last_match(opponent_id)
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
            
                view = PrintRecordButton(stats, selected['clubInfo']['name'].upper())
                message = await interaction.followup.send(embed=embed, view=view)

                async def delete_after_timeout():
                    await asyncio.sleep(60)
                    try:
                        await message.delete()
                    except Exception as e:
                        print(f"[ERROR] Failed to delete message after timeout: {e}")
                
                asyncio.create_task(delete_after_timeout())

                return


            # Build options from top 25
            options = [
                discord.SelectOption(label=c['clubInfo']['name'], value=str(c['clubInfo']['clubId']))
                for c in valid_clubs[:25]
            ]
            options.append(discord.SelectOption(label="None of these", value="none"))

            view = ClubDropdownView(interaction, options, valid_clubs)
            await interaction.followup.send("Multiple clubs found. Please choose the correct one:", view=view)

        except Exception as e:
            print(f"Error in /versus: {e}")
            await interaction.followup.send("An error occurred while fetching opponent stats.")


@tree.command(name="vs", description="Alias for /versus")
@app_commands.describe(club="Club name or club ID")
async def vs_command(interaction: discord.Interaction, club: str):
    await versus_command.callback(interaction, club)

@client.event
async def on_ready():
    await tree.sync()
    print(f"Bot is ready as {client.user}")

    client.loop.create_task(rotate_presence())

    # Optional announcement
    channel_id = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))  # replace with actual ID if needed
    channel = client.get_channel(channel_id)
    if channel:
        await channel.send("✅ - omitS Bot (<:discord:1363127822209646612>) is now online and ready for commands!")
    else:
        print(f"[WARN] Could not find channel with ID {channel_id}")

client.run(TOKEN)
