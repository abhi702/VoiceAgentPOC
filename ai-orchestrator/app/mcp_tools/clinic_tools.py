import re
from datetime import date, datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from redis.asyncio import Redis

from app.pipeline.memory import SessionMemory


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckSlotsInput(ToolInput):
    date: str | None = Field(default=None, description="ISO YYYY-MM-DD; omit for next available")
    doctor: str | None = Field(default=None, max_length=100)

    @field_validator("date")
    @classmethod
    def valid_date(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = date.fromisoformat(value)
            if value != parsed.isoformat():
                raise ValueError("Use YYYY-MM-DD")
        return value

    @field_validator("doctor")
    @classmethod
    def normalize_doctor_preference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if value.casefold() in {
            "", "any", "any doctor", "any doctors", "all", "all doctors",
            "no preference", "either",
        }:
            return None
        return value


class IdentityInput(ToolInput):
    patient_name: str = Field(min_length=2, max_length=120)
    patient_dob: str = Field(description="Date of birth YYYY-MM-DD")

    @field_validator("patient_name")
    @classmethod
    def full_name(cls, value: str) -> str:
        if len(value.split()) < 2:
            raise ValueError("Provide the full patient name")
        return value.strip()

    @field_validator("patient_dob")
    @classmethod
    def valid_dob(cls, value: str) -> str:
        parsed = date.fromisoformat(value)
        if value != parsed.isoformat() or parsed > date.today():
            raise ValueError("Date of birth must be a past date in YYYY-MM-DD format")
        return value


class BookAppointmentInput(IdentityInput):
    slot_id: str = Field(min_length=1, max_length=36)


class PrescriptionInput(IdentityInput):
    medication: str = Field(min_length=1, max_length=120)


TOOL_SCHEMAS: dict[str, type[ToolInput]] = {
    "check_slot_availability": CheckSlotsInput,
    "book_appointment": BookAppointmentInput,
    "check_prescription_status": IdentityInput,
    "request_repeat_prescription": PrescriptionInput,
}
DESCRIPTIONS = {
    "check_slot_availability": "Read real available slots. Offer at most two.",
    "book_appointment": "Prepare a booking; does not book until server receives confirmation next turn.",
    "check_prescription_status": "Read synthetic prescription status after full name and DOB.",
    "request_repeat_prescription": "Prepare an existing-medication repeat request; requires next-turn confirmation.",
}


class ClinicTools:
    def __init__(self, client: httpx.AsyncClient, redis: Redis, memory: SessionMemory):
        self.client, self.redis, self.memory = client, redis, memory

    async def audit(self, call_id: str, event_type: str, summary: str) -> None:
        response = await self.client.post("/audit", json={
            "call_id": call_id, "event_type": event_type, "summary": summary[:2000],
        })
        response.raise_for_status()

    async def release_hold(self, pending: dict[str, Any] | None) -> None:
        if pending and pending["kind"] == "booking":
            await self.redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1, f"voice:slot:{pending['input']['slot_id']}", pending["hold_token"],
            )

    async def clear_pending(self, call_id: str, state: dict[str, Any]) -> None:
        await self.release_hold(state.get("pending"))
        state["pending"] = None
        await self.memory.save(call_id, state)

    async def execute(
        self, name: str, raw: str, call_id: str, state: dict[str, Any],
    ) -> dict[str, Any]:
        if name not in TOOL_SCHEMAS:
            raise ValueError("Tool is not allowed")
        payload = TOOL_SCHEMAS[name].model_validate_json(raw).model_dump()
        await self.audit(call_id, "tool", name)
        if state["emergency"]:
            return {"error": "Emergency override active; no routine transactions allowed"}
        if name == "check_slot_availability":
            requested_date = payload.get("date")
            latest_user_message = next(
                (
                    message.get("content", "")
                    for message in reversed(state["messages"])
                    if message.get("role") == "user"
                ),
                "",
            )
            if requested_date and date.fromisoformat(requested_date) < datetime.now(
                ZoneInfo("Europe/London")
            ).date():
                if re.search(r"\b\d{4}\b", latest_user_message):
                    return {
                        "error": (
                            "That availability date has passed. Ask for a future date, "
                            "or omit the date to check upcoming slots."
                        )
                    }
                payload["date"] = None
            params = {key: value for key, value in payload.items() if value is not None}
            response = await self.client.get("/bookings/available", params=params)
            response.raise_for_status()
            state["offered_slots"] = response.json()
            return {"slots": state["offered_slots"][:2]}
        if name == "check_prescription_status":
            response = await self.client.post("/prescriptions/status", json=payload)
            response.raise_for_status()
            return {"prescriptions": response.json()}

        await self.clear_pending(call_id, state)
        pending = {"input": payload, "idempotency_key": str(uuid4())}
        if name == "book_appointment":
            slot = next(
                (slot for slot in state.get("offered_slots", [])[:2] if slot["slot_id"] == payload["slot_id"]),
                None,
            )
            if not slot:
                return {"error": "Check availability and choose one of the offered slots first"}
            hold_token = str(uuid4())
            held = await self.redis.set(f"voice:slot:{payload['slot_id']}", hold_token, nx=True, ex=30)
            if not held:
                return {"error": "Another caller is considering this slot; check alternatives"}
            pending.update(kind="booking", hold_token=hold_token)
            prompt = (
                f"Please confirm: {payload['patient_name']}, date of birth {payload['patient_dob']}, "
                f"with {slot['doctor_name']} on {slot['slot_date']} at {slot['slot_time'][:5]}. "
                "Say yes to book, or no to cancel."
            )
        else:
            pending.update(kind="prescription")
            prompt = (
                f"Please confirm a repeat request for {payload['medication']} for "
                f"{payload['patient_name']}, date of birth {payload['patient_dob']}. "
                "Say yes to submit, or no to cancel."
            )
        pending["prompt"] = prompt
        pending["announced"] = False
        state["pending"] = pending
        await self.memory.save(call_id, state)
        return {"pending_confirmation": True, "spoken_prompt": prompt}

    async def confirm(self, call_id: str, state: dict[str, Any]) -> str:
        pending = state.get("pending")
        if not pending or not pending.get("announced"):
            return "Please let me repeat the details before you confirm."
        if state["emergency"]:
            raise ValueError("Emergency calls cannot confirm routine requests")
        if pending["kind"] == "booking":
            owner = await self.redis.get(f"voice:slot:{pending['input']['slot_id']}")
            if owner != pending["hold_token"] and not pending.get("attempted"):
                await self.clear_pending(call_id, state)
                return "The temporary slot hold expired; nothing was booked. Please check availability again."
            endpoint = "/bookings/confirm"
        else:
            endpoint = "/prescriptions/request"
        # Preserve the same pending ID on timeouts: a retry must not create a second mutation.
        pending["attempted"] = True
        await self.memory.save(call_id, state)
        response = await self.client.post(endpoint, json={
            **pending["input"], "call_id": call_id, "idempotency_key": pending["idempotency_key"],
        })
        response.raise_for_status()
        await self.clear_pending(call_id, state)
        return (
            "Your appointment is booked in the demonstration clinic."
            if pending["kind"] == "booking" else
            "Your repeat prescription request was submitted for review; it is not clinical approval."
        )
