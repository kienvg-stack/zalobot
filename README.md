# Zalo Gemini Bot (@KienVG)

Bot Zalo trả lời trong nhóm chat bằng Gemini (key free). Trong nhóm, gõ `@KienVG câu hỏi` là bot trả lời. Chat riêng với bot thì hỏi thẳng.

## 1. Tạo bot Zalo

1. Vào https://bot.zaloplatforms.com, đăng nhập Zalo → **Tạo bot**, **đặt tên `KienVG`**.
2. Copy **Bot Token** (dạng `123456789:abc...`).
3. Add bot vào nhóm Zalo.

## 2. Lấy key Gemini (free)

1. Vào https://aistudio.google.com/apikey, đăng nhập Gmail.
2. Bấm **Create API key** → copy key (dạng `AIza...`).

Gói free: không cần thẻ, nhưng giới hạn số lượt hỏi mỗi phút/ngày, và Google có thể dùng dữ liệu để cải thiện sản phẩm — đừng hỏi bot thông tin nội bộ.

## 3. Cài và chạy

Cần Python 3.10+ (https://www.python.org/downloads/, nhớ tick "Add to PATH").

```bash
pip install -r requirements.txt
```

Mở file `.env`, dán token Zalo vào `ZALO_BOT_TOKEN=` và key Gemini vào `GEMINI_API_KEY=`, rồi:

```bash
python bot.py
```

Thấy dòng `Bot đang chạy` là xong. Tắt máy/tắt cửa sổ thì bot ngừng — muốn chạy 24/7 thì đưa lên một VPS/server.

## Chạy 24/7 trên Dokploy

1. Đưa thư mục này lên một repo Git (GitHub/GitLab công ty). File `.env` đã nằm trong `.gitignore`, **không bị đẩy lên**.
2. Dokploy → Create Project → **Create Service → Application**.
3. Provider: chọn repo vừa tạo. Build Type: **Dockerfile**.
4. Tab **Environment**, dán vào:
   ```
   ZALO_BOT_TOKEN=...
   GEMINI_API_KEY=...
   BOT_NAME=KienVG
   GEMINI_MODEL=gemini-3.5-flash-lite
   ```
5. Bấm **Deploy**. Bot không cần domain hay mở port (nó tự gọi ra Zalo).
6. Xem tab **Logs**, thấy `Bot đang chạy` là xong.

**Chỉ chạy 1 bản bot cùng lúc** (máy bạn hoặc Dokploy) — hai bản cùng lấy tin sẽ giành tin của nhau.

## Lệnh trong chat

- `@KienVG <câu hỏi>`: hỏi bất cứ gì
- `@KienVG /reset`: xoá trí nhớ cuộc trò chuyện của nhóm
- Đọc ảnh trong nhóm: gửi ảnh **kèm chú thích có tag bot** (vd `@Bot KienVG đọc ảnh này`). Ảnh gửi riêng không tag thì Zalo không chuyển cho bot.
- Chat riêng với bot: gửi ảnh là bot tự đọc. Nhắn thêm `gửi nhóm` để bot đăng câu trả lời vào nhóm (hoặc ghi chú thích ảnh `gửi nhóm <câu hỏi>` để đăng thẳng).
