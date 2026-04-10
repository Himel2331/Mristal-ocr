import asyncio
import logging
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from mistralai.client import Mistral
except Exception:  # pragma: no cover
    Mistral = None

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_OCR_MODEL = os.getenv("MISTRAL_OCR_MODEL", "mistral-ocr-latest")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_FILE_MB = float(os.getenv("MAX_FILE_MB", "19"))
MAX_FILE_BYTES = int(MAX_FILE_MB * 1024 * 1024)
MISTRAL_BASE_URL = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("mistral_telegram_ocr_bot")

SUPPORTED_DOC_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
    "image/avif",
}

HELP_TEXT = (
    "আমি Mistral OCR bot।\n\n"
    "যা পাঠাতে পারো:\n"
    "- ছবি\n"
    "- PDF\n"
    "- DOCX\n"
    "- PPTX\n\n"
    f"সর্বোচ্চ ফাইল সাইজ: প্রায় {MAX_FILE_MB:g} MB (Telegram download limit অনুযায়ী)।\n\n"
    "আমি OCR করে preview দেখাবো, আর পুরো result .md file হিসেবে পাঠিয়ে দেবো।"
)


class OCRBotError(Exception):
    """Domain-specific bot error."""


class MistralOCRClient:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        self.http = requests.Session()
        self.http.headers.update({"Authorization": f"Bearer {api_key}"})
        self.sdk = Mistral(api_key=api_key) if Mistral is not None else None

    @staticmethod
    def _get_attr(obj: Any, key: str, default: Any = None) -> Any:
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def _upload_via_sdk(self, file_path: Path) -> str:
        if self.sdk is None:
            raise OCRBotError("Mistral SDK not available")

        with file_path.open("rb") as fh:
            response = self.sdk.files.upload(
                file={"file_name": file_path.name, "content": fh},
                purpose="ocr",
                visibility="user",
            )

        file_id = self._get_attr(response, "id")
        if not file_id:
            raise OCRBotError("Mistral SDK upload succeeded but no file_id was returned")
        return str(file_id)

    def _upload_via_http(self, file_path: Path) -> str:
        with file_path.open("rb") as fh:
            response = self.http.post(
                f"{MISTRAL_BASE_URL}/files",
                data={"purpose": "ocr", "visibility": "user"},
                files={"file": (file_path.name, fh)},
                timeout=120,
            )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = response.text[:1000]
            raise OCRBotError(f"Mistral file upload failed: {detail}") from exc

        payload = response.json()
        file_id = payload.get("id")
        if not file_id:
            raise OCRBotError("Mistral HTTP upload succeeded but no file_id was returned")
        return str(file_id)

    def upload_file(self, file_path: Path) -> str:
        sdk_error = None
        if self.sdk is not None:
            try:
                return self._upload_via_sdk(file_path)
            except Exception as exc:  # pragma: no cover
                sdk_error = exc
                logger.warning("SDK upload failed, falling back to HTTP upload: %s", exc)

        try:
            return self._upload_via_http(file_path)
        except Exception as http_exc:
            if sdk_error is not None:
                raise OCRBotError(
                    f"Upload failed via SDK ({sdk_error}) and HTTP ({http_exc})"
                ) from http_exc
            raise

    def _ocr_via_sdk(self, file_id: str) -> Any:
        if self.sdk is None:
            raise OCRBotError("Mistral SDK not available")

        try:
            return self.sdk.ocr.process(
                model=self.model,
                document={"file_id": file_id},
                include_image_base64=False,
            )
        except Exception:
            return self.sdk.ocr.process(
                model=self.model,
                document={"type": "file_id", "file_id": file_id},
                include_image_base64=False,
            )

    def _ocr_via_http(self, file_id: str) -> Any:
        response = self.http.post(
            f"{MISTRAL_BASE_URL}/ocr",
            json={
                "model": self.model,
                "document": {"file_id": file_id},
                "include_image_base64": False,
            },
            timeout=300,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = response.text[:2000]
            raise OCRBotError(f"Mistral OCR failed: {detail}") from exc
        return response.json()

    def process_file(self, file_path: Path) -> tuple[str, str]:
        file_id = self.upload_file(file_path)
        try:
            sdk_error = None
            if self.sdk is not None:
                try:
                    response = self._ocr_via_sdk(file_id)
                except Exception as exc:  # pragma: no cover
                    sdk_error = exc
                    logger.warning("SDK OCR failed, falling back to HTTP OCR: %s", exc)
                    response = self._ocr_via_http(file_id)
            else:
                response = self._ocr_via_http(file_id)

            markdown_text = self._extract_markdown(response)
            if not markdown_text.strip():
                raise OCRBotError("OCR completed but no text was extracted")
            return markdown_text, file_id
        finally:
            with suppress(Exception):
                self.http.delete(f"{MISTRAL_BASE_URL}/files/{file_id}", timeout=60)

    def _extract_markdown(self, response: Any) -> str:
        pages = self._get_attr(response, "pages", []) or []
        parts: list[str] = []
        for page in pages:
            index = self._get_attr(page, "index", None)
            markdown = self._get_attr(page, "markdown", "") or ""
            if not markdown.strip():
                continue
            if index is not None:
                parts.append(f"# Page {index}\n\n{markdown.strip()}")
            else:
                parts.append(markdown.strip())
        return "\n\n---\n\n".join(parts).strip()


def chunk_text(text: str, limit: int = 3500) -> Iterable[str]:
    text = text.strip()
    if not text:
        return []

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit

        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    return chunks


def ensure_config() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not MISTRAL_API_KEY:
        missing.append("MISTRAL_API_KEY")
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


def is_supported_document(mime_type: str | None, file_name: str | None) -> bool:
    if mime_type in SUPPORTED_DOC_MIME_TYPES:
        return True
    if not file_name:
        return False
    suffix = Path(file_name).suffix.lower()
    return suffix in {".pdf", ".docx", ".pptx", ".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif", ".avif"}


async def send_typing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is not None:
        await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def handle_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "এই ফাইল টাইপটা এখন support করি না। ছবি, PDF, DOCX বা PPTX পাঠাও।"
    )


async def process_ocr(update: Update, context: ContextTypes.DEFAULT_TYPE, telegram_file_obj: Any, original_name: str) -> None:
    await send_typing(update, context)

    file_size = getattr(telegram_file_obj, "file_size", None)
    if file_size and file_size > MAX_FILE_BYTES:
        await update.message.reply_text(
            f"ফাইলটা অনেক বড়। {MAX_FILE_MB:g} MB-এর নিচে ফাইল পাঠাও।"
        )
        return

    await update.message.reply_text("ফাইল পেয়েছি। OCR শুরু করছি...")

    with tempfile.TemporaryDirectory(prefix="telegram_ocr_") as tmp_dir:
        local_path = Path(tmp_dir) / original_name
        tg_file = await telegram_file_obj.get_file()
        await tg_file.download_to_drive(custom_path=str(local_path))

        client = MistralOCRClient(api_key=MISTRAL_API_KEY, model=MISTRAL_OCR_MODEL)

        try:
            markdown_text, file_id = await asyncio.to_thread(client.process_file, local_path)
            logger.info("OCR success for %s with uploaded file id %s", local_path.name, file_id)
        except Exception as exc:
            logger.exception("OCR processing failed")
            await update.message.reply_text(f"OCR করতে সমস্যা হয়েছে:\n{exc}")
            return

        preview_chunks = list(chunk_text(markdown_text, 3500))
        preview = preview_chunks[0] if preview_chunks else "কোনো text পাওয়া যায়নি।"

        await update.message.reply_text(
            "OCR শেষ। নিচে preview দিলাম, আর full output file হিসেবেও পাঠাচ্ছি।"
        )
        await update.message.reply_text(preview)

        output_path = Path(tmp_dir) / f"{Path(original_name).stem}_ocr.md"
        output_path.write_text(markdown_text, encoding="utf-8")

        with output_path.open("rb") as fh:
            await update.message.reply_document(
                document=fh,
                filename=output_path.name,
                caption="Full OCR result",
            )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return
    largest_photo = update.message.photo[-1]
    await process_ocr(update, context, largest_photo, "photo.jpg")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.document:
        return

    document = update.message.document
    if not is_supported_document(document.mime_type, document.file_name):
        await handle_unsupported(update, context)
        return

    safe_name = document.file_name or "uploaded_file"
    await process_ocr(update, context, document, safe_name)


async def post_init(application: Application) -> None:
    bot = application.bot
    me = await bot.get_me()
    logger.info("Bot started as @%s", me.username)


def build_application() -> Application:
    ensure_config()

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    return application


def main() -> None:
    application = build_application()
    logger.info("Starting polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
