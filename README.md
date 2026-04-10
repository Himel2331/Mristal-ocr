# Mistral Telegram OCR Bot

Mistral OCR API আর Telegram Bot API ব্যবহার করে বানানো একটা OCR bot।

## কী কী করতে পারে

- ছবি OCR করতে পারে
- PDF OCR করতে পারে
- DOCX / PPTX OCR করতে পারে
- OCR result preview দেখায়
- Full OCR output `.md` file আকারে ফেরত দেয়
- Render background worker হিসেবে deploy করা যায়

## Environment variables

- `TELEGRAM_BOT_TOKEN`
- `MISTRAL_API_KEY`
- `MISTRAL_OCR_MODEL` (default: `mistral-ocr-latest`)
- `MAX_FILE_MB` (default: `19`)
- `LOG_LEVEL` (default: `INFO`)

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

## Render deploy

### Option 1: Blueprint (`render.yaml`) দিয়ে

1. এই project GitHub-এ push করো
2. Render dashboard এ যাও
3. **New > Blueprint** সিলেক্ট করো
4. repo connect করো
5. `TELEGRAM_BOT_TOKEN` আর `MISTRAL_API_KEY` set করো
6. deploy করো

### Option 2: Manual background worker

- **Service type:** Background Worker
- **Runtime:** Python
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `python app.py`

তারপর env vars add করো:

- `PYTHON_VERSION=3.13.5`
- `TELEGRAM_BOT_TOKEN=...`
- `MISTRAL_API_KEY=...`
- `MISTRAL_OCR_MODEL=mistral-ocr-latest`
- `MAX_FILE_MB=19`

## Telegram bot setup

1. Telegram এ `@BotFather` open করো
2. `/newbot` চালাও
3. bot name আর username দাও
4. যে token পাবে, সেটা `TELEGRAM_BOT_TOKEN`

## Notes

- Bot API দিয়ে bot সাধারণত 20 MB পর্যন্ত file download করতে পারে, তাই bot-side download limit ধরেই default `MAX_FILE_MB=19` রাখা হয়েছে।
- Long OCR result হলে bot preview + full markdown file দুইটাই পাঠায়।
- Mistral upload + OCR এর পরে uploaded file delete করার চেষ্টা করে, যাতে অপ্রয়োজনীয় storage জমে না থাকে।
