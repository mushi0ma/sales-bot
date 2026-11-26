# Voice agent (bot.py): Pipecat + Gemini Live on Vertex AI. Credentials: the VM's service account (ADC), no keys.
FROM python:3.13-slim@sha256:7249d5587b78209a006dc4a3b5340678f84ea729239193aa559962cd8c1fce50
RUN pip install --no-cache-dir "pipecat-ai[google,websocket]==1.15.0" "uvicorn==0.54.0" \
 && useradd --system --uid 10001 agent && mkdir /data && chown agent /data
WORKDIR /app
COPY bot.py prompt.md ./
COPY static ./static
USER agent
EXPOSE 8090
CMD ["uvicorn", "bot:app", "--host", "0.0.0.0", "--port", "8090", "--no-access-log"]
