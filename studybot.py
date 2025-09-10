# studybot.py
import os
import discord
from discord.ext import commands
import aiosqlite
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------------------
# 필수 설정
# ---------------------
BOT_PREFIX = "!"

# 토큰은 코드에 박지 말고 환경변수로!
BOT_TOKEN = os.getenv("STUDYBOT_TOKEN", "")

# DB 경로도 환경변수로 교체 가능 (Railway 볼륨 쓰면 /data/studybot.db 권장)
DB_PATH = os.getenv("DB_PATH", "studybot.db")

# 타임존: tzdata 없으면 KST(+9)로 대체
try:
    TIMEZONE = ZoneInfo("Asia/Seoul")  # 한국 시간
except ZoneInfoNotFoundError:
    TIMEZONE = timezone(timedelta(hours=9))
# ---------------------
# 유틸
# ---------------------
def now():
    return datetime.now(tz=TIMEZONE)

def fmt_dur(seconds: int) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "0s"
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    out = []
    if h: out.append(f"{h}h")
    if m: out.append(f"{m}m")
    if s: out.append(f"{s}s")
    return " ".join(out)

def period_range(kind: str):
    cur = now()
    if kind == "today":
        start = cur.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif kind == "week":
        start = (cur - timedelta(days=cur.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
    elif kind == "month":
        start = cur.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start.replace(month=1, year=start.year+1) if start.month == 12
               else start.replace(month=start.month+1))
    elif kind == "year":
        start = cur.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year+1)
    else:
        raise ValueError("unknown period")
    return start, end

# ---------------------
# DB
# ---------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            guild_id INTEGER,
            start_ts TEXT NOT NULL,
            end_ts TEXT,
            duration_seconds INTEGER
        );
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_user ON sessions(user_id);")
        await db.commit()

# ---------------------
# Bot
# ---------------------
intents = discord.Intents.default()
intents.message_content = True  # 일반 명령어 사용에 필요
intents.members = True          # (선택) 멤버 정보

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents, help_command=None)

@bot.event
async def on_ready():
    await init_db()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Ready!")

# ---------------------
# 명령어: 공부시작
# ---------------------
@bot.command(name="공부시작")
async def cmd_start(ctx):
    user_id = ctx.author.id
    user_name = str(ctx.author)
    guild_id = ctx.guild.id if ctx.guild else None
    now_ts = now().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM sessions WHERE user_id=? AND end_ts IS NULL",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                await ctx.reply("이미 공부 중이에요! `!공부끝`으로 종료해 주세요.")
                return
        await db.execute(
            "INSERT INTO sessions (user_id, user_name, guild_id, start_ts) VALUES (?,?,?,?)",
            (user_id, user_name, guild_id, now_ts)
        )
        await db.commit()

    await ctx.reply(f"공부 시작! 시작 시각: {now().strftime('%Y-%m-%d %H:%M:%S')}")

# ---------------------
# 명령어: 공부끝
# ---------------------
@bot.command(name="공부끝")
async def cmd_end(ctx):
    user_id = ctx.author.id
    end_dt = now()
    end_ts = end_dt.isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, start_ts FROM sessions WHERE user_id=? AND end_ts IS NULL ORDER BY id DESC LIMIT 1",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                await ctx.reply("진행 중인 공부가 없어요. `!공부시작`으로 시작해 주세요.")
                return
            sess_id, start_ts = row

        start_dt = datetime.fromisoformat(start_ts)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=TIMEZONE)

        dur = (end_dt - start_dt).total_seconds()
        if dur < 0: dur = 0

        await db.execute(
            "UPDATE sessions SET end_ts=?, duration_seconds=? WHERE id=?",
            (end_ts, int(dur), sess_id)
        )
        await db.commit()

    await ctx.reply(f"공부 종료! 소요 시간: **{fmt_dur(dur)}**")

# ---------------------
# 명령어: 통계
# ---------------------
@bot.command(name="통계")
async def cmd_stats(ctx, period: str = "today"):
    period = period.lower()
    if period not in ("today","week","month","year"):
        await ctx.reply("기간은 `today`, `week`, `month`, `year` 중에서 선택해 주세요. 예) `!통계 week`")
        return

    start, end = period_range(period)
    user_id = ctx.author.id

    async with aiosqlite.connect(DB_PATH) as db:
        q = """
        SELECT IFNULL(SUM(duration_seconds),0) FROM sessions
        WHERE user_id=? AND end_ts IS NOT NULL
          AND datetime(start_ts) >= ? AND datetime(start_ts) < ?
        """
        async with db.execute(q, (user_id, start.isoformat(), end.isoformat())) as cur:
            row = await cur.fetchone()
            total = row[0] if row and row[0] else 0

    await ctx.reply(f"{ctx.author.mention}님의 `{period}` 공부 합계: **{fmt_dur(total)}**")

# ---------------------
# 명령어: 전체현황
# ---------------------
@bot.command(name="전체현황")
async def cmd_overall(ctx):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT user_id, COALESCE(user_name,''), IFNULL(SUM(duration_seconds),0) AS total
            FROM sessions
            WHERE end_ts IS NOT NULL
            GROUP BY user_id
            ORDER BY total DESC
            LIMIT 100
        """) as cur:
            rows = await cur.fetchall()

    if not rows:
        await ctx.reply("아직 기록이 없어요.")
        return

    total_users = len(rows)
    total_time = sum(r[2] for r in rows)
    avg = int(total_time / total_users) if total_users else 0

    embed = discord.Embed(
        title="📊 전체 현황",
        description=f"전체 기록자: **{total_users}명**\n전체 누적: **{fmt_dur(total_time)}**\n평균 누적: **{fmt_dur(avg)}**",
        color=0x2ecc71
    )
    top = rows[:10]
    lines = []
    for i, (_, name, tot) in enumerate(top, start=1):
        display = name if name else "(알 수 없음)"
        lines.append(f"**{i}.** {display} — {fmt_dur(tot)}")
    embed.add_field(name="Top 10", value="\n".join(lines), inline=False)

    await ctx.reply(embed=embed)

# ---------------------
# 명령어: 랭킹
# ---------------------
@bot.command(name="랭킹")
async def cmd_rank(ctx):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT user_id, COALESCE(user_name,''), IFNULL(SUM(duration_seconds),0) AS total
            FROM sessions
            WHERE end_ts IS NOT NULL
            GROUP BY user_id
            ORDER BY total DESC
            LIMIT 10
        """) as cur:
            rows = await cur.fetchall()

    if not rows:
        await ctx.reply("아직 기록이 없어요.")
        return

    embed = discord.Embed(title="🏆 누적 공부 랭킹 (Top 10)", color=0x3498db)
    for i, (_, name, tot) in enumerate(rows, start=1):
        display = name if name else "(알 수 없음)"
        embed.add_field(name=f"{i}. {display}", value=fmt_dur(tot), inline=False)

    await ctx.reply(embed=embed)

# ---------------------
# 도움말
# ---------------------
@bot.command(name="help")
async def cmd_help(ctx):
    msg = (
        "**공부봇 사용법**\n"
        "`!공부시작` — 공부 시작\n"
        "`!공부끝` — 공부 종료 및 저장\n"
        "`!통계 [today|week|month|year]` — 기간별 합계(기본 today)\n"
        "`!전체현황` — 전체 사용자 통계 및 Top 10\n"
        "`!랭킹` — 누적 랭킹 Top 10\n"
    )
    await ctx.reply(msg)

# ---------------------
# 실행
# ---------------------
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❗ STUDYBOT_TOKEN 환경변수에 디스코드 봇 토큰을 넣어주세요.")
    else:
        bot.run(BOT_TOKEN)