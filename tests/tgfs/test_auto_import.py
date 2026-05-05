import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from tgfs.auto_import import AutoImportManager, METADATA_PREFIX


class TestAutoImportManager:
    @pytest.fixture
    def mock_config(self):
        config = Mock()
        config.tgfs.metadata = {
            "3948205614": Mock(name="TGFS-Channel"),
        }
        return config

    @pytest.fixture
    def mock_clients(self):
        client = Mock()
        client.message_api = Mock()
        client.message_api.tdlib = Mock()

        account_api = Mock(spec="tgfs.telegram.impl.telethon.TelethonAPI")
        account_api._client = Mock()
        client.message_api.tdlib.account = account_api

        bot_api = Mock(spec="tgfs.telegram.impl.telethon.TelethonAPI")
        bot_api._client = Mock()
        client.message_api.tdlib.next_bot = bot_api

        return {"TGFS-Channel": client}

    @pytest.fixture
    def manager(self, mock_clients, mock_config):
        return AutoImportManager(mock_clients, mock_config)

    def test_resolve_channel_id(self, manager):
        assert manager._resolve_channel_id("TGFS-Channel") == 3948205614
        assert manager._resolve_channel_id("unknown") is None

    def test_start_creates_tasks(self, manager):
        manager.start()
        assert manager._running is True
        assert len(manager._tasks) == 1
        manager.stop()

    def test_start_prevents_double_start(self, manager):
        manager.start()
        manager.start()
        assert len(manager._tasks) == 1
        manager.stop()

    async def test_stop_cancels_tasks(self, manager):
        manager.start()
        await asyncio.sleep(0)
        await manager.stop()
        assert manager._running is False
        assert len(manager._tasks) == 0

    async def test_stop_noop_when_not_running(self, manager):
        await manager.stop()
        assert manager._running is False

    async def test_fetch_latest_messages_uses_account(self, manager, mock_clients):
        from telethon.helpers import TotalList

        mock_clients["TGFS-Channel"].message_api.tdlib.account._client.get_messages = AsyncMock(
            return_value=TotalList([Mock(id=10, message="hello", media=None)])
        )

        result = await manager._fetch_latest_messages(
            mock_clients["TGFS-Channel"], 3948205614, 0
        )
        assert len(result) == 1
        assert result[0].message_id == 10

    async def test_fetch_latest_messages_falls_back_to_bot(self, manager, mock_clients):
        mock_clients["TGFS-Channel"].message_api.tdlib.account = None

        mock_clients["TGFS-Channel"].message_api.tdlib.next_bot.get_messages = AsyncMock(
            return_value=[
                Mock(message_id=10, document=None, text="hello"),
            ]
        )

        result = await manager._fetch_latest_messages(
            mock_clients["TGFS-Channel"], 3948205614, 0
        )
        assert len(result) == 1
        assert result[0].message_id == 10

    async def test_process_new_messages_skips_no_document(self, manager, mock_clients):
        from telethon.helpers import TotalList

        mock_clients["TGFS-Channel"].message_api.tdlib.account._client.get_messages = AsyncMock(
            return_value=TotalList([Mock(id=10, message="hello", media=None)])
        )

        await manager._process_new_messages("TGFS-Channel", mock_clients["TGFS-Channel"], 3948205614)

        assert manager._last_message_ids.get("TGFS-Channel") != 10

    async def test_process_new_messages_skips_pinned(self, manager, mock_clients):
        from telethon.helpers import TotalList
        from telethon.tl import types as tlt

        mock_clients["TGFS-Channel"].message_api.get_pinned_message = AsyncMock(
            return_value=Mock(message_id=10)
        )
        mock_clients["TGFS-Channel"].message_api.tdlib.account._client.get_messages = AsyncMock(
            return_value=TotalList([
                Mock(
                    id=10,
                    message="hello",
                    media=tlt.MessageMediaDocument(
                        document=tlt.Document(
                            id=1, access_hash=2, file_reference=b"ref",
                            date=None, mime_type="text/plain", size=1024,
                            dc_id=1, attributes=[],
                        )
                    ),
                ),
            ])
        )

        await manager._process_new_messages("TGFS-Channel", mock_clients["TGFS-Channel"], 3948205614)

        assert manager._last_message_ids.get("TGFS-Channel") != 10

    async def test_process_new_messages_skips_metadata(self, manager, mock_clients):
        from telethon.helpers import TotalList
        from telethon.tl import types as tlt

        mock_clients["TGFS-Channel"].message_api.get_pinned_message = AsyncMock(
            side_effect=Exception("no pinned")
        )
        mock_clients["TGFS-Channel"].message_api.tdlib.account._client.get_messages = AsyncMock(
            return_value=TotalList([
                Mock(
                    id=10,
                    message=METADATA_PREFIX + "rest",
                    media=tlt.MessageMediaDocument(
                        document=tlt.Document(
                            id=1, access_hash=2, file_reference=b"ref",
                            date=None, mime_type="text/plain", size=1024,
                            dc_id=1, attributes=[],
                        )
                    ),
                ),
            ])
        )

        await manager._process_new_messages("TGFS-Channel", mock_clients["TGFS-Channel"], 3948205614)

        assert manager._last_message_ids.get("TGFS-Channel") != 10

    @patch("tgfs.auto_import.Ops")
    @patch("tgfs.auto_import.gfc")
    async def test_process_new_messages_imports_successfully(
        self, mock_gfc, mock_ops_class, manager, mock_clients
    ):
        from telethon.helpers import TotalList
        from telethon.tl import types as tlt

        mock_clients["TGFS-Channel"].message_api.get_pinned_message = AsyncMock(
            side_effect=Exception("no pinned")
        )
        mock_clients["TGFS-Channel"].message_api.tdlib.account._client.get_messages = AsyncMock(
            return_value=TotalList([
                Mock(
                    id=10,
                    message="myfile.txt",
                    media=tlt.MessageMediaDocument(
                        document=tlt.Document(
                            id=1, access_hash=2, file_reference=b"ref",
                            date=None, mime_type="text/plain", size=1024,
                            dc_id=1, attributes=[],
                        )
                    ),
                ),
            ])
        )

        mock_ops = Mock()
        mock_ops.import_from_existing_file_message = AsyncMock()
        mock_ops_class.return_value = mock_ops

        await manager._process_new_messages("TGFS-Channel", mock_clients["TGFS-Channel"], 3948205614)

        mock_ops.import_from_existing_file_message.assert_called_once()
        assert manager._last_message_ids.get("TGFS-Channel") == 10

    @patch("tgfs.auto_import.Ops")
    @patch("tgfs.auto_import.gfc")
    async def test_process_new_messages_logs_error(
        self, mock_gfc, mock_ops_class, manager, mock_clients
    ):
        from telethon.helpers import TotalList
        from telethon.tl import types as tlt

        mock_clients["TGFS-Channel"].message_api.get_pinned_message = AsyncMock(
            side_effect=Exception("no pinned")
        )
        mock_clients["TGFS-Channel"].message_api.tdlib.account._client.get_messages = AsyncMock(
            return_value=TotalList([
                Mock(
                    id=10,
                    message="myfile.txt",
                    media=tlt.MessageMediaDocument(
                        document=tlt.Document(
                            id=1, access_hash=2, file_reference=b"ref",
                            date=None, mime_type="text/plain", size=1024,
                            dc_id=1, attributes=[],
                        )
                    ),
                ),
            ])
        )

        mock_ops = Mock()
        mock_ops.import_from_existing_file_message = AsyncMock(
            side_effect=Exception("import failed")
        )
        mock_ops_class.return_value = mock_ops

        await manager._process_new_messages("TGFS-Channel", mock_clients["TGFS-Channel"], 3948205614)

        mock_ops.import_from_existing_file_message.assert_called_once()
        assert manager._last_message_ids.get("TGFS-Channel") == 10

    @patch("tgfs.auto_import.Ops")
    @patch("tgfs.auto_import.gfc")
    async def test_process_new_messages_uses_fallback_filename(
        self, mock_gfc, mock_ops_class, manager, mock_clients
    ):
        from telethon.helpers import TotalList
        from telethon.tl import types as tlt

        mock_clients["TGFS-Channel"].message_api.get_pinned_message = AsyncMock(
            side_effect=Exception("no pinned")
        )
        mock_clients["TGFS-Channel"].message_api.tdlib.account._client.get_messages = AsyncMock(
            return_value=TotalList([
                Mock(
                    id=99,
                    message=None,
                    media=tlt.MessageMediaDocument(
                        document=tlt.Document(
                            id=1, access_hash=2, file_reference=b"ref",
                            date=None, mime_type="text/plain", size=1024,
                            dc_id=1, attributes=[],
                        )
                    ),
                ),
            ])
        )

        mock_ops = Mock()
        mock_ops.import_from_existing_file_message = AsyncMock()
        mock_ops_class.return_value = mock_ops

        await manager._process_new_messages("TGFS-Channel", mock_clients["TGFS-Channel"], 3948205614)

        call_args = mock_ops.import_from_existing_file_message.call_args
        assert "/imported_99" in str(call_args)
        assert manager._last_message_ids.get("TGFS-Channel") == 99

    async def test_poll_channel_stops_when_not_running(self, manager):
        manager._running = False
        await manager._poll_channel("TGFS-Channel")

    async def test_poll_channel_handles_errors(self, manager, mock_clients):
        from telethon.helpers import TotalList

        mock_clients["TGFS-Channel"].message_api.tdlib.account._client.get_messages = AsyncMock(
            return_value=TotalList([])
        )

        manager._running = True

        task = asyncio.create_task(manager._poll_channel("TGFS-Channel"))
        await asyncio.sleep(0.1)
        manager._running = False
        await asyncio.sleep(0.1)

        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert True
