import os
import re
import html
import shutil
import sqlite3
import asyncio
import hashlib
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)
import yt_dlp
from langs import T, LANG_NAMES

logging.basicConfig(level=logging.INFO)

# ============ SOZLAMALAR ============
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
AD_TEXT = os.getenv("AD_TEXT", "")
COOKIES_FILE = os.getenv("COOKIES_FILE", "/etc/secrets/cookies.txt")
MAX_SIZE = 49 * 1024 * 1024
HAS_FFMPEG = shutil.which("ffmpeg") is not None
DB_PATH = os.getenv("DB_PATH", "bot.db")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
sem = asyncio.Semaphore(2)
URL_CACHE = {}

# ============ BAZA ============
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, lang TEXT DEFAULT 'uz',
    downloads INTEGER DEFAULT 0, joined TEXT DEFAULT CURRENT_TIMESTAMP)""")
db.commit()


def get_lang(uid):
    r = db.execute("SELECT lang FROM users WHERE id=?", (uid,)).fetchone()
    return r[0] if r and r[0] in T else "uz"


def ensure_user(uid, code=None):
    """Yangi foydalanuvchi: Telegram tilini avtomatik aniqlaydi."""
    lang = (code or "uz")[:2]
    if lang not in T:
        lang = "uz"
    db.execute("INSERT OR IGNORE INTO users(id, lang) VALUES(?, ?)", (uid, lang))
    db.commit()


def set_lang(uid, lang):
    ensure_user(uid)
    db.execute("UPDATE users SET lang=? WHERE id=?", (lang, uid))
    db.commit()


def add_download(uid):
    db.execute("UPDATE users SET downloads=downloads+1 WHERE id=?", (uid,))
    db.commit()


# ============ TIL YORDAMCHILARI ============
def t(uid, key, **kw):
    s = T[get_lang(uid)].get(key) or T["uz"][key]
    return s.format(**kw) if kw else s


def all_langs(key):
    return [T[l][key] for l in T]


def main_kb(uid):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(uid, "b_search"))],
            [KeyboardButton(text=t(uid, "b_stats")), KeyboardButton(text=t(uid, "b_lang"))],
            [KeyboardButton(text=t(uid, "b_info"))],
        ],
        resize_keyboard=True,
    )


def lang_kb():
    rows, row = [], []
    for code, name in LANG_NAMES.items():
        row.append(InlineKeyboardButton(text=name, callback_data=f"lang:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============ YUKLASH YORDAMCHILARI ============
def base_opts():
    o = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "outtmpl": "downloads/%(id)s_%(height)s.%(ext)s",
        "socket_timeout": 30,
        "retries": 3,
    }
    tmp = "/tmp/cookies.txt"
    if not os.path.exists(tmp):
        if os.getenv("COOKIES_TEXT"):
            with open(tmp, "w") as f:
                f.write(os.getenv("COOKIES_TEXT"))
        elif os.path.exists(COOKIES_FILE):
            shutil.copy(COOKIES_FILE, tmp)
    if os.path.exists(tmp):
        o["cookiefile"] = tmp
    return o


def video_opts(q):
    o = base_opts()
    if HAS_FFMPEG:
        o["format"] = f"b[height<={q}][ext=mp4]/bv*[height<={q}][ext=mp4]+ba[ext=m4a]/b[height<={q}]/b"
        o["merge_output_format"] = "mp4"
    else:
        o["format"] = f"b[height<={q}][ext=mp4]/b[height<={q}]/b[ext=mp4]/b"
    return o


def audio_opts():
    o = base_opts()
    o["outtmpl"] = "downloads/%(id)s.%(ext)s"
    if HAS_FFMPEG:
        o["format"] = "bestaudio/best"
        o["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        o["format"] = "bestaudio[ext=m4a]/bestaudio"
    return o


def run_download(url, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if "entries" in info:
            info = info["entries"][0]
        rd = info.get("requested_downloads") or []
        path = rd[0].get("filepath") if rd else ydl.prepare_filename(info)
        return info, path


def run_search(q, n=8):
    o = base_opts()
    o["extract_flat"] = True
    with yt_dlp.YoutubeDL(o) as ydl:
        return ydl.extract_info(f"ytsearch{n}:{q}", download=False).get("entries", [])


def fmt_dur(sec):
    if not sec:
        return ""
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def short_err(e):
    s = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    return html.escape(s[:300])


def ad_footer():
    return f"\n\n{AD_TEXT}" if AD_TEXT else ""


def cleanup(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# ============ BUYRUQLAR ============
@dp.message(Command("start"))
async def start_cmd(m: types.Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    await m.answer(t(m.from_user.id, "welcome"), reply_markup=main_kb(m.from_user.id))


@dp.message(Command("lang"))
async def lang_cmd(m: types.Message):
    ensure_user(m.from_user.id, m.from_user.language_code)
    await m.answer(t(m.from_user.id, "select_lang"), reply_markup=lang_kb())


@dp.message(Command("stats"))
async def admin_stats(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    dl = db.execute("SELECT COALESCE(SUM(downloads),0) FROM users").fetchone()[0]
    await m.answer(f"👑 <b>Admin</b>\n\n👥 Users: <b>{users}</b>\n📥 Downloads: <b>{dl}</b>")


@dp.message(Command("broadcast"))
async def admin_broadcast(m: types.Message, command: CommandObject):
    if m.from_user.id != ADMIN_ID or not command.args:
        return
    ids = [r[0] for r in db.execute("SELECT id FROM users").fetchall()]
    ok = 0
    for uid in ids:
        try:
            await bot.send_message(uid, command.args)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"✅ {ok}/{len(ids)}")


# ============ MENYU TUGMALARI ============
@dp.message(F.text.in_(all_langs("b_lang")))
async def change_lang(m: types.Message):
    await m.answer(t(m.from_user.id, "select_lang"), reply_markup=lang_kb())


@dp.callback_query(F.data.startswith("lang:"))
async def lang_cb(c: types.CallbackQuery):
    code = c.data.split(":")[1]
    if code not in T:
        return await c.answer()
    set_lang(c.from_user.id, code)
    await c.answer(t(c.from_user.id, "lang_ok"))
    await c.message.delete()
    await c.message.answer(t(c.from_user.id, "welcome"), reply_markup=main_kb(c.from_user.id))


@dp.message(F.text.in_(all_langs("b_info")))
async def info_cmd(m: types.Message):
    await m.answer(t(m.from_user.id, "info"))


@dp.message(F.text.in_(all_langs("b_search")))
async def ask_search(m: types.Message):
    await m.answer(t(m.from_user.id, "ask_search"))


@dp.message(F.text.in_(all_langs("b_stats")))
async def my_stats(m: types.Message):
    r = db.execute("SELECT downloads FROM users WHERE id=?", (m.from_user.id,)).fetchone()
    await m.answer(t(m.from_user.id, "stats", n=r[0] if r else 0))


# ============ HAVOLA YOKI QIDIRUV ============
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(m: types.Message):
    uid = m.from_user.id
    ensure_user(uid, m.from_user.language_code)
    text = m.text.strip()

    if re.match(r"https?://", text):
        token = hashlib.md5(text.encode()).hexdigest()[:12]
        URL_CACHE[token] = text
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=f"{t(uid, 'video')} 360p", callback_data=f"v:360:{token}"),
                InlineKeyboardButton(text=f"{t(uid, 'video')} 720p", callback_data=f"v:720:{token}"),
            ],
            [InlineKeyboardButton(text=t(uid, "audio"), callback_data=f"a:{token}")],
        ])
        await m.answer(t(uid, "choose"), reply_markup=kb)
        return

    msg = await m.answer(t(uid, "searching"))
    try:
        loop = asyncio.get_running_loop()
        entries = await loop.run_in_executor(None, run_search, text)
        if not entries:
            await msg.edit_text(t(uid, "none"))
            return
        rows = []
        for i, e in enumerate(entries, 1):
            title = (e.get("title") or "Music")[:38]
            d = fmt_dur(e.get("duration"))
            label = f"{i}. {title}" + (f" · {d}" if d else "")
            rows.append([InlineKeyboardButton(text=label, callback_data=f"s:{e['id']}")])
        await msg.edit_text(t(uid, "found", q=html.escape(text)),
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception as e:
        await msg.edit_text(t(uid, "err") + f"<code>{short_err(e)}</code>")


# ============ YUKLASH ============
async def do_video(c: types.CallbackQuery, url: str, q: str):
    uid = c.from_user.id
    await c.answer()
    await c.message.edit_text(t(uid, "loading"))
    path = None
    try:
        async with sem:
            loop = asyncio.get_running_loop()
            info, path = await loop.run_in_executor(None, run_download, url, video_opts(q))
        if os.path.getsize(path) > MAX_SIZE:
            await c.message.edit_text(t(uid, "too_big"))
            return
        await c.message.edit_text(t(uid, "uploading"))
        me = await bot.get_me()
        cap = f"🎬 <b>{html.escape(info.get('title') or 'Video')[:200]}</b>\n📥 @{me.username}{ad_footer()}"
        await c.message.answer_video(
            types.FSInputFile(path), caption=cap,
            supports_streaming=True,
            width=info.get("width"), height=info.get("height"),
            duration=int(info.get("duration") or 0) or None,
        )
        add_download(uid)
        await c.message.delete()
    except Exception as e:
        logging.exception("video error")
        await c.message.edit_text(t(uid, "err") + f"<code>{short_err(e)}</code>")
    finally:
        cleanup(path)


async def do_audio(c: types.CallbackQuery, url: str):
    uid = c.from_user.id
    await c.answer()
    await c.message.edit_text(t(uid, "loading"))
    path = None
    try:
        async with sem:
            loop = asyncio.get_running_loop()
            info, path = await loop.run_in_executor(None, run_download, url, audio_opts())
        if HAS_FFMPEG:
            mp3 = os.path.splitext(path)[0] + ".mp3"
            if os.path.exists(mp3):
                path = mp3
        if os.path.getsize(path) > MAX_SIZE:
            await c.message.edit_text(t(uid, "too_big"))
            return
        await c.message.edit_text(t(uid, "uploading"))
        title = info.get("title") or "Music"
        artist = info.get("artist") or info.get("uploader") or "Artist"
        me = await bot.get_me()
        await c.message.answer_audio(
            types.FSInputFile(path),
            title=title[:64], performer=artist[:64],
            duration=int(info.get("duration") or 0) or None,
            caption=f"🎵 <b>{html.escape(title)[:200]}</b>\n📥 @{me.username}{ad_footer()}",
        )
        add_download(uid)
        await c.message.delete()
    except Exception as e:
        logging.exception("audio error")
        await c.message.edit_text(t(uid, "err") + f"<code>{short_err(e)}</code>")
    finally:
        cleanup(path)


@dp.callback_query(F.data.startswith("v:"))
async def cb_video(c: types.CallbackQuery):
    _, q, token = c.data.split(":")
    url = URL_CACHE.get(token)
    if not url:
        return await c.answer(t(c.from_user.id, "expired"), show_alert=True)
    await do_video(c, url, q)


@dp.callback_query(F.data.startswith("a:"))
async def cb_audio(c: types.CallbackQuery):
    url = URL_CACHE.get(c.data.split(":")[1])
    if not url:
        return await c.answer(t(c.from_user.id, "expired"), show_alert=True)
    await do_audio(c, url)


@dp.callback_query(F.data.startswith("s:"))
async def cb_search_pick(c: types.CallbackQuery):
    vid = c.data.split(":", 1)[1]
    await do_audio(c, f"https://www.youtube.com/watch?v={vid}")


# ============ RENDER UCHUN WEB SERVER ============
async def health(_):
    return web.Response(text="Bot is running ✅")


async def start_web():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()


async def main():
    os.makedirs("downloads", exist_ok=True)
    await start_web()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("ffmpeg: %s | cookies: %s", HAS_FFMPEG, bool(os.getenv("COOKIES_TEXT")) or os.path.exists(COOKIES_FILE))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
