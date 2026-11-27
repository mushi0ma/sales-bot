# orchestrator (main.py): event ingress + Hatchet worker running the roles (the card in Twenty first).
FROM python:3.13-slim@sha256:7249d5587b78209a006dc4a3b5340678f84ea729239193aa559962cd8c1fce50
RUN pip install --no-cache-dir "hatchet-sdk==1.44.0" \
 && useradd --system --uid 10001 orch && mkdir /data && chown orch /data
WORKDIR /app
COPY main.py cards.py funnel.py ./
USER orch
EXPOSE 8080
CMD ["python3", "-u", "main.py"]
