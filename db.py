import asyncpg
import os

async def get_connection():
    return await asyncpg.connect(
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", 5432))
    )

async def fetch_club_name(club_id: str) -> str | None:
    conn = await get_connection()
    row = await conn.fetchrow("SELECT club_name FROM club_mapping WHERE club_id = $1", club_id)
    await conn.close()
    return row['club_name'] if row else None

async def insert_club_mapping(club_id: str, club_name: str):
    conn = await get_connection()
    await conn.execute("""
        INSERT INTO club_mapping (club_id, club_name)
        VALUES ($1, $2)
        ON CONFLICT (club_id) DO NOTHING
    """, club_id, club_name)
    await conn.close()
