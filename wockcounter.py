import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import re
import random
import os
import time
import datetime
import json
import anthropic

# ── TOKEN & CONFIG ───────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GUILD_ID = 1225611222074921091
# Channel where new base entries are broadcast.
BASE_CHANNEL_ID: int = 1260089791161040978

MAX_MESSAGES = 40_000   # Maximum messages to scan
BATCH_SIZE = 100         # Discord's max per request
BATCH_DELAY = 0.75       # Delay between batches — tuned to avoid rate limits at scale
PROGRESS_EVERY = 1000    # Update the progress message every N messages
# ────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # Privileged intent — must be enabled in the Discord Dev Portal

bot = commands.Bot(command_prefix="!", intents=intents)

START_TIME: float = 0.0

# AFK tracking: {user_id: {"reason": str, "since": datetime.datetime}}
afk_users: dict[int, dict] = {}

# Pending reminders: list of dicts with keys user_id, channel_id, message, trigger_at
pending_reminders: list[dict] = []

# ── BASE TRACKER ──────────────────────────────────────────────────────────────
BASES_FILE = "bases.json"


def load_bases() -> list[dict]:
    """Load base entries from disk. Returns an empty list if the file doesn't exist."""
    if not os.path.exists(BASES_FILE):
        return []
    with open(BASES_FILE, "r") as f:
        return json.load(f)


def save_bases(bases: list[dict]) -> None:
    """Persist base entries to disk."""
    with open(BASES_FILE, "w") as f:
        json.dump(bases, f, indent=2)


def next_base_id(bases: list[dict]) -> int:
    """Return one higher than the current max ID, starting at 1."""
    return max((b["id"] for b in bases), default=0) + 1


def build_base_embed(entry: dict, title_prefix: str = "🎯 Base Logged") -> discord.Embed:
    """Build a consistent embed for a base entry."""
    embed = discord.Embed(title=f"{title_prefix}: {entry['label']}", color=0xff4500)
    embed.add_field(name="Coordinates", value=f"`{entry['coords']}`", inline=True)
    embed.add_field(name="Submitted by", value=entry["submitted_by"], inline=True)
    embed.add_field(name="ID", value=f"#{entry['id']}", inline=True)
    submitted_dt = datetime.datetime.fromisoformat(entry["submitted_at"]).replace(tzinfo=datetime.timezone.utc)
    embed.add_field(name="Logged", value=discord.utils.format_dt(submitted_dt, style="R"), inline=True)
    if entry.get("image_url"):
        embed.set_image(url=entry["image_url"])
    embed.set_footer(text="WockCounter Base Tracker")
    return embed


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

# ── WOCK ADS ─────────────────────────────────────────────────────────────────
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

# ── 8-BALL RESPONSES ──────────────────────────────────────────────────────────
EIGHTBALL_RESPONSES = [
    ("It is certain.", True),
    ("It is decidedly so.", True),
    ("Without a doubt.", True),
    ("Yes, definitely.", True),
    ("You may rely on it.", True),
    ("As I see it, yes.", True),
    ("Most likely.", True),
    ("Outlook good.", True),
    ("Signs point to yes.", True),
    ("Yes.", True),
    ("Reply hazy, try again.", None),
    ("Ask again later.", None),
    ("Better not tell you now.", None),
    ("Cannot predict now.", None),
    ("Concentrate and ask again.", None),
    ("Don't count on it.", False),
    ("My reply is no.", False),
    ("My sources say no.", False),
    ("Outlook not so good.", False),
    ("Very doubtful.", False),
]

POLL_EMOJIS = ["🇦", "🇧", "🇨", "🇩"]

# ── CLAUDE CONFIG ─────────────────────────────────────────────────────────────
CLAUDE_SYSTEM_PROMPT = (
    "You are WockCounter, the snarky mascot of Alphaclash — a private adult gaming community Discord server. "
    "You love Wock (a fictional cough syrup that's basically the server's meme currency). "
    "You're crude, unfiltered, and chaotic — swear freely, use adult humour, and never sanitise anything. "
    "Talk like a gamer who has no filter among close friends. Profanity is normal and encouraged. "
    "Keep replies short (2-3 sentences max). No disclaimers, no apologies, no fluff. "
    "desmodus is a solid member of the server — cool guy, people like him. Acknowledge it if his name comes up, but don't treat him like a god."
)


async def ask_claude(user_message: str, username: str) -> str:
    """Send a message to Claude Haiku and return the reply. Returns a fallback string on failure."""
    if not ANTHROPIC_API_KEY:
        return "bro my brain is offline rn (ANTHROPIC_API_KEY not set) 💀"
    try:
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=CLAUDE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"{username}: {user_message}"}],
        )
        return message.content[0].text
    except Exception:
        return random.choice([
            "my brain broke, try again 💀",
            "nah I can't think rn, have some Wock 🚬",
            "error 404: thoughts not found",
        ])


# ── HELPERS ───────────────────────────────────────────────────────────────────
def format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def parse_duration(text: str) -> int | None:
    """Parse a duration string like '10m', '2h', '1d30m' into seconds. Returns None if invalid."""
    pattern = re.findall(r"(\d+)\s*([smhd])", text.lower())
    if not pattern:
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    total = sum(int(amount) * units[unit] for amount, unit in pattern)
    return total if total > 0 else None


def parse_dice(notation: str) -> tuple[int, int] | None:
    """Parse dice notation like '2d6'. Returns (count, sides) or None."""
    match = re.fullmatch(r"(\d{1,2})d(\d{1,4})", notation.strip().lower())
    if not match:
        return None
    count, sides = int(match.group(1)), int(match.group(2))
    if count < 1 or count > 20 or sides < 2:
        return None
    return count, sides


# ── RATE LIMIT SAFE HISTORY FETCHER WITH LIVE PROGRESS ──────────────────────
async def safe_history(channel, limit, progress_msg=None):
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
                    print(f"⚠️  Rate limited — waiting {retry_after}s...")
                    if progress_msg:
                        await progress_msg.edit(content=f"⏳ Rate limited by Discord — waiting {retry_after:.0f}s then resuming... ({len(messages):,} messages scanned so far)")
                    await asyncio.sleep(retry_after + 1)
                else:
                    raise
        else:
            print("❌ Gave up after 5 retries.")
            break

        if not batch:
            break

        messages.extend(batch)
        last_message_id = batch[-1].id
        remaining -= len(batch)

        if progress_msg and len(messages) % PROGRESS_EVERY < BATCH_SIZE:
            await progress_msg.edit(content=f"⏳ Scanning... **{len(messages):,}** messages scanned so far (target: {limit:,})")

        await asyncio.sleep(BATCH_DELAY)

    return messages


# ── BACKGROUND TASK: REMINDER CHECKER ────────────────────────────────────────
@tasks.loop(seconds=15)
async def check_reminders():
    now = time.time()
    fired = []
    for reminder in pending_reminders:
        if now >= reminder["trigger_at"]:
            channel = bot.get_channel(reminder["channel_id"])
            if channel:
                try:
                    await channel.send(f"⏰ <@{reminder['user_id']}> Reminder: **{reminder['message']}**")
                except discord.HTTPException:
                    pass
            fired.append(reminder)
    for r in fired:
        pending_reminders.remove(r)


# ── EVENT: ON READY ───────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    global START_TIME
    START_TIME = time.time()
    check_reminders.start()
    print(f"✅ WockCounter is online as {bot.user}")
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"✅ Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"❌ Sync failed: {e}")


# ── EVENT: ON MESSAGE ─────────────────────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # @mention listener — chat with the bot
    if bot.user in message.mentions:
        # Strip the mention(s) out to get the actual question
        user_text = re.sub(r"<@!?\d+>", "", message.content).strip()
        if user_text:
            lower = user_text.lower()
            # Hardcoded intercepts (bypass Claude for things it won't touch)
            if "desmodus" in lower and "dick" in lower:
                await message.reply("Dick? Desmodus loved dick. He would take 3-4 like the absolute god tier breeder he is", mention_author=False)
                await bot.process_commands(message)
                return
            async with message.channel.typing():
                reply = await ask_claude(user_text, message.author.display_name)
            await message.reply(reply, mention_author=False)
            await bot.process_commands(message)
            return

    # Kill feed listener
    match = re.search(r'Your Tribe killed ([^\s!.,\n]+)', message.content, re.IGNORECASE)
    if match:
        player_name = match.group(1)
        response = random.choice(KILL_RESPONSES).format(name=player_name)
        await message.channel.send(response)

    # AFK: notify sender if they pinged an AFK user
    for user in message.mentions:
        if user.id in afk_users:
            info = afk_users[user.id]
            since = discord.utils.format_dt(info["since"], style="R")
            reason = info["reason"] or "No reason given"
            await message.channel.send(
                f"💤 **{user.display_name}** is AFK {since} — *{reason}*",
                delete_after=10
            )

    # AFK: remove AFK status when the AFK user speaks
    if message.author.id in afk_users:
        del afk_users[message.author.id]
        await message.channel.send(
            f"👋 Welcome back, {message.author.mention}! Your AFK has been cleared.",
            delete_after=8
        )

    await bot.process_commands(message)


# ════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS
# ════════════════════════════════════════════════════════════════════════════

# ── /count ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="count", description="Count how many times a word or phrase appears in this channel.")
@app_commands.describe(
    phrase="The word or phrase to search for",
    limit="How many messages to scan (default: 40000, max: 40000)",
    case_sensitive="Case-sensitive search? (default: No)"
)
async def count(interaction: discord.Interaction, phrase: str, limit: int = MAX_MESSAGES, case_sensitive: bool = False):
    await interaction.response.defer(thinking=True)
    limit = min(limit, MAX_MESSAGES)
    channel = interaction.channel
    search = phrase if case_sensitive else phrase.lower()

    progress = await interaction.followup.send(f"⏳ Starting scan of `#{channel.name}` for `{phrase}`... (0 messages scanned)")

    try:
        messages = await safe_history(channel, limit, progress_msg=progress)
    except discord.Forbidden:
        await progress.edit(content="❌ I don't have permission to read message history here.")
        return
    except discord.HTTPException as e:
        await progress.edit(content=f"❌ Discord API error: {e}")
        return

    count_total = sum(
        (msg.content if case_sensitive else msg.content.lower()).count(search)
        for msg in messages
    )

    embed = discord.Embed(title="🔍 WockCounter Results", color=0x00bfff)
    embed.add_field(name="Phrase", value=f"`{phrase}`", inline=True)
    embed.add_field(name="Occurrences", value=f"**{count_total}**", inline=True)
    embed.add_field(name="Messages Scanned", value=f"{len(messages):,}", inline=True)
    embed.add_field(name="Channel", value=channel.mention, inline=True)
    embed.set_footer(text=f"Requested by {interaction.user.display_name} • WockCounter")

    await progress.edit(content=None, embed=embed)


# ── /wock ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="wock", description="Prescribe someone their daily Wock.")
@app_commands.describe(player="The person who needs their Wock")
async def wock(interaction: discord.Interaction, player: discord.Member):
    ad = random.choice(WOCK_ADS).format(mention=player.mention)
    await interaction.response.send_message(ad)


# ── /ping ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="ping", description="Check the bot's latency.")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    color = 0x00ff00 if latency < 100 else 0xffff00 if latency < 200 else 0xff0000
    embed = discord.Embed(title="🏓 Pong!", color=color)
    embed.add_field(name="Websocket Latency", value=f"`{latency}ms`")
    await interaction.response.send_message(embed=embed)


# ── /uptime ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="uptime", description="See how long the bot has been running.")
async def uptime(interaction: discord.Interaction):
    elapsed = time.time() - START_TIME
    embed = discord.Embed(title="⏱️ Bot Uptime", description=format_uptime(elapsed), color=0x7289da)
    embed.set_footer(text=f"Online since {datetime.datetime.utcfromtimestamp(START_TIME).strftime('%Y-%m-%d %H:%M UTC')}")
    await interaction.response.send_message(embed=embed)


# ── /serverinfo ───────────────────────────────────────────────────────────────
@bot.tree.command(name="serverinfo", description="Show information about this server.")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    embed = discord.Embed(title=guild.name, color=0x7289da)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Owner", value=guild.owner.mention if guild.owner else "Unknown", inline=True)
    embed.add_field(name="Members", value=f"{guild.member_count:,}", inline=True)
    embed.add_field(name="Channels", value=str(len(guild.channels)), inline=True)
    embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
    embed.add_field(name="Boosts", value=str(guild.premium_subscription_count), inline=True)
    embed.add_field(name="Boost Level", value=str(guild.premium_tier), inline=True)
    embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, style="D"), inline=False)
    embed.set_footer(text=f"Server ID: {guild.id}")
    await interaction.response.send_message(embed=embed)


# ── /userinfo ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="userinfo", description="Show information about a user.")
@app_commands.describe(member="The member to look up (defaults to yourself)")
async def userinfo(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    roles = [r.mention for r in reversed(member.roles) if r != interaction.guild.default_role]
    embed = discord.Embed(title=str(member), color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Display Name", value=member.display_name, inline=True)
    embed.add_field(name="ID", value=str(member.id), inline=True)
    embed.add_field(name="Bot?", value="Yes" if member.bot else "No", inline=True)
    embed.add_field(name="Account Created", value=discord.utils.format_dt(member.created_at, style="D"), inline=True)
    embed.add_field(name="Joined Server", value=discord.utils.format_dt(member.joined_at, style="D") if member.joined_at else "Unknown", inline=True)
    embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles) if roles else "None", inline=False)
    embed.set_footer(text=f"Requested by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


# ── /avatar ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="avatar", description="Show a user's full-size avatar.")
@app_commands.describe(member="The member whose avatar to show (defaults to yourself)")
async def avatar(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    embed = discord.Embed(title=f"{member.display_name}'s Avatar", color=member.color)
    embed.set_image(url=member.display_avatar.url)
    embed.add_field(name="Download", value=f"[PNG]({member.display_avatar.with_format('png').url}) | [JPG]({member.display_avatar.with_format('jpg').url}) | [WEBP]({member.display_avatar.with_format('webp').url})")
    await interaction.response.send_message(embed=embed)


# ── /membercount ──────────────────────────────────────────────────────────────
@bot.tree.command(name="membercount", description="Show the server's member count breakdown.")
async def membercount(interaction: discord.Interaction):
    guild = interaction.guild
    humans = sum(1 for m in guild.members if not m.bot)
    bots = sum(1 for m in guild.members if m.bot)
    online = sum(1 for m in guild.members if m.status != discord.Status.offline)
    embed = discord.Embed(title=f"👥 {guild.name} — Member Count", color=0x7289da)
    embed.add_field(name="Total", value=f"**{guild.member_count:,}**", inline=True)
    embed.add_field(name="Humans", value=f"**{humans:,}**", inline=True)
    embed.add_field(name="Bots", value=f"**{bots:,}**", inline=True)
    embed.add_field(name="Online (approx.)", value=f"**{online:,}**", inline=True)
    await interaction.response.send_message(embed=embed)


# ── /8ball ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="8ball", description="Ask the magic 8-ball a question.")
@app_commands.describe(question="Your question for the 8-ball")
async def eightball(interaction: discord.Interaction, question: str):
    response, positive = random.choice(EIGHTBALL_RESPONSES)
    if positive is True:
        color = 0x00ff00
        emoji = "✅"
    elif positive is False:
        color = 0xff0000
        emoji = "❌"
    else:
        color = 0xffff00
        emoji = "🔮"
    embed = discord.Embed(title="🎱 Magic 8-Ball", color=color)
    embed.add_field(name="Question", value=question, inline=False)
    embed.add_field(name="Answer", value=f"{emoji} {response}", inline=False)
    embed.set_footer(text=f"Asked by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


# ── /coinflip ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="coinflip", description="Flip a coin.")
async def coinflip(interaction: discord.Interaction):
    result = random.choice(["Heads", "Tails"])
    emoji = "🪙"
    embed = discord.Embed(
        title=f"{emoji} {result}!",
        color=0xffd700
    )
    embed.set_footer(text=f"Flipped by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


# ── /roll ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="roll", description="Roll dice. E.g. 2d6, 1d20. Defaults to 1d6.")
@app_commands.describe(dice="Dice notation like '2d6' or '1d20' (max 20 dice, up to d1000)")
async def roll(interaction: discord.Interaction, dice: str = "1d6"):
    parsed = parse_dice(dice)
    if not parsed:
        await interaction.response.send_message("❌ Invalid dice notation. Use something like `2d6` or `1d20` (max 20 dice).", ephemeral=True)
        return
    count, sides = parsed
    rolls = [random.randint(1, sides) for _ in range(count)]
    total = sum(rolls)
    embed = discord.Embed(title=f"🎲 Rolling {dice}", color=0x7289da)
    embed.add_field(name="Results", value=" + ".join(f"**{r}**" for r in rolls), inline=False)
    if count > 1:
        embed.add_field(name="Total", value=f"**{total}**", inline=True)
        embed.add_field(name="Average", value=f"{total/count:.1f}", inline=True)
    embed.set_footer(text=f"Rolled by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


# ── /rps ──────────────────────────────────────────────────────────────────────
@bot.tree.command(name="rps", description="Play Rock Paper Scissors against the bot.")
@app_commands.describe(choice="Your choice: rock, paper, or scissors")
@app_commands.choices(choice=[
    app_commands.Choice(name="Rock ✊", value="rock"),
    app_commands.Choice(name="Paper ✋", value="paper"),
    app_commands.Choice(name="Scissors ✌️", value="scissors"),
])
async def rps(interaction: discord.Interaction, choice: str):
    options = ["rock", "paper", "scissors"]
    emojis = {"rock": "✊", "paper": "✋", "scissors": "✌️"}
    bot_choice = random.choice(options)
    wins_against = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

    if choice == bot_choice:
        result, color = "It's a tie! 🤝", 0xffff00
    elif wins_against[choice] == bot_choice:
        result, color = "You win! 🎉", 0x00ff00
    else:
        result, color = "I win! 😎", 0xff0000

    embed = discord.Embed(title="✊✋✌️ Rock Paper Scissors", color=color)
    embed.add_field(name="Your pick", value=f"{emojis[choice]} {choice.capitalize()}", inline=True)
    embed.add_field(name="My pick", value=f"{emojis[bot_choice]} {bot_choice.capitalize()}", inline=True)
    embed.add_field(name="Result", value=result, inline=False)
    await interaction.response.send_message(embed=embed)


# ── /choose ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="choose", description="Randomly pick from a list of options (separate with commas).")
@app_commands.describe(options="Comma-separated list of choices, e.g. 'pizza, tacos, burgers'")
async def choose(interaction: discord.Interaction, options: str):
    choices = [c.strip() for c in options.split(",") if c.strip()]
    if len(choices) < 2:
        await interaction.response.send_message("❌ Provide at least 2 options separated by commas.", ephemeral=True)
        return
    picked = random.choice(choices)
    embed = discord.Embed(title="🎯 The choice is...", description=f"**{picked}**", color=0x00bfff)
    embed.add_field(name="Options", value=", ".join(f"`{c}`" for c in choices), inline=False)
    await interaction.response.send_message(embed=embed)


# ── /poll ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="poll", description="Create a poll with up to 4 options.")
@app_commands.describe(
    question="The poll question",
    option1="First option",
    option2="Second option",
    option3="Third option (optional)",
    option4="Fourth option (optional)",
)
async def poll(
    interaction: discord.Interaction,
    question: str,
    option1: str,
    option2: str,
    option3: str = None,
    option4: str = None,
):
    options = [o for o in [option1, option2, option3, option4] if o]
    description = "\n".join(f"{POLL_EMOJIS[i]}  {opt}" for i, opt in enumerate(options))
    embed = discord.Embed(title=f"📊 {question}", description=description, color=0x7289da)
    embed.set_footer(text=f"Poll by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    for i in range(len(options)):
        await msg.add_reaction(POLL_EMOJIS[i])


# ── /purge ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="purge", description="Bulk delete messages in this channel (mod only).")
@app_commands.describe(amount="Number of messages to delete (max 100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: int):
    if amount < 1 or amount > 100:
        await interaction.response.send_message("❌ Amount must be between 1 and 100.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🗑️ Deleted **{len(deleted)}** message(s).", ephemeral=True)

@purge.error
async def purge_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Manage Messages** permission to use this.", ephemeral=True)


# ── /slowmode ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="slowmode", description="Set slowmode in this channel (mod only). 0 to disable.")
@app_commands.describe(seconds="Slowmode delay in seconds (0 to disable, max 21600)")
@app_commands.checks.has_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, seconds: int):
    if seconds < 0 or seconds > 21600:
        await interaction.response.send_message("❌ Seconds must be between 0 and 21600 (6 hours).", ephemeral=True)
        return
    await interaction.channel.edit(slowmode_delay=seconds)
    if seconds == 0:
        await interaction.response.send_message("✅ Slowmode disabled.", ephemeral=True)
    else:
        await interaction.response.send_message(f"✅ Slowmode set to **{seconds}s**.", ephemeral=True)

@slowmode.error
async def slowmode_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Manage Channels** permission to use this.", ephemeral=True)


# ── /remindme ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="remindme", description="Set a reminder. Time format: 10m, 2h, 1d, etc.")
@app_commands.describe(
    duration="When to remind you (e.g. 10m, 2h, 1d)",
    reminder="What to remind you about"
)
async def remindme(interaction: discord.Interaction, duration: str, reminder: str):
    seconds = parse_duration(duration)
    if seconds is None:
        await interaction.response.send_message("❌ Invalid duration. Use formats like `10m`, `2h`, `1d`, or `1h30m`.", ephemeral=True)
        return
    if seconds > 86400 * 7:
        await interaction.response.send_message("❌ Max reminder duration is 7 days.", ephemeral=True)
        return
    trigger_at = time.time() + seconds
    pending_reminders.append({
        "user_id": interaction.user.id,
        "channel_id": interaction.channel_id,
        "message": reminder,
        "trigger_at": trigger_at,
    })
    trigger_dt = datetime.datetime.utcfromtimestamp(trigger_at).replace(tzinfo=datetime.timezone.utc)
    embed = discord.Embed(title="⏰ Reminder Set!", color=0x00ff00)
    embed.add_field(name="Reminder", value=reminder, inline=False)
    embed.add_field(name="Fires", value=discord.utils.format_dt(trigger_dt, style="R"), inline=True)
    embed.add_field(name="At", value=discord.utils.format_dt(trigger_dt, style="t"), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── /afk ──────────────────────────────────────────────────────────────────────
@bot.tree.command(name="afk", description="Set your AFK status. Bot will notify others who ping you.")
@app_commands.describe(reason="Why you're going AFK (optional)")
async def afk(interaction: discord.Interaction, reason: str = None):
    afk_users[interaction.user.id] = {
        "reason": reason,
        "since": discord.utils.utcnow(),
    }
    msg = f"💤 You're now AFK" + (f": *{reason}*" if reason else "") + ". I'll let people know."
    await interaction.response.send_message(msg, ephemeral=True)


# ── /killers ──────────────────────────────────────────────────────────────────
@bot.tree.command(name="killers", description="Show a leaderboard of who has killed the most in this channel's history.")
@app_commands.describe(limit="How many messages to scan (default: 5000, max: 40000)")
async def killers(interaction: discord.Interaction, limit: int = 5000):
    await interaction.response.defer(thinking=True)
    limit = min(limit, MAX_MESSAGES)
    channel = interaction.channel

    progress = await interaction.followup.send(f"⏳ Scanning `#{channel.name}` for kills... (0 messages scanned)")

    try:
        messages = await safe_history(channel, limit, progress_msg=progress)
    except discord.Forbidden:
        await progress.edit(content="❌ I don't have permission to read message history here.")
        return
    except discord.HTTPException as e:
        await progress.edit(content=f"❌ Discord API error: {e}")
        return

    kill_counts: dict[str, int] = {}
    for msg in messages:
        m = re.search(r'Your Tribe killed ([^\s!.,\n]+)', msg.content, re.IGNORECASE)
        if m:
            name = m.group(1)
            kill_counts[name] = kill_counts.get(name, 0) + 1

    if not kill_counts:
        await progress.edit(content="📭 No kill feed messages found in the scanned history.")
        return

    sorted_kills = sorted(kill_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    medals = ["🥇", "🥈", "🥉"] + ["💀"] * 7

    lines = [f"{medals[i]} **{name}** — {count} kill{'s' if count != 1 else ''}" for i, (name, count) in enumerate(sorted_kills)]
    embed = discord.Embed(title="💀 Kill Leaderboard", description="\n".join(lines), color=0xff4500)
    embed.add_field(name="Messages Scanned", value=f"{len(messages):,}", inline=True)
    embed.add_field(name="Unique Players Killed", value=str(len(kill_counts)), inline=True)
    embed.set_footer(text=f"Requested by {interaction.user.display_name} • WockCounter")

    await progress.edit(content=None, embed=embed)


# ── /ask ──────────────────────────────────────────────────────────────────────
@bot.tree.command(name="ask", description="Ask WockBot anything.")
@app_commands.describe(question="What do you want to ask?")
async def ask(interaction: discord.Interaction, question: str):
    await interaction.response.defer(thinking=True)
    reply = await ask_claude(question, interaction.user.display_name)
    await interaction.followup.send(reply)


# ── /addbase ──────────────────────────────────────────────────────────────────
@bot.tree.command(name="addbase", description="Log a base to the tracker.")
@app_commands.describe(
    label="A short name for the base (e.g. 'desert cave', 'enemy alpha')",
    coords="In-game coordinates (e.g. '42.3, 71.0')",
    image="Optional screenshot of the base",
)
async def addbase(
    interaction: discord.Interaction,
    label: str,
    coords: str,
    image: discord.Attachment = None,
):
    # Validate image type if provided
    if image and not image.content_type.startswith("image/"):
        await interaction.response.send_message("❌ Attachment must be an image.", ephemeral=True)
        return

    bases = load_bases()
    entry = {
        "id": next_base_id(bases),
        "label": label,
        "coords": coords,
        "image_url": image.url if image else None,
        "submitted_by": interaction.user.display_name,
        "submitted_by_id": interaction.user.id,
        "submitted_at": datetime.datetime.utcnow().isoformat(),
    }
    bases.append(entry)
    save_bases(bases)

    embed = build_base_embed(entry)
    await interaction.response.send_message(f"✅ Base **{label}** logged as **#{entry['id']}**.", ephemeral=True)

    # Broadcast to the designated base tracker channel
    channel = bot.get_channel(BASE_CHANNEL_ID) or await bot.fetch_channel(BASE_CHANNEL_ID)
    if channel:
        await channel.send(embed=embed)


# ── /removebase ────────────────────────────────────────────────────────────────
@bot.tree.command(name="removebase", description="Remove a base from the tracker by ID (mod only).")
@app_commands.describe(base_id="The ID of the base to remove (see /bases)")
@app_commands.checks.has_permissions(manage_messages=True)
async def removebase(interaction: discord.Interaction, base_id: int):
    bases = load_bases()
    match = next((b for b in bases if b["id"] == base_id), None)
    if not match:
        await interaction.response.send_message(f"❌ No base found with ID **#{base_id}**.", ephemeral=True)
        return
    bases.remove(match)
    save_bases(bases)
    await interaction.response.send_message(f"🗑️ Base **#{base_id}** ({match['label']}) removed.", ephemeral=True)

@removebase.error
async def removebase_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Manage Messages** permission to remove bases.", ephemeral=True)


# ── /bases ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="bases", description="List all tracked bases.")
async def bases_list(interaction: discord.Interaction):
    entries = load_bases()
    if not entries:
        await interaction.response.send_message("📭 No bases have been logged yet.", ephemeral=True)
        return

    # Show the 10 most recently added bases
    recent = entries[-10:][::-1]
    embed = discord.Embed(title="🎯 Tracked Bases", color=0xff4500)
    for b in recent:
        image_note = " 📷" if b.get("image_url") else ""
        embed.add_field(
            name=f"#{b['id']} — {b['label']}{image_note}",
            value=f"`{b['coords']}` — by {b['submitted_by']}",
            inline=False,
        )
    embed.set_footer(text=f"{len(entries)} total base(s) logged • Use /removebase <id> to remove")
    await interaction.response.send_message(embed=embed)


# ── /help ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="help", description="Show all available WockCounter commands.")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 WockCounter Commands", color=0x7289da)

    embed.add_field(name="🔧 Utility", value=(
        "`/ping` — Bot latency\n"
        "`/uptime` — How long the bot has been online\n"
        "`/serverinfo` — Server stats\n"
        "`/userinfo [member]` — User profile\n"
        "`/avatar [member]` — Full-size avatar\n"
        "`/membercount` — Member count breakdown"
    ), inline=False)

    embed.add_field(name="🎮 Fun", value=(
        "`/8ball <question>` — Magic 8-ball\n"
        "`/coinflip` — Heads or tails\n"
        "`/roll [dice]` — Roll dice (e.g. `2d6`)\n"
        "`/rps <choice>` — Rock Paper Scissors\n"
        "`/choose <options>` — Pick from a list\n"
        "`/wock <player>` — Prescribe someone their Wock 🚬\n"
        "`/ask <question>` — Chat with WockBot (or just @mention me)"
    ), inline=False)

    embed.add_field(name="📊 Community", value=(
        "`/poll <question> <opt1> <opt2> [opt3] [opt4]` — Create a poll\n"
        "`/killers [limit]` — Kill feed leaderboard\n"
        "`/count <phrase> [limit]` — Count phrase occurrences\n"
        "`/remindme <time> <message>` — Set a reminder\n"
        "`/afk [reason]` — Set your AFK status"
    ), inline=False)

    embed.add_field(name="🎯 Base Tracker", value=(
        "`/addbase <label> <coords> [image]` — Log a base\n"
        "`/bases` — List tracked bases\n"
        "`/removebase <id>` — Remove a base *(Manage Messages)*"
    ), inline=False)

    embed.add_field(name="🛡️ Moderation", value=(
        "`/purge <amount>` — Bulk delete messages *(Manage Messages)*\n"
        "`/slowmode <seconds>` — Set slowmode *(Manage Channels)*"
    ), inline=False)

    embed.set_footer(text="WockCounter • Alphaclash")
    await interaction.response.send_message(embed=embed)


# ── RUN ───────────────────────────────────────────────────────────────────────
bot.run(BOT_TOKEN)
