import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import re

# ── PASTE YOUR BOT TOKEN BETWEEN THE QUOTES BELOW ───────────────────────────
import os
BOT_TOKEN = os.environ.get("BOT_TOKEN")
# ────────────────────────────────────────────────────────────────────────────

MAX_MESSAGES = 10_000
BATCH_SIZE = 100       # Messages fetched per API call (Discord max is 100)
BATCH_DELAY = 0.5      # Seconds to wait between batches (increase if still getting rate limited)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ── KILL MESSAGE LISTENER ────────────────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    # Don't respond to the bot's own messages
    if message.author == bot.user:
        return

    # Look for "Your Tribe killed <name>" anywhere in the message
    # Captures everything after "killed " until end of string or punctuation
    match = re.search(r'Your Tribe killed ([^\s!.,\n]+)', message.content, re.IGNORECASE)
    if match:
        player_name = match.group(1)
        await message.channel.send(f'RIP "{player_name}" BOZO, NEW PACK 🚬')

    # Make sure other commands still work
    await bot.process_commands(message)


# ── RATE LIMIT SAFE HISTORY FETCHER ─────────────────────────────────────────
async def safe_history(channel, limit):
    messages = []
    last_message_id = None
    remaining = limit

    while remaining > 0:
        fetch_size = min(BATCH_SIZE, remaining)

        for attempt in range(5):
            try:
                kwargs = {"limit": fetch_size}
                if last_message_id:
                    kwargs["before"] = discord.Object(id=last_message_id)

                batch = [m async for m in channel.history(**kwargs)]
                break

            except discord.HTTPException as e:
                if e.status == 429:
                    retry_after = float(e.response.headers.get("Retry-After", 5))
                    print(f"⚠️  Rate limited — waiting {retry_after}s before retrying...")
                    await asyncio.sleep(retry_after + 1)
                else:
                    raise
        else:
            print("❌ Gave up after 5 rate limit retries.")
            break

        if not batch:
            break

        messages.extend(batch)
        last_message_id = batch[-1].id
        remaining -= len(batch)

        await asyncio.sleep(BATCH_DELAY)

    return messages


# ── SLASH COMMAND: /count ────────────────────────────────────────────────────
@bot.tree.command(name="count", description="Count how many times a word or phrase appears in this channel.")
@app_commands.describe(
    phrase="The word or phrase to search for",
    case_sensitive="Case-sensitive search? (default: No)"
)
async def count(interaction: discord.Interaction, phrase: str, case_sensitive: bool = False):
    await interaction.response.defer(thinking=True)

    channel = interaction.channel
    search = phrase if case_sensitive else phrase.lower()

    progress = await interaction.followup.send(f"⏳ Scanning `#{channel.name}` for `{phrase}`... this may take a moment for large channels.")

    try:
        messages = await safe_history(channel, MAX_MESSAGES)
    except discord.Forbidden:
        await progress.edit(content="❌ I don't have permission to read message history here.")
        return
    except discord.HTTPException as e:
        await progress.edit(content=f"❌ Discord API error: {e}")
        return

    count_total = 0
    for message in messages:
        content = message.content if case_sensitive else message.content.lower()
        count_total += content.count(search)

    embed = discord.Embed(title="🔍 WockCounter Results", color=0x00bfff)
    embed.add_field(name="Phrase", value=f"`{phrase}`", inline=True)
    embed.add_field(name="Occurrences", value=f"**{count_total}**", inline=True)
    embed.add_field(name="Messages Scanned", value=f"{len(messages):,}", inline=True)
    embed.add_field(name="Channel", value=channel.mention, inline=True)
    embed.set_footer(text=f"Requested by {interaction.user.display_name} • WockCounter")

    await progress.edit(content=None, embed=embed)


# ── STARTUP ──────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ WockCounter is online as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"❌ Sync failed: {e}")


bot.run(BOT_TOKEN)
