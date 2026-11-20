import asyncio
import contextlib
import json
import logging
import re
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from openai import APIError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import settings
from app.mcp_tools.clinic_tools import ClinicTools
from app.pipeline.guardrails import EMERGENCY_SCRIPT, evaluate_clinical_red_flags
from app.pipeline.llm_router import AgentRouter
from app.pipeline.memory import SessionMemory
from app.pipeline.sentence_chunker import stream_sentences_from_tokens
from app.skills.loader import load_markdown_skill

logger = logging.getLogger("voice-poc")
logging.basicConfig(level=logging.INFO)


class Telemetry:
    def __init__(self) -> None:
        self.listeners: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self.recent: dict[str, deque] = {}

    def publish(self, call_id: str, event: dict) -> None:
        if call_id not in self.recent:
            if len(self.recent) >= 100:
                self.recent.pop(next(iter(self.recent)))
            self.recent[call_id] = deque(maxlen=80)
        self.recent[call_id].append(event)
        for queue in self.listeners.get(call_id, set()):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from pathlib import Path

    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    client = httpx.AsyncClient(
        base_url=settings.NESTJS_BACKEND_URL, timeout=15,
        headers={"x-clinic-token": settings.CLINIC_API_TOKEN},
    )
    memory = SessionMemory(redis)
    clinic = ClinicTools(client, redis, memory)
    skill = load_markdown_skill(Path(__file__).parent / "skills" / "gp_triage_skill.md")
    router = AgentRouter(skill, clinic)
    app.state.redis, app.state.memory = redis, memory
    app.state.clinic, app.state.router = clinic, router
    app.state.client, app.state.telemetry = client, Telemetry()
    try:
        yield
    finally:
        await router.close()
        await client.aclose()
        await redis.aclose()


app = FastAPI(title="Synthetic clinic voice POC", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=[settings.ALLOWED_ORIGIN],
    allow_methods=["GET", "POST"], allow_headers=["Content-Type"],
)


def check_origin(origin: str | None) -> None:
    if origin != settings.ALLOWED_ORIGIN:
        raise HTTPException(403, "Only the configured local dashboard origin is allowed")


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except RedisError:
        checks["redis"] = "unavailable"
    try:
        response = await request.app.state.client.get("/health")
        response.raise_for_status()
        checks["clinic"] = "ok"
    except httpx.HTTPError:
        checks["clinic"] = "unavailable"
    checks["speech_configured"] = bool(settings.AZURE_SPEECH_KEY)
    checks["model_configured"] = bool(settings.GITHUB_TOKEN or settings.GROQ_API_KEY)
    checks["rag_mode"] = settings.RAG_MODE
    checks["status"] = "ok" if checks["redis"] == checks["clinic"] == "ok" else "degraded"
    return checks


@app.post("/api/sessions")
async def create_session(request: Request) -> dict[str, str]:
    check_origin(request.headers.get("origin"))
    return await request.app.state.memory.create()


@app.get("/api/telemetry")
async def telemetry(request: Request, call_id: UUID, token: str) -> StreamingResponse:
    check_origin(request.headers.get("origin"))
    key = str(call_id)
    if not await request.app.state.memory.authorize(key, token):
        raise HTTPException(401, "Invalid or expired session")
    hub: Telemetry = request.app.state.telemetry

    async def events() -> AsyncIterator[str]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        hub.listeners[key].add(queue)
        try:
            yield "data: " + json.dumps({"type": "snapshot", "events": list(hub.recent.get(key, []))}) + "\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            hub.listeners[key].discard(queue)
            if not hub.listeners[key]:
                del hub.listeners[key]
    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


YES = re.compile(r"^(yes|yes please|yes that is correct|yes that's correct|confirm|confirmed|correct)[.! ]*$", re.I)
NO = re.compile(r"^(no|no thanks|cancel|stop)[.! ]*$", re.I)


@app.websocket("/ws/voice")
async def voice_socket(ws: WebSocket, call_id: UUID, token: str, voice: bool = False) -> None:
    if ws.headers.get("origin") != settings.ALLOWED_ORIGIN:
        await ws.close(code=1008, reason="Dashboard origin required")
        return
    app_state = ws.app.state
    call = str(call_id)
    memory: SessionMemory = app_state.memory
    redis: Redis = app_state.redis
    clinic: ClinicTools = app_state.clinic
    router: AgentRouter = app_state.router
    state = await memory.authorize(call, token)
    if not state:
        await ws.close(code=1008, reason="Invalid or expired call")
        return
    owner = str(uuid4())
    active_key = f"voice:active:{call}"
    if not await redis.set(active_key, owner, nx=True, ex=60):
        await ws.close(code=1008, reason="Call already connected")
        return
    await ws.accept()
    send_lock = asyncio.Lock()
    control_lock = asyncio.Lock()
    turn_task: asyncio.Task | None = None
    turn_id = 0
    bridge = None
    vad = None
    closing = False
    committing = False
    commit_finished = asyncio.Event()
    commit_finished.set()
    error_tasks: set[asyncio.Task] = set()

    async def send(event: dict[str, Any]) -> None:
        if closing:
            return
        app_state.telemetry.publish(call, event)
        async with send_lock:
            await ws.send_json(event)

    async def report(message: str) -> None:
        logger.error("Call %s: %s", call, message)
        await send({"type": "error", "message": message})
        try:
            await clinic.audit(call, "error", message)
        except httpx.HTTPError:
            logger.exception("Could not persist error audit for call %s", call)

    async def run_turn(text: str, generation: int) -> None:
        nonlocal state, committing
        started = perf_counter()
        try:
            loaded = await memory.load(call)
        except RedisError as error:
            await report(f"Cannot load call state: {type(error).__name__}")
            return
        if not loaded:
            await report("Session expired. End this call and start a new one.")
            return
        state = loaded
        state["messages"].append({"role": "user", "content": text})
        baseline = len(state["messages"])
        prior_pending = state.get("pending")
        await send({"type": "user", "text": text, "turn_id": generation})
        await send({"type": "turn_start", "turn_id": generation})
        flag = evaluate_clinical_red_flags(text)
        try:
            answer = None
            if flag.triggered or state["emergency"]:
                state["emergency"] = True
                await clinic.clear_pending(call, state)
                await send({"type": "red_flag", "category": flag.category or "emergency_latched"})
                # The emergency response is delivered even when audit persistence fails.
                try:
                    await clinic.audit(call, "red_flag", flag.category or "emergency_latched")
                except httpx.HTTPError:
                    logger.exception("Emergency audit persistence failed for call %s", call)
                    await send({"type": "error", "message": "Emergency audit could not be persisted"})
                answer = EMERGENCY_SCRIPT
            elif state.get("pending"):
                if YES.fullmatch(text.strip()) and state["pending"].get("announced"):
                    committing = True
                    commit_finished.clear()
                    try:
                        answer = await clinic.confirm(call, state)
                    finally:
                        committing = False
                        commit_finished.set()
                elif NO.fullmatch(text.strip()):
                    if state["pending"].get("attempted"):
                        answer = (
                            "The previous submission may already have completed; I cannot cancel it here. "
                            "Check the dashboard or say yes to safely retry the same request."
                        )
                    else:
                        await clinic.clear_pending(call, state)
                        answer = "Cancelled; I have not submitted the pending request."
                elif state["pending"].get("attempted"):
                    answer = (
                        "The previous submission has an unknown outcome. "
                        "Check the dashboard or say yes to safely retry the same request."
                    )
                elif not state["pending"].get("announced") or YES.fullmatch(text.strip()):
                    answer = state["pending"]["prompt"]
                else:
                    await clinic.clear_pending(call, state)
            first_text = None
            first_audio = None
            if answer:
                state["messages"].append({"role": "assistant", "content": answer})
                sentences = single_sentence(answer)
            else:
                sentences = stream_sentences_from_tokens(router.turn(text, call, state))
            async for sentence in sentences:
                if first_text is None:
                    first_text = round((perf_counter() - started) * 1000)
                await send({"type": "assistant_sentence", "text": sentence, "turn_id": generation})
                if bridge:
                    async for audio in bridge.synthesize(sentence):
                        if first_audio is None:
                            first_audio = round((perf_counter() - started) * 1000)
                        async with send_lock:
                            await ws.send_bytes(audio)
            if state.get("pending"):
                state["pending"]["announced"] = not voice
                state["pending"]["turn_id"] = generation
            await memory.save(call, state)
            await send({"type": "metrics", "turn_id": generation, "first_text_ms": first_text,
                        "ttfa_ms": first_audio, "total_ms": round((perf_counter() - started) * 1000)})
            await send({"type": "turn_done", "turn_id": generation})
        except asyncio.CancelledError:
            state["messages"] = state["messages"][:baseline]
            state["messages"].append({"role": "assistant", "content": "[Response interrupted by caller]"})
            if state.get("pending") and state["pending"] is not prior_pending:
                await clinic.clear_pending(call, state)
            await memory.save(call, state)
            raise
        except (APIError, httpx.HTTPError, RedisError, ValueError, RuntimeError) as error:
            # Incomplete tool turns must never be replayed to a provider.
            state["messages"] = state["messages"][:baseline]
            state["messages"].append({"role": "assistant", "content": "[Turn failed; no success confirmed]"})
            await memory.save(call, state)
            if isinstance(error, httpx.HTTPStatusError):
                message = f"Clinic request failed ({error.response.status_code}): {error.response.text[:300]}"
            elif isinstance(error, APIError):
                message = f"Model request failed ({type(error).__name__}); check provider model, quota and credentials."
            else:
                message = f"{type(error).__name__}: {error}"
            await report(message)
            await send({"type": "turn_done", "turn_id": generation})

    async def _interrupt() -> None:
        if state["emergency"] or committing:
            return
        if turn_task and not turn_task.done():
            turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await turn_task
        await send({"type": "interrupt", "turn_id": turn_id})

    async def interrupt() -> None:
        async with control_lock:
            await _interrupt()

    async def _submit(text: str) -> None:
        nonlocal turn_task, turn_id
        text = text.strip()
        if not text:
            return
        if len(text) > 2000:
            await report("Please keep each message under 2000 characters.")
            return
        if committing:
            await send({"type": "state", "message": "Saving your confirmed request; please wait."})
            await commit_finished.wait()
        if turn_task and not turn_task.done():
            if state["emergency"]:
                return
            await _interrupt()
        turn_id += 1
        turn_task = asyncio.create_task(run_turn(text, turn_id))
        def completed(task: asyncio.Task) -> None:
            if not task.cancelled() and (error := task.exception()):
                logger.error("Call turn failed for %s", call, exc_info=(type(error), error, error.__traceback__))
        turn_task.add_done_callback(completed)

    async def submit(text: str) -> None:
        async with control_lock:
            await _submit(text)

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(20)
            renewed = await redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('expire', KEYS[1], 60) else return 0 end",
                1, active_key, owner,
            )
            if not renewed:
                await report("Call ownership expired; reconnect with a new session.")
                await ws.close(code=1008)
                return
            await redis.expire(memory.key(call), 3600)

    def schedule(coro) -> None:
        if not closing:
            task = asyncio.create_task(coro)
            error_tasks.add(task)
            def finished(done: asyncio.Task) -> None:
                error_tasks.discard(done)
                if not done.cancelled() and (error := done.exception()):
                    logger.error("Call callback failed", exc_info=(type(error), error, error.__traceback__))
            task.add_done_callback(finished)
        else:
            coro.close()

    def interim(text: str) -> None:
        schedule(send({"type": "interim", "text": text}))
        if len(text.split()) > 2:
            schedule(interrupt())

    def final(text: str) -> None:
        schedule(submit(text))

    lease_task = asyncio.create_task(heartbeat())
    try:
        await clinic.audit(call, "call_started", "voice" if voice else "text")
        if voice:
            from app.speech.azure_speech import AzureVoiceBridge
            from app.speech.vad import SileroDetector

            bridge = AzureVoiceBridge(interim, final, lambda message: schedule(report(message)))
            if settings.VAD_ENABLED:
                vad = await asyncio.to_thread(SileroDetector)
            await bridge.start()
        await send({"type": "connected", "call_id": call, "voice": voice,
                    "message": "Synthetic data only. I am an automated demo, not a clinician."})
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is not None:
                if not bridge or len(data) > 32000 or len(data) % 2:
                    await report("Expected at most one second of 16 kHz mono PCM16 audio.")
                    continue
                bridge.write(data)
                if vad and await asyncio.to_thread(vad.speech_started, data):
                    await interrupt()
            elif message.get("text"):
                try:
                    payload = json.loads(message["text"])
                    if not isinstance(payload, dict):
                        raise ValueError("Expected an object")
                    kind = payload.get("type")
                    if kind == "text" and isinstance(payload.get("text"), str):
                        await submit(payload["text"])
                    elif kind == "interrupt":
                        await interrupt()
                    elif kind == "playback_done" and payload.get("turn_id") == turn_id:
                        current = await memory.load(call)
                        if current and current.get("pending") and current["pending"].get("turn_id") == turn_id:
                            current["pending"]["announced"] = True
                            state = current
                            await memory.save(call, current)
                    else:
                        raise ValueError("Unknown or malformed message")
                except (json.JSONDecodeError, ValueError) as error:
                    await report(str(error))
    except WebSocketDisconnect:
        pass
    except (httpx.HTTPError, RedisError, ValueError, RuntimeError, ImportError) as error:
        await report(f"Call failed: {type(error).__name__}: {error}")
        await ws.close(code=1011)
    finally:
        closing = True
        lease_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await lease_task
        if turn_task and not turn_task.done():
            if committing:
                await turn_task
            else:
                turn_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await turn_task
        for task in list(error_tasks):
            task.cancel()
        if error_tasks:
            await asyncio.gather(*error_tasks, return_exceptions=True)
        if bridge:
            await bridge.close()
        current = await memory.load(call)
        if current:
            await clinic.clear_pending(call, current)
        await redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end",
            1, active_key, owner,
        )
        try:
            await clinic.audit(call, "call_ended", "Call disconnected")
        except httpx.HTTPError:
            logger.exception("Could not persist call-ended audit for %s", call)


async def single_sentence(text: str) -> AsyncIterator[str]:
    yield text
