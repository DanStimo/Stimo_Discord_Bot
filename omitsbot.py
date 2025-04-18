import discord
from discord import app_commands
import httpx
from fuzzywuzzy import process, fuzz
import os
from dotenv import load_dotenv
from db import fetch_club_name, insert_club_mapping

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CLUB_ID = os.getenv("CLUB_ID", "167054")  # fallback/default
PLATFORM = os.getenv("PLATFORM", "common-gen5")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

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
    url = f"https://proclubs.ea.com/api/fc/clubs/matches?matchType=leagueMatch&platform={PLATFORM}&clubIds={club_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                matches = response.json()
                if not matches:
                    print("[DEBUG] No matches returned from EA.")
                    return []

                results = []
                for match in matches[:5]:
                    clubs_data = match.get("clubs", {})
                    club_data = clubs_data.get(str(club_id))
                    opponent_id = next((cid for cid in clubs_data if cid != str(club_id)), None)
                    opponent_data = clubs_data.get(opponent_id) if opponent_id else None

                    if not club_data or not opponent_data or "goals" not in club_data or "goals" not in opponent_data:
                        continue

                    our_score = int(club_data["goals"])
                    opponent_score = int(opponent_data["goals"])

                    if our_score > opponent_score:
                        results.append("✅")
                    elif our_score < opponent_score:
                        results.append("❌")
                    else:
                        results.append("➖")
                return results
    except Exception as e:
        print(f"Error fetching recent matches: {e}")
    return []

@tree.command(name="record", description="Show Wingus FC's current record.")
async def record_command(interaction: discord.Interaction):
    await interaction.response.defer()
    stats = await get_club_stats(CLUB_ID)
    recent_form = await get_recent_form(CLUB_ID)
    form_string = ' '.join(recent_form) if recent_form else "No recent matches found."

    if stats:
        embed = discord.Embed(
            title="📊 Wingus FC Club Stats",
            color=0xB30000
        )
        embed.add_field(name="Skill Rating", value=f"🏅 {stats['skillRating']}", inline=False)
        embed.add_field(name="Matches Played", value=f"📊 {stats['matchesPlayed']}", inline=False)
        embed.add_field(name="Wins", value=f"✅ {stats['wins']}", inline=False)
        embed.add_field(name="Draws", value=f"➖ {stats['draws']}", inline=False)
        embed.add_field(name="Losses", value=f"❌ {stats['losses']}", inline=False)
        embed.add_field(name="Win Streak", value=f"{stats['winStreak']} {streak_emoji(stats['winStreak'])}", inline=False)
        embed.add_field(name="Unbeaten Streak", value=f"{stats['unbeatenStreak']} {streak_emoji(stats['unbeatenStreak'])}", inline=False)
        embed.add_field(name="Recent Form", value=form_string, inline=False)
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send("Could not fetch club stats.")

@tree.command(name="versus", description="Check another club's stats by name or ID.")
@app_commands.describe(club="Club name or club ID")
async def versus_command(interaction: discord.Interaction, club: str):
    await interaction.response.defer()
    headers = {"User-Agent": "Mozilla/5.0"}

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            if club.isdigit():
                opponent_id = club
                opponent_name = await fetch_club_name(opponent_id)
                club_name_formatted = opponent_name.upper() if opponent_name else f"CLUB ID {opponent_id}"
            else:
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

                club_names = [club.get("clubInfo", {}).get("name", "") for club in search_data]
                matches = process.extract(club, club_names, scorer=fuzz.token_set_ratio, limit=5)
                good_matches = [match for match in matches if match[1] >= 50]

                if not good_matches:
                    await interaction.followup.send(f"No clubs found that match '{club}'.")
                    return

                best_match_name = good_matches[0][0]
                club_data = next((c for c in search_data if c.get("clubInfo", {}).get("name", "") == best_match_name), None)

                if not club_data:
                    await interaction.followup.send("Could not retrieve club data.")
                    return

                opponent_id = str(club_data.get("clubInfo", {}).get("clubId"))
                club_name_formatted = best_match_name.upper()

                await insert_club_mapping(opponent_id, best_match_name)

            stats = await get_club_stats(opponent_id)
            if not stats:
                await interaction.followup.send("Opponent stats not found.")
                return

            recent_form = await get_recent_form(opponent_id)
            form_string = ' '.join(recent_form) if recent_form else "No recent matches found."

            embed = discord.Embed(
                title=f"📋 {club_name_formatted} Club Stats",
                color=0xB30000
            )
            embed.add_field(name="Skill Rating", value=f"🏅 {stats['skillRating']}", inline=False)
            embed.add_field(name="Matches Played", value=f"📊 {stats['matchesPlayed']}", inline=False)
            embed.add_field(name="Wins", value=f"✅ {stats['wins']}", inline=False)
            embed.add_field(name="Draws", value=f"➖ {stats['draws']}", inline=False)
            embed.add_field(name="Losses", value=f"❌ {stats['losses']}", inline=False)
            embed.add_field(name="Win Streak", value=f"{stats['winStreak']} {streak_emoji(stats['winStreak'])}", inline=False)
            embed.add_field(name="Unbeaten Streak", value=f"{stats['unbeatenStreak']} {streak_emoji(stats['unbeatenStreak'])}", inline=False)
            embed.add_field(name="Recent Form", value=form_string, inline=False)
            await interaction.followup.send(embed=embed)

        except Exception as e:
            print(f"Error in /versus: {e}")
            await interaction.followup.send("An error occurred while fetching opponent stats.")

@client.event
async def on_ready():
    await tree.sync()
    print(f"Bot is ready as {client.user}")

client.run(TOKEN)
