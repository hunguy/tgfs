import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "poolside/laguna-m.1:free"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


class FilenameInferenceService:
    """Uses an LLM via OpenRouter to infer a descriptive filename from message text."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self._model = model or os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """Remove path traversal and invalid filesystem characters."""
        name = name.strip()
        # Replace path separators and null bytes
        name = name.replace("/", "_").replace("\\", "_").replace("\x00", "")
        # Remove other problematic characters
        name = re.sub(r'[<>:"|?*]', "_", name)
        # Collapse multiple underscores
        name = re.sub(r"_+", "_", name)
        return name.strip("._")

    @staticmethod
    def _build_prompt(text: str, mime_type: Optional[str]) -> str:
        ext_hint = f"The file's MIME type is {mime_type}." if mime_type else ""
        return (
            "You are a filename generator. Given a Telegram message caption or text, "
            "produce a short, descriptive filename (max 80 chars) with an appropriate extension. "
            "Do NOT include paths. Do NOT explain. Output ONLY the filename.\n\n"
            f"{ext_hint}\n"
            f"Caption: {text}\n\n"
            "Filename:"
        )

    async def infer(self, text: str, mime_type: Optional[str] = None) -> Optional[str]:
        if not self._api_key:
            logger.debug("[filename-inference] No OPENROUTER_API_KEY set; skipping LLM inference")
            return None

        if not text or not text.strip():
            logger.debug("[filename-inference] No caption text provided; skipping LLM inference")
            return None

        client = await self._get_client()
        prompt = self._build_prompt(text, mime_type)

        try:
            response = await client.post(
                OPENROUTER_API_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 100,
                    "temperature": 0.3,
                },
            )
            response.raise_for_status()
            data = response.json()

            if not data.get("choices"):
                logger.warning("[filename-inference] OpenRouter returned no choices")
                return None

            raw = (data["choices"][0].get("message", {}).get("content") or "").strip()
            if not raw:
                logger.warning("[filename-inference] OpenRouter returned empty content")
                return None

            # Sometimes LLMs wrap the filename in markdown code blocks
            raw = raw.removeprefix("```").removeprefix("`").removesuffix("```").removesuffix("`").strip()

            sanitized = self._sanitize_filename(raw)
            if not sanitized:
                logger.warning("[filename-inference] Sanitized filename is empty")
                return None

            logger.info(f"[filename-inference] Inferred filename: {sanitized}")
            return sanitized

        except httpx.HTTPStatusError as e:
            logger.error(f"[filename-inference] OpenRouter HTTP error {e.response.status_code}: {e.response.text}")
            return None
        except httpx.RequestError as e:
            logger.error(f"[filename-inference] OpenRouter request error: {e}")
            return None
        except Exception as e:
            logger.error(f"[filename-inference] Unexpected error: {e}", exc_info=True)
            return None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
