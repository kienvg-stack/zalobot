"""Zalo bot trả lời trong nhóm chat bằng Gemini (key free từ Google AI Studio).

Chạy: python bot.py  (đọc cấu hình từ file .env)
Nhận tin bằng long-polling (getUpdates). Riêng trả lời bằng giọng nói cần PUBLIC_URL:
bot tự mở web server nhỏ (cổng PORT) để Zalo tải file .aac về.
"""

import functools
import os
import random
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

load_dotenv()

ZALO_TOKEN = os.environ["ZALO_BOT_TOKEN"]
ZALO_API = f"https://bot-api.zaloplatforms.com/bot{ZALO_TOKEN}"
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
BOT_NAME = os.getenv("BOT_NAME", "KienVG")
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "50"))
ENABLE_WEB_SEARCH = os.getenv("ENABLE_WEB_SEARCH", "1") == "1"
TTS_MODEL = os.getenv("TTS_MODEL", "gemini-3.8-flash-lite-tts")
TTS_VOICE = os.getenv("TTS_VOICE", "Kore")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # vd http://zalobot-1-2-3-4.traefik.me
PORT = int(os.getenv("PORT", "8080"))
ENABLE_STICKERS = os.getenv("ENABLE_STICKERS", "1") == "1"  # gửi kèm sticker hợp ngữ cảnh

SYSTEM_PROMPT = f"""Bạn là {BOT_NAME}, một trợ lý AI trong nhóm chat Zalo.
- Trả lời bằng ngôn ngữ người hỏi dùng (mặc định tiếng Việt), thân thiện, dễ thương, ngắn gọn, đi thẳng vào ý.
- Tin nhắn người dùng có dạng "Tên nói: nội dung" để bạn biết ai đang nói. Gọi người hỏi bằng tên bình thường (vd "Ryan ơi"), không bao giờ đặt tên trong ngoặc vuông [ ], và không mở đầu câu trả lời bằng "Tên nói:".
- Mỗi tin có kèm thời gian hiện tại (giờ Việt Nam); dùng nó khi được hỏi ngày giờ.
- Zalo hiển thị markdown hạn chế: dùng **in đậm** và gạch đầu dòng đơn giản, tránh bảng và heading.
- Câu hỏi cần thông tin mới (tin tức, giá cả, thời tiết...) thì dùng Google Search nếu có.
- Khi hợp ngữ cảnh (chào hỏi, khen, an ủi, đùa vui, cảm ơn...), thêm ĐÚNG MỘT thẻ sticker ở cuối câu trả lời, dạng [[sticker:cảm_xúc]].
  Cảm xúc chọn trong: vui, yeu, chill, buon, bat_ngo, xui, hoang, tu_tin, buc_minh, met, suy_nghi, tien, da.
  Câu trả lời nghiêm túc, dài hoặc mang tính thông tin thuần thì không cần sticker."""

VN_TZ = timezone(timedelta(hours=7))
WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# Lịch sử hội thoại theo từng chat_id (giữ trong RAM, mất khi restart)
histories: dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY_TURNS * 2))
chat_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


# ---------- Zalo API ----------

def zalo(method: str, **params):
    r = requests.post(f"{ZALO_API}/{method}", json=params, timeout=60)
    r.raise_for_status()
    return r.json()


def send_text(chat_id: str, text: str):
    # Zalo giới hạn 2000 ký tự / tin -> chia nhỏ theo dòng
    for chunk in split_text(text, 1900):
        try:
            zalo("sendMessage", chat_id=chat_id, text=chunk, parse_mode="markdown")
        except requests.HTTPError:
            zalo("sendMessage", chat_id=chat_id, text=chunk)


def split_text(text: str, limit: int):
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        chunks.append(cur)
    return chunks or [text[:limit]]


def typing(chat_id: str):
    try:
        zalo("sendChatAction", chat_id=chat_id, action="typing")
    except Exception:
        pass


# ---------- Sticker ----------

# Bộ "Zapy Công Sở" (linh vật Zalo), mã lấy từ stickers.zaloapp.com/oa/detail?cid=743df4e6c8a321fd78b2
# Gemini chọn cảm xúc hợp ngữ cảnh bằng thẻ [[sticker:<cảm xúc>]] ở cuối câu trả lời.
STICKERS = {
    "vui": ["a643836dbf2856760f39", "4fa37c8d40c8a996f0d9"],       # giơ ngón cái, phấn khích
    "yeu": ["d063f44dc80821567819"],                               # ôm tim
    "chill": ["7722510c6d498417dd58", "fd81daafe6ea0fb456fb"],     # uống trà sữa, thiền
    "buon": ["7eb25e9c62d98b87d2c8", "0d1e3f300375ea2bb364"],      # khóc RIP, mếu
    "bat_ngo": ["e136c018fc5d15034c4c", "98ffa4d1989471ca2885", "7b31461f7a5a9304ca4b"],  # Ủa, sét đánh, xịt keo
    "xui": ["611743397f7c9622cf6d"],                               # xu cà na
    "hoang": ["095c2a721637ff69a626", "8fdbb4f588b061ee38a1"],     # SOS, chạy đi
    "tu_tin": ["34900abe36fbdfa586ea"],                            # sợ à?
    "buc_minh": ["64985bb667f38eadd7e2", "c468fd46c103285d7112"],  # khó chịu, nổi điên
    "met": ["c91bf135cd70242e7d61", "5d62694c5509bc57e518", "528a63a45fe1b6bfeff0"],  # drama, thức khuya, truyền nước
    "suy_nghi": ["ae05942ba86e4130187f", "8430b11e8d5b64053d4a"],  # có learn, ngẫm
    "tien": ["a4689246ae03475d1e12", "0fcd38e304a6edf8b4b7"],      # cơm áo gạo tiền, giàu
    "da": ["d1b2e19cddd934876dc8"],                                # dạ (lễ phép, nhận lời)
}
ALL_STICKERS = [s for ids in STICKERS.values() for s in ids]
STICKER_TAG_RE = re.compile(r"\[\[\s*sticker\s*:\s*([a-z_]+)\s*\]\]", re.IGNORECASE)


def pop_sticker_tag(answer: str) -> tuple[str, str | None]:
    """Tách thẻ [[sticker:...]] khỏi câu trả lời, trả về (câu trả lời, mã sticker hoặc None)."""
    m = STICKER_TAG_RE.search(answer)
    sticker = random.choice(STICKERS[m.group(1).lower()]) if m and m.group(1).lower() in STICKERS else None
    return STICKER_TAG_RE.sub("", answer).strip(), sticker


def send_sticker(chat_id: str, sticker: str | None):
    if not (ENABLE_STICKERS and sticker):
        return
    try:
        zalo("sendSticker", chat_id=chat_id, sticker=sticker)
        print(f"[gửi sticker] {chat_id}: {sticker}", flush=True)
    except Exception as e:
        print(f"[sticker lỗi] {e}", flush=True)


# ---------- Giọng nói ----------

VOICE_DIR = Path(tempfile.gettempdir()) / "zalo-voice"
VOICE_DIR.mkdir(exist_ok=True)


def start_voice_server():
    # Web server nhỏ phục vụ file .aac cho Zalo tải về (chỉ cần khi có PUBLIC_URL)
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(QuietHandler, directory=str(VOICE_DIR))
    server = ThreadingHTTPServer(("0.0.0.0", PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Voice server: cổng {PORT} -> {PUBLIC_URL}", flush=True)


def text_to_voice_url(text: str) -> str:
    """Gemini TTS -> ffmpeg ra .aac -> trả về link công khai."""
    for f in VOICE_DIR.glob("*.aac"):  # dọn file cũ hơn 1 giờ
        if time.time() - f.stat().st_mtime > 3600:
            f.unlink(missing_ok=True)

    r = client.models.generate_content(
        model=TTS_MODEL,
        contents=text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=TTS_VOICE))
            ),
        ),
    )
    audio = r.candidates[0].content.parts[0].inline_data
    # Gemini trả về WAV hoặc PCM thô (audio/L16;rate=24000)
    if "wav" in audio.mime_type:
        src_args = ["-f", "wav"]
    else:
        rate = re.search(r"rate=(\d+)", audio.mime_type)
        src_args = ["-f", "s16le", "-ar", rate.group(1) if rate else "24000", "-ac", "1"]
    name = f"{uuid.uuid4().hex}.aac"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", *src_args, "-i", "pipe:0", "-c:a", "aac", "-b:a", "64k",
         str(VOICE_DIR / name)],
        input=audio.data, check=True,
    )
    return f"{PUBLIC_URL}/{name}"


def download_audio(url: str) -> types.Part:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    mime = r.headers.get("Content-Type", "audio/aac").split(";")[0]
    if not mime.startswith("audio/"):
        mime = "audio/aac"
    return types.Part.from_bytes(data=r.content, mime_type=mime)


# ---------- Gemini ----------

def generate(contents, use_search: bool):
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(google_search=types.GoogleSearch())] if use_search else None,
    )
    return client.models.generate_content(model=MODEL, contents=contents, config=config)


def download_image(url: str) -> types.Part:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    return types.Part.from_bytes(data=r.content, mime_type=mime)


def clean_answer(answer: str, sender: str) -> str:
    answer = answer.strip()
    answer = re.sub(rf"^\[?{re.escape(sender)}\]?\s*(nói)?\s*:\s*", "", answer)  # "Tên nói:" ở đầu
    # Bỏ ngoặc vuông quanh tên: "[Gia Kiện - Ryan] ơi" -> "Gia Kiện - Ryan ơi" (giữ link markdown [x](url))
    return re.sub(r"\[([^\[\]\n]{1,60})\](?!\()", r"\1", answer).strip()


VOICE_STYLE = ("(Hệ thống sẽ TỰ ĐỘNG đọc câu trả lời của bạn thành tin nhắn thoại gửi cho người dùng, "
               "nên bạn hoàn toàn trả lời bằng giọng nói được: đừng nói mình không đọc/nói được, cứ trả lời thẳng "
               "nội dung được yêu cầu (vd được bảo kể chuyện cười thì kể luôn). Viết như đang nói chuyện, tự nhiên, "
               "không markdown, không emoji, không gạch đầu dòng, không thẻ sticker, tối đa khoảng 80 từ.)")


def ask_gemini(chat_id: str, sender: str, text: str, image_url: str | None = None,
               audio_url: str | None = None, for_voice: bool = False) -> tuple[str, str | None]:
    """Trả về (câu trả lời, mã sticker hợp ngữ cảnh hoặc None)."""
    history = histories[chat_id]
    # Lịch sử chỉ lưu chữ (không lưu ảnh/âm thanh) cho gọn
    note = " (kèm 1 ảnh)" if image_url else " (gửi tin nhắn thoại)" if audio_url else ""
    user_turn = {"role": "user", "parts": [{"text": f"{sender}{note} nói: {text}"}]}
    now = datetime.now(VN_TZ)
    now_text = f"(Bây giờ là {WEEKDAYS[now.weekday()]}, {now:%d/%m/%Y %H:%M})"
    parts = [download_image(image_url)] if image_url else []
    if audio_url:
        parts.append(download_audio(audio_url))
    extra = f"\n{VOICE_STYLE}" if for_voice else ""
    parts.append(types.Part.from_text(text=f"{now_text}{extra}\n{sender} nói: {text}"))
    contents = list(history) + [types.Content(role="user", parts=parts)]

    try:
        response = generate(contents, ENABLE_WEB_SEARCH)
    except errors.ClientError:
        if not ENABLE_WEB_SEARCH:
            raise
        # Gói free có thể hết lượt Google Search -> thử lại không search
        response = generate(contents, False)

    body, sticker = pop_sticker_tag(response.text or "")
    answer = clean_answer(body, sender)
    if not answer:
        return "(Mình chưa có câu trả lời, bạn hỏi lại giúp mình nhé.)", None

    history.append(user_turn)
    history.append({"role": "model", "parts": [{"text": answer}]})
    return answer, sticker


# ---------- Xử lý tin nhắn ----------

MENTION_RE = re.compile(rf"@(?:Bot\s+)?{re.escape(BOT_NAME)}\b", re.IGNORECASE)


IMAGE_PROMPT = "Đọc và giải thích nội dung ảnh này."
IMAGE_TTL = 300  # ảnh gửi không kèm câu hỏi được giữ 5 phút chờ người tag bot hỏi tiếp

ASKS_ABOUT_IMAGE_RE = re.compile(r"\b(ảnh|hình|hinh|anh)\b.{0,15}\b(trên|tren|này|nay|đó|do|vừa)\b", re.IGNORECASE)
IMAGE_HOWTO = (
    f"Trong nhóm mình chỉ thấy được tin có tag mình, nên chưa thấy ảnh đó 😅\n"
    f"Bạn gửi lại ảnh và **ghi chú thích có tag @{BOT_NAME}** (vd: \"@{BOT_NAME} đọc ảnh này\"), "
    f"hoặc gửi ảnh vào chat riêng với mình nhé."
)

# Ảnh gần nhất của mỗi chat: chat_id -> (photo_url, thời điểm nhận)
recent_images: dict[str, tuple[str, float]] = {}

# "gửi nhóm" trong chat riêng: đăng câu trả lời của bot vào nhóm
SEND_GROUP_RE = re.compile(r"g[ửu]i\s*nh[óo]m", re.IGNORECASE)
DEFAULT_GROUP_ID = os.getenv("DEFAULT_GROUP_ID", "")
user_groups: dict[str, str] = {}  # user_id -> nhóm gần nhất người đó tag bot
last_private: dict[str, tuple[str, str]] = {}  # user_id -> (câu hỏi, câu trả lời) về ảnh đang chờ "gửi nhóm"
YES_WORDS = {"1", "có", "co", "ok", "gửi", "gui", "yes", "y"}
NO_WORDS = {"2", "không", "khong", "ko", "k", "no", "thôi", "thoi"}
SEND_GROUP_PROMPT = "👉 Gửi câu trả lời này vào nhóm?\n**1** - Gửi nhóm\n**2** - Không"


def post_to_group(user_id: str, sender: str, question: str, answer: str) -> str:
    group_id = user_groups.get(user_id) or DEFAULT_GROUP_ID
    if not group_id:
        return f"Mình chưa biết gửi vào nhóm nào. Bạn vào nhóm tag @{BOT_NAME} 1 lần rồi thử lại nhé."
    send_text(group_id, f"📷 **{sender}** hỏi: {question}\n\n{answer}")
    print(f"[gửi nhóm] {user_id} -> {group_id}", flush=True)
    return "Đã gửi câu trả lời vào nhóm ✅"


VOICE_RE = re.compile(
    r"voice|giọng nói|giong noi|bằng giọng|bang giong|tin nhắn thoại|ghi âm|(nói|đọc)( cho)?( tui| tôi| mình| em| anh)? nghe",
    re.IGNORECASE,
)
VOICE_PROMPT = "Hãy nghe tin nhắn thoại đính kèm và trả lời."


def handle_sticker(chat_id: str, is_group: bool, msg: dict):
    code = msg.get("sticker")
    if not code:
        return
    print(f"[sticker] {code} {msg.get('url', '')}", flush=True)
    if not is_group:  # gửi sticker cho bot thì bot "thả" lại 1 sticker vui
        send_sticker(chat_id, random.choice(STICKERS["vui"] + STICKERS["yeu"]))


def handle_update(update: dict):
    print(f"[nhận] {update}", flush=True)
    event = update.get("event_name")
    if event not in ("message.text.received", "message.image.received",
                     "message.sticker.received", "message.voice.received"):
        return
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    sender = (msg.get("from") or {}).get("display_name", "Ai đó")
    user_id = (msg.get("from") or {}).get("id", "")
    if not chat_id or (msg.get("from") or {}).get("is_bot"):
        return
    is_group = chat.get("chat_type") == "GROUP"
    if is_group and user_id:
        user_groups[user_id] = chat_id

    if event == "message.sticker.received":
        handle_sticker(chat_id, is_group, msg)
        return

    image_url = audio_url = None
    to_group = False
    if event == "message.voice.received":
        audio_url = msg.get("voice_url")
        if not audio_url:
            return
        text = VOICE_PROMPT
    elif event == "message.image.received":
        image_url = msg.get("photo_url")
        raw = (msg.get("caption") or "").strip()
        if not image_url:
            return
        # Ảnh trong nhóm không tag bot: giữ lại, chờ người tag bot hỏi về nó
        if chat.get("chat_type") == "GROUP" and not MENTION_RE.search(raw):
            recent_images[chat_id] = (image_url, time.time())
            return
        text = MENTION_RE.sub("", raw).strip() or IMAGE_PROMPT
    else:
        raw = (msg.get("text") or "").strip()
        if not raw:
            return
        text = MENTION_RE.sub("", raw).strip() or "Xin chào"
        pending = recent_images.pop(chat_id, None)
        if pending and time.time() - pending[1] < IMAGE_TTL:
            image_url = pending[0]
        elif chat.get("chat_type") == "GROUP" and ASKS_ABOUT_IMAGE_RE.search(text):
            # Zalo không chuyển ảnh không tag bot trong nhóm -> hướng dẫn cách gửi
            send_text(chat_id, IMAGE_HOWTO)
            return

    if text.lower() in ("/reset", "/clear"):
        histories.pop(chat_id, None)
        recent_images.pop(chat_id, None)
        send_text(chat_id, "Đã xoá trí nhớ cuộc trò chuyện.")
        return

    # Zalo Bot API không có nút bấm -> trả lời nhanh bằng 1 / 2 sau khi bot đọc ảnh
    if not is_group and user_id in last_private:
        choice = text.lower().strip(" .!")
        if choice in YES_WORDS:
            text = "gửi nhóm"
        elif choice in NO_WORDS:
            last_private.pop(user_id)
            send_text(chat_id, "Ok, không gửi 👍")
            return

    if not is_group and SEND_GROUP_RE.search(text):
        rest = SEND_GROUP_RE.sub("", text).strip(" .,:-")
        if not rest and not image_url:
            # Chỉ nhắn "gửi nhóm": đăng câu trả lời gần nhất trong chat riêng
            if user_id not in last_private:
                send_text(chat_id, "Chưa có câu trả lời nào để gửi. Gửi ảnh/câu hỏi cho mình trước nhé.")
            else:
                send_text(chat_id, post_to_group(user_id, sender, *last_private.pop(user_id)))
            return
        text = rest or IMAGE_PROMPT
        to_group = True

    # Chỉ trả lời bằng giọng nói khi người dùng yêu cầu ("voice", "nói cho tui nghe"...); bình thường trả lời chữ.
    # Zalo chỉ cho bot gửi voice trong chat riêng, và cần PUBLIC_URL để Zalo tải file.
    wants_voice = bool(VOICE_RE.search(text)) and not to_group
    use_voice = wants_voice and not is_group and bool(PUBLIC_URL)

    with chat_locks[chat_id]:  # mỗi nhóm trả lời lần lượt, các nhóm khác chạy song song
        typing(chat_id)
        sticker = None
        try:
            answer, sticker = ask_gemini(chat_id, sender, text, image_url, audio_url, for_voice=use_voice)
        except errors.ClientError as e:
            print(f"[Gemini lỗi {e.code}] {e.message}")
            if e.code == 429:
                answer = "Hết lượt hỏi miễn phí rồi, thử lại sau ít phút (hoặc mai) nhé."
            else:
                answer = "Có lỗi khi gọi Gemini, thử lại sau nhé."
        except errors.ServerError as e:
            print(f"[Gemini lỗi {e.code}] {e.message}")
            answer = "Gemini đang quá tải, thử lại sau nhé."
        except Exception as e:
            print(f"[Lỗi] {e!r}")
            answer = "Có lỗi xảy ra, thử lại sau nhé."
        if to_group:
            send_text(chat_id, post_to_group(user_id, sender, text, answer))
        elif use_voice:
            try:
                zalo("sendVoice", chat_id=chat_id, voice_url=text_to_voice_url(answer))
                print(f"[gửi voice] {chat_id}", flush=True)
            except Exception as e:
                print(f"[voice lỗi] {e!r}", flush=True)
                send_text(chat_id, answer)  # không tạo/gửi được voice thì trả lời bằng chữ
        else:
            if wants_voice and is_group:
                answer += "\n\n_(Zalo chỉ cho bot gửi tin nhắn thoại trong chat riêng nha)_"
            send_text(chat_id, answer)
            send_sticker(chat_id, sticker)
            if not is_group and image_url:
                last_private[user_id] = (text, answer)
                send_text(chat_id, SEND_GROUP_PROMPT)
            elif not is_group:
                last_private.pop(user_id, None)  # hỏi câu khác thì bỏ lời mời gửi nhóm cũ
        print(f"[đã trả lời] {chat_id}: {answer[:80]!r}", flush=True)


def main():
    me = zalo("getMe")
    print(f"Bot đang chạy: {me.get('result')}  | model={MODEL} | {len(ALL_STICKERS)} sticker", flush=True)
    if PUBLIC_URL:
        start_voice_server()
    try:
        zalo("deleteWebhook")  # getUpdates không chạy được khi đang có webhook
    except Exception:
        pass

    while True:
        try:
            data = zalo("getUpdates", timeout="30")
        except requests.RequestException as e:
            print(f"[getUpdates lỗi] {e}")
            time.sleep(3)
            continue
        if not data.get("ok"):
            continue
        result = data.get("result")
        updates = result if isinstance(result, list) else [result] if result else []
        for u in updates:
            threading.Thread(target=handle_update, args=(u,), daemon=True).start()


if __name__ == "__main__":
    main()
