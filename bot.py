import os
import re
import sys
import shutil
import logging
import subprocess
from pathlib import Path

import requests

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


# ============================================================
# CONFIGURATION
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

GOOGLE_DRIVE_REFRESH_TOKEN = os.environ[
    "GOOGLE_DRIVE_REFRESH_TOKEN"
]

GOOGLE_OAUTH_CLIENT_ID = os.environ[
    "GOOGLE_OAUTH_CLIENT_ID"
]

GOOGLE_OAUTH_CLIENT_SECRET = os.environ[
    "GOOGLE_OAUTH_CLIENT_SECRET"
]

DRIVE_INPUT_FOLDER_ID = os.environ[
    "DRIVE_INPUT_FOLDER_ID"
]

WORK_DIR = Path("work")
WORK_DIR.mkdir(exist_ok=True)

MAX_DOWNLOAD_SIZE_MB = 500

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"

TELEGRAM_API = (
    "https://api.telegram.org/bot"
    + TELEGRAM_BOT_TOKEN
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("telegram-video-bot")


# ============================================================
# TELEGRAM
# ============================================================

def telegram_request(method, data=None):
    url = f"{TELEGRAM_API}/{method}"

    response = requests.post(
        url,
        data=data or {},
        timeout=60,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {result}"
        )

    return result.get("result")


def send_message(chat_id, text):
    try:
        telegram_request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
            },
        )
    except Exception as exc:
        logger.error(
            "Could not send Telegram message: %s",
            exc,
        )


# ============================================================
# LINK DETECTION
# ============================================================

URL_PATTERN = re.compile(
    r"https?://[^\s]+",
    re.IGNORECASE,
)


def extract_url(text):
    if not text:
        return None

    match = URL_PATTERN.search(text)

    if not match:
        return None

    url = match.group(0).strip()

    url = url.rstrip(
        ".,!?)]}>\"'"
    )

    return url


def is_supported_url(url):
    lowered = url.lower()

    supported_domains = (
        "youtube.com",
        "youtu.be",
        "instagram.com",
        "www.instagram.com",
    )

    return any(
        domain in lowered
        for domain in supported_domains
    )


# ============================================================
# GOOGLE DRIVE
# ============================================================

def get_drive_service():
    credentials = Credentials(
        token=None,
        refresh_token=GOOGLE_DRIVE_REFRESH_TOKEN,
        token_uri=(
            "https://oauth2.googleapis.com/token"
        ),
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=[DRIVE_SCOPE],
    )

    return build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def upload_to_drive(
    drive_service,
    file_path,
):
    file_name = file_path.name

    metadata = {
        "name": file_name,
        "parents": [
            DRIVE_INPUT_FOLDER_ID
        ],
    }

    # Use the actual file type instead of always declaring MP4.
    mimetype = {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".m4v": "video/x-m4v",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
    }.get(
        file_path.suffix.lower(),
        "application/octet-stream",
    )

    media = MediaFileUpload(
        str(file_path),
        mimetype=mimetype,
        resumable=True,
        chunksize=8 * 1024 * 1024,
    )

    request = (
        drive_service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id,name",
        )
    )

    response = None

    while response is None:
        status, response = (
            request.next_chunk()
        )

        if status:
            logger.info(
                "Drive upload progress: %.1f%%",
                status.progress() * 100,
            )

    return response


# ============================================================
# DUPLICATE CHECK
# ============================================================

def drive_file_exists(
    drive_service,
    file_name,
):
    escaped_name = (
        file_name.replace(
            "'",
            "''",
        )
    )

    query = (
        f"'{DRIVE_INPUT_FOLDER_ID}' "
        "in parents "
        "and name = "
        f"'{escaped_name}' "
        "and trashed = false"
    )

    response = (
        drive_service.files()
        .list(
            q=query,
            pageSize=10,
            fields="files(id,name)",
        )
        .execute()
    )

    return bool(
        response.get("files")
    )


# ============================================================
# VIDEO DOWNLOAD
# ============================================================

def download_video(url):
    output_template = (
        str(WORK_DIR)
        + "/%(title).100s-%(id)s.%(ext)s"
    )

    command = [
        sys.executable,
        "-m",
        "yt_dlp",

        "--no-playlist",

        "--max-filesize",
        f"{MAX_DOWNLOAD_SIZE_MB}M",

        "--js-runtimes",
        "deno",

        "--merge-output-format",
        "mp4",

        "-o",
        output_template,

        url,
    ]

    logger.info(
        "Starting yt-dlp download."
    )

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error(
            "yt-dlp stdout:\n%s",
            result.stdout,
        )

        logger.error(
            "yt-dlp stderr:\n%s",
            result.stderr,
        )

        raise RuntimeError(
            "Video download failed."
        )

    video_files = [
        path
        for path in WORK_DIR.iterdir()
        if path.is_file()
        and path.suffix.lower()
        in {
            ".mp4",
            ".mov",
            ".m4v",
            ".webm",
            ".mkv",
        }
    ]

    if not video_files:
        raise RuntimeError(
            "Download completed but no video file was found."
        )

    video_files.sort(
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    return video_files[0]


# ============================================================
# SAFE FILE NAME
# ============================================================

def safe_filename(name):
    name = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        name,
    )

    name = name.strip()

    if not name:
        name = "telegram_video"

    return name


# ============================================================
# PROCESS ONE TELEGRAM MESSAGE
# ============================================================

def process_message(
    drive_service,
    message,
):
    chat = message.get("chat", {})
    chat_id = chat.get("id")

    text = message.get(
        "text",
        "",
    )

    if not chat_id:
        logger.error(
            "No Telegram chat ID was provided."
        )
        return

    url = extract_url(text)

    if not url:
        send_message(
            chat_id,
            "❌ I couldn't find a supported YouTube or Instagram link.",
        )
        return

    if not is_supported_url(url):
        send_message(
            chat_id,
            "❌ Supported links are currently YouTube and Instagram.",
        )
        return

    # Cloudflare already sends this immediately.
    # We keep this here for safety if process_message
    # is called directly.
    send_message(
        chat_id,
        "📥 Receiving link...",
    )

    logger.info(
        "Received URL: %s",
        url,
    )

    try:
        send_message(
            chat_id,
            "⬇️ Downloading...",
        )

        video_path = download_video(url)

        size_mb = (
            video_path.stat().st_size
            / 1024
            / 1024
        )

        logger.info(
            "Downloaded: %s (%.1f MB)",
            video_path.name,
            size_mb,
        )

        send_message(
            chat_id,
            "✅ Download complete",
        )

        original_suffix = (
            video_path.suffix.lower()
        )

        if original_suffix != ".mp4":
            send_message(
                chat_id,
                "ℹ️ Downloaded video format will be uploaded as received.",
            )

        new_name = safe_filename(
            video_path.stem
        )

        if not new_name.lower().endswith(
            video_path.suffix.lower()
        ):
            new_name += video_path.suffix

        renamed_path = (
            WORK_DIR
            / new_name
        )

        if (
            renamed_path.resolve()
            != video_path.resolve()
        ):
            shutil.move(
                str(video_path),
                str(renamed_path),
            )
            video_path = renamed_path

        if drive_file_exists(
            drive_service,
            video_path.name,
        ):
            send_message(
                chat_id,
                "⚠️ A file with the same name already exists in 01_INPUT. Skipping duplicate upload.",
            )

            video_path.unlink(
                missing_ok=True
            )

            return

        send_message(
            chat_id,
            "☁️ Uploading to Google Drive...",
        )

        uploaded = upload_to_drive(
            drive_service,
            video_path,
        )

        send_message(
            chat_id,
            "✅ Added to 01_INPUT",
        )

        send_message(
            chat_id,
            "🤖 AI processing pending",
        )

        logger.info(
            "Drive file uploaded: %s",
            uploaded,
        )

        video_path.unlink(
            missing_ok=True
        )

    except Exception:
        logger.exception(
            "Processing failed."
        )

        send_message(
            chat_id,
            "❌ Processing failed.\n\n"
            "The video was not added to Google Drive.\n"
            "Please try the link again.",
        )


# ============================================================
# MAIN
# ============================================================

def main():
    logger.info(
        "========== TELEGRAM BOT START =========="
    )

    # --------------------------------------------------------
    # IMPORTANT:
    # Cloudflare Worker sends the Telegram link to GitHub
    # through repository_dispatch.
    #
    # These values come from:
    # github.event.client_payload.url
    # github.event.client_payload.chat_id
    # --------------------------------------------------------

    telegram_url = os.environ.get(
        "TELEGRAM_URL",
        "",
    ).strip()

    telegram_chat_id = os.environ.get(
        "TELEGRAM_CHAT_ID",
        "",
    ).strip()

    if not telegram_url or not telegram_chat_id:
        logger.info(
            "No Telegram event data found."
        )
        logger.info(
            "This workflow must be triggered by repository_dispatch."
        )
        return

    try:
        chat_id = int(
            telegram_chat_id
        )
    except ValueError:
        logger.error(
            "Invalid Telegram chat ID: %s",
            telegram_chat_id,
        )
        return

    logger.info(
        "Processing Telegram event."
    )

    logger.info(
        "URL: %s",
        telegram_url,
    )

    drive_service = (
        get_drive_service()
    )

    # Build a Telegram-like message object
    # so the existing processing function can
    # remain simple and stable.

    message = {
        "chat": {
            "id": chat_id
        },
        "text": telegram_url,
    }

    process_message(
        drive_service,
        message,
    )

    logger.info(
        "========== TELEGRAM BOT END =========="
    )


if __name__ == "__main__":
    main()        url,
        data=data or {},
        timeout=60,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {result}"
        )

    return result.get("result")


def send_message(chat_id, text):
    try:
        telegram_request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
            },
        )
    except Exception as exc:
        logger.error(
            "Could not send Telegram message: %s",
            exc,
        )


# ============================================================
# LINK DETECTION
# ============================================================

URL_PATTERN = re.compile(
    r"https?://[^\s]+",
    re.IGNORECASE,
)


def extract_url(text):
    if not text:
        return None

    match = URL_PATTERN.search(text)

    if not match:
        return None

    url = match.group(0).strip()

    url = url.rstrip(
        ".,!?)]}>\"'"
    )

    return url


def is_supported_url(url):
    lowered = url.lower()

    supported_domains = (
        "youtube.com",
        "youtu.be",
        "instagram.com",
        "www.instagram.com",
    )

    return any(
        domain in lowered
        for domain in supported_domains
    )


# ============================================================
# GOOGLE DRIVE
# ============================================================

def get_drive_service():
    credentials = Credentials(
        token=None,
        refresh_token=GOOGLE_DRIVE_REFRESH_TOKEN,
        token_uri=(
            "https://oauth2.googleapis.com/token"
        ),
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=[DRIVE_SCOPE],
    )

    return build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def upload_to_drive(
    drive_service,
    file_path,
):
    file_name = file_path.name

    metadata = {
        "name": file_name,
        "parents": [
            DRIVE_INPUT_FOLDER_ID
        ],
    }

    media = MediaFileUpload(
        str(file_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=8 * 1024 * 1024,
    )

    request = (
        drive_service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id,name",
        )
    )

    response = None

    while response is None:
        status, response = (
            request.next_chunk()
        )

        if status:
            logger.info(
                "Drive upload progress: %.1f%%",
                status.progress() * 100,
            )

    return response


# ============================================================
# DUPLICATE CHECK
# ============================================================

def drive_file_exists(
    drive_service,
    file_name,
):
    escaped_name = (
        file_name.replace(
            "'",
            "''",
        )
    )

    query = (
        f"'{DRIVE_INPUT_FOLDER_ID}' "
        "in parents "
        "and name = "
        f"'{escaped_name}' "
        "and trashed = false"
    )

    response = (
        drive_service.files()
        .list(
            q=query,
            pageSize=10,
            fields="files(id,name)",
        )
        .execute()
    )

    return bool(
        response.get("files")
    )


# ============================================================
# VIDEO DOWNLOAD
# ============================================================

def download_video(url):
    output_template = (
        str(WORK_DIR)
        + "/%(title).100s-%(id)s.%(ext)s"
    )

    command = [
        sys.executable,
        "-m",
        "yt_dlp",

        "--no-playlist",

        "--max-filesize",
        f"{MAX_DOWNLOAD_SIZE_MB}M",

        "--merge-output-format",
        "mp4",

        "-o",
        output_template,

        url,
    ]

    logger.info(
        "Starting yt-dlp download."
    )

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error(
            "yt-dlp stdout:\n%s",
            result.stdout,
        )

        logger.error(
            "yt-dlp stderr:\n%s",
            result.stderr,
        )

        raise RuntimeError(
            "Video download failed."
        )

    video_files = [
        path
        for path in WORK_DIR.iterdir()
        if path.is_file()
        and path.suffix.lower()
        in {
            ".mp4",
            ".mov",
            ".m4v",
            ".webm",
            ".mkv",
        }
    ]

    if not video_files:
        raise RuntimeError(
            "Download completed but no video file was found."
        )

    video_files.sort(
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    return video_files[0]


# ============================================================
# SAFE FILE NAME
# ============================================================

def safe_filename(name):
    name = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        name,
    )

    name = name.strip()

    if not name:
        name = "telegram_video"

    return name


# ============================================================
# PROCESS ONE TELEGRAM MESSAGE
# ============================================================

def process_message(
    drive_service,
    message,
):
    chat = message.get("chat", {})
    chat_id = chat.get("id")

    text = message.get(
        "text",
        "",
    )

    url = extract_url(text)

    if not url:
        send_message(
            chat_id,
            "❌ I couldn't find a supported YouTube or Instagram link.",
        )
        return

    if not is_supported_url(url):
        send_message(
            chat_id,
            "❌ Supported links are currently YouTube and Instagram.",
        )
        return

    send_message(
        chat_id,
        "📥 Receiving link...",
    )

    logger.info(
        "Received URL: %s",
        url,
    )

    try:
        send_message(
            chat_id,
            "⬇️ Downloading...",
        )

        video_path = download_video(url)

        size_mb = (
            video_path.stat().st_size
            / 1024
            / 1024
        )

        logger.info(
            "Downloaded: %s (%.1f MB)",
            video_path.name,
            size_mb,
        )

        send_message(
            chat_id,
            "✅ Download complete",
        )

        original_suffix = (
            video_path.suffix.lower()
        )

        if original_suffix != ".mp4":
            send_message(
                chat_id,
                "ℹ️ Downloaded video format will be uploaded as received.",
            )

        new_name = safe_filename(
            video_path.stem
        )

        if not new_name.lower().endswith(
            video_path.suffix.lower()
        ):
            new_name += video_path.suffix

        renamed_path = (
            WORK_DIR
            / new_name
        )

        if (
            renamed_path.resolve()
            != video_path.resolve()
        ):
            shutil.move(
                str(video_path),
                str(renamed_path),
            )
            video_path = renamed_path

        if drive_file_exists(
            drive_service,
            video_path.name,
        ):
            send_message(
                chat_id,
                "⚠️ A file with the same name already exists in 01_INPUT. Skipping duplicate upload.",
            )

            video_path.unlink(
                missing_ok=True
            )

            return

        send_message(
            chat_id,
            "☁️ Uploading to Google Drive...",
        )

        uploaded = upload_to_drive(
            drive_service,
            video_path,
        )

        send_message(
            chat_id,
            "✅ Added to 01_INPUT",
        )

        send_message(
            chat_id,
            "🤖 AI processing pending",
        )

        logger.info(
            "Drive file uploaded: %s",
            uploaded,
        )

        video_path.unlink(
            missing_ok=True
        )

    except Exception as exc:
        logger.exception(
            "Processing failed."
        )

        send_message(
            chat_id,
            "❌ Processing failed.\n\n"
            "The video was not added to Google Drive.\n"
            "Please try the link again.",
        )


# ============================================================
# TELEGRAM UPDATE HANDLING
# ============================================================

def get_updates(offset=None):
    data = {
        "timeout": 10,
        "allowed_updates": json.dumps(
            ["message"]
        ),
    }

    if offset is not None:
        data["offset"] = offset

    return telegram_request(
        "getUpdates",
        data,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    logger.info(
        "========== TELEGRAM BOT START =========="
    )

    drive_service = (
        get_drive_service()
    )

    # Get the latest available update.
    #
    # GitHub Actions will run this program repeatedly.
    # The Telegram update_id is handled during this run.
    #
    # We deliberately process only the latest pending message
    # to keep each GitHub Actions run short and inexpensive.

    updates = get_updates()

    if not updates:
        logger.info(
            "No new Telegram messages."
        )
        return

    logger.info(
        "Found %d Telegram update(s).",
        len(updates),
    )

    # Process the newest message first.
    latest_update = updates[-1]

    message = latest_update.get(
        "message"
    )

    if message:
        process_message(
            drive_service,
            message,
        )

    # Acknowledge all updates returned by Telegram
    # so old messages aren't processed repeatedly.
    next_offset = (
        updates[-1]["update_id"] + 1
    )

    get_updates(
        offset=next_offset
    )

    logger.info(
        "========== TELEGRAM BOT END =========="
    )


if __name__ == "__main__":
    main()
