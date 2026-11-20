from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )
    APP_ENV: str = "development"
    ALLOWED_ORIGIN: str = "http://localhost:3000"
    NESTJS_BACKEND_URL: str = "http://localhost:3001"
    CLINIC_API_TOKEN: str
    REDIS_URL: str = "redis://localhost:6379/0"
    AZURE_SPEECH_KEY: str = ""
    AZURE_SPEECH_REGION: str = "uksouth"
    AZURE_SPEECH_LOCALE: str = "en-GB"
    AZURE_TTS_VOICE: str = "en-GB-SoniaNeural"
    VAD_ENABLED: bool = True
    GITHUB_TOKEN: str = ""
    GITHUB_MODELS_BASE_URL: str = "https://models.github.ai/inference"
    PRIMARY_REASONING_MODEL: str = "openai/gpt-4o"
    GROQ_API_KEY: str = ""
    GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
    FAST_ROUTER_MODEL: str = "llama-3.3-70b-versatile"
    RAG_MODE: Literal["local", "azure", "off"] = "local"
    EMBEDDING_MODEL: str = "BAAI/bge-small-en-v1.5"
    AZURE_SEARCH_ENDPOINT: str = ""
    AZURE_SEARCH_API_KEY: str = ""
    AZURE_SEARCH_INDEX_NAME: str = "clinic-sop-index"


settings = Settings()
