import requests          # שולח בקשות HTTP (ל-API של הפלטפורמות השונות, ולאתרי חברות)
import json               # קורא וכותב את קבצי ה-state (companies.json, seen_jobs.json)
import os                 # קורא משתנה סביבה (האם seen_jobs.json כבר קיים)
from bs4 import BeautifulSoup   # מפרק HTML לעץ שאפשר לחפש בו לפי CSS selector
from common import COMPANIES_FILE, FETCH_HEADERS, send_telegram

SEEN_JOBS_FILE = "seen_jobs.json"      # קובץ ה-state - אילו משרות כבר ראינו בפעם הקודמת


def get_jobs_lever(slug: str, api_domain: str) -> list[str]:
    """שולף רשימת משרות מחברה שמשתמשת בפלטפורמת Lever, דרך ה-API הרשמי שלה (JSON נקי, בלי scraping)."""
    url = f"https://{api_domain}/v0/postings/{slug}?mode=json"     # כתובת ה-API הציבורי של Lever לחברה הזו
    r = requests.get(url, timeout=15)                               # מבצע את הבקשה, עם timeout כדי לא להיתקע
    r.raise_for_status()                                            # זורק שגיאה אם הסטטוס לא 200 (למשל slug שגוי)
    postings = r.json()                                             # ממיר את תשובת ה-API לרשימת dict-ים ב-Python
    jobs = [
        f"{p['text']} — {p.get('categories', {}).get('location', '')}"  # בונה שורה: "שם משרה — מיקום"
        for p in postings                                           # עובר על כל משרה שחזרה מה-API
    ]
    return sorted(set(jobs))                                        # מסיר כפילויות וממיין לסדר עקבי בין ריצות


def get_jobs_html(url: str, selector: str) -> list[str]:
    """שולף רשימת משרות מאתר קריירות רגיל, לפי CSS selector שמצביע על כותרות המשרות."""
    r = requests.get(url, headers=FETCH_HEADERS, timeout=15)        # מוריד את ה-HTML של עמוד הקריירות
    r.raise_for_status()                                            # זורק שגיאה אם הבקשה נכשלה
    soup = BeautifulSoup(r.text, "html.parser")                     # מפרסר את ה-HTML לעץ ניתן לחיפוש
    elements = soup.select(selector)                                # מוצא את כל האלמנטים שתואמים ל-selector
    jobs = [el.get_text(strip=True) for el in elements]             # מחלץ את הטקסט הנקי מכל אלמנט (כותרת המשרה)
    return sorted(set(jobs))                                        # מסיר כפילויות וממיין


def get_jobs_ashby(slug: str) -> list[str]:
    """שולף רשימת משרות מחברה שמשתמשת בפלטפורמת Ashby, דרך ה-API הציבורי שלה."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"   # endpoint ציבורי, לא דורש אימות
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    jobs = [f"{j['title']} — {j.get('location', '')}" for j in data.get("jobs", [])]
    return sorted(set(jobs))


def get_jobs_recruitee(company: str) -> list[str]:
    """שולף רשימת משרות מחברה שמשתמשת בפלטפורמת Recruitee, דרך ה-API הציבורי שלה."""
    url = f"https://{company}.recruitee.com/api/offers/"           # לכל חברה תת-דומיין משלה ב-Recruitee
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    jobs = [
        f"{o['title']} — {o.get('city') or o.get('location', '')}"
        for o in data.get("offers", [])
    ]
    return sorted(set(jobs))


def get_jobs_smartrecruiters(company: str) -> list[str]:
    """שולף רשימת משרות מחברה שמשתמשת בפלטפורמת SmartRecruiters, דרך ה-API הציבורי שלה."""
    url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    jobs = [
        f"{p['name']} — {p.get('location', {}).get('city', '')}"
        for p in data.get("content", [])
    ]
    return sorted(set(jobs))


def get_jobs_workday(tenant: str, wd_host: str, site: str) -> list[str]:
    """שולף רשימת משרות מחברה שמשתמשת ב-Workday, דרך ה-API הפנימי (cxs) שלה. עם pagination כי יש הגבלת limit לבקשה."""
    url = f"https://{tenant}.{wd_host}/wday/cxs/{tenant}/{site}/jobs"
    jobs = []
    offset, limit = 0, 20
    while True:
        payload = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings", [])
        jobs.extend(f"{p['title']} — {p.get('locationsText', '')}" for p in postings)
        offset += limit
        if not postings or offset >= data.get("total", 0):           # הגענו לסוף הרשימה - עוצרים
            break
    return sorted(set(jobs))


def get_jobs(company: dict) -> list[str]:
    """מפנה כל חברה לשיטת השליפה הנכונה לפי ה-type שלה ב-companies.json."""
    company_type = company.get("type")
    if company_type == "lever":
        return get_jobs_lever(company["slug"], company["api_domain"])
    if company_type == "ashby":
        return get_jobs_ashby(company["slug"])
    if company_type == "recruitee":
        return get_jobs_recruitee(company["company"])
    if company_type == "smartrecruiters":
        return get_jobs_smartrecruiters(company["company"])
    if company_type == "workday":
        return get_jobs_workday(company["tenant"], company["wd_host"], company["site"])
    return get_jobs_html(company["url"], company["selector"])       # ברירת מחדל - scraping רגיל לפי selector


def main() -> None:
    with open(COMPANIES_FILE, encoding="utf-8") as f:                # טוען את רשימת החברות למעקב
        companies = json.load(f)

    if os.path.exists(SEEN_JOBS_FILE):                              # אם כבר יש היסטוריה משמורת מריצה קודמת...
        with open(SEEN_JOBS_FILE, encoding="utf-8") as f:           # ...טוען אותה
            seen = json.load(f)
    else:
        seen = {}                                                    # אחרת מתחילים ממילון ריק (ריצה ראשונה)

    for company in companies:                                       # עובר על כל חברה ברשימה
        name = company["name"]                                      # שם החברה, משמש כמפתח ב-seen ובהודעות
        try:
            current_jobs = get_jobs(company)                        # שולף את רשימת המשרות הנוכחית של החברה
        except Exception as e:                                      # אם השליפה נכשלה (אתר לא זמין, שינוי מבנה וכו')
            print(f"שגיאה בבדיקת {name}: {e}")                      # מדפיס ללוג של GitHub Actions לצורך דיבוג
            continue                                                 # מדלג לחברה הבאה, לא עוצר את כל הריצה

        previous_jobs = seen.get(name, [])                          # רשימת המשרות שראינו לחברה הזו בפעם הקודמת
        new_jobs = [j for j in current_jobs if j not in previous_jobs]  # משרות שקיימות עכשיו אך לא היו קודם

        if new_jobs and previous_jobs:                               # שולחים התראה רק אם יש חדשות *וגם* זו לא הריצה הראשונה
            message = f"🔔 משרות חדשות ב-{name}:\n" + "\n".join(new_jobs)  # מרכיב הודעה קריאה עם כל המשרות החדשות
            send_telegram(message)                                   # שולח את ההתראה בטלגרם

        seen[name] = current_jobs                                    # מעדכן את ה-state עם התמונה העדכנית לחברה הזו

    with open(SEEN_JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)
    # שומר את ה-state המעודכן לקובץ, כדי שהריצה הבאה תדע מה כבר ראינו
    # ensure_ascii=False - שומר עברית כטקסט קריא ולא כ-\uXXXX
    # indent=2 - פורמט קריא לבני אדם אם תרצה לפתוח את הקובץ ולבדוק


if __name__ == "__main__":       # מבטיח שהפונקציה main תרוץ רק כשהסקריפט מופעל ישירות (לא כשהוא מיובא)
    main()
