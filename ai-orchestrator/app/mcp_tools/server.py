"""Optional local stdio MCP adapter; it cannot execute a booking confirmation."""
import json
from uuid import UUID, uuid4

import httpx
from mcp.server.fastmcp import FastMCP
from redis.asyncio import Redis

from app.config import settings
from app.mcp_tools.clinic_tools import ClinicTools
from app.pipeline.memory import SessionMemory

mcp = FastMCP("Local synthetic clinic")


async def invoke(name: str, call_id: str, token: str, arguments: dict) -> str:
    UUID(call_id)
    async with Redis.from_url(settings.REDIS_URL, decode_responses=True) as redis:
        memory = SessionMemory(redis)
        state = await memory.authorize(call_id, token)
        if not state:
            raise ValueError("Invalid active call credentials")
        owner = str(uuid4())
        if not await redis.set(f"voice:active:{call_id}", owner, nx=True, ex=60):
            raise ValueError("Call is active in the browser; disconnect before using MCP")
        try:
            async with httpx.AsyncClient(
                base_url=settings.NESTJS_BACKEND_URL, timeout=15,
                headers={"x-clinic-token": settings.CLINIC_API_TOKEN},
            ) as client:
                result = await ClinicTools(client, redis, memory).execute(name, json.dumps(arguments), call_id, state)
                await memory.save(call_id, state)
                return json.dumps(result)
        finally:
            await redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1, f"voice:active:{call_id}", owner,
            )


@mcp.tool()
async def check_slot_availability(call_id: str, token: str, date: str | None = None, doctor: str | None = None) -> str:
    return await invoke("check_slot_availability", call_id, token, {"date": date, "doctor": doctor})


@mcp.tool()
async def book_appointment(call_id: str, token: str, slot_id: str, patient_name: str, patient_dob: str) -> str:
    return await invoke("book_appointment", call_id, token, {
        "slot_id": slot_id, "patient_name": patient_name, "patient_dob": patient_dob,
    })


@mcp.tool()
async def check_prescription_status(call_id: str, token: str, patient_name: str, patient_dob: str) -> str:
    return await invoke("check_prescription_status", call_id, token, {
        "patient_name": patient_name, "patient_dob": patient_dob,
    })


@mcp.tool()
async def request_repeat_prescription(
    call_id: str, token: str, patient_name: str, patient_dob: str, medication: str,
) -> str:
    return await invoke("request_repeat_prescription", call_id, token, {
        "patient_name": patient_name, "patient_dob": patient_dob, "medication": medication,
    })


if __name__ == "__main__":
    mcp.run(transport="stdio")
