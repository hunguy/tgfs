import asyncio
import logging
from typing import Dict, Optional

from tgfs.config import Config
from tgfs.core import Clients
from tgfs.core.ops import Ops
from tgfs.reqres import Document, MessageRespWithDocument
from tgfs.telegram.interface import TDLibApi

logger = logging.getLogger(__name__)

METADATA_PREFIX = '{"type":"F"'
POLL_INTERVAL_SECONDS = 10
MESSAGES_PER_POLL = 50


class AutoImportManager:
    def __init__(self, clients: Clients, config: Config):
        self._clients = clients
        self._config = config
        self._tasks: list = []
        self._running = False
        self._last_message_ids: Dict[str, int] = {}

    def _resolve_channel_id(self, client_name: str) -> Optional[int]:
        for cid, meta in self._config.tgfs.metadata.items():
            if meta.name == client_name:
                return int(cid)
        return None

    async def _poll_channel(self, client_name: str) -> None:
        channel_id = self._resolve_channel_id(client_name)
        if channel_id is None:
            logger.error(f"Could not find channel ID for client {client_name}")
            return

        client = self._clients[client_name]

        while self._running:
            try:
                await self._process_new_messages(client_name, client, channel_id)
            except Exception as ex:
                logger.error(f"Error polling channel {client_name}: {ex}", exc_info=True)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _process_new_messages(
        self, client_name: str, client, channel_id: int
    ) -> None:
        tdlib: TDLibApi = client.message_api.tdlib
        bot = tdlib.next_bot

        from tgfs.reqres import GetMessagesReq

        latest_messages = await bot.get_messages(
            GetMessagesReq(
                chat=channel_id,
                message_ids=tuple(range(-MESSAGES_PER_POLL, 0)),
            )
        )

        latest_messages = [m for m in latest_messages if m is not None]
        if not latest_messages:
            return

        last_processed = self._last_message_ids.get(client_name, 0)
        new_messages = [m for m in latest_messages if m.message_id > last_processed]

        if not new_messages:
            return

        try:
            pinned = await client.message_api.get_pinned_message()
            pinned_id = pinned.message_id
        except Exception:
            pinned_id = None

        for message in sorted(new_messages, key=lambda m: m.message_id):
            if not message.document:
                continue
            if pinned_id and message.message_id == pinned_id:
                continue
            if message.text and message.text.startswith(METADATA_PREFIX):
                continue

            await self._import_message(client_name, client, message)
            self._last_message_ids[client_name] = message.message_id

    async def _import_message(
        self, client_name: str, client, message: MessageRespWithDocument
    ) -> None:
        ops = Ops(client)

        file_name = message.text.strip() if message.text else None
        if not file_name:
            file_name = f"imported_{message.message_id}"

        file_name = file_name.replace('/', '_').replace('\\', '_')
        target_path = f"/{client_name}/{file_name}"

        logger.info(
            f"Auto-importing message {message.message_id} "
            f"(size: {message.document.size}, name: {file_name})"
        )

        from tgfs.app.fs_cache import gfc
        gfc[client_name].reset(f"/{file_name}")

        try:
            await ops.import_from_existing_file_message(
                MessageRespWithDocument(
                    message_id=message.message_id,
                    text=message.text or "",
                    document=Document(
                        size=message.document.size,
                        id=message.document.id,
                        access_hash=message.document.access_hash,
                        file_reference=message.document.file_reference,
                        mime_type=message.document.mime_type,
                    ),
                ),
                target_path,
            )
            logger.info(
                f"Successfully auto-imported message {message.message_id} as {file_name}"
            )
        except Exception as ex:
            logger.error(
                f"Failed to auto-import message {message.message_id}: {ex}",
                exc_info=True,
            )

    def start(self) -> None:
        if self._running:
            logger.warning("AutoImportManager is already running")
            return

        self._running = True

        for client_name in self._clients:
            task = asyncio.create_task(self._poll_channel(client_name))
            self._tasks.append(task)
            logger.info(f"Started auto-import polling for channel {client_name}")

    async def stop(self) -> None:
        if not self._running:
            return

        self._running = False

        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._tasks.clear()
        logger.info("AutoImportManager stopped")
