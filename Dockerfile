FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock \
    && playwright install chromium --with-deps \
    && python3 -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')" \
    && echo "Whisper model pre-downloaded."

COPY . /app

RUN mkdir -p /app/data /app/logs \
    && chmod +x /app/bin/run_v2_app /app/bin/run_ai_worker /app/bin/run_agent_worker /app/bin/run_earnings_ingest_worker

EXPOSE 8080

CMD ["/app/bin/run_v2_app"]
