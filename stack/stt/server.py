#!/usr/bin/env python3
"""Local speech-to-text for the sales-bot TEST stack: OpenAI-compatible /v1/audio/transcriptions on faster-whisper.

Voice notes never leave the VM. The model is baked into the image at build time and the hub is offline at
runtime. Internal docker network only, Bearer STT_API_TOKEN on every /v1 route.

  POST /v1/audio/transcriptions   multipart: file, model, language, response_format (json|verbose_json|text)
  GET  /v1/models                 the one model served (the agents vault probe calls it)
  GET  /healthz                   no auth
Logs carry method, path, status, audio seconds and processing time only, never text.
"""
import email.parser, email.policy, hmac, json, os, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from faster_whisper import WhisperModel

PORT = int(os.environ.get("PORT", "8000"))
MODEL_DIR = os.environ["WHISPER_MODEL_DIR"]
MODEL_ID = os.environ.get("WHISPER_MODEL_ID", "whisper-small")
MAX_BODY = 25 * 1024 * 1024
model = WhisperModel(MODEL_DIR, device="cpu", compute_type="int8",
                     cpu_threads=int(os.environ.get("STT_THREADS", "3")), num_workers=1)
_busy = threading.Lock()  # one transcription at a time: CPU-bound, and the VM runs the whole stack


def parse_multipart(content_type, raw):
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw)
    if not msg.is_multipart():
        raise ValueError("expected multipart/form-data")
    fields, audio = {}, None
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name == "file":
            audio = part.get_payload(decode=True)
        elif name:
            fields[name] = (part.get_payload(decode=True) or b"").decode("utf-8", "replace").strip()
    if not audio:
        raise ValueError("file is required")
    return audio, fields


def transcribe(audio, fields):
    with tempfile.NamedTemporaryFile(dir="/tmp", suffix=".audio") as f:
        f.write(audio)
        f.flush()
        with _busy:
            segments, info = model.transcribe(
                f.name, language=(fields.get("language") or None), vad_filter=True, beam_size=1,
                temperature=0.0, condition_on_previous_text=False)
            segs = list(segments)
    text = " ".join(s.text.strip() for s in segs).strip()
    return text, info, segs


class Handler(BaseHTTPRequestHandler):
    server_version = "salesbot-stt/1"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def log_request(self, code="-", size="-"):
        extra = getattr(self, "_extra", "")
        sys.stderr.write(f"{self.log_date_time_string()} {self.command} {urlsplit(self.path).path} {code}{extra}\n")

    def send(self, code, payload, ctype="application/json; charset=utf-8"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authed(self):
        token = os.environ.get("STT_API_TOKEN", "")
        got = self.headers.get("Authorization", "")
        if token and hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
            return True
        self.send(401, {"error": {"message": "unauthorized"}})
        return False

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/healthz":
            return self.send(200, {"ok": True})
        if path == "/v1/models":
            if self.authed():
                self.send(200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}]})
            return
        self.send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if urlsplit(self.path).path != "/v1/audio/transcriptions":
            return self.send(404, {"error": {"message": "not found"}})
        if not self.authed():
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                raise ValueError("body missing or too large")
            audio, fields = parse_multipart(self.headers.get("Content-Type", ""), self.rfile.read(n))
            t0 = time.monotonic()
            text, info, segs = transcribe(audio, fields)
            self._extra = f" audio={info.duration:.1f}s took={time.monotonic() - t0:.1f}s"
        except ValueError as e:
            return self.send(400, {"error": {"message": str(e)}})
        except Exception as e:  # undecodable audio and the like: say so without echoing content
            sys.stderr.write(f"transcribe failed: {type(e).__name__}\n")
            return self.send(400, {"error": {"message": "could not decode or transcribe audio"}})
        fmt = fields.get("response_format", "json")
        if fmt == "text":
            return self.send(200, text.encode(), "text/plain; charset=utf-8")
        if fmt == "verbose_json":
            return self.send(200, {
                "task": "transcribe", "language": info.language, "duration": info.duration, "text": text,
                "segments": [{"id": i, "start": s.start, "end": s.end, "text": s.text,
                              "avg_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob,
                              "compression_ratio": s.compression_ratio, "temperature": s.temperature}
                             for i, s in enumerate(segs)]})
        return self.send(200, {"text": text})


if __name__ == "__main__":
    if not os.environ.get("STT_API_TOKEN"):
        sys.exit("STT_API_TOKEN is not set")
    sys.stderr.write(f"stt: {MODEL_ID} from {MODEL_DIR}, listening on :{PORT}\n")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
