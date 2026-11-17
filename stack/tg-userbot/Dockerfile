FROM python:3.13-alpine@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e
# Pinned versions with hashes (uv pip compile --generate-hashes); pyaes ships only as a pure-Python sdist.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt
COPY userbot.py /app/userbot.py
RUN mkdir -p /data && chown 65534:65534 /data
USER 65534:65534
ENTRYPOINT ["python3", "-I", "-u", "/app/userbot.py"]
CMD ["run"]
