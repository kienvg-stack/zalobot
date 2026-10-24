"""Zalo bot trả lời trong nhóm chat bằng Gemini (key free từ Google AI Studio).

Chạy: python bot.py  (đọc cấu hình từ file .env)
Dùng long-polling (getUpdates) nên không cần server/public URL.
"""

import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

load_dotenv()

ZALO_TOKEN = os.environ["ZALO_BOT_TOKEN"]
ZALO_API = f"https://bot-api.zaloplatforms.com/bot{ZALO_TOKEN}"
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
BOT_NAME = os.getenv("BOT_NAME", "KienVG")
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "20"))
ENABLE_WEB_SEARCH = os.getenv("ENABLE_WEB_SEARCH", "1") == "1"

SYSTEM_PROMPT = f"""Bạn là {BOT_NAME}, một trợ lý AI trong nhóm chat Zalo.
- Trả lời bằng ngôn ngữ người hỏi dùng (mặc định tiếng Việt), thân thiện, ngắn gọn, đi thẳng vào ý.
- Tin nhắn người dùng có dạng "[Tên]: nội dung" để bạn biết ai đang nói trong nhóm. Câu trả lời của bạn KHÔNG được mở đầu bằng "[Tên]:".
- Mỗi tin có kèm thời gian hiện tại (giờ Việt Nam); dùng nó khi được hỏi ngày giờ.
- Zalo hiển thị markdown hạn chế: dùng **in đậm** và gạch đầu dòng đơn giản, tránh bảng và heading.
- Câu hỏi cần thông tin mới (tin tức, giá cả, thời tiết...) thì dùng Google Search nếu có."""

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


def ask_gemini(chat_id: str, sender: str, text: str, image_url: str | None = None) -> str:
    history = histories[chat_id]
    # Lịch sử chỉ lưu chữ (không lưu ảnh) cho gọn
    note = " (kèm 1 ảnh)" if image_url else ""
    user_turn = {"role": "user", "parts": [{"text": f"[{sender}]{note}: {text}"}]}
    now = datetime.now(VN_TZ)
    now_text = f"(Bây giờ là {WEEKDAYS[now.weekday()]}, {now:%d/%m/%Y %H:%M})"
    parts = [download_image(image_url)] if image_url else []
    parts.append(types.Part.from_text(text=f"{now_text}\n[{sender}]: {text}"))
    contents = list(history) + [types.Content(role="user", parts=parts)]

    try:
        response = generate(contents, ENABLE_WEB_SEARCH)
    except errors.ClientError:
        if not ENABLE_WEB_SEARCH:
            raise
        # Gói free có thể hết lượt Google Search -> thử lại không search
        response = generate(contents, False)

    answer = re.sub(r"^\[[^\]\n]{1,60}\]:\s*", "", (response.text or "").strip())
    if not answer:
        return "(Mình chưa có câu trả lời, bạn hỏi lại giúp mình nhé.)"

    history.append(user_turn)
    history.append({"role": "model", "parts": [{"text": answer}]})
    return answer


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


def handle_update(update: dict):
    print(f"[nhận] {update}", flush=True)
    event = update.get("event_name")
    if event not in ("message.text.received", "message.image.received"):
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

    image_url = None
    to_group = False
    if event == "message.image.received":
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

    with chat_locks[chat_id]:  # mỗi nhóm trả lời lần lượt, các nhóm khác chạy song song
        typing(chat_id)
        try:
            answer = ask_gemini(chat_id, sender, text, image_url)
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
        else:
            send_text(chat_id, answer)
            if not is_group and image_url:
                last_private[user_id] = (text, answer)
                send_text(chat_id, SEND_GROUP_PROMPT)
            elif not is_group:
                last_private.pop(user_id, None)  # hỏi câu khác thì bỏ lời mời gửi nhóm cũ
        print(f"[đã trả lời] {chat_id}: {answer[:80]!r}", flush=True)


def main():
    me = zalo("getMe")
    print(f"Bot đang chạy: {me.get('result')}  | model={MODEL}")
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
