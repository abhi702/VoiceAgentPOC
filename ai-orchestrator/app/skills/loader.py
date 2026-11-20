from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class DeclarativeSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    version: str
    temperature: float = Field(ge=0, le=1)
    max_tokens: int = Field(gt=0, le=500)
    allowed_tools: list[str]
    instructions: str


def load_markdown_skill(path: Path) -> DeclarativeSkill:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise ValueError(f"Missing skill frontmatter: {path.name}")
    parts = raw.split("---", 2)
    if len(parts) != 3:
        raise ValueError(f"Invalid skill frontmatter: {path.name}")
    metadata = yaml.safe_load(parts[1])
    if not isinstance(metadata, dict):
        raise ValueError("Skill metadata must be a mapping")
    return DeclarativeSkill(**metadata, instructions=parts[2].strip())
