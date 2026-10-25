#!/usr/bin/env python3
"""
PSN India gift-card watcher for Amazon.in -> Telegram alerts.

Checks a watchlist of known PlayStation Store / PSN Wallet Top-Up products on
amazon.in, plus a few searches to discover new listings, and sends a Telegram
message whenever something is available to buy.

Environment variables:
  TELEGRAM_BOT_TOKEN   (required) token from @BotFather
  TELEGRAM_CHAT_ID     (required) your chat id; several ids may be comma separated
  ALERT_MODE           "every"  -> alert on every check while something is in stock (default)
                       "change" -> alert only when an item becomes available / sells out
  INDIA_ONLY           "true" (default) ignore listings for non-Indian stores (USD, UK, UAE...)
  STATE_FILE           path of the state file (default: state.json)

  BOT_TIMEZONE         time zone for times shown in Telegram (default: Asia/Jerusalem)

Telegram commands (only from the chat ids in TELEGRAM_CHAT_ID):
  /check  check Amazon now and reply with what is available
  /status last check result     /menu  buttons     /help  help

Usage:
  python watcher.py                 # one check, no commands
  python watcher.py --listen 280    # one check, then answer Telegram commands for 280 s (GitHub Actions)
  python watcher.py --loop 300      # forever: check every 300 s and answer commands (own PC/server)
  python watcher.py --test          # send a test message to Telegram and exit
  python watcher.py --dry-run       # do not send stock alerts
"""
from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

import requests as http

BASE = "https://www.amazon.in"
ROOT = Path(__file__).resolve().parent
WATCHLIST_FILE = ROOT / "watchlist.txt"
STATE_FILE = Path(os.environ.get("STATE_FILE", ROOT / "state.json"))

SEARCH_QUERIES = [
    "playstation store gift card",
    "playstation wallet top up",
    "psn wallet top up india",
    "sony playstation network wallet",
    "playstation wallet digital code",
]

# A title must match PS_RE and CARD_RE, and must not match EXCLUDE_RE.
PS_RE = re.compile(r"playstation|\bpsn\b|\bps ?store\b|\bps[45]\b|\bsony\b", re.I)
CARD_RE = re.compile(
    r"gift ?card|wallet|top[- ]?up|voucher|store credit|\bpsn (card|code|key)|redeem|e-?gift|digital code",
    re.I,
)
EXCLUDE_RE = re.compile(
    r"memory card|controller|stand|holder|cover|skin|case\b|adapter|charger|cable|console|headset|"
    r"\bedition\b|\bdisc\b|steam|xbox|nintendo|google play|roblox|razer",
    re.I,
)
FOREIGN_RE = re.compile(
    r"\b(usd|usa|us|uk|gbp|eur|euro|aed|uae|sar|ksa|saudi|turkey|try|hkd|jpy|japan|cad|aud|"
    r"europe|german|france|poland|brazil|kuwait|qatar|oman|bahrain)\b|\$|£|€",
    re.I,
)
# "Sony Interactive Entertainment Europe" is just the publisher name on the official Indian cards
PUBLISHER_NOISE_RE = re.compile(r"sony interactive entertainment europe", re.I)

CAPTCHA_MARKERS = ("validateCaptcha", "Type the characters you see in this image",
                   "Enter the characters you see below", "api-services-support@amazon.com")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"

BLOCK_ALERT_AFTER = 6          # consecutive fully-blocked runs before warning (6 x 5 min = 30 min)
BLOCK_REALERT_SECONDS = 12 * 3600


# --------------------------------------------------------------------------- helpers

def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def now_ts() -> int:
    return int(time.time())


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def load_watchlist() -> dict[str, str]:
    """Returns {asin: note} from watchlist.txt (lines: ASIN  # optional note)."""
    items: dict[str, str] = {}
    if WATCHLIST_FILE.exists():
        for line in WATCHLIST_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            asin, _, note = line.partition("#")
            asin = asin.strip().upper()
            if re.fullmatch(r"[A-Z0-9]{10}", asin):
                items[asin] = note.strip()
    return items


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def is_relevant_title(title: str, india_only: bool) -> bool:
    if not (PS_RE.search(title) and CARD_RE.search(title)):
        return False
    if EXCLUDE_RE.search(title):
        return False
    if india_only and FOREIGN_RE.search(PUBLISHER_NOISE_RE.sub("", title)):
        return False
    return True


# --------------------------------------------------------------------------- amazon

class Blocked(Exception):
    pass


class Amazon:
    def __init__(self) -> None:
        self.s = http.Session()
        self.s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-IN,en;q=0.9",
        })

    def get(self, path: str) -> tuple[int, str]:
        """Plain GET. If Amazon answers with a CAPTCHA / rate-limit, raise Blocked and the
        run stops right away (no retries) - we back off until the next scheduled run."""
        url = path if path.startswith("http") else BASE + path
        try:
            r = self.s.get(url, timeout=30, allow_redirects=True)
            status, text = r.status_code, r.text
        except Exception as e:  # network error
            log(f"  network error on {url}: {e}")
            status, text = 0, ""
        if status in (0, 429, 503) or (status != 404 and any(m in text for m in CAPTCHA_MARKERS)):
            log(f"  blocked/captcha (status {status}) on {url} - stopping this run")
            raise Blocked(url)
        return status, text

    @staticmethod
    def pause() -> None:
        time.sleep(random.uniform(1.5, 3.5))

    # ---- product page
    def product(self, asin: str) -> dict:
        status, text = self.get(f"/dp/{asin}")
        info = {"asin": asin, "url": f"{BASE}/dp/{asin}", "status": "unknown",
                "title": "", "price": "", "seller": "", "note": ""}
        if status == 404:
            info["status"] = "gone"
            return info
        soup = BeautifulSoup(text, "lxml")
        info["title"] = clean(soup.select_one("#productTitle").get_text()) if soup.select_one("#productTitle") else ""
        info.update(parse_product(soup))
        return info

    # ---- search page
    def search(self, query: str) -> list[dict]:
        status, text = self.get("/s?k=" + urllib.parse.quote_plus(query))
        if status != 200:
            return []
        soup = BeautifulSoup(text, "lxml")
        results = parse_search(soup)
        if not results:
            t = clean(soup.title.get_text()) if soup.title else ""
            log(f"  search page had no results ({len(text)} bytes, title: {t[:80]})")
        return results


def first_text(soup: BeautifulSoup, selectors: list[str]) -> str:
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            t = clean(el.get("value") if el.name == "input" else el.get_text())
            if t:
                return t
    return ""


def parse_product(soup: BeautifulSoup) -> dict:
    """Decide availability of a product page. Returns status/price/seller/note."""
    # availability text (only the visible spans - #availability also contains scripts)
    avail_el = soup.select_one("#availability")
    avail = ""
    if avail_el:
        for s in avail_el.find_all(["script", "style"]):
            s.decompose()
        avail = clean(avail_el.get_text())

    oos_el = soup.select_one("#outOfStock")
    has_buy = bool(soup.select_one("#add-to-cart-button, #buy-now-button"))
    other_sellers = bool(soup.select_one("#buybox-see-all-buying-choices, #buybox-see-all-buying-choices-announce"))

    price = first_text(soup, [
        "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-offscreen",
        "#corePrice_feature_div .a-offscreen",
        "#apex_desktop .priceToPay .a-offscreen",
        "#price_inside_buybox",
        "#newBuyBoxPrice",
        'input[name="items[0.base][customerVisiblePrice][displayString]"]',
    ])
    seller = first_text(soup, [
        "#sellerProfileTriggerId",
        "#merchantInfoFeature_feature_div .offer-display-feature-text-message",
        "#merchant-info",
    ])

    low = avail.lower()
    if oos_el or "currently unavailable" in low or "out of stock" in low:
        status = "unavailable"
        price = ""  # price shown on an OOS page usually belongs to another product
    elif has_buy:
        status = "available"
    elif other_sellers:
        status = "available"
        avail = avail or "Available from other sellers (See All Buying Options)"
    elif "in stock" in low or "left in stock" in low or "usually dispatched" in low:
        status = "available"
    else:
        status = "unknown"
    return {"status": status, "price": price, "seller": seller, "note": avail[:120]}


def parse_search(soup: BeautifulSoup) -> list[dict]:
    results = []
    cards = soup.select('div[data-component-type="s-search-result"]') or soup.select("div.s-result-item[data-asin]")
    for d in cards:
        asin = (d.get("data-asin") or "").strip()
        h2 = d.select_one("h2")
        if not asin or not h2:
            continue
        title = clean(h2.get("aria-label") or h2.get_text())
        price_el = d.select_one(".a-price .a-offscreen")
        text = clean(d.get_text()).lower()
        results.append({
            "asin": asin,
            "title": title,
            "price": clean(price_el.get_text()) if price_el else "",
            # Amazon.in search hides most out-of-stock items; a visible price means buyable
            "buyable": bool(price_el) and "currently unavailable" not in text,
        })
    return results


# --------------------------------------------------------------------------- telegram

TZ_NAME = os.environ.get("BOT_TIMEZONE", "Asia/Jerusalem")
MANUAL_CHECK_COOLDOWN = 60  # seconds between manual checks


def tg(method: str, **params):
    """Call the Telegram Bot API. Returns the 'result' field, or None on error."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None
    data = json.dumps(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=int(params.get("timeout", 0)) + 20) as r:
            return json.loads(r.read()).get("result")
    except Exception as e:
        body = getattr(e, "read", lambda: b"")()
        log(f"telegram {method} FAILED: {e} {body[:200]!r}")
        return None


def allowed_chats() -> list[str]:
    return [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]


def telegram_send(text: str, buttons: list | None = None, chat: str | int | None = None) -> None:
    """Send a message (with optional inline buttons) to one chat, or to all configured chats."""
    chats = [str(chat)] if chat is not None else allowed_chats()
    if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or not chats:
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing - printing message instead:\n" + text)
        return
    for c in chats:
        params = {"chat_id": c, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        if tg("sendMessage", **params) is not None:
            log(f"telegram: sent to {c}")


# ---- texts & buttons (Hebrew UI)

MENU_BUTTONS = [
    [{"text": "🔄 בדוק עכשיו", "callback_data": "check"}],
    [{"text": "📋 מצב אחרון", "callback_data": "status"}, {"text": "❓ עזרה", "callback_data": "help"}],
]

HELP_TEXT = (
    "🤖 <b>בוט מעקב גיפט קארדים PSN – אמזון הודו</b>\n\n"
    "הבוט בודק את Amazon.in כל כמה דקות ושולח התראה ברגע שיש גיפט קארד של PlayStation Store הודו במלאי.\n\n"
    "<b>פקודות:</b>\n"
    "/check – בדיקה עכשיו ומה זמין כרגע\n"
    "/status – תוצאות הבדיקה האחרונה\n"
    "/menu – תפריט הכפתורים\n"
    "/help – ההסבר הזה\n\n"
    "ℹ️ לפעמים אמזון חוסם בדיקה (בדיקת רובוט). במקרה כזה הבוט לא שולח התראה שגויה ומנסה שוב בבדיקה הבאה."
)

WELCOME_TEXT = "👋 <b>היי!</b> אני עוקב בשבילך אחרי גיפט קארדים של PlayStation Store הודו באמזון.\nבחר פעולה:"

STATUS_ICON = {"available": "✅", "unavailable": "❌", "gone": "🚫", "unknown": "❔", "unchecked": "⚠️"}
STATUS_TEXT = {"available": "זמין!", "unavailable": "אזל", "gone": "המוצר הוסר מאמזון",
               "unknown": "לא ברור", "unchecked": "לא נבדק (אמזון חסם)"}


def now_local() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TZ_NAME))
    except Exception:
        return datetime.now(timezone.utc)


def fmt_time(ts: float) -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(ts, ZoneInfo(TZ_NAME)).strftime("%H:%M · %d/%m")
    except Exception:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M UTC · %d/%m")


def fmt_duration(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 1:
        return "פחות מדקה"
    if m < 60:
        return f"{m} דקות"
    h, m = divmod(m, 60)
    return f"{h} שעות ו-{m} דקות" if m else f"{h} שעות"


AMOUNT_RE = re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]{3,6})|([\d,]{3,6})\s*(?:rs|inr|₹)", re.I)


def short_label(title: str, asin: str = "") -> str:
    m = AMOUNT_RE.search(title or "")
    if m:
        amount = (m.group(1) or m.group(2)).replace(",", "")
        return f"₹{int(amount):,}"
    if re.search(r"e-?gift", title or "", re.I):
        return "E-Gift Card"
    return (title or asin)[:28]


def buy_buttons(items: list[dict]) -> list:
    rows = [[{"text": f"🛒 קנה {short_label(i['title'], i['asin'])}", "url": i["url"]}] for i in items[:8]]
    rows.append([{"text": "🔄 בדוק שוב", "callback_data": "check"}])
    return rows


def fmt_item(it: dict) -> str:
    title = html.escape(it.get("title") or it["asin"])[:150]
    parts = [f"• <a href=\"{it['url']}\">{title}</a>"]
    extra = []
    if it.get("price"):
        extra.append(f"💰 {html.escape(it['price'])}")
    if it.get("seller"):
        extra.append(f"🏪 {html.escape(it['seller'])[:40]}")
    if it.get("note") and it["note"].lower() not in ("in stock", "in stock."):
        extra.append(html.escape(it["note"])[:80])
    if extra:
        parts.append("   " + " | ".join(extra))
    return "\n".join(parts)


def format_report(report: dict | None, title: str = "📋 <b>מצב המלאי</b>") -> tuple[str, list]:
    """Full status list of every tracked card + buttons."""
    if not report:
        return ("עדיין לא בוצעה בדיקה. לחץ על \"בדוק עכשיו\".", MENU_BUTTONS)
    items = sorted(report["items"].values(),
                   key=lambda i: (i["status"] != "available", short_label(i["title"], i["asin"])))
    lines = [f"{title} · {fmt_time(report['ts'])}", ""]
    for i in items:
        if i["status"] == "gone":
            continue
        icon = STATUS_ICON.get(i["status"], "❔")
        label = html.escape(short_label(i["title"], i["asin"]))
        line = f"{icon} <a href=\"{i['url']}\">{label}</a> – {STATUS_TEXT.get(i['status'], i['status'])}"
        if i["status"] == "available" and i.get("price"):
            line += f" ({html.escape(i['price'])})"
        lines.append(line)
    avail = [i for i in items if i["status"] == "available"]
    lines.append("")
    if avail:
        lines.append(f"🟢 <b>{len(avail)} זמינים עכשיו!</b>")
    else:
        lines.append("🔴 אין כרגע גיפט קארד זמין.")
    if report.get("blocked"):
        lines.append("⚠️ אמזון חסם חלק מהבדיקות – התוצאה לא מלאה.")
    buttons = buy_buttons(avail) if avail else MENU_BUTTONS
    return "\n".join(lines), buttons


# --------------------------------------------------------------------------- main check

def run_check(dry_run: bool = False, manual: bool = False) -> dict:
    """Check Amazon, send alerts, save state. Returns a report of every tracked card."""
    alert_mode = os.environ.get("ALERT_MODE", "every").strip().lower()
    india_only = os.environ.get("INDIA_ONLY", "true").strip().lower() not in ("0", "false", "no")

    state = load_state()
    state.setdefault("discovered", {})
    state.setdefault("available", {})
    state.setdefault("blocked_streak", 0)
    state["last_check_ts"] = now_ts()

    watch = load_watchlist()
    for asin, title in state["discovered"].items():
        watch.setdefault(asin, title)

    amz = Amazon()
    requests_ok = 0
    requests_blocked = 0
    found: dict[str, dict] = {}
    checked: dict[str, dict] = {}

    def check_products(asins: list[str]) -> None:
        nonlocal requests_ok, requests_blocked
        for asin in asins:
            if requests_blocked:
                return
            try:
                info = amz.product(asin)
                requests_ok += 1
            except Blocked:
                requests_blocked += 1
                return
            finally:
                amz.pause()
            title = info["title"] or watch.get(asin, "")
            info["title"] = title
            if title and not is_relevant_title(title, india_only) and asin in state["discovered"]:
                log(f"  {asin}: not a relevant card after all, skipping")
                continue
            log(f"product {asin}: {info['status']:<11} {info['price']:<10} {title[:70]}")
            checked[asin] = info
            if info["status"] == "available":
                found[asin] = info

    # 1) product pages - authoritative availability. Checked first, so they get
    #    through even if Amazon starts blocking later requests in this run.
    asins = list(watch)
    random.shuffle(asins)
    check_products(asins)

    # 2) search pages - discover new listings, then check the new ones right away
    new_asins: list[str] = []
    queries = SEARCH_QUERIES[:]
    random.shuffle(queries)
    for q in queries:
        if requests_blocked:
            break
        try:
            res = amz.search(q)
            requests_ok += 1
        except Blocked:
            requests_blocked += 1
            break
        finally:
            amz.pause()
        hits = [r for r in res if is_relevant_title(r["title"], india_only)]
        log(f"search '{q}': {len(res)} results, {len(hits)} relevant")
        for r in hits:
            if r["asin"] not in watch:
                log(f"  discovered new listing {r['asin']}: {r['title'][:80]}")
                state["discovered"][r["asin"]] = r["title"][:100]
                watch[r["asin"]] = r["title"][:100]
                new_asins.append(r["asin"])
    check_products(new_asins)

    log(f"requests ok={requests_ok} blocked={requests_blocked}; available={len(found)}")
    partial = requests_blocked > 0

    # report of every tracked card (used by /check and /status)
    items = {}
    for asin, note in watch.items():
        if asin in checked:
            i = checked[asin]
            items[asin] = {"asin": asin, "title": i["title"], "status": i["status"],
                           "price": i["price"], "url": i["url"]}
        elif asin not in state["discovered"] or asin in state["available"]:
            items[asin] = {"asin": asin, "title": note, "status": "unchecked", "price": "",
                           "url": f"{BASE}/dp/{asin}"}
    report = {"ts": now_ts(), "items": items, "blocked": partial, "manual": manual}
    state["last_report"] = report

    # 3) blocked bookkeeping - a run that hit a CAPTCHA is incomplete
    if partial:
        state["blocked_streak"] += 1
        log(f"run stopped by Amazon CAPTCHA (streak {state['blocked_streak']})")
        last = state.get("last_block_alert", 0)
        if (not manual and state["blocked_streak"] >= BLOCK_ALERT_AFTER
                and now_ts() - last > BLOCK_REALERT_SECONDS and not dry_run):
            telegram_send(
                "⚠️ <b>Amazon חוסם את הבדיקות</b>\n"
                f"{state['blocked_streak']} בדיקות ברצף נחסמו (CAPTCHA). הבוט ימשיך לנסות.\n"
                "אפשר תמיד לנסות ידנית עם /check.",
                MENU_BUTTONS,
            )
            state["last_block_alert"] = now_ts()
        if not found:
            save_state(state)
            return report
    else:
        if state["blocked_streak"] >= BLOCK_ALERT_AFTER and state.get("last_block_alert") and not dry_run:
            telegram_send("✅ הבדיקות מול Amazon חזרו לעבוד.")
        state["blocked_streak"] = 0
        state["last_ok"] = now_ts()

    # 4) alerts
    prev = set(state["available"])
    newly = [a for a in found if a not in prev]
    # only report "sold out" after a complete run, so an unchecked item isn't mistaken for sold out
    gone = [] if partial else [a for a in prev if a not in found]

    messages: list[tuple[str, list | None]] = []
    # a manual check already shows the full list, so only real changes are announced
    if found and ((alert_mode == "every" and not manual) or newly):
        header = "🟢 <b>יש גיפט קארד PSN זמין באמזון הודו!</b>"
        if newly and len(newly) < len(found):
            header += f"\n(חדש: {len(newly)})"
        ordered = sorted(found, key=lambda a: a not in newly)
        lines = [header, ""] + [fmt_item(found[a]) for a in ordered]
        messages.append(("\n".join(lines), buy_buttons([found[a] for a in ordered])))
    if gone:
        lines = ["🔴 <b>נגמר המלאי:</b>"]
        for a in gone:
            info = state["available"][a]
            label = html.escape(short_label(info.get("title", ""), a))
            lasted = fmt_duration(now_ts() - info.get("since", now_ts()))
            lines.append(f"• {label} – היה במלאי {lasted}")
        messages.append(("\n".join(lines), None))

    for text, buttons in messages:
        if dry_run:
            log("DRY RUN - would send:\n" + text)
        else:
            telegram_send(text, buttons)

    new_avail = {a: {"title": i["title"], "price": i["price"],
                     "since": state["available"].get(a, {}).get("since", now_ts())}
                 for a, i in found.items()}
    if partial:  # keep previously-available items we could not check this time
        state["available"].update(new_avail)
    else:
        state["available"] = new_avail
    save_state(state)
    return report


# --------------------------------------------------------------------------- telegram commands

COMMANDS = [
    {"command": "check", "description": "בדיקה עכשיו – מה זמין כרגע"},
    {"command": "status", "description": "תוצאות הבדיקה האחרונה"},
    {"command": "menu", "description": "תפריט"},
    {"command": "help", "description": "עזרה"},
]


def manual_check(chat, dry_run: bool = False) -> None:
    state = load_state()
    since = now_ts() - state.get("last_check_ts", 0)
    if since < MANUAL_CHECK_COOLDOWN and state.get("last_report"):
        text, buttons = format_report(state["last_report"])
        telegram_send("⏳ בדיקה רצה לפני פחות מדקה, הנה התוצאה שלה:\n\n" + text, buttons, chat=chat)
        return
    telegram_send("🔎 בודק את אמזון הודו… (כחצי דקה)", chat=chat)
    try:
        report = run_check(dry_run, manual=True)
    except Exception as e:
        log(f"manual check failed: {e!r}")
        telegram_send("❗ הבדיקה נכשלה. נסה שוב בעוד דקה.", MENU_BUTTONS, chat=chat)
        return
    text, buttons = format_report(report, "🔎 <b>בדיקה ידנית</b>")
    telegram_send(text, buttons, chat=chat)


def handle_update(u: dict, dry_run: bool = False) -> None:
    chat = text = None
    if "callback_query" in u:
        cq = u["callback_query"]
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        chat = cq.get("message", {}).get("chat", {}).get("id")
        text = "/" + (cq.get("data") or "")
    elif "message" in u:
        chat = u["message"].get("chat", {}).get("id")
        text = u["message"].get("text") or ""
    if chat is None or not text:
        return
    if str(chat) not in allowed_chats():
        log(f"ignoring message from unknown chat {chat}")
        return
    cmd = text.strip().split()[0].split("@")[0].lower()
    log(f"command {cmd} from {chat}")
    if cmd in ("/check", "/בדוק"):
        manual_check(chat, dry_run)
    elif cmd == "/status":
        text_, buttons = format_report(load_state().get("last_report"))
        telegram_send(text_, buttons, chat=chat)
    elif cmd == "/help":
        telegram_send(HELP_TEXT, MENU_BUTTONS, chat=chat)
    else:  # /start, /menu, anything else
        telegram_send(WELCOME_TEXT, MENU_BUTTONS, chat=chat)


def serve(duration: float | None, interval: float, dry_run: bool = False) -> None:
    """Answer Telegram commands, and run a scheduled check every `interval` seconds.
    duration=None runs forever (own PC/server); on GitHub Actions each run listens ~5 minutes."""
    end = time.time() + duration if duration else float("inf")
    has_tg = bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())
    if has_tg:
        tg("setMyCommands", commands=COMMANDS)
    next_check = time.time()
    while time.time() < end:
        if time.time() >= next_check:
            try:
                run_check(dry_run)
            except Exception as e:
                log(f"check failed: {e!r}")
            next_check = time.time() + interval
        wait = min(end, next_check) - time.time()
        if wait <= 0:
            continue
        if not has_tg:
            time.sleep(wait)
            continue
        offset = load_state().get("tg_offset", 0)
        updates = tg("getUpdates", offset=offset, timeout=int(max(1, min(25, wait))),
                     allowed_updates=["message", "callback_query"])
        if updates is None:
            time.sleep(min(5, max(0, wait)))
            continue
        for u in updates:
            st = load_state()
            st["tg_offset"] = u["update_id"] + 1
            save_state(st)
            try:
                handle_update(u, dry_run)
            except Exception as e:
                log(f"command failed: {e!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", type=int, metavar="SECONDS",
                    help="check once, then answer Telegram commands for N seconds (GitHub Actions)")
    ap.add_argument("--loop", type=int, metavar="SECONDS",
                    help="run forever: check every N seconds and answer commands (own PC/server)")
    ap.add_argument("--test", action="store_true", help="send a Telegram test message and exit")
    ap.add_argument("--dry-run", action="store_true", help="do not send stock alerts")
    args = ap.parse_args()

    if args.test:
        telegram_send("✅ בוט מעקב PSN אמזון הודו מחובר ועובד. תקבל כאן התראה כשיהיה גיפט קארד זמין.",
                      MENU_BUTTONS)
        return 0
    if args.loop:
        serve(None, max(60, args.loop), args.dry_run)
        return 0
    if args.listen:
        serve(args.listen, float("inf"), args.dry_run)
        return 0
    run_check(args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
