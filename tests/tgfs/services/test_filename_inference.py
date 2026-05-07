import os
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from tgfs.services.filename_inference import FilenameInferenceService


class TestFilenameInferenceService:
    @pytest.fixture
    def service(self):
        return FilenameInferenceService(api_key="test-key", model="test-model")

    def test_init_from_env(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "env-key", "OPENROUTER_MODEL": "env-model"}):
            svc = FilenameInferenceService()
            assert svc._api_key == "env-key"
            assert svc._model == "env-model"

    def test_init_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            svc = FilenameInferenceService()
            assert svc._api_key is None
            assert svc._model == "poolside/laguna-m.1:free"

    def test_sanitize_filename(self, service):
        assert service._sanitize_filename("hello world.mp4") == "hello world.mp4"
        assert service._sanitize_filename("path/../file.txt") == "path_.._file.txt"
        assert service._sanitize_filename("file<name>.txt") == "file_name_.txt"
        assert service._sanitize_filename("a\\b/c.txt") == "a_b_c.txt"
        assert service._sanitize_filename("  spaces  ") == "spaces"

    def test_build_prompt_with_mime_type(self, service):
        prompt = service._build_prompt("Summer vacation video", "video/mp4")
        assert "Summer vacation video" in prompt
        assert "video/mp4" in prompt
        assert "Filename:" in prompt

    def test_build_prompt_without_mime_type(self, service):
        prompt = service._build_prompt("Summer vacation video", None)
        assert "Summer vacation video" in prompt
        assert "MIME type" not in prompt

    async def test_infer_no_api_key(self):
        svc = FilenameInferenceService(api_key=None)
        result = await svc.infer("some text")
        assert result is None

    async def test_infer_no_text(self, service):
        result = await service.infer("")
        assert result is None
        result = await service.infer("   ")
        assert result is None

    @patch("httpx.AsyncClient.post")
    async def test_infer_success(self, mock_post, service):
        mock_post.return_value = Mock(
            status_code=200,
            raise_for_status=Mock(),
            json=Mock(return_value={
                "choices": [{"message": {"content": "summer_vacation_2025.mp4"}}]
            }),
        )

        result = await service.infer("Our summer vacation in Bali", "video/mp4")
        assert result == "summer_vacation_2025.mp4"

        call_args = mock_post.call_args
        assert call_args[1]["headers"]["Authorization"] == "Bearer test-key"
        assert call_args[1]["json"]["model"] == "test-model"
        assert "Our summer vacation in Bali" in call_args[1]["json"]["messages"][0]["content"]

    @patch("httpx.AsyncClient.post")
    async def test_infer_strips_markdown_code_blocks(self, mock_post, service):
        mock_post.return_value = Mock(
            status_code=200,
            raise_for_status=Mock(),
            json=Mock(return_value={
                "choices": [{"message": {"content": "```file_name.mp4```"}}]
            }),
        )

        result = await service.infer("text", "video/mp4")
        assert result == "file_name.mp4"

    @patch("httpx.AsyncClient.post")
    async def test_infer_strips_inline_code(self, mock_post, service):
        mock_post.return_value = Mock(
            status_code=200,
            raise_for_status=Mock(),
            json=Mock(return_value={
                "choices": [{"message": {"content": "`inline.mp4`"}}]
            }),
        )

        result = await service.infer("text", "video/mp4")
        assert result == "inline.mp4"

    @patch("httpx.AsyncClient.post")
    async def test_infer_empty_choices(self, mock_post, service):
        mock_post.return_value = Mock(
            status_code=200,
            raise_for_status=Mock(),
            json=Mock(return_value={"choices": []}),
        )

        result = await service.infer("text")
        assert result is None

    @patch("httpx.AsyncClient.post")
    async def test_infer_http_error(self, mock_post, service):
        mock_post.return_value = Mock(
            status_code=429,
            raise_for_status=Mock(side_effect=httpx.HTTPStatusError(
                "Rate limited",
                request=Mock(),
                response=Mock(status_code=429, text="rate limited"),
            )),
        )

        result = await service.infer("text")
        assert result is None

    @patch("httpx.AsyncClient.post")
    async def test_infer_request_error(self, mock_post, service):
        mock_post.side_effect = httpx.RequestError("Connection failed")

        result = await service.infer("text")
        assert result is None

    async def test_close(self, service):
        service._client = Mock()
        service._client.aclose = AsyncMock()
        await service.close()
        service._client.aclose.assert_awaited_once()
        assert service._client is None
