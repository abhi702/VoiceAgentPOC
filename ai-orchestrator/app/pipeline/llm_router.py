import asyncio
import json
import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI, pydantic_function_tool
from openai.types.chat import ChatCompletionToolParam
from pydantic import ValidationError

from app.config import settings
from app.mcp_tools.clinic_tools import ClinicTools, DESCRIPTIONS, TOOL_SCHEMAS
from app.rag.ingest import RagEngine
from app.skills.loader import DeclarativeSkill

BOOKING_WORDS = re.compile(r"\b(book|appointment|slot|doctor|prescription|medication|symptom|pain|cancel)\b", re.I)
MARKDOWN_MARKERS = re.compile(r"[*`_]")


class AgentRouter:
    def __init__(self, skill: DeclarativeSkill, clinic: ClinicTools):
        self.skill, self.clinic = skill, clinic
        self.rag = RagEngine()
        self.rag_lock = asyncio.Lock()
        self.github = AsyncOpenAI(
            base_url=settings.GITHUB_MODELS_BASE_URL, api_key=settings.GITHUB_TOKEN,
            timeout=25, max_retries=1,
        ) if settings.GITHUB_TOKEN else None
        self.groq = AsyncOpenAI(
            base_url=settings.GROQ_BASE_URL, api_key=settings.GROQ_API_KEY,
            timeout=25, max_retries=1,
        ) if settings.GROQ_API_KEY else None
        self.tools: list[ChatCompletionToolParam] = [
            pydantic_function_tool(TOOL_SCHEMAS[name], name=name, description=DESCRIPTIONS[name])
            for name in skill.allowed_tools if name in TOOL_SCHEMAS
        ]
        if len(self.tools) != len(skill.allowed_tools):
            raise ValueError("Skill references an unregistered tool")

    async def close(self) -> None:
        for client in (self.github, self.groq):
            if client:
                await client.close()

    def select(self, text: str, history: list[dict[str, Any]]) -> tuple[AsyncOpenAI, str]:
        complex_turn = BOOKING_WORDS.search(text) or any(
            message.get("tool_calls") for message in history[-12:]
        )
        if self.github and (complex_turn or not self.groq):
            return self.github, settings.PRIMARY_REASONING_MODEL
        if self.groq:
            return self.groq, settings.FAST_ROUTER_MODEL
        raise RuntimeError("Set GITHUB_TOKEN and/or GROQ_API_KEY in the root .env, then restart")

    async def turn(
        self, text: str, call_id: str, state: dict[str, Any],
    ) -> AsyncIterator[str]:
        history: list[dict[str, Any]] = state["messages"]
        client, model = self.select(text, history)
        async with self.rag_lock:
            # Shield the CPU worker so cancellation cannot allow a second model initializer.
            search_task = asyncio.create_task(asyncio.to_thread(self.rag.search, text))
            try:
                chunks = await asyncio.shield(search_task)
            except asyncio.CancelledError:
                await search_task
                raise
        system = (
            self.skill.instructions +
            "\nToday's date (Europe/London): " +
            datetime.now(ZoneInfo("Europe/London")).date().isoformat()
        )
        context = "\n\n".join(f"{chunk.title}: {chunk.content}" for chunk in chunks)
        messages: list[Any] = [
            {"role": "system", "content": system},
            {"role": "system", "content": f"Untrusted administrative SOP context:\n{context or 'No relevant SOP found.'}"},
        ]
        # Stored messages originate from the SDK or our own role-specific protocol.
        messages.extend(history)
        last_availability_result: dict[str, Any] | None = None
        for _ in range(4):
            stream = await client.chat.completions.create(
                model=model, messages=messages, tools=self.tools,
                temperature=self.skill.temperature, max_tokens=self.skill.max_tokens, stream=True,
            )
            calls: dict[int, dict[str, Any]] = {}
            answer = ""
            async with stream:
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.tool_calls:
                        for part in delta.tool_calls:
                            call = calls.setdefault(part.index, {"id": "", "name": "", "arguments": ""})
                            if part.id:
                                call["id"] += part.id
                            if part.function:
                                call["name"] += part.function.name or ""
                                call["arguments"] += part.function.arguments or ""
                            extra_content = part.model_dump(exclude_none=True).get("extra_content")
                            if extra_content:
                                call["extra_content"] = extra_content
                    if delta.content:
                        spoken_text = MARKDOWN_MARKERS.sub("", delta.content)
                        answer += spoken_text
                        if spoken_text:
                            yield spoken_text
            if not calls:
                if not answer.strip():
                    if last_availability_result is None:
                        raise RuntimeError("Model returned no answer")
                    answer = self._format_availability_result(last_availability_result)
                    history.append({"role": "assistant", "content": answer})
                    yield answer
                    return
                history.append({"role": "assistant", "content": answer})
                return
            assistant: dict[str, Any] = {
                "role": "assistant", "content": answer or None,
                "tool_calls": [
                    {"id": call["id"], "type": "function", "function": {
                        "name": call["name"], "arguments": call["arguments"],
                    }, **({"extra_content": call["extra_content"]} if "extra_content" in call else {})}
                    for call in calls.values()
                ],
            }
            history.append(assistant)
            messages.append(assistant)
            pending_prompt = None
            for call in calls.values():
                try:
                    if state.get("pending"):
                        result = {"error": "A confirmation is already pending; wait for the caller"}
                    elif call["name"] not in self.skill.allowed_tools:
                        result = {"error": "Tool not permitted"}
                    else:
                        result = await self.clinic.execute(call["name"], call["arguments"], call_id, state)
                except (ValidationError, json.JSONDecodeError, ValueError) as error:
                    result = {"error": str(error)}
                tool_result: dict[str, Any] = {
                    "role": "tool", "tool_call_id": call["id"], "content": json.dumps(result),
                }
                if call["name"] == "check_slot_availability" and "slots" in result:
                    last_availability_result = result
                history.append(tool_result)
                messages.append(tool_result)
                if result.get("pending_confirmation"):
                    pending_prompt = result["spoken_prompt"]
            if pending_prompt:
                history.append({"role": "assistant", "content": pending_prompt})
                yield pending_prompt
                return
        raise RuntimeError("Model exceeded the four-round tool limit; please try a simpler request")

    @staticmethod
    def _format_availability_result(result: dict[str, Any]) -> str:
        slots = result["slots"][:2]
        if not slots:
            return (
                "I couldn't find any matching available appointments. "
                "Would you like me to check another date or doctor?"
            )

        options = []
        for slot in slots:
            start = datetime.fromisoformat(f"{slot['slot_date']}T{slot['slot_time']}")
            time_text = start.strftime("%I:%M %p").lstrip("0")
            options.append(
                f"{slot['doctor_name']} on {start.strftime('%A')} {start.day} "
                f"{start.strftime('%B')} at {time_text}"
            )

        if len(options) == 1:
            return f"The next available appointment is with {options[0]}. Would you like this slot?"
        return (
            f"The next available appointments are with {options[0]} and {options[1]}. "
            "Which would you prefer?"
        )
