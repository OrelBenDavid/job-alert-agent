# job-alert-agent

בוט טלגרם שעוקב אחרי דפי קריירה של חברות ומתריע על משרות חדשות (רלוונטיות
לישראל, כולל remote מסויג). רץ כולו על GitHub Actions - אין שרת.

## איך זה עובד

- **פרופיל = רישום.** כל חברה היא קובץ `profiles/<slug>.json`, מבנה
  מלא ב-`SKILL v2` של `career-site-profiler`. אין `companies.json` נפרד.
- **ה-dispatcher** (`src/fetchers/__init__.py`) קורא `fetch_type` מהפרופיל
  ומפנה למודול המתאים (`api.py` / `html.py` / `browser.py`), שקוראים בעצמם
  שדות נוספים (`platform`, `pagination.method`, `israel_filter.structure`)
  כדי לבחור אסטרטגיה. חברה חדשה על שילוב קיים = קובץ JSON חדש, אפס קוד.
- **הזיהוי "משרה חדשה"** רץ תמיד על `Job.id` (מזהה ATS או קישור מנורמל),
  לעולם לא על טקסט תצוגה - כדי ששינוי קוסמטי אצל החברה לא יגרום להתראה
  כפולה.
- **שער בריאות**: אם חברה שהחזירה משרות בעבר מחזירה פתאום 0, זה נחשב
  כשל (סלקטור שנשבר), לא "אין משרות". ה-state לא נדרס, ומתקבלת התראת
  תחזוקה אחרי שני כשלים רצופים.

## הוספת חברה

1. פתח שיחה עם Claude, בקש להוסיף את החברה - זה מפעיל את הסקיל
   `career-site-profiler`.
2. שמור את ה-`profile.json` שהתקבל תחת `profiles/<slug>.json`.
3. הרץ `python run.py --seed` (ידני, דרך `workflow_dispatch` או מקומית)
   כדי לזרוע state בלי לשלוח התראות על כל המשרות הקיימות.
4. מהריצה הבאה של ה-cron החברה נכנסת למעקב הרגיל.

**`/add` בטלגרם לא עושה את זה אוטומטית (v1 מכוון)** - הוא רק אומר
שצריך סשן פרופיילינג ידני. `/remove <slug>` מכבה חברה (`enabled: false`)
בלי למחוק היסטוריה. `/list` מציג את כל החברות וסטטוס שלהן (שמות בלבד,
לא משרות). `/jobs <slug>` שולף בזמן אמת (לא מה-state) את המשרות הפתוחות
הרלוונטיות לישראל כרגע אצל אותה חברה - עד 20 בהודעה אחת (מגבלת 4096
תווים של טלגרם), עם קישור לעמוד הקריירות המלא אם יש יותר.

**פקודות טלגרם לא רצות ברקע.** אין תהליך שמאזין - הן נקראות פעם בכל
ריצה של ה-cron (piggyback), אז זמן התגובה הוא עד מרווח ה-cron (3 שעות)
+ עיכוב GitHub הרגיל. זו החלטת עיצוב מכוונת: cron ייעודי לפקודות היה
שורף לבד את כל מכסת ה-Actions של רפו פרטי.

## הרצה מקומית

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium   # רק אם יש חברת playwright
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
cd src
python run.py --seed   # בפעם הראשונה
python run.py          # ריצה רגילה
```

## בדיקות

```bash
cd tests && python -m pytest -v
```

## סטטוס נוכחי

- `mobileye`, `wiz` — פרופילי API (Lever/Greenhouse), **אומתו חי** ב-2026-08-11:
  מובילאיי מחזירה 122 משרות רלוונטיות לישראל (`expected_min_jobs: 20`,
  `zero_is_plausible: false`), Wiz מחזירה 0 (ה-board האמיתי שלה מכיל כרגע
  רק 2 משרות בסך הכל, שתיהן מחוץ לישראל - `zero_is_plausible: true`
  נשאר נכון). ה-state כבר נזרע (`state/seed/`), אין seed gap.
- `wix` — טרם קיים פרופיל. הטרייס הישן (`investigation_playbook.md`)
  לא כלל את פילטר ה-Israel לפי v2 - דורש פרופיילינג מחדש מלא.
