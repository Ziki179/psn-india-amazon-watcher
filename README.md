# PSN India – Amazon.in watcher 🎮

בוט שבודק את **Amazon.in** כל ~5 דקות ושולח לך הודעה ב-**Telegram** כשיש גיפט קארד / PlayStation Wallet Top-Up להודו זמין לקנייה.

- בודק רשימה של מוצרים קבועים (`watchlist.txt`) – כל הסכומים הרשמיים (₹500, ₹1000, ₹2000, ₹4000, ₹12000, E-Gift Card ועוד).
- מחפש באמזון בכל ריצה ומוסיף לרשימה **אוטומטית** מוצרי PSN חדשים שהוא מוצא.
- מסנן כרטיסים של חנויות אחרות (USD, UK, UAE...) – רק חנות הודו.
- שולח הודעה בכל בדיקה שיש מלאי, והודעה כשהמלאי נגמר.
- אם אמזון מציג CAPTCHA, הבוט עוצר את הריצה מיד ומנסה שוב בריצה הבאה. אחרי 30 דקות של חסימה הוא שולח אזהרה.

---

## התקנה (5 דקות)

### 1. צור בוט בטלגרם
1. פתח את [@BotFather](https://t.me/BotFather) ושלח `/newbot`.
2. בחר שם ושם משתמש. תקבל **Token** שנראה כך: `123456789:AAH...`
3. פתח את הבוט החדש ושלח לו הודעה כלשהי (למשל `hi`). בלי זה הבוט לא יכול לשלוח לך הודעות.

### 2. מצא את ה-Chat ID שלך
פתח בדפדפן (במקום `<TOKEN>` שים את הטוקן שלך):
```
https://api.telegram.org/bot<TOKEN>/getUpdates
```
חפש את `"chat":{"id":123456789` – המספר הוא ה-Chat ID.
(אפשר גם לשלוח הודעה ל-[@userinfobot](https://t.me/userinfobot).)

### 3. הוסף את הסודות לריפו
ב-GitHub: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | הטוקן מ-BotFather |
| `TELEGRAM_CHAT_ID` | ה-Chat ID שלך (אפשר כמה, מופרדים בפסיק) |

### 4. הפעל ובדוק
1. לשונית **Actions** → אם מופיעה בקשה להפעיל workflows, לחץ **Enable**.
2. בחר **PSN India watcher** → **Run workflow** → סמן ✅ *Only send a Telegram test message* → **Run**.
   תוך דקה אמורה להגיע הודעת בדיקה בטלגרם.
3. הרץ שוב **Run workflow** בלי הסימון, ופתח את הלוג של הריצה כדי לראות מה הבוט מצא.

מעכשיו הבוט ירוץ לבד כל ~5 דקות. ✅

---

## הגדרות (לא חובה)
ב-**Settings → Secrets and variables → Actions → Variables**:

| Variable | ערכים | ברירת מחדל |
|---|---|---|
| `ALERT_MODE` | `every` – הודעה בכל בדיקה שיש מלאי · `change` – רק כשמשהו חוזר למלאי / נגמר | `every` |
| `INDIA_ONLY` | `false` כדי לקבל גם כרטיסים של חנויות אחרות | `true` |

להוספת מוצר ידנית – הוסף את ה-ASIN שלו (10 התווים אחרי `/dp/` בכתובת) לקובץ `watchlist.txt`.

---

## טוב לדעת
- **GitHub לא מדויק בתזמון** – ריצה "כל 5 דקות" יכולה להתעכב ל-10–15 דקות בשעות עומס.
- **אמזון לפעמים חוסם שרתים של GitHub** עם CAPTCHA. הבוט לא מנסה לעקוף את זה – הוא מדלג ומנסה בריצה הבאה. אם קיבלת אזהרת חסימה שנמשכת שעות, הרץ את הבוט מהמחשב שלך (למטה) – חיבור ביתי כמעט לא נחסם.
- **כרטיסי PSN להודו נדירים באמזון** – Sony מגבילה את כמות הטעינות שכל קמעונאי יכול למכור בשבוע, ולכן הם נגמרים מהר. לכן כדאי לפעול מהר כשמגיעה הודעה.
- ריפו ציבורי ב-GitHub נעצר אחרי 60 יום בלי פעילות. ה-workflow `keepalive` דואג לזה אוטומטית.

## הרצה מהמחשב / שרת (חלופה)
```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="123:ABC..."   # ב-Windows: set TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID="123456789"
python watcher.py --test          # הודעת בדיקה
python watcher.py --dry-run       # בדיקה אחת בלי לשלוח
python watcher.py --loop 300      # בדיקה כל 5 דקות, ללא הפסקה
```

## בדיקות
```bash
pip install pytest && python -m pytest -q
```
