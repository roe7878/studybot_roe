# studybot.py
import os
import discord
from discord.ext import commands
import aiosqlite
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ===== 기본 설정 =====
BOT_PREFIX = "!"
BOT_TOKEN = os.getenv("STUDYBOT_TOKEN", "")
DB_PATH   = os.getenv("DB_PATH", "studybot.db")

# 타임존: tzdata 없으면 KST(+9)
try:
    TIMEZONE = ZoneInfo("Asia/Seoul")
except ZoneInfoNotFoundError:
    TIMEZONE = timezone(timedelta(hours=9))

# DB 경로 폴더 자동 생성
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

# ===== 유틸 =====
def now_dt() -> datetime:
    return datetime.now(tz=TIMEZONE)

def now_ts() -> int:
    return int(now_dt().timestamp())

def fmt_dur(seconds: int) -> str:
    s = int(seconds or 0)
    if s <= 0: return "0s"
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    out = []
    if h: out.append(f"{h}h")
    if m: out.append(f"{m}m")
    if s: out.append(f"{s}s")
    return " ".join(out)

def period_range(kind: str):
    """[start, end) (둘 다 timezone-aware)"""
    cur = now_dt()
    if kind == "today":
        start = cur.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif kind == "week":
        start = (cur - timedelta(days=cur.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
    elif kind == "month":
        start = cur.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    elif kind == "year":
        start = cur.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1)
    elif kind == "all":
        # 오래된 과거~지금 이후
        start = datetime(1970, 1, 1, tzinfo=TIMEZONE)
        end = cur + timedelta(days=365 * 100)
    else:
        raise ValueError("unknown period")
    return start, end

def guild_id_of(ctx) -> int:
    return ctx.guild.id if ctx.guild else 0

# SQLite: TEXT(iso)와 INTEGER(epoch) 혼재 호환을 위한 epoch 변환 표현식
def epoch_expr(col: str) -> str:
    # typeof(col)='integer'면 그대로, TEXT면 strftime로 epoch 변환
    return f"CASE WHEN typeof({col})='integer' THEN {col} ELSE CAST(strftime('%s', {col}) AS INTEGER) END"

# ===== DB =====
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # 타입은 선언만, SQLite는 동적 타입이므로 INTEGER/TEXT 혼재 허용
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                guild_id INTEGER NOT NULL DEFAULT 0,
                start_ts INTEGER NOT NULL,     -- epoch seconds (권장; 기존 TEXT도 허용)
                end_ts   INTEGER,              -- NULL = 진행 중
                duration_seconds INTEGER       -- 종료 시 확정
            );
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_user ON sessions(user_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_guild ON sessions(guild_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_end   ON sessions(end_ts);")
        await db.commit()

# ===== 합계/랭킹 계산 로직 =====
async def sum_user_between(db, user_id: int, guild_id: int, start_s: int, end_s: int, include_active=True) -> int:
    """end_ts 기준으로 [start_s, end_s) 합계 + (옵션)진행중 포함"""
    e_end = epoch_expr("end_ts")
    # 종료된 세션 합계
    q = f"""
        SELECT COALESCE(SUM(duration_seconds), 0)
        FROM sessions
        WHERE user_id=? AND guild_id=?
          AND end_ts IS NOT NULL
          AND {e_end} >= ? AND {e_end} < ?
    """
    row = await db.execute_fetchone(q, (user_id, guild_id, start_s, end_s))
    total = int(row[0] or 0)

    if include_active:
        # 진행 중 세션(기간 끝 이전에 시작한 것)
        e_start = epoch_expr("start_ts")
        rows = await db.execute_fetchall(
            f"""SELECT {e_start} FROM sessions
                WHERE user_id=? AND guild_id=? AND end_ts IS NULL
                  AND {e_start} < ?""",
            (user_id, guild_id, end_s)
        )
        clamp_now = min(now_ts(), end_s)
        for (st,) in rows:
            st = int(st)
            overlap = max(0, clamp_now - max(st, start_s))
            total += overlap
    return total

async def rank_between(db, guild_id: int, start_s: int, end_s: int, include_active=True, limit=10):
    """길드 기준 랭킹(Top N). 종료+진행중 포함."""
    e_end = epoch_expr("end_ts")
    # 종료된 세션 합계 먼저
    rows = await db.execute_fetchall(
        f"""
        SELECT user_id, COALESCE(MAX(user_name), ''), COALESCE(SUM(duration_seconds),0) AS total
        FROM sessions
        WHERE guild_id=? AND end_ts IS NOT NULL
          AND {e_end} >= ? AND {e_end} < ?
        GROUP BY user_id
        """, (guild_id, start_s, end_s)
    )
    totals = {uid: int(t) for uid, _, t in rows}
    names  = {uid: name for uid, name, _ in rows}

    if include_active:
        # 진행중 세션을 사용자별로 더한다
        e_start = epoch_expr("start_ts")
        rows_a = await db.execute_fetchall(
            f"""SELECT user_id, COALESCE(MAX(user_name), ''), {e_start}
                FROM sessions
                WHERE guild_id=? AND end_ts IS NULL
                  AND {e_start} < ?
                GROUP BY id""",  # id별 row; 사용자별 합산은 아래에서
            (guild_id, end_s)
        )
        clamp_now = min(now_ts(), end_s)
        for uid, name, st in rows_a:
            st = int(st)
            overlap = max(0, clamp_now - max(st, start_s))
            if overlap > 0:
                totals[uid] = totals.get(uid, 0) + overlap
                if uid not in names:
                    names[uid] = name or ""

    # 정렬 및 Top N
    ordered = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:limit]
    return [(uid, names.get(uid, ""), tot) for uid, tot in ordered]

# ===== Bot =====
intents = discord.Intents.default()
intents.message_content = True  # 일반 텍스트 명령
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents, help_command=None)

@bot.event
async def on_ready():
    await init_db()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Ready!")

# --- 명령: 공부시작 ---
@bot.command(name="공부시작")
async def cmd_start(ctx):
    user_id = ctx.author.id
    user_name = str(ctx.author)
    g_id = guild_id_of(ctx)

    async with aiosqlite.connect(DB_PATH) as db:
        # 이미 진행중인지 확인
        row = await db.execute_fetchone(
            "SELECT id FROM sessions WHERE user_id=? AND guild_id=? AND end_ts IS NULL",
            (user_id, g_id)
        )
        if row:
            await ctx.reply("이미 공부 중이에요! `!공부끝`으로 종료해 주세요.")
            return

        await db.execute(
            "INSERT INTO sessions (user_id, user_name, guild_id, start_ts) VALUES (?,?,?,?)",
            (user_id, user_name, g_id, now_ts())
        )
        await db.commit()

    await ctx.reply(f"공부 시작! 시작 시각: {now_dt().strftime('%Y-%m-%d %H:%M:%S')}")

# --- 명령: 공부끝 ---
@bot.command(name="공부끝")
async def cmd_end(ctx):
    user_id = ctx.author.id
    g_id = guild_id_of(ctx)
    end_sec = now_ts()

    async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchone(
            "SELECT id, start_ts FROM sessions WHERE user_id=? AND guild_id=? AND end_ts IS NULL ORDER BY id DESC LIMIT 1",
            (user_id, g_id)
        )
        if not row:
            await ctx.reply("진행 중인 공부가 없어요. `!공부시작`으로 시작해 주세요.")
            return

        sess_id, start_sec = row
        start_sec = int(start_sec)
        dur = max(0, end_sec - start_sec)

        await db.execute(
            "UPDATE sessions SET end_ts=?, duration_seconds=? WHERE id=?",
            (end_sec, dur, sess_id)
        )
        await db.commit()

    await ctx.reply(f"공부 종료! 소요 시간: **{fmt_dur(dur)}**")

# --- 명령: 통계 (개인) ---
@bot.command(name="통계")
async def cmd_stats(ctx, period: str = ""):
    """
    사용법:
      !통계            -> 오늘/주/월/연 요약 임베드
      !통계 week       -> 해당 기간만
    """
    g_id = guild_id_of(ctx)
    user_id = ctx.author.id

    async with aiosqlite.connect(DB_PATH) as db:
        if period.lower() in ("today", "week", "month", "year"):
            start, end = period_range(period.lower())
            total = await sum_user_between(db, user_id, g_id, int(start.timestamp()), int(end.timestamp()), include_active=True)
            await ctx.reply(f"{ctx.author.mention}님의 `{period.lower()}` 공부 합계(진행 중 포함): **{fmt_dur(total)}**")
            return

        # 요약 임베드(today/week/month/year)
        periods = ["today", "week", "month", "year"]
        fields = []
        for p in periods:
            s, e = period_range(p)
            tot = await sum_user_between(db, user_id, g_id, int(s.timestamp()), int(e.timestamp()), include_active=True)
            fields.append((p, tot))

    embed = discord.Embed(
        title=f"📈 {ctx.author.display_name} 님의 통계 (진행 중 포함)",
        color=0x2ecc71,
        timestamp=now_dt()
    )
    name_map = {"today":"오늘", "week":"이번주", "month":"이번달", "year":"올해"}
    for k, v in fields:
        embed.add_field(name=name_map[k], value=fmt_dur(v), inline=False)

    await ctx.reply(embed=embed)

# --- 명령: 랭킹 (서버 기준) ---
@bot.command(name="랭킹")
async def cmd_rank(ctx, period: str = "today"):
    """
    사용법:
      !랭킹            -> 기본 today
      !랭킹 week|month|year|all
    """
    period = period.lower()
    if period not in ("today", "week", "month", "year", "all"):
        await ctx.reply("기간은 `today|week|month|year|all` 중에서 선택해 주세요. 예) `!랭킹 month`")
        return

    g_id = guild_id_of(ctx)
    s, e = period_range(period)

    async with aiosqlite.connect(DB_PATH) as db:
        rows = await rank_between(db, g_id, int(s.timestamp()), int(e.timestamp()), include_active=True, limit=10)

    if not rows:
        await ctx.reply("아직 기록이 없어요.")
        return

    title_map = {"today":"오늘", "week":"이번주", "month":"이번달", "year":"올해", "all":"전체"}
    embed = discord.Embed(title=f"🏆 {title_map[period]} 랭킹 (Top 10, 진행 중 포함)", color=0x3498db)
    for i, (uid, name, tot) in enumerate(rows, start=1):
        display = name or f"<@{uid}>"
        embed.add_field(name=f"{i}. {display}", value=fmt_dur(tot), inline=False)

    await ctx.reply(embed=embed)

# --- 명령: 전체현황(서버) ---
@bot.command(name="전체현황")
async def cmd_overall(ctx):
    g_id = guild_id_of(ctx)
    async with aiosqlite.connect(DB_PATH) as db:
        # 전체 누적(종료+진행중) 사용자 단위
        s_all, e_all = period_range("all")
        rows = await rank_between(db, g_id, int(s_all.timestamp()), int(e_all.timestamp()), include_active=True, limit=100)

    if not rows:
        await ctx.reply("아직 기록이 없어요.")
        return

    total_users = len(rows)
    total_time = sum(t for _, _, t in rows)
    avg = int(total_time / total_users) if total_users else 0

    embed = discord.Embed(
        title="📊 전체 현황 (진행 중 포함)",
        description=f"전체 기록자: **{total_users}명**\n전체 누적: **{fmt_dur(total_time)}**\n1인 평균: **{fmt_dur(avg)}**",
        color=0x9b59b6
    )
    top = rows[:10]
    lines = []
    for i, (_, name, tot) in enumerate(top, start=1):
        display = name or "(알 수 없음)"
        lines.append(f"**{i}.** {display} — {fmt_dur(tot)}")
    embed.add_field(name="Top 10", value="\n".join(lines), inline=False)
    await ctx.reply(embed=embed)

# --- 도움말 ---
@bot.command(name="help")
async def cmd_help(ctx):
    msg = (
        "**공부봇 사용법**\n"
        "`!공부시작` — 공부 시작\n"
        "`!공부끝` — 공부 종료 및 저장\n"
        "`!통계 [today|week|month|year]` — 기간별 합계(기본은 4개 기간 요약, 진행 중 포함)\n"
        "`!랭킹 [today|week|month|year|all]` — 서버 랭킹(진행 중 포함)\n"
        "`!전체현황` — 서버 전체 요약 + Top 10 (진행 중 포함)\n"
    )
    await ctx.reply(msg)

# --- 실행 ---
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❗ STUDYBOT_TOKEN 환경변수에 디스코드 봇 토큰을 넣어주세요.")
    else:
        intents = discord.Intents.default()
        intents.message_content = True
        bot.run(BOT_TOKEN)
