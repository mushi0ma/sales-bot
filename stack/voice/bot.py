"""Voice agent of e-KNOT: Gemini Live on Vertex AI through Pipecat.

Doors:
  /wa       a WhatsApp call to the test number. The connector inside Chatwoot answers the call and dials this
            WebSocket (JSON frames: start, answered, media every 60 ms, stop; we send media, clear, hangup), 16 kHz
            PCM both ways. Chatwoot hands the call over when WHATSAPP_VOICE_AGENT_URL is set (stack/patches).
  /wa?campaign=votes&name=...  an outbound call the agent makes for the ОСИ chairman (stack/voice/dial.sh): the
            connector rings the number and dials this WebSocket; `answered` comes when the person picks up. The
            campaign (campaigns.json: what to say, what to find out) picks prompt-outbound.md; the result goes to
            the outcomes table.
  /browser  the same agent from a browser page (/), raw PCM over a WebSocket: 16 kHz up, 24 kHz down. For
            testing through an SSH tunnel to localhost (a secure context, so the microphone works).

Knowledge: the e-KNOT catalog (services.json) in the prompt, page snapshots (kb/pages) through the service_info
tool. Leads go to SQLite (/data/voice.db) with the call's transcript.
Limits (the mentor's "no runaway spend"): MAX_CALLS at once, MAX_CALL_SECS per call, DAILY_CALLS per day.
"""
import asyncio, base64, json, logging, os, re, sqlite3, sys, time
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
_cp = Path(E("CAMPAIGNS_PATH", "/app/campaigns.json"))
CAMPAIGNS = {k: v for k, v in (json.loads(_cp.read_text(encoding="utf-8")) if _cp.exists() else {}).items()
             if not k.startswith("_")}

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
CREATE TABLE IF NOT EXISTS outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT, call_id INTEGER NOT NULL, peer TEXT,
  campaign TEXT NOT NULL, result TEXT NOT NULL, note TEXT, callback TEXT, at REAL NOT NULL);
""")
if "campaign" not in {r[1] for r in db.execute("PRAGMA table_info(calls)")}:
    db.execute("ALTER TABLE calls ADD COLUMN campaign TEXT")
active = 0


def prompt():
    lines = [f"- {s['id']}: {s['name']}. {s['what']}" for s in CATALOG["services"]]
    prices = json.dumps(CATALOG.get("company", {}).get("prices", {}), ensure_ascii=False)
    return (HERE / "prompt.md").read_text(encoding="utf-8").replace(
        "{catalog}", "\n".join(lines) + f"\nЦены с сайта: {prices}")


def prompt_outbound(campaign, name):
    c = CAMPAIGNS[campaign]
    return (HERE / "prompt-outbound.md").read_text(encoding="utf-8").format(
        house=c["house"], chairman=c["chairman"], title=c["title"], goal=c["goal"], ask=c["ask"],
        facts="\n".join(f"- {f}" for f in c["facts"]), name=name or "неизвестно")


def calls_today():
    midnight = time.time() - (time.time() + 5 * 3600) % 86400  # Astana midnight
    return db.execute("SELECT COUNT(*) FROM calls WHERE started_at >= ?", (midnight,)).fetchone()[0]


# ---------------------------------------------------------------- wire formats

class ConnectorSerializer(FrameSerializer):
    """The WhatsApp connector's media stream (vendor/whatsapp-connector/contract/PROTOCOL.md, call.accept stream)."""

    def __init__(self):
        super().__init__()
        self.peer, self.call_id, self.answered, self.ended = None, None, asyncio.Event(), asyncio.Event()

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
        elif ev == "stop":  # the call ended: rejected or unanswered before anybody picked up, or hung up
            self.ended.set()
        return None


class BrowserSerializer(FrameSerializer):
    """Binary frames are PCM16 audio (16 kHz in, 24 kHz out); text frames are JSON events for the page."""

    def __init__(self):
        super().__init__()
        self.peer, self.call_id, self.answered, self.ended = "browser", None, asyncio.Event(), asyncio.Event()
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

async def run_call(ws: WebSocket, door: str, serializer, out_rate: int, campaign=None, name=None):
    global active
    if active >= MAX_CALLS or calls_today() >= DAILY_CALLS:
        log.warning("call refused: active=%s today=%s", active, calls_today())
        if door == "wa":
            await ws.send_text(json.dumps({"event": "hangup", "by": "agent", "reason": "busy"}))
        await ws.close()
        return
    active += 1
    call_id = db.execute("INSERT INTO calls (door, started_at, campaign) VALUES (?, ?, ?)",
                         (door, time.time(), campaign)).lastrowid
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

    async def save_outcome(params: FunctionCallParams):
        a = params.arguments
        db.execute("INSERT INTO outcomes (call_id, peer, campaign, result, note, callback, at) VALUES (?,?,?,?,?,?,?)",
                   (call_id, serializer.peer, campaign, a.get("result", ""), a.get("note"), a.get("callback"),
                    time.time()))
        log.info("call=%s outcome %s", call_id, a.get("result"))
        await params.result_callback({"saved": True})

    async def end_call(params: FunctionCallParams):
        asked = time.time()
        await params.result_callback({"ok": True})
        await asyncio.sleep(5)  # let the goodbye play out
        # The person said something after the goodbye ("подождите", "вы куда?"): the conversation goes on.
        if any(x["who"] == "user" and x["t"] > asked for x in transcript):
            log.info("call=%s end_call skipped: the person spoke after the goodbye", call_id)
            return
        await task.queue_frame(EndFrame())

    service_tool = FunctionSchema(
        "service_info", "Подробности об услуге e-KNOT со страницы сайта: что входит, тарифы, FAQ.",
        {"service_id": {"type": "string", "enum": list(SERVICES)}}, ["service_id"], service_info)
    end_tool = FunctionSchema("end_call", "Положить трубку после прощания.", {}, [], end_call)
    if campaign:
        tools = ToolsSchema(standard_tools=[
            FunctionSchema("save_outcome", "Записать итог звонка для председателя.",
                           {"result": {"type": "string", "enum": CAMPAIGNS[campaign]["outcomes"]},
                            "note": {"type": "string", "description": "вопросы и жалобы для председателя, кратко"},
                            "callback": {"type": "string", "description": "когда перезвонить или когда оплатит/"
                                                                          "проголосует, словами человека"}},
                           ["result"], save_outcome),
            service_tool, end_tool])
    else:
        tools = ToolsSchema(standard_tools=[
            service_tool,
            FunctionSchema("save_lead", "Записать заявку для менеджера e-KNOT, когда председатель или УК заинтересованы.",
                           {"service": {"type": "string"}, "role": {"type": "string", "description": "председатель, УК, "
                                                                    "член совета, жилец"},
                            "house": {"type": "string", "description": "город и дом или ЖК"},
                            "apartments": {"type": "string"}, "wants": {"type": "string"},
                            "name": {"type": "string", "description": "как представился, если представился"}},
                           ["service", "role", "wants"], save_lead),
            end_tool])
    llm = GeminiLiveVertexLLMService(
        project_id=PROJECT, location=LOCATION,
        system_instruction=prompt_outbound(campaign, name) if campaign else prompt(), tools=tools,
        settings=GeminiLiveVertexLLMService.Settings(model=MODEL, voice=VOICE, language="ru-RU"))
    context = LLMContext([{"role": "user", "content": "(человек взял трубку)" if campaign else "(звонок принят)"}],
                         tools)
    agg = LLMContextAggregatorPair(context)
    transport = FastAPIWebsocketTransport(ws, FastAPIWebsocketParams(
        audio_in_enabled=True, audio_out_enabled=True, add_wav_header=False, serializer=serializer,
        session_timeout=MAX_CALL_SECS))
    task = PipelineTask(Pipeline([transport.input(), agg.user(), llm, transport.output(), agg.assistant()]),
                        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=out_rate),
                        idle_timeout_secs=120)

    async def said(who, text):
        text = re.sub(r"<no speech>|\{pause\}", "", text or "").strip()  # Gemini Live's markers, not speech
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
        # An outbound call rings until the person picks up, rejects it, or WhatsApp gives up (~60 s).
        waits = [asyncio.create_task(serializer.answered.wait()), asyncio.create_task(serializer.ended.wait())]
        await asyncio.wait(waits, timeout=90 if campaign else 30, return_when=asyncio.FIRST_COMPLETED)
        for w in waits:
            w.cancel()
        if not serializer.answered.is_set():
            log.info("call=%s not answered (%s)", call_id, "ended" if serializer.ended.is_set() else "timeout")
            await task.cancel()
            return
        await task.queue_frame(LLMRunFrame())

    @transport.event_handler("on_client_disconnected")
    async def _gone(_, __):
        await task.cancel()

    @transport.event_handler("on_session_timeout")
    async def _timeout(_, __):
        await task.queue_frame(EndFrame())

    log.info("call=%s door=%s campaign=%s started (active=%s)", call_id, door, campaign, active)
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
    campaign = ws.query_params.get("campaign") or None
    if campaign and campaign not in CAMPAIGNS:
        await ws.close(code=4404)
        return
    await ws.accept()
    await run_call(ws, "wa-out" if campaign else "wa", ConnectorSerializer(), 16000, campaign,
                   (ws.query_params.get("name") or "")[:60] or None)


@app.websocket("/browser")
async def browser(ws: WebSocket):
    await ws.accept()
    await run_call(ws, "browser", BrowserSerializer(), 24000)
