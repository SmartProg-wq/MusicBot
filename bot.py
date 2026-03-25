import asyncio
import logging
import aiohttp
import io
import uuid
import time
import os
import socket
from datetime import datetime, timedelta

from telethon import TelegramClient, events
from telethon.tl.functions.messages import EditInlineBotMessageRequest
from telethon.tl.custom.button import Button
from telethon.tl.types import (
    UpdateBotInlineSend,
    InputMediaUploadedDocument,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    InputWebDocument,
    DocumentAttributeImageSize,
    KeyboardButtonUrl,
    KeyboardButtonRow,
    ReplyInlineMarkup,
)
from telethon.extensions import html
from telethon.sessions import StringSession

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
import uvicorn

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.DEBUG,
)
logger = logging.getLogger(__name__)
logging.getLogger("telethon").setLevel(logging.WARNING)

BOT_TOKEN = "8704405944:AAExc5BsFjcMibMuWnuE4fRW0RsbV5WEZWY"
API_ID = 15515318
API_HASH = "e04ab312a57e56e6ba42dac8dab8a5f5"
RATE_LIMIT_WINDOW_MINUTES = 1
SEARCH_API = "https://saavn.sumit.co/api/search/songs"
MAX_TG_FILE_SIZE = 50 * 1024 * 1024
CHANNEL_URL = "https://t.me/thesmartdev"
TMP_DIR = "/tmp"

user_download_timestamps: dict[int, datetime] = {}
song_cache: dict[str, dict] = {}

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_client: TelegramClient | None = None
_bot_username: str = "unknown"


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def is_rate_limited(user_id: int) -> bool:
    ts = user_download_timestamps.get(user_id)
    return bool(ts and datetime.now() - ts < timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES))


def update_rate_limit(user_id: int) -> None:
    user_download_timestamps[user_id] = datetime.now()


async def search_songs(query: str) -> list:
    logger.info(f"[SEARCH] Searching for: {query}")
    async with aiohttp.ClientSession() as session:
        async with session.get(
            SEARCH_API,
            params={"query": query},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
            results = data.get("data", {}).get("results", [])
            logger.info(f"[SEARCH] Got {len(results)} results")
            return results


async def download_song_to_tmp(song_url: str, label: str) -> str:
    logger.info(f"[DOWNLOAD] Starting download: {label}")
    logger.info(f"[DOWNLOAD] URL: {song_url[:100]}")
    tmp_path = os.path.join(TMP_DIR, f"song_{uuid.uuid4().hex}.mp3")
    total = 0
    headers = {"User-Agent": BROWSER_UA}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(
            song_url,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            logger.info(
                f"[DOWNLOAD] HTTP {resp.status} | "
                f"Content-Length={resp.headers.get('Content-Length', 'unknown')} | "
                f"Content-Type={resp.headers.get('Content-Type', 'unknown')}"
            )
            if resp.status != 200:
                raise RuntimeError(
                    f"HTTP {resp.status} downloading song. "
                    f"URL may have expired — try again."
                )
            with open(tmp_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(512 * 1024):
                    f.write(chunk)
                    total += len(chunk)
                    if total % (5 * 1024 * 1024) < 512 * 1024:
                        logger.info(f"[DOWNLOAD] Progress: {total // 1024 // 1024} MB downloaded")
    if total == 0:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise RuntimeError("Downloaded 0 bytes — URL expired or returned empty response")
    logger.info(f"[DOWNLOAD] Complete: {total // 1024} KB ({total / 1024 / 1024:.1f} MB) -> {tmp_path}")
    return tmp_path


def cleanup_tmp(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
            logger.info(f"[CLEANUP] Removed {path}")
    except Exception as e:
        logger.warning(f"[CLEANUP] Could not remove {path}: {e}")


async def safe_edit_text(client, msg_id, text: str) -> None:
    logger.info(f"[EDIT] Updating inline message text: {text[:80]}")
    try:
        await client(EditInlineBotMessageRequest(
            id=msg_id,
            message=text,
            media=None,
        ))
    except Exception as e:
        logger.warning(f"[EDIT] Could not edit inline message: {e}")


async def progress_bar(current, total, start_time, last_update):
    elapsed_time = time.time() - start_time
    if elapsed_time == 0:
        elapsed_time = 0.1

    percentage = (current / total) * 100
    progress = f"{'▓' * int(percentage // 5)}{'░' * (20 - int(percentage // 5))}"
    speed = current / elapsed_time / 1024 / 1024
    uploaded = current / 1024 / 1024
    total_size = total / 1024 / 1024

    if time.time() - last_update[0] < 1:
        return None

    last_update[0] = time.time()

    text = (
        f"🎵 <b>Smart Upload Progress Bar ✅</b>\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"{progress}\n"
        f"<b>Percentage:</b> {percentage:.2f}%\n"
        f"<b>Speed:</b> {speed:.2f} MB/s\n"
        f"<b>Status:</b> {uploaded:.2f} MB of {total_size:.2f} MB\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"<b>Smooth Transfer → Activated ✅</b>"
    )

    return text


async def process_inline_song(client, msg_id, song_data: dict, user_id: int) -> None:
    song_name = song_data.get("name", "Unknown")
    artists = song_data.get("artists", {})
    primary_artists = artists.get("primary", [])
    artist_names = ", ".join([a.get("name", "") for a in primary_artists])
    album = song_data.get("album", {})
    album_name = album.get("name", "Unknown Album")
    duration = song_data.get("duration", 0)
    image_list = song_data.get("image", [])
    download_urls = song_data.get("downloadUrl", [])
    play_count = song_data.get("playCount", 0)
    song_url = song_data.get("url", "")

    logger.info(f"[INLINE_PROC] User {user_id} | Song: {song_name}")

    tmp_path = None
    try:
        await safe_edit_text(client, msg_id, "🔍 Searching The Audio...")

        if is_rate_limited(user_id):
            logger.info(f"[INLINE_PROC] User {user_id} is rate limited")
            await safe_edit_text(
                client, msg_id,
                f"Rate limited. Wait {RATE_LIMIT_WINDOW_MINUTES} minute(s)."
            )
            return

        if not download_urls:
            await safe_edit_text(client, msg_id, f"No download URLs found.\n{song_name}")
            return

        best_url = None
        best_quality = ""
        for url_info in download_urls:
            quality = url_info.get("quality", "")
            url_dl = url_info.get("url", "")
            if url_dl:
                if quality in ("320kbps", "160kbps"):
                    best_url = url_dl
                    best_quality = quality
                    break
                elif quality == "96kbps" and not best_url:
                    best_url = url_dl
                    best_quality = quality

        if not best_url:
            await safe_edit_text(client, msg_id, f"No suitable download URL found.\n{song_name}")
            return

        await safe_edit_text(client, msg_id, "Found ☑️ Downloading...")

        logger.info(f"[INLINE_PROC] Downloading: {best_quality}")

        tmp_path = await download_song_to_tmp(best_url, best_quality)
        size_bytes = os.path.getsize(tmp_path)
        size_mb = size_bytes / 1024 / 1024
        logger.info(f"[INLINE_PROC] Downloaded {size_mb:.1f} MB -> {tmp_path}")

        if size_bytes > MAX_TG_FILE_SIZE:
            await safe_edit_text(
                client, msg_id,
                f"File too large ({size_mb:.1f} MB). Telegram limit is 50 MB.\n{song_name}"
            )
            return

        start_time = time.time()
        last_update = [time.time()]

        async def progress_callback(current, total):
            progress_text = await progress_bar(current, total, start_time, last_update)
            if progress_text:
                try:
                    progress_text_parsed, progress_entities = html.parse(progress_text)
                    await client(EditInlineBotMessageRequest(
                        id=msg_id,
                        message=progress_text_parsed,
                        media=None,
                        entities=progress_entities
                    ))
                except Exception as e:
                    logger.warning(f"[PROGRESS] Could not update: {e}")

        with open(tmp_path, "rb") as f:
            song_bytes = f.read()

        buf = io.BytesIO(song_bytes)
        buf.name = "song.mp3"

        uploaded = await client.upload_file(
            buf,
            file_name="song.mp3",
            file_size=size_bytes,
            progress_callback=progress_callback
        )
        logger.info(f"[INLINE_PROC] Upload complete")

        cleanup_tmp(tmp_path)
        tmp_path = None

        escaped_title = song_name
        user_info = f"<a href='tg://user?id={user_id}'>User {user_id}</a>"

        caption_html = (
            f"🎵 <b>Title:</b> <code>{escaped_title}</code>\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"👁️‍🗨️ <b>Views:</b> <b>{play_count}</b>\n"
            f"<b>🔗 Url:</b> <a href=\"{song_url}\">Listen On Saavn</a>\n"
            f"⏱️ <b>Duration:</b> <b>{duration}s</b>\n"
            f"🎤 <b>Artist:</b> <b>{artist_names}</b>\n"
            f"💿 <b>Album:</b> <b>{album_name}</b>\n"
            f"🎧 <b>Quality:</b> <b>{best_quality}</b>\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"<b>Downloaded By:</b> {user_info}"
        )

        caption_text, caption_entities = html.parse(caption_html)

        media = InputMediaUploadedDocument(
            file=uploaded,
            mime_type="audio/mpeg",
            attributes=[
                DocumentAttributeAudio(duration=duration, title=escaped_title, performer=artist_names),
                DocumentAttributeFilename(file_name="song.mp3"),
            ],
        )

        button = KeyboardButtonUrl("📢 Join Channel", CHANNEL_URL)
        row = KeyboardButtonRow(buttons=[button])
        reply_markup = ReplyInlineMarkup(rows=[row])

        await client(EditInlineBotMessageRequest(
            id=msg_id,
            message=caption_text,
            media=media,
            reply_markup=reply_markup,
            entities=caption_entities
        ))

        update_rate_limit(user_id)
        logger.info(f"[INLINE_PROC] Done. Inline message replaced with audio for user {user_id}")

    except Exception as e:
        logger.exception(f"[INLINE_PROC] Fatal error: {e}")
        if tmp_path:
            cleanup_tmp(tmp_path)
        try:
            await safe_edit_text(client, msg_id, f"Error: {str(e)[:200]}\n{song_name}")
        except Exception as e2:
            logger.warning(f"[INLINE_PROC] Could not send error edit: {e2}")


async def start_bot() -> TelegramClient:
    global _client, _bot_username
    logger.info("[BOT] Creating Telegram client with in-memory StringSession...")
    client = TelegramClient(StringSession(), API_ID, API_HASH)

    logger.info("[BOT] Connecting with bot token...")
    await client.start(bot_token=BOT_TOKEN)
    logger.info("[BOT] Connected successfully!")

    me = await client.get_me()
    _bot_username = me.username or "unknown"
    logger.info(f"[BOT] Bot username: @{_bot_username}, id: {me.id}")

    @client.on(events.NewMessage(pattern="/start"))
    async def start_handler(event):
        user = await event.get_sender()
        name = getattr(user, "first_name", "there")
        logger.info(f"[START] User {user.id} ({name}) used /start")
        await event.reply(
            f"Hi {name}!\n\nUse inline mode: @{_bot_username} song name"
        )
        raise events.StopPropagation

    @client.on(events.InlineQuery)
    async def inline_handler(event):
        query = event.text.strip()
        user_id = event.query.user_id
        logger.info(f"[INLINE] User {user_id} query: '{query}'")

        if not query:
            await event.answer([])
            return

        if is_rate_limited(user_id):
            logger.info(f"[INLINE] User {user_id} rate limited")
            await event.answer([])
            return

        builder = event.builder

        try:
            items = await search_songs(query)
        except Exception as e:
            logger.error(f"[INLINE] Search error: {e}")
            await event.answer([])
            return

        results = []
        for i, item in enumerate(items[:10]):
            title = item.get("name", "Unknown")
            artists = item.get("artists", {})
            primary_artists = artists.get("primary", [])
            artist_names = ", ".join([a.get("name", "") for a in primary_artists])
            album = item.get("album", {})
            album_name = album.get("name", "Unknown")
            duration = item.get("duration", 0)
            image_list = item.get("image", [])
            thumb_url = image_list[0].get("url", "") if image_list else ""
            song_id = item.get("id", "")

            if not song_id:
                continue

            logger.info(f"[INLINE] Building result {i + 1}: {title[:40]}")

            cache_key = str(uuid.uuid4())
            song_cache[cache_key] = item

            try:
                result = await builder.article(
                    id=cache_key,
                    title=title,
                    description=f"{artist_names} | {album_name}",
                    text=f"Preparing...\n{title}",
                    thumb=InputWebDocument(
                        url=thumb_url, size=0, mime_type="image/jpeg",
                        attributes=[DocumentAttributeImageSize(w=226, h=226)],
                    ) if thumb_url else None,
                    link_preview=False,
                    buttons=Button.inline("Download", data=cache_key.encode()[:64]),
                )
                results.append(result)
            except Exception as e:
                logger.warning(f"[INLINE] Skipping result {i}: {e}")

        logger.info(f"[INLINE] Answering with {len(results)} results")
        await event.answer(results, cache_time=30, private=True)

    @client.on(events.Raw(UpdateBotInlineSend))
    async def chosen_inline_handler(update):
        user_id = update.user_id
        result_id = update.id
        msg_id = update.msg_id
        logger.info(f"[CHOSEN] User {user_id} | result_id='{result_id}' | msg_id={msg_id}")

        if msg_id is None:
            logger.error("[CHOSEN] msg_id is None! Enable inline feedback in @BotFather")
            return

        song_data = song_cache.get(result_id)
        if not song_data:
            logger.error(f"[CHOSEN] Song data not found for: {result_id}")
            return

        logger.info(f"[CHOSEN] Launching download for: {song_data.get('name')}")
        asyncio.create_task(process_inline_song(client, msg_id, song_data, user_id))

    logger.info("[BOT] All handlers registered.")
    _client = client
    return client


@asynccontextmanager
async def lifespan(app: FastAPI):
    local_ip = get_local_ip()
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"[API] FastAPI starting on 0.0.0.0:{port}")
    logger.info(f"[API] Actual local IP: {local_ip}:{port}")
    logger.info(f"[API] Loopback access: 127.0.0.1:{port}")

    bot_task = asyncio.create_task(_run_bot())
    yield
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        logger.info("[BOT] Bot task cancelled cleanly.")
    if _client and _client.is_connected():
        await _client.disconnect()
        logger.info("[BOT] Disconnected Telegram client.")


async def _run_bot():
    try:
        client = await start_bot()
        logger.info("[BOT] Running until disconnected...")
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"[BOT] Fatal error in bot task: {e}")


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    global _bot_username
    username = _bot_username or "loading..."
    html_content = (
        "<!DOCTYPE html>"
        "<html lang='en'>"
        "<head>"
        "<meta charset='UTF-8'/>"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'/>"
        "<title>🎵 Music Bot</title>"
        "<link rel='preconnect' href='https://fonts.googleapis.com'/>"
        "<link href='https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=DM+Mono:wght@400;500&display=swap' rel='stylesheet'/>"
        "<style>"
        "*{margin:0;padding:0;box-sizing:border-box}"
        "body{"
        "min-height:100vh;display:flex;align-items:center;justify-content:center;"
        "background:#0a0a0f;"
        "font-family:'Syne',sans-serif;"
        "overflow:hidden;"
        "}"
        ".bg-orb{"
        "position:fixed;border-radius:50%;filter:blur(80px);pointer-events:none;z-index:0;"
        "}"
        ".orb1{width:500px;height:500px;background:radial-gradient(circle,#7c3aed33,transparent);top:-100px;left:-100px;animation:drift1 8s ease-in-out infinite alternate;}"
        ".orb2{width:400px;height:400px;background:radial-gradient(circle,#06b6d433,transparent);bottom:-80px;right:-80px;animation:drift2 10s ease-in-out infinite alternate;}"
        ".orb3{width:300px;height:300px;background:radial-gradient(circle,#f43f5e22,transparent);top:40%;left:40%;animation:drift1 12s ease-in-out infinite alternate-reverse;}"
        "@keyframes drift1{0%{transform:translate(0,0)}100%{transform:translate(40px,30px)}}"
        "@keyframes drift2{0%{transform:translate(0,0)}100%{transform:translate(-30px,-40px)}}"
        ".card{"
        "position:relative;z-index:1;"
        "background:rgba(255,255,255,0.04);"
        "border:1px solid rgba(255,255,255,0.08);"
        "border-radius:24px;"
        "padding:56px 48px;"
        "max-width:480px;width:90%;"
        "backdrop-filter:blur(20px);"
        "-webkit-backdrop-filter:blur(20px);"
        "box-shadow:0 0 0 1px rgba(124,58,237,0.15),0 32px 64px rgba(0,0,0,0.6);"
        "text-align:center;"
        "animation:fadeUp 0.7s cubic-bezier(.16,1,.3,1) both;"
        "}"
        "@keyframes fadeUp{from{opacity:0;transform:translateY(24px)}to{opacity:1;transform:translateY(0)}}"
        ".pulse-ring{"
        "width:80px;height:80px;border-radius:50%;"
        "background:linear-gradient(135deg,#7c3aed,#06b6d4);"
        "display:flex;align-items:center;justify-content:center;"
        "margin:0 auto 32px;"
        "font-size:36px;"
        "position:relative;"
        "animation:iconPop 0.5s 0.3s cubic-bezier(.34,1.56,.64,1) both;"
        "}"
        "@keyframes iconPop{from{opacity:0;transform:scale(0.4)}to{opacity:1;transform:scale(1)}}"
        ".pulse-ring::before{"
        "content:'';"
        "position:absolute;inset:-8px;border-radius:50%;"
        "border:2px solid rgba(124,58,237,0.4);"
        "animation:ringPulse 2s ease-out infinite;"
        "}"
        ".pulse-ring::after{"
        "content:'';"
        "position:absolute;inset:-16px;border-radius:50%;"
        "border:2px solid rgba(124,58,237,0.2);"
        "animation:ringPulse 2s 0.4s ease-out infinite;"
        "}"
        "@keyframes ringPulse{0%{transform:scale(1);opacity:1}100%{transform:scale(1.4);opacity:0}}"
        ".status-badge{"
        "display:inline-flex;align-items:center;gap:8px;"
        "background:rgba(16,185,129,0.12);"
        "border:1px solid rgba(16,185,129,0.3);"
        "color:#34d399;"
        "font-family:'DM Mono',monospace;"
        "font-size:12px;font-weight:500;"
        "padding:6px 14px;border-radius:100px;"
        "margin-bottom:24px;"
        "letter-spacing:0.05em;"
        "}"
        ".dot{width:7px;height:7px;border-radius:50%;background:#34d399;animation:blink 1.4s ease-in-out infinite;}"
        "@keyframes blink{0%,100%{opacity:1}50%{opacity:0.3}}"
        "h1{font-size:28px;font-weight:800;color:#fff;letter-spacing:-0.5px;margin-bottom:8px;}"
        ".username{"
        "font-family:'DM Mono',monospace;"
        "font-size:15px;color:#7c3aed;"
        "background:rgba(124,58,237,0.1);"
        "border:1px solid rgba(124,58,237,0.25);"
        "padding:8px 20px;border-radius:8px;"
        "display:inline-block;margin:16px 0 24px;"
        "letter-spacing:0.02em;"
        "}"
        ".desc{font-size:14px;color:rgba(255,255,255,0.45);line-height:1.7;margin-bottom:32px;}"
        ".btn{"
        "display:inline-flex;align-items:center;gap:10px;"
        "background:linear-gradient(135deg,#7c3aed,#6d28d9);"
        "color:#fff;text-decoration:none;"
        "font-family:'Syne',sans-serif;font-weight:700;font-size:14px;"
        "padding:14px 28px;border-radius:12px;"
        "letter-spacing:0.03em;"
        "transition:transform 0.2s,box-shadow 0.2s;"
        "box-shadow:0 4px 20px rgba(124,58,237,0.4);"
        "}"
        ".btn:hover{transform:translateY(-2px);box-shadow:0 8px 28px rgba(124,58,237,0.55);}"
        ".divider{height:1px;background:rgba(255,255,255,0.06);margin:32px 0;}"
        ".meta{font-family:'DM Mono',monospace;font-size:11px;color:rgba(255,255,255,0.2);letter-spacing:0.04em;}"
        "</style>"
        "</head>"
        "<body>"
        "<div class='bg-orb orb1'></div>"
        "<div class='bg-orb orb2'></div>"
        "<div class='bg-orb orb3'></div>"
        "<div class='card'>"
        "<div class='pulse-ring'>🎵</div>"
        "<div class='status-badge'><span class='dot'></span>ONLINE &amp; RUNNING</div>"
        f"<h1>Music Download Bot</h1>"
        f"<div class='username'>@{username}</div>"
        "<p class='desc'>Inline music bot powered by JioSaavn.<br/>Search any song in any Telegram chat.</p>"
        f"<a class='btn' href='https://t.me/{username}'>Open in Telegram ↗</a>"
        "<div class='divider'></div>"
        "<p class='meta'>TELETHON · FASTAPI · VERCEL</p>"
        "</div>"
        "</body></html>"
    )
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    local_ip = get_local_ip()
    port = int(os.environ.get("PORT", 8000))
    print(f"[SERVER] Binding to 0.0.0.0:{port}")
    print(f"[SERVER] Actual local IP: {local_ip}:{port}")
    print(f"[SERVER] Loopback: 127.0.0.1:{port}")
    uvicorn.run("bot:app", host="0.0.0.0", port=port, reload=False)
