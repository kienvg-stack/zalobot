FROM python:3.12-slim

# ffmpeg: đổi giọng nói Gemini sang .aac cho Zalo
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py ./

EXPOSE 8080
CMD ["python", "bot.py"]
