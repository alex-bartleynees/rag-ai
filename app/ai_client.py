import asyncio
import logging

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

_ai_client: AsyncOpenAI | None = None


async def init_ai_client():
    global _ai_client
    _ai_client = AsyncOpenAI(
        base_url=settings.llm_base_url, api_key=settings.llm_api_key
    )
    logger.info("AI client initialized")


async def close_ai_client():
    global _ai_client
    client, _ai_client = _ai_client, None
    if client is not None:
        await client.close()
        logger.info("AI client closed")


def get_ai_client() -> AsyncOpenAI:
    if _ai_client is None:
        raise RuntimeError("AI client not initialised")
    return _ai_client


async def check_embedding():
    client = get_ai_client().with_options(max_retries=0)
    async with asyncio.timeout(settings.embedding_probe_timeout_seconds):
        response = await client.embeddings.create(
            model=settings.embedding_model, input="rag-ai startup check"
        )
    if (
        len(response.data) != 1
        or len(response.data[0].embedding) != settings.embedding_dimensions
    ):
        raise ValueError("Ollama returned an unexpected embedding dimension")


async def check_embedding_model():
    client = get_ai_client().with_options(max_retries=0)
    async with asyncio.timeout(settings.dependency_timeout_seconds):
        models = await client.models.list()
    model = settings.embedding_model
    if not any(
        item.id == model or item.id == f"{model}:latest" for item in models.data
    ):
        raise ValueError("Configured embedding model is unavailable")
