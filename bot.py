import asyncio
import logging
import aiohttp
import io
import uuid
import time
import os
import socket
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import EditInlineBotMessageRequest
from telethon.tl.types import (
    InputMediaUploadedDocument,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    KeyboardButtonUrl,
    KeyboardButtonRow,
    ReplyInlineMarkup,
)
from telethon.extensions import html as tl_html

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
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
TG_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
BOT_USERNAME = "thesmartdevbot"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

user_download_timestamps: dict[int, datetime] = {}
song_cache: dict[str, dict] = {}


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


async def tg_api(session: aiohttp.ClientSession, method: str, payload: dict) -> dict:
    url = f"{TG_API_BASE}/{method}"
    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        data = await resp.json()
        if not data.get("ok"):
            logger.warning(f"[TG_API] {method} failed: {data}")
        return data


async def edit_inline_message_text(session: aiohttp.ClientSession, inline_message_id: str, text: str) -> dict:
    return await tg_api(session, "editMessageText", {
        "inline_message_id": inline_message_id,
        "text": text,
        "parse_mode": "HTML",
    })


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


async def progress_bar(current: int, total: int, start_time: float, last_update: list) -> str | None:
    elapsed_time = time.time() - start_time
    if elapsed_time == 0:
        elapsed_time = 0.1
    percentage = (current / total) * 100
    filled = int(percentage // 5)
    progress = f"{'▓' * filled}{'░' * (20 - filled)}"
    speed = current / elapsed_time / 1024 / 1024
    uploaded_mb = current / 1024 / 1024
    total_mb = total / 1024 / 1024
    if time.time() - last_update[0] < 1:
        return None
    last_update[0] = time.time()
    return (
        f"🎵 <b>Smart Upload Progress Bar ✅</b>\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"{progress}\n"
        f"<b>Percentage:</b> {percentage:.2f}%\n"
        f"<b>Speed:</b> {speed:.2f} MB/s\n"
        f"<b>Status:</b> {uploaded_mb:.2f} MB of {total_mb:.2f} MB\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"<b>Smooth Transfer → Activated ✅</b>"
    )


async def process_chosen_inline(inline_message_id: str, result_id: str, user_id: int) -> None:
    logger.info(f"[PROC] inline_message_id={inline_message_id} result_id={result_id} user={user_id}")

    song_data = song_cache.get(result_id)
    if not song_data:
        logger.error(f"[PROC] Song data not found for result_id={result_id}")
        async with aiohttp.ClientSession() as session:
            await edit_inline_message_text(session, inline_message_id, "❌ Session expired. Search again.")
        return

    song_name = song_data.get("name", "Unknown")
    artists = song_data.get("artists", {})
    primary_artists = artists.get("primary", [])
    artist_names = ", ".join([a.get("name", "") for a in primary_artists])
    album = song_data.get("album", {})
    album_name = album.get("name", "Unknown Album")
    duration = song_data.get("duration", 0)
    download_urls = song_data.get("downloadUrl", [])
    play_count = song_data.get("playCount", 0)
    song_url = song_data.get("url", "")

    tmp_path = None

    async with aiohttp.ClientSession() as session:
        try:
            await edit_inline_message_text(session, inline_message_id, "🔍 Searching The Audio...")

            if is_rate_limited(user_id):
                logger.info(f"[PROC] User {user_id} rate limited")
                await edit_inline_message_text(
                    session, inline_message_id,
                    f"⏳ Rate limited. Wait {RATE_LIMIT_WINDOW_MINUTES} minute(s)."
                )
                return

            if not download_urls:
                await edit_inline_message_text(session, inline_message_id, f"❌ No download URLs.\n{song_name}")
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
                await edit_inline_message_text(session, inline_message_id, f"❌ No suitable URL.\n{song_name}")
                return

            await edit_inline_message_text(session, inline_message_id, "Found ☑️ Downloading...")
            logger.info(f"[PROC] Downloading {best_quality} for {song_name}")

        except Exception as e:
            logger.exception(f"[PROC] Pre-download phase error: {e}")
            return

    try:
        tmp_path = await download_song_to_tmp(best_url, best_quality)
        size_bytes = os.path.getsize(tmp_path)
        size_mb = size_bytes / 1024 / 1024
        logger.info(f"[PROC] Downloaded {size_mb:.1f} MB")

        if size_bytes > MAX_TG_FILE_SIZE:
            async with aiohttp.ClientSession() as session:
                await edit_inline_message_text(
                    session, inline_message_id,
                    f"❌ File too large ({size_mb:.1f} MB). Telegram limit is 50 MB.\n{song_name}"
                )
            cleanup_tmp(tmp_path)
            return

        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.start(bot_token=BOT_TOKEN)
        logger.info("[PROC] Telethon client started for MTProto upload")

        try:
            with open(tmp_path, "rb") as f:
                song_bytes = f.read()

            cleanup_tmp(tmp_path)
            tmp_path = None

            buf = io.BytesIO(song_bytes)
            buf.name = "song.mp3"

            start_time = time.time()
            last_update = [time.time()]

            async def progress_callback(current, total):
                bar_text = await progress_bar(current, total, start_time, last_update)
                if bar_text:
                    try:
                        async with aiohttp.ClientSession() as s:
                            await edit_inline_message_text(s, inline_message_id, bar_text)
                    except Exception as pe:
                        logger.warning(f"[PROGRESS] Update failed: {pe}")

            uploaded = await client.upload_file(
                buf,
                file_name="song.mp3",
                file_size=size_bytes,
                progress_callback=progress_callback,
            )
            logger.info("[PROC] File uploaded via MTProto")

            user_info = f"<a href='tg://user?id={user_id}'>User {user_id}</a>"
            caption_html = (
                f"🎵 <b>Title:</b> <code>{song_name}</code>\n"
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

            caption_text, caption_entities = tl_html.parse(caption_html)

            media = InputMediaUploadedDocument(
                file=uploaded,
                mime_type="audio/mpeg",
                attributes=[
                    DocumentAttributeAudio(duration=duration, title=song_name, performer=artist_names),
                    DocumentAttributeFilename(file_name="song.mp3"),
                ],
            )

            button = KeyboardButtonUrl("📢 Join Channel", CHANNEL_URL)
            row = KeyboardButtonRow(buttons=[button])
            reply_markup = ReplyInlineMarkup(rows=[row])

            await client(EditInlineBotMessageRequest(
                id=inline_message_id,
                message=caption_text,
                media=media,
                reply_markup=reply_markup,
                entities=caption_entities,
            ))

            update_rate_limit(user_id)
            logger.info(f"[PROC] Done — audio sent for user {user_id}")

        finally:
            await client.disconnect()
            logger.info("[PROC] Telethon client disconnected")

    except Exception as e:
        logger.exception(f"[PROC] Fatal error: {e}")
        if tmp_path:
            cleanup_tmp(tmp_path)
        try:
            async with aiohttp.ClientSession() as session:
                await edit_inline_message_text(
                    session, inline_message_id,
                    f"❌ Error: {str(e)[:200]}\n{song_name}"
                )
        except Exception as e2:
            logger.warning(f"[PROC] Could not send error edit: {e2}")


async def handle_inline_query(update: dict) -> None:
    iq = update["inline_query"]
    inline_query_id = iq["id"]
    query = iq.get("query", "").strip()
    user_id = iq["from"]["id"]

    logger.info(f"[INLINE] User {user_id} query: '{query}'")

    if not query:
        async with aiohttp.ClientSession() as session:
            await tg_api(session, "answerInlineQuery", {
                "inline_query_id": inline_query_id,
                "results": [],
                "cache_time": 0,
            })
        return

    if is_rate_limited(user_id):
        logger.info(f"[INLINE] User {user_id} rate limited")
        async with aiohttp.ClientSession() as session:
            await tg_api(session, "answerInlineQuery", {
                "inline_query_id": inline_query_id,
                "results": [],
                "cache_time": 0,
            })
        return

    try:
        items = await search_songs(query)
    except Exception as e:
        logger.error(f"[INLINE] Search error: {e}")
        async with aiohttp.ClientSession() as session:
            await tg_api(session, "answerInlineQuery", {
                "inline_query_id": inline_query_id,
                "results": [],
                "cache_time": 0,
            })
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

        cache_key = str(uuid.uuid4())
        song_cache[cache_key] = item

        logger.info(f"[INLINE] Result {i + 1}: {title[:40]} -> cache_key={cache_key}")

        result: dict = {
            "type": "article",
            "id": cache_key,
            "title": title,
            "description": f"{artist_names} | {album_name} | {duration}s",
            "input_message_content": {
                "message_text": f"⏳ Preparing: {title}",
            },
        }

        if thumb_url:
            result["thumbnail_url"] = thumb_url
            result["thumbnail_width"] = 226
            result["thumbnail_height"] = 226

        results.append(result)

    logger.info(f"[INLINE] Answering with {len(results)} results")
    async with aiohttp.ClientSession() as session:
        await tg_api(session, "answerInlineQuery", {
            "inline_query_id": inline_query_id,
            "results": results,
            "cache_time": 30,
            "is_personal": True,
        })


async def handle_chosen_inline_result(update: dict) -> None:
    cir = update["chosen_inline_result"]
    result_id = cir["result_id"]
    user_id = cir["from"]["id"]
    inline_message_id = cir.get("inline_message_id")

    logger.info(f"[CHOSEN] user={user_id} result_id={result_id} inline_message_id={inline_message_id}")

    if not inline_message_id:
        logger.error("[CHOSEN] inline_message_id is None — enable inline feedback in @BotFather (100%)")
        return

    asyncio.create_task(process_chosen_inline(inline_message_id, result_id, user_id))


async def handle_message(update: dict) -> None:
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")
    first_name = message.get("from", {}).get("first_name", "there")
    user_id = message.get("from", {}).get("id")

    logger.info(f"[MSG] user={user_id} text='{text}'")

    if text.startswith("/start"):
        reply = (
            f"Hi {first_name}!\n\n"
            f"Use inline mode: @{BOT_USERNAME} song name\n\n"
            f"Example: @{BOT_USERNAME} Kesariya"
        )
        async with aiohttp.ClientSession() as session:
            await tg_api(session, "sendMessage", {
                "chat_id": chat_id,
                "text": reply,
            })


@asynccontextmanager
async def lifespan(app: FastAPI):
    local_ip = get_local_ip()
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"[API] FastAPI starting — bound to 0.0.0.0:{port}")
    logger.info(f"[API] Actual local IP: {local_ip}:{port}")
    logger.info(f"[API] Loopback: 127.0.0.1:{port}")
    yield
    logger.info("[API] Shutting down.")


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
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
        "body{min-height:100vh;display:flex;align-items:center;justify-content:center;"
        "background:#0a0a0f;font-family:'Syne',sans-serif;overflow:hidden;}"
        ".bg-orb{position:fixed;border-radius:50%;filter:blur(80px);pointer-events:none;z-index:0;}"
        ".orb1{width:500px;height:500px;background:radial-gradient(circle,#7c3aed33,transparent);"
        "top:-100px;left:-100px;animation:drift1 8s ease-in-out infinite alternate;}"
        ".orb2{width:400px;height:400px;background:radial-gradient(circle,#06b6d433,transparent);"
        "bottom:-80px;right:-80px;animation:drift2 10s ease-in-out infinite alternate;}"
        ".orb3{width:300px;height:300px;background:radial-gradient(circle,#f43f5e22,transparent);"
        "top:40%;left:40%;animation:drift1 12s ease-in-out infinite alternate-reverse;}"
        "@keyframes drift1{0%{transform:translate(0,0)}100%{transform:translate(40px,30px)}}"
        "@keyframes drift2{0%{transform:translate(0,0)}100%{transform:translate(-30px,-40px)}}"
        ".card{position:relative;z-index:1;background:rgba(255,255,255,0.04);"
        "border:1px solid rgba(255,255,255,0.08);border-radius:24px;padding:56px 48px;"
        "max-width:480px;width:90%;backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);"
        "box-shadow:0 0 0 1px rgba(124,58,237,0.15),0 32px 64px rgba(0,0,0,0.6);"
        "text-align:center;animation:fadeUp 0.7s cubic-bezier(.16,1,.3,1) both;}"
        "@keyframes fadeUp{from{opacity:0;transform:translateY(24px)}to{opacity:1;transform:translateY(0)}}"
        ".pulse-ring{width:80px;height:80px;border-radius:50%;"
        "background:linear-gradient(135deg,#7c3aed,#06b6d4);"
        "display:flex;align-items:center;justify-content:center;margin:0 auto 32px;font-size:36px;"
        "position:relative;animation:iconPop 0.5s 0.3s cubic-bezier(.34,1.56,.64,1) both;}"
        "@keyframes iconPop{from{opacity:0;transform:scale(0.4)}to{opacity:1;transform:scale(1)}}"
        ".pulse-ring::before{content:'';position:absolute;inset:-8px;border-radius:50%;"
        "border:2px solid rgba(124,58,237,0.4);animation:ringPulse 2s ease-out infinite;}"
        ".pulse-ring::after{content:'';position:absolute;inset:-16px;border-radius:50%;"
        "border:2px solid rgba(124,58,237,0.2);animation:ringPulse 2s 0.4s ease-out infinite;}"
        "@keyframes ringPulse{0%{transform:scale(1);opacity:1}100%{transform:scale(1.4);opacity:0}}"
        ".status-badge{display:inline-flex;align-items:center;gap:8px;"
        "background:rgba(16,185,129,0.12);border:1px solid rgba(16,185,129,0.3);"
        "color:#34d399;font-family:'DM Mono',monospace;font-size:12px;font-weight:500;"
        "padding:6px 14px;border-radius:100px;margin-bottom:24px;letter-spacing:0.05em;}"
        ".dot{width:7px;height:7px;border-radius:50%;background:#34d399;"
        "animation:blink 1.4s ease-in-out infinite;}"
        "@keyframes blink{0%,100%{opacity:1}50%{opacity:0.3}}"
        "h1{font-size:28px;font-weight:800;color:#fff;letter-spacing:-0.5px;margin-bottom:8px;}"
        ".username{font-family:'DM Mono',monospace;font-size:15px;color:#7c3aed;"
        "background:rgba(124,58,237,0.1);border:1px solid rgba(124,58,237,0.25);"
        "padding:8px 20px;border-radius:8px;display:inline-block;margin:16px 0 24px;"
        "letter-spacing:0.02em;}"
        ".desc{font-size:14px;color:rgba(255,255,255,0.45);line-height:1.7;margin-bottom:32px;}"
        ".btn{display:inline-flex;align-items:center;gap:10px;"
        "background:linear-gradient(135deg,#7c3aed,#6d28d9);color:#fff;text-decoration:none;"
        "font-family:'Syne',sans-serif;font-weight:700;font-size:14px;"
        "padding:14px 28px;border-radius:12px;letter-spacing:0.03em;"
        "transition:transform 0.2s,box-shadow 0.2s;box-shadow:0 4px 20px rgba(124,58,237,0.4);}"
        ".btn:hover{transform:translateY(-2px);box-shadow:0 8px 28px rgba(124,58,237,0.55);}"
        ".divider{height:1px;background:rgba(255,255,255,0.06);margin:32px 0;}"
        ".meta{font-family:'DM Mono',monospace;font-size:11px;color:rgba(255,255,255,0.2);"
        "letter-spacing:0.04em;}"
        "</style>"
        "</head>"
        "<body>"
        "<div class='bg-orb orb1'></div>"
        "<div class='bg-orb orb2'></div>"
        "<div class='bg-orb orb3'></div>"
        "<div class='card'>"
        "<div class='pulse-ring'>🎵</div>"
        "<div class='status-badge'><span class='dot'></span>ONLINE &amp; RUNNING</div>"
        "<h1>Music Download Bot</h1>"
        f"<div class='username'>@{BOT_USERNAME}</div>"
        "<p class='desc'>Inline music bot powered by JioSaavn.<br/>Search any song in any Telegram chat.</p>"
        f"<a class='btn' href='https://t.me/{BOT_USERNAME}'>Open in Telegram ↗</a>"
        "<div class='divider'></div>"
        "<p class='meta'>TELETHON · FASTAPI · VERCEL</p>"
        "</div>"
        "</body></html>"
    )
    return HTMLResponse(content=html_content)


@app.post("/webhook")
async def webhook(request: Request):
    try:
        update = await request.json()
    except Exception as e:
        logger.error(f"[WEBHOOK] Failed to parse JSON: {e}")
        return Response(status_code=400)

    logger.info(f"[WEBHOOK] Update keys: {list(update.keys())}")

    if "inline_query" in update:
        asyncio.create_task(handle_inline_query(update))

    elif "chosen_inline_result" in update:
        asyncio.create_task(handle_chosen_inline_result(update))

    elif "message" in update:
        asyncio.create_task(handle_message(update))

    return Response(status_code=200)


@app.get("/set_webhook")
async def set_webhook(request: Request):
    host = request.headers.get("host", "")
    webhook_url = f"https://{host}/webhook"
    logger.info(f"[SETUP] Setting webhook to: {webhook_url}")
    async with aiohttp.ClientSession() as session:
        result = await tg_api(session, "setWebhook", {
            "url": webhook_url,
            "allowed_updates": ["message", "inline_query", "chosen_inline_result"],
            "drop_pending_updates": True,
        })
    return JSONResponse(content=result)


@app.get("/webhook_info")
async def webhook_info():
    async with aiohttp.ClientSession() as session:
        result = await tg_api(session, "getWebhookInfo", {})
    return JSONResponse(content=result)


@app.get("/delete_webhook")
async def delete_webhook():
    async with aiohttp.ClientSession() as session:
        result = await tg_api(session, "deleteWebhook", {"drop_pending_updates": True})
    return JSONResponse(content=result)


if __name__ == "__main__":
    local_ip = get_local_ip()
    port = int(os.environ.get("PORT", 8000))
    print(f"[SERVER] Binding to 0.0.0.0:{port}")
    print(f"[SERVER] Actual local IP: {local_ip}:{port}")
    print(f"[SERVER] Loopback: 127.0.0.1:{port}")
    uvicorn.run("bot:app", host="0.0.0.0", port=port, reload=False)
