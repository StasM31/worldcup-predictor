#!/usr/bin/env python3
"""
Run this script ONCE to register the Telegram webhook with your bot.
Usage: python setup_webhook.py
"""
import os, httpx, sys

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
APP_URL = os.environ.get("APP_URL", "")  # e.g. https://your-app.railway.app

if not BOT_TOKEN or not APP_URL:
    print("ERROR: Set TELEGRAM_BOT_TOKEN and APP_URL environment variables")
    sys.exit(1)

webhook_url = f"{APP_URL}/api/telegram/webhook"
resp = httpx.post(
    f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
    json={"url": webhook_url}
)
print(resp.json())
