import json
import secrets
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis

TTL = 3600


class SessionMemory:
    def __init__(self, redis: Redis):
        self.redis = redis

    def key(self, call_id: str) -> str:
        return f"voice:call:{call_id}"

    async def create(self) -> dict[str, str]:
        call_id, token = str(uuid4()), secrets.token_urlsafe(32)
        await self.save(call_id, {"token": token, "messages": [], "pending": None, "emergency": False})
        return {"call_id": call_id, "token": token}

    async def load(self, call_id: str) -> dict[str, Any] | None:
        value = await self.redis.get(self.key(call_id))
        return json.loads(value) if value else None

    async def save(self, call_id: str, state: dict[str, Any]) -> None:
        # The sliding window drops complete user turns, never orphaning tool messages.
        messages = list(state["messages"])
        while len(messages) > 50:
            next_user = next(
                (i for i in range(1, len(messages)) if messages[i]["role"] == "user"), None
            )
            if next_user is None:
                break
            del messages[:next_user]
        await self.redis.set(self.key(call_id), json.dumps({**state, "messages": messages}), ex=TTL)

    async def authorize(self, call_id: str, token: str) -> dict[str, Any] | None:
        state = await self.load(call_id)
        if state and secrets.compare_digest(state["token"], token):
            return state
        return None
