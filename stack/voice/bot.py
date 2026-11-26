"""Voice agent of e-KNOT: Gemini Live on Vertex AI through Pipecat.

Two doors into the same conversation:
  /wa       a WhatsApp call to the test number. The connector inside Chatwoot answers the call and dials this
            WebSocket (JSON frames: start, answered, media every 60 ms, stop; we send media, clear, hangup), 16 kHz
            PCM both ways. Chatwoot hands the call over when WHATSAPP_VOICE_AGENT_URL is set (stack/patches).
  /browser  the same agent from a browser page (/), raw PCM over a WebSocket: 16 kHz up, 24 kHz down. For
            testing through an SSH tunnel to localhost (a secure context, so the microphone works).

Knowledge: the e-KNOT catalog (services.json) in the prompt, page snapshots (kb/pages) through the service_info
tool. Leads go to SQLite (/data/voice.db) with the call's transcript.
Limits (the mentor's "no runaway spend"): MAX_CALLS at once, MAX_CALL_SECS per call, DAILY_CALLS per day.
"""
import asyncio, base64, json, logging, os, sqlite3, sys, time
from pathlib import Path

from loguru import logger

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (AudioRawFrame, CancelFrame, EndFrame, Frame, InputAudioRawFrame, InterruptionFrame,
                                   LLMRunFrame, OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.services.google.gemini_live.vertex.llm import GeminiLiveVertexLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

E = os.environ.get
PROJECT = E("GCP_PROJECT", "")
LOCATION = E("VOICE_LOCATION", "eu")
MODEL = E("VOICE_MODEL", "google/gemini-3.8-live")
VOICE = E("VOICE_NAME", "Kore")
MAX_CALLS = int(E("MAX_CALLS", "2"))
MAX_CALL_SECS = int(E("MAX_CALL_SECS", "600"))
DAILY_CALLS = int(E("DAILY_CALLS", "40"))
DB_PATH = E("DB_PATH", "/data/voice.db")
HERE = Path(__file__).resolve().parent
CATALOG = json.loads(Path(E("CATALOG_PATH", "/app/services.json")).read_text(encoding="utf-8"))
SERVICES = {s["id"]: s for s in CATALOG["services"]}
KB_DIR = Path(E("KB_DIR", "/app/kb"))

# Pipecat logs the system prompt, tool arguments and transcripts at DEBUG; the transcript lives in voice.db only.
logger.remove()
logger.add(sys.stderr, level=E("PIPECAT_LOG_LEVEL", "INFO"))
log = logging.getLogger("voice")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
db.executescript("""
CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY AUTOINCREMENT, door TEXT NOT NULL, peer TEXT,
  started_at REAL NOT NULL, ended_at REAL, transcript TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY AUTOINCREMENT, call_id INTEGER NOT NULL, peer TEXT,
  at REAL NOT NULL, data TEXT NOT NULL);
""")
active = 0


def prompt():
    lines = [f"- {s['id']}: {s['name']}. {s['what']}" for s in CATALOG["services"]]
    prices = json.dumps(CATALOG.get("company", {}).get("prices", {}), ensure_ascii=False)
    return (HERE / "prompt.md").read_text(encoding="utf-8").replace(
        "{catalog}", "\n".join(lines) + f"\nЦены с сайта: {prices}")


def calls_today():
    midnight = time.time() - (time.time() + 5 * 3600) % 86400  # Astana midnight
    return db.execute("SELECT COUNT(*) FROM calls WHERE started_at >= ?", (midnight,)).fetchone()[0]


# ---------------------------------------------------------------- wire formats

class ConnectorSerializer(FrameSerializer):
    """The WhatsApp connector's media stream (vendor/whatsapp-connector/contract/PROTOCOL.md, call.accept stream)."""

    def __init__(self):
        super().__init__()
        self.peer, self.call_id, self.answered = None, None, asyncio.Event()

    async def serialize(self, frame: Frame):
        if isinstance(frame, (EndFrame, CancelFrame)):
            return json.dumps({"event": "hangup", "by": "agent"})
        if isinstance(frame, InterruptionFrame):
            return json.dumps({"event": "clear"})
        if isinstance(frame, AudioRawFrame) and frame.audio:
            return json.dumps({"event": "media", "payload": base64.b64encode(frame.audio).decode()})
        return None

    async def deserialize(self, data):
        try:
            m = json.loads(data)
        except (ValueError, TypeError):
            return None
        ev = m.get("event")
        if ev == "media" and m.get("payload"):
            return InputAudioRawFrame(audio=base64.b64decode(m["payload"]), sample_rate=16000, num_channels=1)
        if ev == "start":
            self.call_id, self.peer = m.get("call_id"), m.get("peer")
        elif ev == "answered":
            self.answered.set()
        return None


class BrowserSerializer(FrameSerializer):
    """Binary frames are PCM16 audio (16 kHz in, 24 kHz out); text frames are JSON events for the page."""

    def __init__(self):
        super().__init__()
        self.peer, self.call_id, self.answered = "browser", None, asyncio.Event()
        self.answered.set()

    async def serialize(self, frame: Frame):
        if isinstance(frame, InterruptionFrame):
            return json.dumps({"type": "interrupted"})
        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            return json.dumps(frame.message)
        if isinstance(frame, AudioRawFrame) and frame.audio:
            return frame.audio
        return None

    async def deserialize(self, data):
        if isinstance(data, (bytes, bytearray)):
            return InputAudioRawFrame(audio=bytes(data), sample_rate=16000, num_channels=1)
        return None


# ---------------------------------------------------------------- one call

async def run_call(ws: WebSocket, door: str, serializer, out_rate: int):
    global active
    if active >= MAX_CALLS or calls_today() >= DAILY_CALLS:
        log.warning("call refused: active=%s today=%s", active, calls_today())
        if door == "wa":
            await ws.send_text(json.dumps({"event": "hangup", "by": "agent", "reason": "busy"}))
        await ws.close()
        return
    active += 1
    call_id = db.execute("INSERT INTO calls (door, started_at) VALUES (?, ?)", (door, time.time())).lastrowid
    transcript = []
    task = None

    async def service_info(params: FunctionCallParams):
        s = SERVICES.get(params.arguments.get("service_id", ""))
        if not s:
            await params.result_callback({"error": "нет такой услуги", "ids": list(SERVICES)})
            return
        page = KB_DIR / f"{s['id']}.md"
        await params.result_callback({
            "name": s["name"], "url": s["url"], "what": s["what"], "details": s.get("details", {}),
            "page": page.read_text(encoding="utf-8")[:6000] if page.exists() else ""})

    async def save_lead(params: FunctionCallParams):
        db.execute("INSERT INTO leads (call_id, peer, at, data) VALUES (?,?,?,?)",
                   (call_id, serializer.peer, time.time(), json.dumps(params.arguments, ensure_ascii=False)))
        log.info("call=%s lead saved: %s", call_id, sorted(params.arguments))
        await params.result_callback({"saved": True})

    async def end_call(params: FunctionCallParams):
        await params.result_callback({"ok": True})
        await asyncio.sleep(4)  # let the goodbye play out
        await task.queue_frame(EndFrame())

    tools = ToolsSchema(standard_tools=[
        FunctionSchema("service_info", "Подробности об услуге e-KNOT со страницы сайта: что входит, тарифы, FAQ.",
                       {"service_id": {"type": "string", "enum": list(SERVICES)}}, ["service_id"], service_info),
        FunctionSchema("save_lead", "Записать заявку для менеджера e-KNOT, когда председатель или УК заинтересованы.",
                       {"service": {"type": "string"}, "role": {"type": "string", "description": "председатель, УК, "
                                                                "член совета, жилец"},
                        "house": {"type": "string", "description": "город и дом или ЖК"},
                        "apartments": {"type": "string"}, "wants": {"type": "string"},
                        "name": {"type": "string", "description": "как представился, если представился"}},
                       ["service", "role", "wants"], save_lead),
        FunctionSchema("end_call", "Положить трубку после прощания.", {}, [], end_call),
    ])
    llm = GeminiLiveVertexLLMService(
        project_id=PROJECT, location=LOCATION, system_instruction=prompt(), tools=tools,
        settings=GeminiLiveVertexLLMService.Settings(model=MODEL, voice=VOICE, language="ru-RU"))
    context = LLMContext([{"role": "user", "content": "(звонок принят)"}], tools)
    agg = LLMContextAggregatorPair(context)
    transport = FastAPIWebsocketTransport(ws, FastAPIWebsocketParams(
        audio_in_enabled=True, audio_out_enabled=True, add_wav_header=False, serializer=serializer,
        session_timeout=MAX_CALL_SECS))
    task = PipelineTask(Pipeline([transport.input(), agg.user(), llm, transport.output(), agg.assistant()]),
                        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=out_rate),
                        idle_timeout_secs=120)

    async def said(who, text):
        if not text:
            return
        transcript.append({"who": who, "text": text, "t": round(time.time(), 1)})
        if door == "browser":
            await task.queue_frame(OutputTransportMessageUrgentFrame(message={"type": "transcript", "who": who,
                                                                              "text": text}))

    @agg.user().event_handler("on_user_turn_message_added")
    async def _user(_, message):
        await said("user", message.content)

    @agg.assistant().event_handler("on_assistant_turn_stopped")
    async def _bot(_, message):
        await said("bot", message.content)

    @transport.event_handler("on_client_connected")
    async def _connected(_, __):
        try:
            await asyncio.wait_for(serializer.answered.wait(), 30)
        except asyncio.TimeoutError:
            await task.cancel()
            return
        await task.queue_frame(LLMRunFrame())

    @transport.event_handler("on_client_disconnected")
    async def _gone(_, __):
        await task.cancel()

    @transport.event_handler("on_session_timeout")
    async def _timeout(_, __):
        await task.queue_frame(EndFrame())

    log.info("call=%s door=%s started (active=%s)", call_id, door, active)
    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        active -= 1
        db.execute("UPDATE calls SET ended_at=?, peer=?, transcript=? WHERE id=?",
                   (time.time(), serializer.peer, json.dumps(transcript, ensure_ascii=False), call_id))
        log.info("call=%s ended: %s turns", call_id, len(transcript))


app = FastAPI()


@app.get("/")
async def page():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/healthz")
async def health():
    return {"ok": True, "active": active}


@app.websocket("/wa")
async def wa(ws: WebSocket):
    await ws.accept()
    await run_call(ws, "wa", ConnectorSerializer(), 16000)


@app.websocket("/browser")
async def browser(ws: WebSocket):
    await ws.accept()
    await run_call(ws, "browser", BrowserSerializer(), 24000)
