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

# 타임존
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
    cur = now_dt()
    if kind == "today":
        start = cur.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif kind == "week":
        start = (cur - timedelta(days=cur.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
    elif kind == "month":
        start = cur.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year+1, month=1) if start.month == 12 else start.replace(month=start.month+1)
    elif kind == "year":
        start = cur.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year+1)
    elif kind == "all":
        start = datetime(1970, 1, 1, tzinfo=TIMEZONE)
        end   = cur + timedelta(days=365*100)
    else:
        raise ValueError("unknown period")
    return start, end

def guild_id_of(ctx) -> int:
    return ctx.guild.id if ctx.guild else 0

def epoch_expr(col: str) -> str:
    return f"CASE WHEN typeof({col})='integer' THEN {col} ELSE CAST(strftime('%s', {col}) AS INTEGER) END"

# === aiosqlite fetch 헬퍼 ===
async def fetchone(db, q, params=()):
    async with db.execute(q, params) as cur:
        return await cur.fetchone()

async def fetchall(db, q, params=()):
    async with db.execute(q, params) as cur:
        return await cur.fetchall()

def _to_epoch_mixed(v) -> int:
    if isinstance(v, int):
        return v
    try:
        dt = datetime.fromisoformat(str(v))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TIMEZONE)
        return int(dt.timestamp())
    except Exception:
        return int(str(v))

# ===== DB =====
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                guild_id INTEGER NOT NULL DEFAULT 0,
                start_ts INTEGER NOT NULL,
                end_ts   INTEGER,
                duration_seconds INTEGER
            );
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_user ON sessions(user_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_guild ON sessions(guild_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_end   ON sessions(end_ts);")
        # 예전 데이터 보정: duration 빈 값 채우기
        await db.execute(
            f"""UPDATE sessions
                SET duration_seconds = ({epoch_expr('end_ts')} - {epoch_expr('start_ts')})
                WHERE end_ts IS NOT NULL AND (duration_seconds IS NULL OR duration_seconds <= 0)"""
        )
        await db.commit()

# ===== 합계/랭킹 =====
# ▶ 합계: 모든 세션을 가져와 '기간과의 교집합' 길이로 정확히 계산 (문자/정수 타입 혼용 안전)
async def sum_user_between(db, user_id: int, guild_id: int, start_s: int, end_s: int, include_active=True) -> int:
    total = 0

    # 종료된 세션
    rows = await fetchall(
        db,
        "SELECT start_ts, end_ts FROM sessions WHERE user_id=? AND guild_id=? AND end_ts IS NOT NULL",
        (user_id, guild_id)
    )
    for st, et in rows:
        st_e = _to_epoch_mixed(st)
        et_e = _to_epoch_mixed(et)
        if et_e <= st_e:
            continue
        total += max(0, min(et_e, end_s) - max(st_e, start_s))

    # 진행중 세션
    if include_active:
        rows_a = await fetchall(
            db,
            "SELECT start_ts FROM sessions WHERE user_id=? AND guild_id=? AND end_ts IS NULL",
            (user_id, guild_id)
        )
        clamp_now = min(now_ts(), end_s)
        for (st,) in rows_a:
            st_e = _to_epoch_mixed(st)
            total += max(0, clamp_now - max(st_e, start_s))

    return int(total)

# ▶ 랭킹: 유저별로 위와 같은 방식으로 누적(종료 + 진행중) → 정렬
async def rank_between(db, guild_id: int, start_s: int, end_s: int, include_active=True, limit=10):
    totals = {}   # user_id -> seconds
    names  = {}   # user_id -> display name

    # 종료된 세션 모두 반영
    rows = await fetchall(
        db,
        "SELECT user_id, COALESCE(user_name,''), start_ts, end_ts "
        "FROM sessions WHERE guild_id=? AND end_ts IS NOT NULL",
        (guild_id,)
    )
    for uid, name, st, et in rows:
        st_e = _to_epoch_mixed(st)
        et_e = _to_epoch_mixed(et)
        if et_e <= st_e:
            continue
        overlap = max(0, min(et_e, end_s) - max(st_e, start_s))
        if overlap > 0:
            totals[uid] = totals.get(uid, 0) + overlap
            if uid not in names or not names[uid]:
                names[uid] = name or ""

    # 진행중 세션 반영
    if include_active:
        rows_a = await fetchall(
            db,
            "SELECT user_id, COALESCE(user_name,''), start_ts "
            "FROM sessions WHERE guild_id=? AND end_ts IS NULL",
            (guild_id,)
        )
        clamp_now = min(now_ts(), end_s)
        for uid, name, st in rows_a:
            st_e = _to_epoch_mixed(st)
            overlap = max(0, clamp_now - max(st_e, start_s))
            if overlap > 0:
                totals[uid] = totals.get(uid, 0) + overlap
                if uid not in names or not names[uid]:
                    names[uid] = name or ""

    ordered = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:limit]
    return [(uid, names.get(uid, ""), tot) for uid, tot in ordered]

# ===== Bot =====
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents, help_command=None)

@bot.event
async def on_ready():
    await init_db()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Ready!")

# --- 공부시작 ---
@bot.command(name="공부시작")
async def cmd_start(ctx):
    user_id = ctx.author.id
    user_name = str(ctx.author)
    g_id = guild_id_of(ctx)

    async with aiosqlite.connect(DB_PATH) as db:
        row = await fetchone(
            db,
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

# --- 공부끝 ---
@bot.command(name="공부끝")
async def cmd_end(ctx):
    user_id = ctx.author.id
    g_id = guild_id_of(ctx)
    end_sec = now_ts()

    async with aiosqlite.connect(DB_PATH) as db:
        row = await fetchone(
            db,
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

# --- 통계 ---
@bot.command(name="통계")
async def cmd_stats(ctx, period: str = ""):
    g_id = guild_id_of(ctx)
    user_id = ctx.author.id

    async with aiosqlite.connect(DB_PATH) as db:
        if period.lower() in ("today", "week", "month", "year"):
            s, e = period_range(period.lower())
            tot = await sum_user_between(db, user_id, g_id, int(s.timestamp()), int(e.timestamp()), include_active=True)
            await ctx.reply(f"{ctx.author.mention}님의 `{period.lower()}` 공부 합계(진행 중 포함): **{fmt_dur(tot)}**")
            return

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

# --- 랭킹 ---
@bot.command(name="랭킹")
async def cmd_rank(ctx, period: str = "today"):
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

# --- 전체현황 ---
@bot.command(name="전체현황")
async def cmd_overall(ctx):
    g_id = guild_id_of(ctx)
    async with aiosqlite.connect(DB_PATH) as db:
        s_all, e_all = period_range("all")
        rows = await rank_between(db, g_id, int(s_all.timestamp()), int(e_all.timestamp()), include_active=True, limit=100)

    if not rows:
        await ctx.reply("아직 기록이 없어요.")
        return

    total_users = len(rows)
    total_time  = sum(t for _, _, t in rows)
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

# --- 디버그 / 세션목록 ---
@bot.command(name="디버그")
async def cmd_debug(ctx):
    g = guild_id_of(ctx)
    async with aiosqlite.connect(DB_PATH) as db:
        total = (await fetchone(db, "SELECT COUNT(*) FROM sessions WHERE guild_id=?", (g,)))[0]
        open_  = (await fetchone(db, "SELECT COUNT(*) FROM sessions WHERE guild_id=? AND end_ts IS NULL", (g,)))[0]
    await ctx.reply(f"DB_PATH: `{DB_PATH}`\nGuild: `{g}`\nrows: {total}, open: {open_}")

@bot.command(name="기록")
async def cmd_sessions(ctx, period: str = "today"):
    period = period.lower()
    if period not in ("today", "week", "month", "year"):
        return await ctx.reply("기간은 `today|week|month|year` 중에서 골라줘!")
    s_dt, e_dt = period_range(period)
    s_ts, e_ts = int(s_dt.timestamp()), int(e_dt.timestamp())
    g_id, uid = guild_id_of(ctx), ctx.author.id

    async with aiosqlite.connect(DB_PATH) as db:
        rows = await fetchall(
            db,
            """SELECT start_ts, end_ts
               FROM sessions
               WHERE user_id=? AND guild_id=?
               ORDER BY COALESCE(end_ts, start_ts) DESC
               LIMIT 20""",
            (uid, g_id)
        )

    lines, tot = [], 0
    for st, et in rows:
        st_e = _to_epoch_mixed(st)
        et_e = _to_epoch_mixed(et) if et is not None else None
        if et_e is None:
            clamp_now = min(now_ts(), e_ts)
            add = max(0, clamp_now - max(st_e, s_ts))
            ended = "NOW"; tag = " (진행중)"
        else:
            add = max(0, min(et_e, e_ts) - max(st_e, s_ts))
            ended = datetime.fromtimestamp(et_e, tz=TIMEZONE).strftime("%H:%M"); tag = ""
        if add > 0: tot += add
        start_s = datetime.fromtimestamp(st_e, tz=TIMEZONE).strftime("%m-%d %H:%M")
        lines.append(f"{start_s}~{ended} {fmt_dur(add)}{tag}")

    desc = "\n".join(lines) if lines else "표시할 세션이 없어요."
    em = discord.Embed(title=f"📝 `{period}` 세션(최근 20개, 기간 기준)", description=desc, color=0x95a5a6)
    em.add_field(name="합계", value=fmt_dur(tot), inline=False)
    await ctx.reply(embed=em)

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
        "`!기록 [today|week|month|year]` — 기간 내 세션 목록 + 합계\n"
        "`!디버그` — DB 경로/세션 수 확인\n"
    )
    await ctx.reply(msg)

# --- 실행 ---
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❗ STUDYBOT_TOKEN 환경변수에 디스코드 봇 토큰을 넣어주세요.")
    else:
        bot.run(BOT_TOKEN)
