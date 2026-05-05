<p align="center">
  <img src="https://raw.githubusercontent.com/TheodoreKrypton/tgfs/master/tgfs.png" alt="logo" width="100"/>
</p>

# tgfs

Telegram becomes a WebDAV server.

This fork includes **auto-import**: forward any file to your Telegram channel and it will be automatically imported into your tgfs filesystem.

## Prerequisites

- A Hetzner (or similar) VPS with Ubuntu
- Dokploy installed on the VPS
- A Telegram bot token
- A Telegram API ID and hash
- A private Telegram channel for file storage

## Step 1: Create Your Telegram App

1. Visit [my.telegram.org/apps](https://my.telegram.org/apps)
2. Create a new application
3. Note down your **App api_id** and **App api_hash**

## Step 2: Create Your Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow instructions
3. Copy your bot token (looks like `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`)
4. Add the bot to your private file channel as an administrator

## Step 3: Create a Private Telegram Channel

1. Create a new private channel on Telegram
2. Add your bot as an **administrator**
3. Forward a message from the channel to yourself and copy the link to get the channel ID (e.g., `https://t.me/c/1234567890/1` — the channel ID is `1234567890`)

## Step 4: Clone and Configure

```bash
git clone <your-repo-url>
cd tgfs
```

Create `config.yaml` in the project root:

```yaml
telegram:
  api_id: 'YOUR_API_ID'
  api_hash: YOUR_API_HASH
  lib: telethon
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
  download:
    chunk_size_kb: 1024
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

**Generate a random JWT secret:**
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Step 5: Run Telegram Login Locally

The account session must be generated locally (to handle 2FA/phone code) then copied to the server.

```bash
# Install dependencies locally
poetry install

# Run the app — it will prompt for phone number and login code
poetry run python main.py
```

Follow the prompts:
1. Enter your phone number with country code (e.g., `+1234567890`)
2. Enter the login code sent to your Telegram
3. If you have 2FA, enter your password

This creates `account.session` and `bot.session` files.

## Step 6: Prepare the Server Data Directory

On your Hetzner VM, create the data directory:

```bash
ssh root@YOUR_SERVER_IP
mkdir -p /opt/tgfs-data
```

Copy the session files from your local machine:

```bash
scp account.session bot.session root@YOUR_SERVER_IP:/opt/tgfs-data/
scp config.yaml root@YOUR_SERVER_IP:/opt/tgfs-data/
```

Set proper permissions:

```bash
ssh root@YOUR_SERVER_IP
chown -R 1000:1000 /opt/tgfs-data/
chmod -R 755 /opt/tgfs-data/
```

## Step 7: Deploy with Dokploy

### 7.1 Push to Git

```bash
git add .
git commit -m "Add docker-compose and auto-import"
git push origin main
```

### 7.2 Create Dokploy Application

1. Open Dokploy dashboard: `https://dokploy.YOURDOMAIN.com`
2. Click **Create Service → Compose**
3. Select your Git provider and repository
4. In **Build Path**, enter `.`
5. In **Compose File**, enter `docker-compose.yml`

### 7.3 Configure the Compose Service

In Dokploy, navigate to your service and set:

**Domains:**
- Click **Add Domain**
- Enter your subdomain (e.g., `tgfs.yourdomain.com`)
- Select **HTTPS** (Let's Encrypt)
- Port: `1900`

**Volumes (in Dokploy UI or docker-compose.yml):**
The `docker-compose.yml` already contains:
```yaml
volumes:
  - /opt/tgfs-data:/home/tgfs/.tgfs
```

**Environment Variables:**
```
TGFS_DATA_DIR=/home/tgfs/.tgfs
TGFS_CONFIG_FILE=config.yaml
```

### 7.4 Deploy

Click **Deploy** in Dokploy. Wait for the build to complete.

Check logs:
```bash
ssh root@YOUR_SERVER_IP
docker logs -f tgfs
```

You should see:
```
logged in as @your_username
logged in as @your_bot
Starting TGFS server on 0.0.0.0:1900
[auto-import] Starting AutoImportManager
[auto-import] Created polling task for TGFS-Channel
```

## Step 8: Verify WebDAV Access

### Using rclone

```bash
rclone config
# Select 'n' for new remote
# Name: tgfs
# Type: WebDAV
# URL: https://tgfs.yourdomain.com/webdav
# Vendor: Other
# User: your_username
# Password: your_password
```

Test:
```bash
rclone lsd tgfs:
rclone ls tgfs:
```

### Using macOS Finder

1. Open Finder → Go → Connect to Server (Command + K)
2. Enter: `https://tgfs.yourdomain.com/webdav`
3. Enter your username and password
4. The drive appears under `/Volumes/`

## Step 9: Auto-Import (Forward Files to Channel)

Forward any file (video, document, etc.) to your private Telegram channel. Within 10 seconds, it will automatically appear in your tgfs filesystem.

**How it works:**
- The server polls the channel every 10 seconds
- It fetches the last 50 messages using the account client
- Documents (not text files) are imported with their original filename
- Promotional captions are ignored; the real filename from Telegram's document attributes is used

**Log output:**
```
[auto-import] Found 1 new messages for TGFS-Channel
[auto-import] Importing message 123 (size: 104857600, name: myvideo.mp4)
[auto-import] Successfully imported message 123 as myvideo.mp4
```

## Troubleshooting

### Container exits immediately
Check logs: `docker logs tgfs`
Common causes:
- Missing `config.yaml` in `/opt/tgfs-data/`
- Wrong permissions on `/opt/tgfs-data/` (should be owned by UID 1000)
- Invalid session files (re-run login locally)

### BotMethodInvalidError
Bots cannot fetch message history. The auto-import feature requires the **account session** (`account.session`) to use `GetHistoryRequest`. Make sure:
- `config.yaml` has `account:` section configured
- `account.session` exists and is valid

### Files appear with wrong names
If files show up with promotional text instead of filenames:
- Make sure you're using the latest version with the `_message_resp_from_telethon` fix
- The filename is now extracted from `document.attributes.file_name`, not the message caption

### WebDAV 500 errors on GET
`GET /webdav/` on folders returns 500 — this is expected. Use `PROPFIND` instead. rclone handles this automatically.

### No auto-import messages in logs
- Check that `[auto-import] Starting AutoImportManager` appears in logs
- Verify the account client is logged in (look for `logged in as @your_username`)
- Ensure the bot is an admin in the channel

## Development

Install dependencies:
```bash
poetry install
```

Run locally:
```bash
poetry run python main.py
```

Typecheck & lint:
```bash
make mypy
make ruff
```

Run tests:
```bash
poetry run pytest
```

## License

See [LICENSE](LICENSE)
