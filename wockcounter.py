import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import re
import random
import os

# ── TOKEN ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GUILD_ID = 1225611222074921091  # Replace with your actual server ID
# ────────────────────────────────────────────────────────────────────────────

MAX_MESSAGES = 40_000
BATCH_SIZE = 100
BATCH_DELAY = 0.5

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ── KILL FEED RESPONSES ──────────────────────────────────────────────────────
KILL_RESPONSES = [
    'RIP "{name}" BOZO, NEW PACK 🚬',
    '"{name}" just got sent to the Ark respawn screen 💀 NEW PACK 🚬',
    'Damn "{name}" got cooked. Pour one out. NEW PACK 🚬',
    '"{name}" has been unalived by the tribe. Tragic. NEW PACK 🚬',
    'YOOOO "{name}" is DONE. Pack it up literally. NEW PACK 🚬',
    '"{name}" thought they were built different. They were not. NEW PACK 🚬',
    'RIP "{name}" — gone but not forgotten. Actually no, forgotten. NEW PACK 🚬',
    '"{name}" has left the server (involuntarily). NEW PACK 🚬',
    'F in chat for "{name}" 🚬 NEW PACK',
    '"{name}" got their base wiped AND their life taken. Rough day. NEW PACK 🚬',
]

# ── WOCK AD RESPONSES ────────────────────────────────────────────────────────
WOCK_ADS = [
    "{mention} you look stressed. Have you tried **Wock**? The official cough syrup of Alphaclash. Available at your nearest drop crate. 🚬",
    "{mention} your Wock subscription is ready for pickup. Bring the Wock to Alphaclash. 🚬",
    "Attention {mention} — your doctor has prescribed **Wock**. Symptoms include: winning fights, looking fresh, and bringing the pack. 🚬",
    "{mention} has been selected for a complimentary **Wock** sample. Don't ask questions. Just drink it. 🚬",
    "⚠️ {mention} — our records show you haven't had your daily **Wock**. This is your final notice. 🚬",
    "{mention} tried to quit **Wock** once. They lasted 4 minutes. Welcome back. 🚬",
    "BREAKING: Scientists confirm **Wock** makes you 40% harder to raid. {mention} take note. 🚬",
    "{mention} — Wock. It's not just a cough syrup. It's a lifestyle. It's Alphaclash. 🚬",
    "Dear {mention}, the tribe has voted. You need **Wock**. Immediately. 🚬",
    "{mention} one sip of **Wock** and your tames will never die again. Probably. NEW PACK 🚬",
]


# ── KILL MESSAGE LISTENER ────────────────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    match = re.search(r'Your Tribe killed ([^\s!.,\n]+)', message.content, re.IGNORECASE)
    if match:
        player_name = match.group(1)
        response = random.choice(KILL_RESPONSES).format(name=player_name)
        await message.channel.send(response)

    await bot.process_commands(message)


# ── SLASH COMMAND: /wock ─────────────────────────────────────────────────────
@bot.tree.command(name="wock", description="Prescribe someone their daily Wock.")
@app_commands.describe(player="The person who needs their Wock")
async def wock(interaction: discord.Interaction, player: discord.Member):
    ad = random.choice(WOCK_ADS).format(mention=player.mention)
    await interaction.response.send_message(ad)


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
    for msg in messages:
        content = msg.content if case_sensitive else msg.content.lower()
        count_total += content.count(search)

    embed = discord.Embed(title="🔍 WockCounter Results", color=0x00bfff)
    embed.add_field(name="Phrase", value=f"`{phrase}`", inline=True)
    embed.add_field(name="Occurrences", value=f"**{count_total}**", inline=True)
    embed.add_field(name="Messages Scanned", value=f"{len(messages):,}", inline=True)
    embed.add_field(name="Channel", value=channel.mention, inline=True)
    embed.set_footer(text=f"Requested by {interaction.user.display_name} • WockCounter")

    await progress.edit(content=None, embed=embed)


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


# ── STARTUP ──────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ WockCounter is online as {bot.user}")
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"✅ Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"❌ Sync failed: {e}")


bot.run(BOT_TOKEN)

