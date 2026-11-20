# Healthcare Voice & Triage Agent: Technical Implementation Specification & Copilot Build Guide

This specification defines the complete architecture, directory structure, implementation contracts, and step-by-step build instructions for an **NHS Primary Care Voice & Triage AI Receptionist**. Drop this file into the root of a VS Code workspace (as `SPEC.md` or `.github/copilot-instructions.md`) and use the phase prompts with **GitHub Copilot Chat / Copilot Edits** to generate and wire the codebase.

## 1. System Architecture & Technology Stack

| Layer | Technology | Implementation Role |
| :--- | :--- | :--- |
| **Voice Orchestrator (Core)** | Python 3.12, `uv`, FastAPI, `asyncio`, WebSockets, Pydantic V2 | Manages full-duplex audio WebSockets, pre-LLM clinical safety guardrails, sentence-boundary streaming (`<800ms` TTFA), and barge-in cancellation. |
| **Speech Engine (STT / TTS / VAD)** | Azure Cognitive Services Speech SDK (`F0` Free Tier), Silero VAD | Streams 16kHz PCM audio via `PushAudioInputStream`, applies `PhraseListGrammar` for clinical terms, detects turn endings/interruptions, and synthesizes `en-GB-SoniaNeural` audio. |
| **Multi-LLM Router** | OpenAI Python SDK (`AsyncOpenAI`) | Routes fast intent/slot extraction turns to `llama-3.3-70b-versatile` (Groq Free API) and complex multi-symptom triage or tool calls to `gpt-4o` / `Mistral` (GitHub Models Azure Endpoint). |
| **Hybrid RAG Engine** | Azure AI Search (`F` Free Tier), `sentence-transformers` (`BAAI/bge-small-en-v1.5`) | Indexes clinic SOP Markdown files, runs dense vector + sparse BM25 queries, and merges results using pure-Python Reciprocal Rank Fusion (RRF). |
| **Governed Tool Server** | `FastMCP` (Model Context Protocol), Pydantic | Exposes strongly typed tools (`check_slot_availability`, `book_appointment`, `request_repeat_prescription`) with strict schema validation and audit logging. |
| **Clinic Transactional API** | NestJS (TypeScript), `mysql2` / TypeORM | Handles ACID appointment bookings using MySQL `SELECT ... FOR UPDATE` locking and streams live updates to the clinic dashboard. |
| **Caller & Receptionist UI** | Next.js 14 (React, TypeScript, Tailwind CSS, TanStack Query) | Split-screen web interface: left panel simulates the patient web phone (mic capture + waveform + barge-in); right panel displays live transcripts, latency metrics, and bookings. |
| **Persistence & Speed Layer** | MySQL 8.4, Redis 7 (Docker Compose) | Redis stores active call state (`<2ms` reads), conversation sliding windows, and 30-second soft slot holds; MySQL stores appointments, patients, and call audit logs. |

## 2. Target Repository Structure

```text
healthcare-voice-agent/
├── docker-compose.yml
├── .env.example
├── infra/
│   └── mysql/
│       └── init.sql
├── ai-orchestrator/                  # Python 3.12 + FastAPI + uv
│   ├── pyproject.toml
│   ├── Dockerfile
│   └── app/
│       ├── __init__.py
│       ├── config.py                 # Pydantic BaseSettings (.env loader)
│       ├── main.py                   # FastAPI app, /ws/voice WebSocket & /api/telemetry SSE
│       ├── skills/
│       │   ├── loader.py             # Parses YAML frontmatter + Markdown instructions
│       │   └── gp_triage_skill.md    # Declarative voice prompt & clinical triage rules
│       ├── speech/
│       │   └── azure_speech.py       # PushAudioInputStream STT, PhraseList & Neural TTS
│       ├── pipeline/
│       │   ├── guardrails.py         # Deterministic pre-LLM NHS red-flag detector
│       │   ├── sentence_chunker.py   # Async regex sentence-boundary streamer (<800ms TTFA)
│       │   ├── memory.py             # Redis session state & sliding-window history
│       │   └── llm_router.py         # Tiered routing (Groq LLaMA-3 vs GitHub Models GPT-4o)
│       ├── rag/
│       │   ├── ingest.py             # Header-aware Markdown chunker & indexer
│       │   └── hybrid_rrf.py         # Vector + BM25 search with Reciprocal Rank Fusion
│       └── mcp_tools/
│           └── clinic_tools.py       # Tool schemas, Redis soft-lock & NestJS API client
├── clinic-backend-nestjs/            # NestJS + MySQL transactional API
│   ├── package.json
│   ├── Dockerfile
│   └── src/
│       ├── main.ts
│       ├── app.module.ts
│       └── bookings/
│           ├── bookings.controller.ts
│           └── bookings.service.ts   # MySQL SELECT ... FOR UPDATE locking
└── clinic-dashboard-nextjs/          # Next.js Split-Screen Phone + Receptionist Portal
    ├── package.json
    └── src/
        └── app/
            └── page.tsx
```

## 3. Core Python Voice Orchestrator Implementation (`ai-orchestrator/`)

### 3.1 Project Dependencies (`ai-orchestrator/pyproject.toml`)

Initialize the Python project using `uv` (or `pip`):

```toml
[project]
name = "ai-orchestrator"
version = "0.1.0"
description = "Low-latency Healthcare Voice & RAG Orchestrator"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.30.0",
    "websockets>=13.0",
    "openai>=1.50.0",
    "pydantic>=2.9.0",
    "pydantic-settings>=2.5.0",
    "azure-cognitiveservices-speech>=1.40.0",
    "azure-search-documents>=11.5.0",
    "redis>=5.1.0",
    "httpx>=0.27.0",
    "pyyaml>=6.0.2",
    "tenacity>=9.0.0",
    "sentence-transformers>=3.1.0",
]
```

### 3.2 Typed Configuration Loader (`ai-orchestrator/app/config.py`)

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="../.env", env_file_encoding="utf-8", extra="ignore"
    )

    # Application & Server
    APP_ENV: str = "development"
    ORCHESTRATOR_PORT: int = 8000
    NESTJS_BACKEND_URL: str = "http://localhost:3001"

    # Azure AI Speech (Cognitive Services F0 Free Tier)
    AZURE_SPEECH_KEY: str
    AZURE_SPEECH_REGION: str = "uksouth"
    AZURE_SPEECH_LOCALE: str = "en-GB"
    AZURE_TTS_VOICE: str = "en-GB-SoniaNeural"

    # Multi-LLM Endpoints (GitHub Models on Azure + Groq Free Tier)
    GITHUB_MODELS_BASE_URL: str = "https://models.inference.ai.azure.com"
    GITHUB_TOKEN: str
    PRIMARY_REASONING_MODEL: str = "gpt-4o"

    GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
    GROQ_API_KEY: str
    FAST_ROUTER_MODEL: str = "llama-3.3-70b-versatile"

    # Azure AI Search (Free Tier F)
    AZURE_SEARCH_ENDPOINT: str = ""
    AZURE_SEARCH_API_KEY: str = ""
    AZURE_SEARCH_INDEX_NAME: str = "clinic-sop-index"

    # Redis & MySQL
    REDIS_URL: str = "redis://localhost:6379/0"


settings = Settings()
```

### 3.3 Declarative Markdown Skill & Loader (`ai-orchestrator/app/skills/`)

Create `ai-orchestrator/app/skills/gp_triage_skill.md`:

```markdown
---
name: gp_reception_voice_triage
version: 1.0.0
temperature: 0.2
max_tokens: 220
allowed_tools:
  - check_slot_availability
  - book_appointment
  - check_prescription_status
---
# System Persona & Voice Formatting Rules
You are an automated NHS GP Surgery Voice Receptionist. Your responses are converted directly into speech over a telephone line.
1. Speak in warm, concise British English.
2. Never output Markdown, bullet points, numbered lists, asterisks, or tables.
3. Keep every response to at most 2 short sentences (under 35 words total).
4. Offer at most two appointment slot times at once.
5. Before calling `book_appointment`, always repeat the patient's full name, date of birth, and chosen date/time back to them and wait for an explicit "yes".
```

Create `ai-orchestrator/app/skills/loader.py`:

```python
from pathlib import Path
from typing import Any
import yaml
from pydantic import BaseModel


class DeclarativeSkill(BaseModel):
    name: str
    version: str
    temperature: float = 0.2
    max_tokens: int = 220
    allowed_tools: list[str]
    instructions: str


def load_markdown_skill(skill_path: Path) -> DeclarativeSkill:
    raw_text = skill_path.read_text(encoding="utf-8")
    parts = raw_text.split("---", maxsplit=2)
    if len(parts) < 3:
        raise ValueError(f"Skill file {skill_path} is missing YAML frontmatter.")
    frontmatter: dict[str, Any] = yaml.safe_load(parts[1])
    body = parts[2].strip()
    return DeclarativeSkill(**frontmatter, instructions=body)
```

### 3.4 Deterministic Pre-LLM Clinical Red-Flag Guardrail (`ai-orchestrator/app/pipeline/guardrails.py`)

```python
import re
from dataclasses import dataclass

RED_FLAG_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "CARDIAC_CHEST_PAIN",
        re.compile(
            r"\b(chest\s+pain|crushing\s+pain|tightness\s+in\s+my\s+chest|heart\s+attack)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "STROKE_FAST_SIGNS",
        re.compile(
            r"\b(face\s+droop|slurred\s+speech|arm\s+weakness|having\s+a\s+stroke|one\s+side\s+numb)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "SEVERE_RESPIRATORY_DISTRESS",
        re.compile(
            r"\b(can't\s+breathe|cannot\s+breathe|gasping\s+for\s+air|choking|lips\s+turning\s+blue)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "SEVERE_HEMORRHAGE_OR_CRISIS",
        re.compile(
            r"\b(uncontrolled\s+bleeding|coughing\s+up\s+blood|unconscious|seizure)\b",
            re.IGNORECASE,
        ),
    ),
]

EMERGENCY_VOICE_SCRIPT = (
    "Please stop and listen carefully. Because you mentioned symptoms that may be a medical emergency, "
    "I cannot book a routine appointment. Please hang up immediately and dial 9 9 9, or go to your nearest A and E department."
)


@dataclass
class GuardrailResult:
    triggered: bool
    category: str | None = None
    spoken_override: str | None = None


def evaluate_clinical_red_flags(transcript: str) -> GuardrailResult:
    """Runs in <2ms before any LLM call to enforce NHS DCB0129 clinical safety."""
    for category, pattern in RED_FLAG_PATTERNS:
        if pattern.search(transcript):
            return GuardrailResult(
                triggered=True,
                category=category,
                spoken_override=EMERGENCY_VOICE_SCRIPT,
            )
    return GuardrailResult(triggered=False)
```

### 3.5 Sub-800ms Sentence-Boundary Token Chunker (`ai-orchestrator/app/pipeline/sentence_chunker.py`)

```python
import re
from typing import AsyncGenerator

SENTENCE_BOUNDARY_REGEX = re.compile(r"(?<=[.?!:])\s+")


async def stream_sentences_from_tokens(
    token_generator: AsyncGenerator[str, None],
    min_chars: int = 18,
) -> AsyncGenerator[str, None]:
    """
    Buffers incoming LLM tokens and yields complete spoken sentences immediately
    so Azure Neural TTS starts speaking sentence #1 while the LLM generates sentence #2.
    """
    buffer = ""
    async for token in token_generator:
        buffer += token
        parts = SENTENCE_BOUNDARY_REGEX.split(buffer)
        if len(parts) > 1:
            for sentence in parts[:-1]:
                cleaned = sentence.strip()
                if len(cleaned) >= min_chars:
                    yield cleaned
                else:
                    # Prepend very short fragments (e.g. "Dr.") to the next part
                    parts[-1] = f"{cleaned} {parts[-1]}"
            buffer = parts[-1]

    if buffer.strip():
        yield buffer.strip()
```

### 3.6 Pure-Python Hybrid RRF RAG Engine (`ai-orchestrator/app/rag/hybrid_rrf.py`)

```python
from dataclasses import dataclass


@dataclass
class RetrievedChunk:
    doc_id: str
    title: str
    content: str
    score: float = 0.0


def reciprocal_rank_fusion(
    vector_ranked: list[RetrievedChunk],
    bm25_ranked: list[RetrievedChunk],
    k: int = 60,
    top_n: int = 3,
) -> list[RetrievedChunk]:
    """Merges dense vector and sparse BM25 rankings using RRF: score = sum(1 / (k + rank))."""
    rrf_scores: dict[str, float] = {}
    chunk_lookup: dict[str, RetrievedChunk] = {}

    for rank, chunk in enumerate(vector_ranked, start=1):
        rrf_scores[chunk.doc_id] = rrf_scores.get(chunk.doc_id, 0.0) + (1.0 / (k + rank))
        chunk_lookup[chunk.doc_id] = chunk

    for rank, chunk in enumerate(bm25_ranked, start=1):
        rrf_scores[chunk.doc_id] = rrf_scores.get(chunk.doc_id, 0.0) + (1.0 / (k + rank))
        chunk_lookup[chunk.doc_id] = chunk

    sorted_ids = sorted(rrf_scores.keys(), key=lambda d_id: rrf_scores[d_id], reverse=True)
    results: list[RetrievedChunk] = []
    for doc_id in sorted_ids[:top_n]:
        item = chunk_lookup[doc_id]
        item.score = round(rrf_scores[doc_id], 5)
        results.append(item)
    return results
```

### 3.7 Multi-LLM Router & Governed Tool Loop (`ai-orchestrator/app/pipeline/llm_router.py`)

```python
import asyncio
import json
from typing import Any, AsyncGenerator
from openai import AsyncOpenAI, pydantic_function_tool
from pydantic import BaseModel, Field
from app.config import settings
from app.mcp_tools.clinic_tools import TOOL_REGISTRY, CheckSlotsInput, BookAppointmentInput

# Client 1: Groq Cloud Free API for sub-200ms LLaMA-3 turns
groq_client = AsyncOpenAI(
    base_url=settings.GROQ_BASE_URL,
    api_key=settings.GROQ_API_KEY,
)

# Client 2: GitHub Models on Azure for complex GPT-4o / Mistral reasoning & tool calls
github_azure_client = AsyncOpenAI(
    base_url=settings.GITHUB_MODELS_BASE_URL,
    api_key=settings.GITHUB_TOKEN,
)

TOOLS = [
    pydantic_function_tool(
        CheckSlotsInput,
        name="check_slot_availability",
        description="Check open GP appointment slots for a given date and doctor.",
    ),
    pydantic_function_tool(
        BookAppointmentInput,
        name="book_appointment",
        description="Book a confirmed GP appointment slot after explicit patient verification.",
    ),
]


def select_client_and_model(user_text: str) -> tuple[AsyncOpenAI, str]:
    """Routes simple greetings/FAQs to fast LLaMA-3 on Groq; complex bookings to GPT-4o on Azure."""
    booking_keywords = ("book", "appointment", "schedule", "slot", "doctor", "dr", "cancel", "prescription")
    if any(word in user_text.lower() for word in booking_keywords):
        return github_azure_client, settings.PRIMARY_REASONING_MODEL
    return groq_client, settings.FAST_ROUTER_MODEL


async def run_agent_streaming_turn(
    user_text: str,
    messages: list[dict[str, Any]],
) -> AsyncGenerator[str, None]:
    messages.append({"role": "user", "content": user_text})
    client, model_name = select_client_and_model(user_text)

    response = await client.chat.completions.create(
        model=model_name,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
        temperature=0.2,
    )
    assistant_msg = response.choices[0].message
    messages.append(assistant_msg.model_dump(exclude_none=True))

    if assistant_msg.tool_calls:
        # Emit immediate spoken latency-masking filler before awaiting external tool I/O
        yield "Let me check the surgery schedule for you right now, just a moment."

        async def execute_one_tool(tc: Any) -> dict[str, str]:
            fn_name = tc.function.name
            fn = TOOL_REGISTRY[fn_name]
            raw_args = json.loads(tc.function.arguments)
            result = await fn(**raw_args)
            return {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result),
            }

        tool_outputs = await asyncio.gather(*(execute_one_tool(tc) for tc in assistant_msg.tool_calls))
        messages.extend(tool_outputs)

    # Stream the final spoken answer token-by-token
    stream = await client.chat.completions.create(
        model=model_name,
        messages=messages,
        stream=True,
        temperature=0.2,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            yield delta
```

### 3.8 Azure AI Speech Streaming & Clinical Phrase List (`ai-orchestrator/app/speech/azure_speech.py`)

```python
import asyncio
import azure.cognitiveservices.speech as speechsdk
from app.config import settings

CLINICAL_PHRASE_HINTS = [
    "Amoxicillin",
    "Atorvastatin",
    "Metformin",
    "Omeprazole",
    "Salbutamol",
    "Dr. Patel",
    "Dr. Smith",
    "NHS Number",
    "Repeat Prescription",
]


class AzureVoiceBridge:
    def __init__(self, loop: asyncio.AbstractEventLoop, on_interim_speech, on_final_speech):
        self.loop = loop
        self.speech_config = speechsdk.SpeechConfig(
            subscription=settings.AZURE_SPEECH_KEY,
            region=settings.AZURE_SPEECH_REGION,
        )
        self.speech_config.speech_recognition_language = settings.AZURE_SPEECH_LOCALE
        self.speech_config.speech_synthesis_voice_name = settings.AZURE_TTS_VOICE

        # 16kHz 16-bit mono PCM push stream from browser WebSocket
        stream_format = speechsdk.audio.AudioStreamFormat(
            samples_per_second=16000, bits_per_sample=16, channels=1
        )
        self.push_stream = speechsdk.audio.PushAudioInputStream(stream_format=stream_format)
        audio_config = speechsdk.audio.AudioConfig(stream=self.push_stream)
        self.recognizer = speechsdk.SpeechRecognizer(
            speech_config=self.speech_config, audio_config=audio_config
        )

        # Boost clinical & surgery terms at runtime
        phrase_list = speechsdk.PhraseListGrammar.from_recognizer(self.recognizer)
        for phrase in CLINICAL_PHRASE_HINTS:
            phrase_list.addPhrase(phrase)

        self.recognizer.recognizing.connect(
            lambda evt: self.loop.call_soon_threadsafe(on_interim_speech, evt.result.text)
        )
        self.recognizer.recognized.connect(
            lambda evt: self.loop.call_soon_threadsafe(on_final_speech, evt.result.text)
            if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech
            else None
        )

    def start(self) -> None:
        self.recognizer.start_continuous_recognition_async()

    def write_audio_chunk(self, pcm_bytes: bytes) -> None:
        self.push_stream.write(pcm_bytes)

    def stop(self) -> None:
        self.push_stream.close()
        self.recognizer.stop_continuous_recognition_async()

    def synthesize_sentence_to_pcm(self, sentence: str) -> bytes:
        synthesizer = speechsdk.SpeechSynthesizer(
            speech_config=self.speech_config, audio_config=None
        )
        result = synthesizer.speak_text_async(sentence).get()
        return result.audio_data
```

## 4. Database Schema & Concurrency Locking (`infra/mysql/init.sql`)

```sql
CREATE DATABASE IF NOT EXISTS gp_clinic;
USE gp_clinic;

CREATE TABLE IF NOT EXISTS appointment_slots (
    slot_id VARCHAR(36) PRIMARY KEY,
    doctor_name VARCHAR(100) NOT NULL,
    slot_date DATE NOT NULL,
    slot_time VARCHAR(20) NOT NULL,
    status ENUM('AVAILABLE', 'BOOKED') DEFAULT 'AVAILABLE',
    patient_name VARCHAR(120) NULL,
    patient_dob VARCHAR(20) NULL,
    booked_at TIMESTAMP NULL,
    INDEX idx_date_status (slot_date, status)
);

INSERT INTO appointment_slots (slot_id, doctor_name, slot_date, slot_time, status) VALUES
('SLOT-101', 'Dr. Patel', '2026-10-27', '09:30 AM', 'AVAILABLE'),
('SLOT-102', 'Dr. Patel', '2026-10-27', '11:00 AM', 'AVAILABLE'),
('SLOT-103', 'Dr. Smith', '2026-10-27', '02:15 PM', 'AVAILABLE');
```

In `clinic-backend-nestjs/src/bookings/bookings.service.ts`, implement the transactional booking with `SELECT ... FOR UPDATE`:

```typescript
// Inside NestJS BookingsService.bookSlot(slotId, patientName, patientDob):
// 1. Start transaction on MySQL connection pool
// 2. Run: SELECT status FROM appointment_slots WHERE slot_id = ? FOR UPDATE NOWAIT
// 3. Verify status === 'AVAILABLE'; if not, rollback and throw ConflictException
// 4. Run: UPDATE appointment_slots SET status = 'BOOKED', patient_name = ?, patient_dob = ?, booked_at = NOW() WHERE slot_id = ?
// 5. Commit transaction and emit SSE update to Next.js dashboard
```

## 5. Step-by-Step Prompts for GitHub Copilot in VS Code

Copy and run these 4 prompts sequentially inside **GitHub Copilot Edits / Agent Mode** in VS Code:

* **Copilot Prompt 1 (Infrastructure & Config):**
  > *"Read `SPEC.md`. Create `docker-compose.yml` running `mysql:8.4` (mounting `infra/mysql/init.sql` on port 3306) and `redis:7-alpine` (on port 6379). Also create `ai-orchestrator/pyproject.toml` and `ai-orchestrator/app/config.py` using `pydantic-settings`."*
* **Copilot Prompt 2 (Voice Pipeline, Guardrails & Tools):**
  > *"Read `SPEC.md` Section 3. Generate `ai-orchestrator/app/skills/gp_triage_skill.md`, `loader.py`, `guardrails.py`, `sentence_chunker.py`, `hybrid_rrf.py`, `clinic_tools.py` (with Redis 30-second soft-locks `SET slot_lock NX EX 30` and `httpx` calls to NestJS), and `llm_router.py`."*
* **Copilot Prompt 3 (FastAPI WebSocket Server & Azure Speech):**
  > *"Create `ai-orchestrator/app/speech/azure_speech.py` and `ai-orchestrator/app/main.py`. Expose `/ws/voice` for full-duplex WebSocket streaming (handling binary PCM audio chunks in, barge-in task cancellation when `on_interim_speech` detects >2 words, and streaming synthesized sentence audio out) plus `/api/telemetry` SSE for live latency metrics."*
* **Copilot Prompt 4 (NestJS Booking Service & Next.js Split-Screen UI):**
  > *"Generate `clinic-backend-nestjs` with `/bookings/available` and `/bookings/confirm` using MySQL `SELECT ... FOR UPDATE` transactions, and `clinic-dashboard-nextjs` with a split-screen UI: left side captures browser microphone 16kHz PCM audio over `/ws/voice`, right side uses TanStack Query to show live transcripts, TTFA latency badges, red-flag alerts, and booked MySQL slots."*

## 6. Environment Variables Template (`.env`)

Create a `.env` file in the root folder (`healthcare-voice-agent/.env`) and populate these keys before starting the services:

```ini
# ============================================================================
# 1. APPLICATION & SERVER PORTS
# ============================================================================
APP_ENV=development
ORCHESTRATOR_PORT=8000
NESTJS_BACKEND_URL=http://localhost:3001
NEXT_PUBLIC_ORCHESTRATOR_WS_URL=ws://localhost:8000/ws/voice
NEXT_PUBLIC_NESTJS_API_URL=http://localhost:3001

# ============================================================================
# 2. AZURE COGNITIVE SERVICES SPEECH (Free Tier F0 - 5 hrs STT & 0.5M TTS/mo)
# Create in Azure Portal -> Speech Services -> Pricing Tier: Free F0
# ============================================================================
AZURE_SPEECH_KEY=your_azure_speech_f0_key_here
AZURE_SPEECH_REGION=uksouth
AZURE_SPEECH_LOCALE=en-GB
AZURE_TTS_VOICE=en-GB-SoniaNeural

# ============================================================================
# 3. MULTI-LLM ENDPOINTS (100% Free APIs via OpenAI Python SDK)
# GitHub Models: Create a classic/fine-grained GitHub PAT (no paid scope needed)
# Groq Cloud: Create a free API key at https://console.groq.com/keys
# ============================================================================
GITHUB_MODELS_BASE_URL=https://models.inference.ai.azure.com
GITHUB_TOKEN=ghp_your_github_personal_access_token_here
PRIMARY_REASONING_MODEL=gpt-4o

GROQ_BASE_URL=https://api.groq.com/openai/v1
GROQ_API_KEY=gsk_your_free_groq_api_key_here
FAST_ROUTER_MODEL=llama-3.3-70b-versatile

# ============================================================================
# 4. AZURE AI SEARCH (Optional - Free Tier 'F' for Hybrid Vector + BM25 RAG)
# Leave blank to fall back to local in-memory RRF over Markdown SOP files
# ============================================================================
AZURE_SEARCH_ENDPOINT=https://your-free-search-service.search.windows.net
AZURE_SEARCH_API_KEY=your_azure_search_admin_or_query_key_here
AZURE_SEARCH_INDEX_NAME=clinic-sop-index

# ============================================================================
# 5. LOCAL DOCKER INFRASTRUCTURE (MySQL 8.4 & Redis 7)
# ============================================================================
REDIS_URL=redis://localhost:6379/0
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=clinic_user
MYSQL_PASSWORD=clinic_pass_local
MYSQL_DATABASE=gp_clinic
```
