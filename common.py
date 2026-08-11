import os          # קורא משתני סביבה (BOT_TOKEN, CHAT_ID)
import requests   # שולח את הודעת הטלגרם

BOT_TOKEN = os.environ["BOT_TOKEN"]    # טוקן הבוט - מוזרק כ-secret דרך GitHub Actions
CHAT_ID = os.environ["CHAT_ID"]         # ה-chat_id שלך - גם secret
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"   # בסיס ה-URL לכל קריאות ה-API של טלגרם

COMPANIES_FILE = "companies.json"       # רשימת החברות למעקב - משותף לשני הסקריפטים
FETCH_HEADERS = {"User-Agent": "Mozilla/5.0"}   # חלק מהאתרים חוסמים בקשות בלי User-Agent תקין


def send_telegram(msg: str) -> None:
    """שולח הודעת טקסט אל הצ'אט שלך דרך ה-API של טלגרם."""
    requests.post(f"{TELEGRAM_API}/sendMessage", data={"chat_id": CHAT_ID, "text": msg})
