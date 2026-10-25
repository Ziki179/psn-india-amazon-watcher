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

Usage:
  python watcher.py                 # one check (used by GitHub Actions)
  python watcher.py --loop 120      # check every 120 seconds forever (run on your own PC/server)
  python watcher.py --test          # send a test message to Telegram and exit
  python watcher.py --dry-run       # check, print results, do not send anything
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

def telegram_send(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not token or not chats:
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing - printing message instead:\n" + text)
        return
    for chat in chats:
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
            log(f"telegram: sent to {chat}")
        except Exception as e:
            body = getattr(e, "read", lambda: b"")()
            log(f"telegram: FAILED for {chat}: {e} {body[:200]!r}")


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


# --------------------------------------------------------------------------- main check

def run_check(dry_run: bool = False) -> None:
    alert_mode = os.environ.get("ALERT_MODE", "every").strip().lower()
    india_only = os.environ.get("INDIA_ONLY", "true").strip().lower() not in ("0", "false", "no")

    state = load_state()
    state.setdefault("discovered", {})
    state.setdefault("available", {})
    state.setdefault("blocked_streak", 0)

    watch = load_watchlist()
    for asin, title in state["discovered"].items():
        watch.setdefault(asin, title)

    amz = Amazon()
    requests_ok = 0
    requests_blocked = 0
    found: dict[str, dict] = {}

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

    # 3) blocked bookkeeping - a run that hit a CAPTCHA is incomplete
    partial = requests_blocked > 0
    if partial:
        state["blocked_streak"] += 1
        log(f"run stopped by Amazon CAPTCHA (streak {state['blocked_streak']})")
        last = state.get("last_block_alert", 0)
        if (state["blocked_streak"] >= BLOCK_ALERT_AFTER
                and now_ts() - last > BLOCK_REALERT_SECONDS and not dry_run):
            telegram_send(
                "⚠️ <b>Amazon חוסם את הבדיקות</b>\n"
                f"{state['blocked_streak']} ריצות ברצף נחסמו (CAPTCHA). הבוט ימשיך לנסות בריצות הבאות.\n"
                "אם זה נמשך יותר מכמה שעות - אפשר להריץ את הבוט מהמחשב שלך (ראה README)."
            )
            state["last_block_alert"] = now_ts()
        if not found:
            save_state(state)
            return
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

    messages = []
    if found and (alert_mode == "every" or newly):
        header = "🟢 <b>יש גיפט קארד PSN זמין באמזון הודו!</b>"
        if newly and alert_mode == "every" and len(newly) < len(found):
            header += f"\n(חדש: {len(newly)})"
        lines = [header, ""] + [fmt_item(found[a]) for a in sorted(found, key=lambda a: a not in newly)]
        messages.append("\n".join(lines))
    if gone:
        names = [html.escape(state["available"][a].get("title", a))[:80] for a in gone]
        messages.append("🔴 <b>נגמר המלאי:</b>\n" + "\n".join(f"• {n}" for n in names))

    for m in messages:
        if dry_run:
            log("DRY RUN - would send:\n" + m)
        else:
            telegram_send(m)

    new_avail = {a: {"title": i["title"], "price": i["price"],
                     "since": state["available"].get(a, {}).get("since", now_ts())}
                 for a, i in found.items()}
    if partial:  # keep previously-available items we could not check this time
        state["available"].update(new_avail)
    else:
        state["available"] = new_avail
    save_state(state)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", type=int, metavar="SECONDS", help="repeat the check every N seconds")
    ap.add_argument("--test", action="store_true", help="send a Telegram test message and exit")
    ap.add_argument("--dry-run", action="store_true", help="do not send Telegram messages")
    args = ap.parse_args()

    if args.test:
        telegram_send("✅ בוט מעקב PSN אמזון הודו מחובר ועובד. תקבל כאן התראה כשיהיה גיפט קארד זמין.")
        return 0

    if args.loop:
        interval = max(60, args.loop)
        while True:
            try:
                run_check(args.dry_run)
            except Exception as e:  # keep looping
                log(f"check failed: {e!r}")
            time.sleep(interval + random.uniform(0, 20))
    run_check(args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
