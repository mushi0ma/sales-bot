# Local STT (faster-whisper) for the sales-bot test stack. The model is downloaded at BUILD time and
# baked in; at runtime the hub is offline and the container only talks to the docker network.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1
# av 19 dropped open(metadata_errors=...), which faster-whisper 1.2.1 passes.
RUN pip install "faster-whisper==1.2.1" "av==18.1.0"

ARG WHISPER_MODEL=small
RUN python -c "from faster_whisper import download_model; download_model('${WHISPER_MODEL}', output_dir='/models/${WHISPER_MODEL}')" \
 && rm -rf /root/.cache

ENV HF_HUB_OFFLINE=1 WHISPER_MODEL_DIR=/models/${WHISPER_MODEL} WHISPER_MODEL_ID=whisper-${WHISPER_MODEL}
COPY server.py /app/server.py
USER 65534:65534
EXPOSE 8000
CMD ["python3", "-I", "-u", "/app/server.py"]
