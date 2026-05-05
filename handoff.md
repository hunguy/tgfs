# TGFS Project Handoff

> This document captures critical knowledge, gotchas, and architectural decisions from the current development session. Future LLM sessions should read this before making changes.

---

## Architecture Overview

TGFS is a WebDAV server backed by Telegram. Files are stored as messages in a private Telegram channel. Metadata (directory structure, file descriptors) is stored as a pinned JSON message in the same channel.

```
User → WebDAV Client (rclone/Finder) → FastAPI WebDAV Server → Telegram Channel
                                          ↓
                                   AutoImportManager (polls channel)
```

### Key Components

| Component | File | Role |
|-----------|------|------|
| WebDAV Server | `tgfs/app/__init__.py` | FastAPI app with auth middleware |
| Manager API | `tgfs/app/manager/app.py` | `/api/*` endpoints (import, tasks) |
| Core Client | `tgfs/core/client.py` | Initializes per-channel file/message APIs |
| File Operations | `tgfs/core/ops.py` | `import_from_existing_file_message`, `cd`, etc. |
| Auto-Import | `tgfs/auto_import.py` | NEW: Background polling for forwarded files |
| Telegram Client | `tgfs/telegram/impl/telethon.py` | Telethon wrapper with caching |

---

## Critical Gotchas & Pitfalls

### 1. Negative Message IDs & Cache Mismatch
**What:** Fetching messages with `range(-50, 0)` (negative indices) causes the tgfs cache to fail lookups.
**Why:** The cache stores messages by their positive ID but lookups use the requested (negative) ID.
**Fix:** Use raw Telethon `client.get_messages(entity, limit=50)` which returns actual message objects with positive IDs.
**File:** `tgfs/auto_import.py` — `_fetch_latest_messages_with_account()`

### 2. BotMethodInvalidError (GetHistoryRequest)
**What:** `telethon.errors.rpcerrorlist.BotMethodInvalidError`
**Why:** Telegram bots cannot call `GetHistoryRequest` (used by `client.get_messages(entity, limit=...)`). Only user accounts have this permission.
**Fix:** Auto-import MUST use the **account client** (`tdlib.account`) for fetching message history, not the bot client.
**File:** `tgfs/auto_import.py` — `_fetch_latest_messages()`
**Config Requirement:** `config.yaml` MUST have the `account:` section with a valid `account.session`.

### 3. File Path Format (No Client Name Prefix)
**What:** `FileOrDirectoryDoesNotExist: No such file or directory: 'TGFS-Channel'`
**Why:** The import path was `/{client_name}/{file_name}` but `TGFS-Channel` is not a directory in the filesystem.
**Fix:** Use `/{file_name}` directly. The `client_name` selects the correct ops instance, but the filesystem root is `/`.
**File:** `tgfs/auto_import.py` — `_import_message()`

### 4. Filename Extraction from Captions
**What:** Files imported with promotional text as names (hashtags, links, "✓ Back To Main Channel")
**Why:** The code used `message.text` (caption) as the filename.
**Fix:** Extract `file_name` from `document.attributes` during raw message processing, store it in `self._file_names`, and use that instead of the caption.
**File:** `tgfs/auto_import.py` — `_message_resp_from_telethon()`, `_extract_file_name()`

### 5. Text Files Being Imported
**What:** Forwarded `.txt` files (promotional text) were being imported.
**Fix:** Skip documents with `mime_type == "text/plain"`.
**File:** `tgfs/auto_import.py` — `_process_new_messages()`

### 6. Missing `client_name` Parameter
**What:** `NameError: name 'client_name' is not defined`
**Why:** The `_fetch_latest_messages()` method didn't accept `client_name` but tried to use it.
**Fix:** Add `client_name: str` parameter and pass it from the caller.
**File:** `tgfs/auto_import.py` — `_fetch_latest_messages()`

### 7. WebDAV GET on Folders Returns 500
**What:** `GET /webdav/` returns 500 with `ValueError: Expected a Resource, got a Folder`
**Why:** WebDAV spec uses `PROPFIND` for listing, not `GET`. The tgfs implementation correctly handles `PROPFIND` but not `GET` for collections.
**Fix:** Use `PROPFIND` instead. rclone handles this automatically.
**File:** `tgfs/app/webdav/__init__.py`

### 8. Docker Permissions (Bind Mount)
**What:** `FileNotFoundError: [Errno 2] No such file or directory: '/home/tgfs/.tgfs/config.yaml'`
**Why:** The container runs as a non-root `tgfs` user (UID 1000), but the host `/opt/tgfs-data` may be owned by root.
**Fix:** On the host: `chown -R 1000:1000 /opt/tgfs-data/`
**File:** `Dockerfile` — creates `tgfs` user

---

## Configuration

### Minimal `config.yaml`
```yaml
telegram:
  api_id: 'YOUR_API_ID'
  api_hash: YOUR_API_HASH
  lib: telethon  # MUST be telethon for auto-import
  account:
    session_file: account.session
    used_to_upload: false
    used_to_download: false
  bot:
    session_file: bot.session
    tokens:
      - YOUR_BOT_TOKEN
  private_file_channel:
    - 'YOUR_CHANNEL_ID'

tgfs:
  users:
    your_username:
      password: your_password
  jwt:
    secret: 'GENERATE_A_RANDOM_SECRET'
    algorithm: HS256
    life: 604800
  metadata:
    'YOUR_CHANNEL_ID':
      name: TGFS-Channel
      type: pinned_message
  server:
    host: 0.0.0.0
    port: 1900
```

**CRITICAL:**
- `lib: telethon` is REQUIRED for auto-import (not pyrogram)
- `account:` section is REQUIRED for auto-import (bots can't fetch history)
- `metadata.name` becomes the WebDAV root directory name

---

## Deployment (Dokploy)

### Files That Matter
- `docker-compose.yml` — Defines the service, build context, volume mount, port
- `Dockerfile` — Multi-stage Python 3.13 build, creates `tgfs` user (UID 1000)
- `config.yaml` — Must exist in `/opt/tgfs-data/` on the host
- `account.session` + `bot.session` — Must exist in `/opt/tgfs-data/`

### Volume Mount
```yaml
volumes:
  - /opt/tgfs-data:/home/tgfs/.tgfs
```

### Dokploy Steps
1. Push code to Git
2. Create Compose service in Dokploy
3. Set domain + HTTPS (port 1900)
4. Ensure `/opt/tgfs-data` exists on host with correct permissions
5. Deploy

---

## Auto-Import Feature

### How It Works
1. `main.py` creates `AutoImportManager` after initializing clients
2. `start()` spawns one asyncio task per channel
3. Each task polls every 10 seconds (`POLL_INTERVAL_SECONDS`)
4. Fetches last 50 messages using account client (`get_messages(entity, limit=50)`)
5. Extracts actual filenames from `document.attributes`
6. Skips: pinned messages, metadata messages, text/plain files
7. Imports remaining documents to root (`/{file_name}`)

### Logging
All auto-import logs use `[auto-import]` prefix:
```
[auto-import] Starting AutoImportManager
[auto-import] Polling started for TGFS-Channel (channel 3948205614)
[auto-import] Found 1 new messages for TGFS-Channel
[auto-import] Importing message 123 (size: 104857600, name: myvideo.mp4)
[auto-import] Successfully imported message 123 as myvideo.mp4
```

### Testing
Tests are in `tests/tgfs/test_auto_import.py`.
Key test cases:
- Account vs bot fetching
- Skipping pinned/metadata/text files
- Filename extraction
- Error handling
- Graceful shutdown

---

## File Structure

```
/
├── docker-compose.yml          # Dokploy deployment config
├── Dockerfile                  # Multi-stage Python 3.13 build
├── config.yaml                 # Telegram credentials & settings
├── main.py                     # Entry point, creates clients + auto-import
├── tgfs/
│   ├── auto_import.py          # NEW: AutoImportManager
│   ├── app/
│   │   ├── __init__.py         # FastAPI app with auth & WebDAV mount
│   │   ├── manager/app.py      # /api/* endpoints
│   │   ├── webdav/__init__.py  # WebDAV implementation
│   │   └── fs_cache.py         # File system cache (gfc dict)
│   ├── core/
│   │   ├── client.py           # Client.create() factory
│   │   ├── ops.py              # File operations (import, cd, etc.)
│   │   ├── model/
│   │   │   └── directory.py    # TGFSDirectory, TGFSFileRef
│   │   └── api/
│   │       ├── message/__init__.py  # MessageApi (rate-limited)
│   │       ├── file.py            # FileApi (upload, download)
│   │       └── directory.py       # DirectoryApi
│   ├── telegram/
│   │   └── impl/telethon.py  # TelethonAPI wrapper + login
│   └── config.py               # Config loading (DATA_DIR, CONFIG_FILE)
└── tests/
    └── tgfs/
        └── test_auto_import.py  # Auto-import tests
```

---

## Common Commands

```bash
# Local development
poetry install
poetry run python main.py

# Docker (local)
docker compose up --build

# Hetzner / Dokploy
ssh root@YOUR_IP
cd /opt/tgfs-data
docker logs -f tgfs
docker compose restart tgfs

# rclone
rclone config  # Create 'tgfs' remote
rclone ls tgfs:
rclone delete tgfs:TGFS-Channel/  # Delete all files

# macOS Finder
# Command + K → https://tgfs.yourdomain.com/webdav
```

---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `TGFS_DATA_DIR` | `~/.tgfs` | Where config and sessions live |
| `TGFS_CONFIG_FILE` | `config.yaml` | Config filename |

---

## Last Updated

2026-05-05 — Added auto-import feature with Telethon account client support.
