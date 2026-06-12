#!/usr/bin/env python3
"""
Run this script ONCE to register the Telegram webhook with your bot.
Usage: python setup_webhook.py

Optionally set TELEGRAM_WEBHOOK_SECRET (same value as on the server) to
protect the webhook endpoint from third-party requests.
"""
import os, httpx, sys

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
APP_URL = os.environ.get("APP_URL", "")  # e.g. https://your-app.railway.app
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

if not BOT_TOKEN or not APP_URL:
    print("ERROR: Set TELEGRAM_BOT_TOKEN and APP_URL environment variables")
    sys.exit(1)

webhook_url = f"{APP_URL}/api/telegram/webhook"
payload = {"url": webhook_url}
if WEBHOOK_SECRET:
    payload["secret_token"] = WEBHOOK_SECRET
resp = httpx.post(
    f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
    json=payload
)
print(resp.json())
