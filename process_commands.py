from __future__ import annotations   # מאפשר type hints כמו `dict | None` גם אם מריצים מקומית על Python ישן יותר מ-3.10
import re          # לחילוץ slug/tenant/company מתוך צורות URL שונות
import requests   # שולח בקשות HTTP לטלגרם (קריאת הודעות) ולעמודי קריירות
import json        # קורא וכותב את companies.json ואת telegram_state.json
import os          # קורא משתנה סביבה (ANTHROPIC_API_KEY)
from urllib.parse import urljoin, urlparse   # להשלמת קישורים יחסיים לכתובות מלאות, ולפירוק host/path
from bs4 import BeautifulSoup                 # מפרק HTML כדי לחפש בו קישורים לפלטפורמות גיוס ידועות
from common import TELEGRAM_API, COMPANIES_FILE, FETCH_HEADERS, send_telegram

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")   # אופציונלי - נדרש רק לזיהוי אוטומטי של אתרים לא מוכרים

STATE_FILE = "telegram_state.json"      # שומר איזה update_id כבר טופל, כדי לא לעבד פקודה פעמיים

# רמזים לזיהוי קישור לפלטפורמת גיוס ידועה בתוך HTML של עמוד קריירות עצמאי של חברה
ATS_DOMAIN_HINTS = [
    "jobs.eu.lever.co", "jobs.lever.co", "comeet.com", "greenhouse.io",
    "jobs.ashbyhq.com", "recruitee.com", "smartrecruiters.com", "myworkdayjobs.com",
]

# שני הדומיינים של Lever - זהים בצורת הזיהוי, שונים רק ב-API domain
LEVER_DOMAINS = [
    ("jobs.eu.lever.co/", "api.eu.lever.co"),   # גרסה אירופאית (כמו מובילאיי)
    ("jobs.lever.co/", "api.lever.co"),          # גרסה גלובלית/אמריקאית
]


def get_updates(offset: int) -> list[dict]:
    """מביא מטלגרם את כל ההודעות החדשות שנשלחו לבוט מאז update_id מסוים."""
    url = f"{TELEGRAM_API}/getUpdates"                              # endpoint לקבלת הודעות ממתינות
    params = {"offset": offset, "timeout": 0}                       # offset מונע קבלת הודעות שכבר טופלו
    r = requests.get(url, params=params, timeout=15)                # מבצע את הבקשה
    r.raise_for_status()                                            # זורק שגיאה אם הבקשה נכשלה
    return r.json()["result"]                                       # מחזיר את רשימת ההודעות עצמה


def detect_platform_from_url(url: str) -> dict | None:
    """מזהה פלטפורמת גיוס ישירות לפי צורת ה-URL עצמו, בלי לגשת לרשת."""
    for prefix, api_domain in LEVER_DOMAINS:                        # Lever - אותה לוגיקה בשני הדומיינים האפשריים
        if prefix in url:
            slug = url.rstrip("/").split("/")[-1]                   # שם החברה כפי שמופיע ב-URL (למשל "mobileye")
            return {"type": "lever", "slug": slug, "api_domain": api_domain}

    if "comeet.com" in url:                                         # פלטפורמת Comeet - נפוצה בישראל
        return {"type": "html", "selector": ".position-name"}

    if "greenhouse.io" in url:                                      # פלטפורמת Greenhouse
        return {"type": "html", "selector": ".opening a"}

    if "jobs.ashbyhq.com/" in url:                                  # פלטפורמת Ashby - נפוצה בסטארטאפים
        slug = url.rstrip("/").split("/")[-1].split("?")[0]
        return {"type": "ashby", "slug": slug}

    m = re.search(r"([\w-]+)\.recruitee\.com", url)                 # פלטפורמת Recruitee - יש לה תת-דומיין לכל חברה
    if m:
        return {"type": "recruitee", "company": m.group(1)}

    m = re.search(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)", url)   # פלטפורמת SmartRecruiters
    if m:
        return {"type": "smartrecruiters", "company": m.group(1)}

    workday = detect_workday(url)                                   # Workday - צריך פירוק ייעודי (tenant/site)
    if workday:
        return workday

    return None                                                      # לא זוהתה פלטפורמה מוכרת מה-URL עצמו


def detect_workday(url: str) -> dict | None:
    """מפרק URL של Workday לרכיבים הדרושים לקריאת ה-API (tenant, wd_host, site)."""
    parsed = urlparse(url)
    host_match = re.match(r"([\w-]+)\.(wd\d+\.myworkdayjobs\.com)$", parsed.netloc)   # למשל mobileye.wd5.myworkdayjobs.com
    if not host_match:
        return None

    tenant, wd_host = host_match.groups()
    path_parts = [p for p in parsed.path.split("/") if p]           # מפרק את הנתיב לרכיבים (למשל ["en-US", "External"])
    if not path_parts:
        return None

    site = path_parts[0]
    if re.match(r"^[a-z]{2}-[A-Z]{2}$", site) and len(path_parts) > 1:   # אם הרכיב הראשון הוא קוד שפה כמו en-US...
        site = path_parts[1]                                        # ...הרכיב הבא הוא שם ה-site האמיתי

    return {"type": "workday", "tenant": tenant, "wd_host": wd_host, "site": site}


def find_ats_link_in_page(html: str, base_url: str) -> str | None:
    """סורק עמוד קריירות עצמאי של חברה ומחפש קישור/iframe שמפנה לפלטפורמת גיוס ידועה."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["a", "iframe"]):                      # קישורים רגילים או iframe מוטמע לפלטפורמה חיצונית
        link = tag.get("href") or tag.get("src")
        if not link:
            continue
        full_link = urljoin(base_url, link)                         # משלים קישורים יחסיים לכתובת מלאה
        if any(hint in full_link for hint in ATS_DOMAIN_HINTS):
            return full_link
    return None


def infer_selector_with_llm(html: str, url: str) -> str | None:
    """מוצא CSS selector אוטומטית באמצעות מודל שפה, כשלא זוהתה אף פלטפורמה מוכרת."""
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    "This is the HTML of a company careers page. Find a single CSS selector "
                    "that matches every job listing title on the page (one match per open job "
                    "posting, no duplicates, no unrelated elements). "
                    "Reply with ONLY the raw CSS selector and nothing else. "
                    "If there is no repeating job-listing pattern you can identify, reply with "
                    "exactly: NONE\n\n"
                    f"Page URL: {url}\n\nHTML:\n{html[:15000]}"
                ),
            }],
        )
        selector = response.content[0].text.strip()
        return None if selector == "NONE" else selector
    except Exception:
        return None                                                  # כל כשל בקריאה ל-LLM = לא הצלחנו לזהות, ממשיכים הלאה


def validate_selector(html: str, selector: str) -> bool:
    """בודק שה-selector שחזר (בין אם מ-detect_platform_from_url ובין אם מה-LLM) באמת תופס אלמנטים."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        return len(soup.select(selector)) > 0
    except Exception:
        return False


def build_company_entry(name: str, url: str) -> dict | None:
    """מנסה בכל הדרכים האפשריות לזהות איך לשלוף משרות מהחברה, בלי לערב אף פעם משתמש. מחזיר None אם נכשל לגמרי."""
    detected = detect_platform_from_url(url)                        # שלב 1: זיהוי ישיר מצורת ה-URL
    if detected:
        return {"name": name, "url": url, **detected}

    try:
        r = requests.get(url, headers=FETCH_HEADERS, timeout=15)    # שלב 2: מוריד את העמוד עצמו לניתוח
        r.raise_for_status()
        html = r.text
    except Exception:
        return None                                                  # אין גישה בכלל לעמוד - אין מה לנתח

    llm_url, llm_html = url, html                                    # ברירת מחדל: העמוד המקורי שקיבלנו מהמשתמש

    ats_link = find_ats_link_in_page(html, url)                     # מחפש קישור מוטמע לפלטפורמת גיוס כלשהי
    if ats_link:
        detected = detect_platform_from_url(ats_link)
        if detected:                                                 # הפלטפורמה מוכרת לנו במפורש (Lever/Ashby/וכו')
            return {"name": name, "url": ats_link, **detected}

        try:                                                          # פלטפורמה לא מוכרת במפורש (Personio/Workable/וכו')
            r2 = requests.get(ats_link, headers=FETCH_HEADERS, timeout=15)
            r2.raise_for_status()
            llm_url, llm_html = ats_link, r2.text                    # ה-LLM צריך לנתח את עמוד המשרות עצמו, לא את עמוד הבית
        except Exception:
            pass                                                      # לא הצלחנו לגשת לעמוד הפלטפורמה - ננסה LLM על מה שכבר יש לנו

    selector = infer_selector_with_llm(llm_html, llm_url)            # שלב 3 (מוצא אחרון): מבקש מ-LLM לזהות selector
    if selector and validate_selector(llm_html, selector):
        return {"name": name, "url": llm_url, "type": "html", "selector": selector}

    return None                                                      # לא הצלחנו לזהות בשום דרך אוטומטית


def handle_add(parts: list[str], companies: list[dict]) -> bool:
    """מטפל בפקודת /add <שם> <URL>. מזהה אוטומטית איך לשלוף משרות. מחזיר True אם השתנתה הרשימה."""
    if len(parts) < 3:                                               # פקודה חסרה ארגומנטים
        send_telegram("שימוש: /add <שם חברה> <URL>")
        return False

    name, url = parts[1], parts[2]                                   # שם החברה וכתובת עמוד הקריירות שלה

    if any(c["name"].lower() == name.lower() for c in companies):    # בודק אם החברה כבר קיימת ברשימה
        send_telegram(f"⚠️ {name} כבר ברשימה")
        return False

    entry = build_company_entry(name, url)                           # כל הזיהוי האוטומטי קורה כאן
    if entry is None:
        send_telegram(
            f"❌ לא הצלחתי לזהות אוטומטית איך לשלוף משרות מ-{name} ({url}).\n"
            f"נסה כתובת אחרת של עמוד הקריירות שלהם."
        )
        return False

    companies.append(entry)                                           # מוסיף את הרשומה החדשה לרשימת החברות
    send_telegram(f"✅ נוספה {name} למעקב (זוהתה פלטפורמה: {entry['type']})")
    return True                                                       # מסמן שהרשימה השתנתה - תישמר לקובץ


def handle_list(companies: list[dict]) -> None:
    """מטפל בפקודת /list - שולח את רשימת כל החברות הנמצאות כרגע במעקב."""
    names = "\n".join(c["name"] for c in companies) or "(ריק)"        # מרכיב רשימת שמות, או "(ריק)" אם אין אף אחת
    send_telegram(f"חברות במעקב:\n{names}")                          # שולח את הרשימה בטלגרם


def handle_remove(parts: list[str], companies: list[dict]) -> tuple[list[dict], bool]:
    """מטפל בפקודת /remove <שם חברה>. מחזיר את הרשימה המעודכנת ודגל אם היא השתנתה."""
    if len(parts) < 2:                                                # פקודה חסרה שם חברה
        send_telegram("שימוש: /remove <שם חברה>")
        return companies, False

    name = parts[1]                                                   # שם החברה להסרה
    before = len(companies)                                           # גודל הרשימה לפני ההסרה, לצורך השוואה
    companies = [c for c in companies if c["name"].lower() != name.lower()]  # מסנן החוצה את החברה המבוקשת

    if len(companies) < before:                                       # אם משהו באמת הוסר...
        send_telegram(f"🗑️ הוסרה {name}")
        return companies, True                                        # מסמן שהרשימה השתנתה
    send_telegram(f"לא מצאתי {name} ברשימה")                         # לא נמצאה חברה כזו
    return companies, False


def main() -> None:
    with open(STATE_FILE, encoding="utf-8") as f:                     # טוען את ה-update_id האחרון שטופל
        state = json.load(f)
    with open(COMPANIES_FILE, encoding="utf-8") as f:                 # טוען את רשימת החברות הנוכחית
        companies = json.load(f)

    updates = get_updates(state["last_update_id"] + 1)                # מביא רק הודעות חדשות שעדיין לא טופלו

    changed = False                                                   # דגל: האם companies.json צריך להישמר מחדש

    for update in updates:                                            # עובר על כל הודעה חדשה שהתקבלה
        state["last_update_id"] = update["update_id"]                 # מעדכן את המצב כדי לא לעבד שוב את ההודעה הזו

        message = update.get("message", {})                           # מחלץ את גוף ההודעה (יכול להיות ריק)
        text = message.get("text", "")                                # מחלץ את הטקסט שנשלח (יכול להיות ריק)

        if text.startswith("/add"):                                   # פקודת הוספת חברה
            parts = text.split(maxsplit=2)                             # מפצל ל: ["/add", שם, url]
            if handle_add(parts, companies):                          # מטפל בפקודה, מחזיר True אם היה שינוי
                changed = True                                        # מעדכן את הדגל הכללי

        elif text.startswith("/list"):                                # פקודת הצגת כל החברות
            handle_list(companies)                                    # לא משנה את הרשימה, רק שולח אותה

        elif text.startswith("/remove"):                              # פקודת הסרת חברה
            parts = text.split(maxsplit=1)                             # מפצל ל: ["/remove", שם]
            companies, was_changed = handle_remove(parts, companies)  # מטפל בפקודה
            if was_changed:
                changed = True                                        # מעדכן את הדגל הכללי

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)
    # שומר תמיד את ה-state המעודכן (last_update_id), גם אם לא היו פקודות /add או /remove בפועל

    if changed:                                                        # שומר את companies.json רק אם ממש השתנה
        with open(COMPANIES_FILE, "w", encoding="utf-8") as f:
            json.dump(companies, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":        # מריץ את main רק כשהסקריפט מופעל ישירות
    main()
