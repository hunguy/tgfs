import asyncio
import logging
import mimetypes
from typing import Dict, List, Optional

from tgfs.config import Config
from tgfs.core import Clients
from tgfs.core.ops import Ops
from tgfs.reqres import Document, MessageResp, MessageRespWithDocument
from tgfs.services.filename_inference import FilenameInferenceService
from tgfs.telegram.impl.telethon import TelethonAPI
from telethon.tl.types import PeerChannel
from telethon.helpers import TotalList

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
        self._file_names: Dict[str, Dict[int, str]] = {}
        self._filename_inference = FilenameInferenceService()

    def _resolve_channel_id(self, client_name: str) -> Optional[int]:
        for cid, meta in self._config.tgfs.metadata.items():
            if meta.name == client_name:
                return int(cid)
        return None

    async def _poll_channel(self, client_name: str) -> None:
        channel_id = self._resolve_channel_id(client_name)
        if channel_id is None:
            logger.error(f"[auto-import] Could not find channel ID for client {client_name}")
            return

        client = self._clients[client_name]
        logger.info(f"[auto-import] Polling started for {client_name} (channel {channel_id})")

        while self._running:
            try:
                await self._process_new_messages(client_name, client, channel_id)
            except Exception as ex:
                logger.error(f"[auto-import] Error polling channel {client_name}: {ex}", exc_info=True)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        logger.info(f"[auto-import] Polling stopped for {client_name}")

    def _message_resp_from_telethon(self, client_name: str, m) -> Optional[MessageResp]:
        if not m:
            return None
        from telethon import types as tlt

        obj = MessageResp(
            message_id=m.id,
            text=m.message or "",
            document=None,
        )
        if (
            isinstance(m.media, tlt.MessageMediaDocument)
            and (doc := m.media.document)
            and not isinstance(doc, tlt.DocumentEmpty)
        ):
            obj.document = Document(
                size=doc.size,
                id=doc.id,
                access_hash=doc.access_hash,
                file_reference=doc.file_reference,
                mime_type=doc.mime_type,
            )
            for attr in doc.attributes:
                if hasattr(attr, 'file_name'):
                    self._file_names.setdefault(client_name, {})[m.id] = attr.file_name
                    break
        return obj

    async def _fetch_latest_messages_with_account(
        self, client_name: str, account_api: TelethonAPI, channel_id: int
    ) -> List[MessageResp]:
        raw_messages = await account_api._client.get_messages(
            entity=PeerChannel(channel_id=channel_id),
            limit=MESSAGES_PER_POLL,
        )
        if not isinstance(raw_messages, TotalList):
            logger.warning(f"[auto-import] Unexpected response type: {type(raw_messages)}")
            return []

        return [m for m in (self._message_resp_from_telethon(client_name, m) for m in raw_messages) if m]

    async def _fetch_latest_messages_with_bot(
        self, bot_api: TelethonAPI, channel_id: int, last_processed: int
    ) -> List[MessageResp]:
        result: List[MessageResp] = []
        current_id = last_processed + 1
        consecutive_missing = 0
        max_consecutive_missing = 20
        batch_size = 20

        while consecutive_missing < max_consecutive_missing:
            batch_ids = list(range(current_id, current_id + batch_size))
            from tgfs.reqres import GetMessagesReq

            batch = await bot_api.get_messages(
                GetMessagesReq(
                    chat=channel_id,
                    message_ids=tuple(batch_ids),
                )
            )

            found_in_batch = False
            for msg in batch:
                if msg is not None:
                    found_in_batch = True
                    consecutive_missing = 0
                    result.append(msg)
                else:
                    consecutive_missing += 1

            if not found_in_batch:
                break

            current_id += batch_size

        return result

    async def _fetch_latest_messages(
        self, client_name: str, client, channel_id: int, last_processed: int
    ) -> List[MessageResp]:
        tdlib = client.message_api.tdlib

        if tdlib.account and isinstance(tdlib.account, TelethonAPI):
            logger.debug(f"[auto-import] Using account client to fetch messages for channel {channel_id}")
            return await self._fetch_latest_messages_with_account(client_name, tdlib.account, channel_id)

        bot = tdlib.next_bot
        if not isinstance(bot, TelethonAPI):
            logger.warning(
                f"[auto-import] Auto-import only supports Telethon. Got {type(bot).__name__}"
            )
            return []

        logger.debug(
            f"[auto-import] Using bot client to scan message IDs for channel {channel_id} "
            f"(starting from {last_processed + 1})"
        )
        return await self._fetch_latest_messages_with_bot(bot, channel_id, last_processed)

    async def _process_new_messages(
        self, client_name: str, client, channel_id: int
    ) -> None:
        logger.debug(f"[auto-import] Fetching latest messages for {client_name}")

        last_processed = self._last_message_ids.get(client_name, 0)
        latest_messages = await self._fetch_latest_messages(client_name, client, channel_id, last_processed)
        if not latest_messages:
            logger.debug(f"[auto-import] No messages found for {client_name}")
            return

        logger.debug(f"[auto-import] Fetched {len(latest_messages)} messages for {client_name}")

        new_messages = [m for m in latest_messages if m.message_id > last_processed]

        if not new_messages:
            logger.debug(f"[auto-import] No new messages for {client_name} (last: {last_processed})")
            return

        logger.info(f"[auto-import] Found {len(new_messages)} new messages for {client_name}")

        try:
            pinned = await client.message_api.get_pinned_message()
            pinned_id = pinned.message_id
        except Exception:
            pinned_id = None

        imported_count = 0
        for message in sorted(new_messages, key=lambda m: m.message_id):
            # Always update last_processed so skipped messages don't get re-found every poll
            self._last_message_ids[client_name] = message.message_id

            if not message.document:
                logger.debug(
                    f"[auto-import] Skipping message {message.message_id}: no document (media type not supported)"
                )
                continue
            if pinned_id and message.message_id == pinned_id:
                logger.debug(f"[auto-import] Skipping pinned message {message.message_id}")
                continue
            if message.text and message.text.startswith(METADATA_PREFIX):
                logger.debug(f"[auto-import] Skipping metadata message {message.message_id}")
                continue
            if message.document.mime_type == "text/plain":
                logger.debug(f"[auto-import] Skipping text document {message.message_id}")
                continue

            await self._import_message(client_name, client, message)
            imported_count += 1

        if imported_count:
            logger.info(f"[auto-import] Imported {imported_count} messages for {client_name}")

    async def _extract_file_name(self, client_name: str, message: MessageRespWithDocument) -> str:
        file_name = self._file_names.get(client_name, {}).get(message.message_id)
        if file_name:
            return file_name

        # Try LLM inference from caption text when no explicit filename exists
        if message.text and message.text.strip():
            inferred = await self._filename_inference.infer(
                message.text, message.document.mime_type
            )
            if inferred:
                return inferred

        # Derive extension from mime_type when no explicit filename attribute exists
        ext = ""
        if message.document.mime_type:
            guessed = mimetypes.guess_extension(message.document.mime_type)
            if guessed:
                ext = guessed

        return f"imported_{message.message_id}{ext}"

    async def _import_message(
        self, client_name: str, client, message: MessageRespWithDocument
    ) -> None:
        ops = Ops(client)

        file_name = await self._extract_file_name(client_name, message)
        file_name = file_name.replace('/', '_').replace('\\', '_')
        target_path = f"/{file_name}"

        logger.info(
            f"[auto-import] Importing message {message.message_id} "
            f"(size: {message.document.size}, name: {file_name})"
        )

        from tgfs.app.fs_cache import gfc
        gfc[client_name].reset(target_path)

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
            logger.info(f"[auto-import] Successfully imported message {message.message_id} as {file_name}")
        except Exception as ex:
            logger.error(
                f"[auto-import] Failed to import message {message.message_id}: {ex}",
                exc_info=True,
            )

    def start(self) -> None:
        if self._running:
            logger.warning("[auto-import] Already running")
            return

        self._running = True
        logger.info("[auto-import] Starting AutoImportManager")

        for client_name in self._clients:
            task = asyncio.create_task(self._poll_channel(client_name))
            self._tasks.append(task)
            logger.info(f"[auto-import] Created polling task for {client_name}")

    async def stop(self) -> None:
        if not self._running:
            return

        logger.info("[auto-import] Stopping AutoImportManager")
        self._running = False

        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._tasks.clear()
        await self._filename_inference.close()
        logger.info("[auto-import] AutoImportManager stopped")
