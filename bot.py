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
# TELEGRAM API
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
# URL DETECTION
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
# GOOGLE DRIVE DUPLICATE CHECK
# ============================================================

def drive_file_exists(
    drive_service,
    file_name,
):
    escaped_name = file_name.replace(
        "'",
        "''",
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
# PROCESS ONE VIDEO
# ============================================================

def process_video(
    drive_service,
    chat_id,
    url,
):
    if not chat_id:
        logger.error(
            "No Telegram chat ID."
        )
        return

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

    logger.info(
        "Received URL: %s",
        url,
    )

    video_path = None

    try:
        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        send_message(
            chat_id,
            "⬇️ Downloading...",
        )

        video_path = download_video(
            url
        )

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

        # ----------------------------------------------------
        # SAFE RENAME
        # ----------------------------------------------------

        new_name = safe_filename(
            video_path.stem
        )

        new_name += video_path.suffix.lower()

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

        # ----------------------------------------------------
        # DUPLICATE CHECK
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # GOOGLE DRIVE UPLOAD
        # ----------------------------------------------------

        send_message(
            chat_id,
            "☁️ Uploading to Google Drive...",
        )

        uploaded = upload_to_drive(
            drive_service,
            video_path,
        )

        logger.info(
            "Drive upload successful: %s",
            uploaded,
        )

        send_message(
            chat_id,
            "✅ Added to 01_INPUT",
        )

        send_message(
            chat_id,
            "🤖 AI processing pending",
        )

        # ----------------------------------------------------
        # DELETE LOCAL FILE
        # ----------------------------------------------------

        video_path.unlink(
            missing_ok=True
        )

        video_path = None

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

    finally:
        # Clean up any leftover downloaded file.
        if video_path is not None:
            try:
                video_path.unlink(
                    missing_ok=True
                )
            except Exception:
                pass


# ============================================================
# MAIN
# ============================================================

def main():
    logger.info(
        "========== TELEGRAM BOT START =========="
    )

    # --------------------------------------------------------
    # Cloudflare Worker → GitHub repository_dispatch
    # provides these environment variables.
    # --------------------------------------------------------

    telegram_url = os.environ.get(
        "TELEGRAM_URL",
        "",
    ).strip()

    telegram_chat_id = os.environ.get(
        "TELEGRAM_CHAT_ID",
        "",
    ).strip()

    # --------------------------------------------------------
    # No Telegram polling.
    #
    # Telegram → Cloudflare → GitHub Actions
    # is now the complete communication path.
    # --------------------------------------------------------

    if not telegram_url:
        logger.info(
            "No TELEGRAM_URL found."
        )

        logger.info(
            "Nothing to process."
        )

        return

    if not telegram_chat_id:
        logger.error(
            "No TELEGRAM_CHAT_ID found."
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
        "Telegram event received."
    )

    logger.info(
        "URL: %s",
        telegram_url,
    )

    # --------------------------------------------------------
    # Connect to Google Drive
    # --------------------------------------------------------

    drive_service = (
        get_drive_service()
    )

    # --------------------------------------------------------
    # Process video
    # --------------------------------------------------------

    process_video(
        drive_service,
        chat_id,
        telegram_url,
    )

    logger.info(
        "========== TELEGRAM BOT END =========="
    )


# ============================================================
# PROGRAM START
# ============================================================

if __name__ == "__main__":
    main()
