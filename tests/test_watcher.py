"""Offline tests: python -m pytest -q  (no network needed)."""
import json
import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watcher  # noqa: E402

OOS_PAGE = """<html><body><span id="productTitle"> Rs.1000 Sony PlayStation Store Gift Card (Email Delivery in 1 hour- Digital Voucher Code) </span>
<div id="buybox"><div id="outOfStock"><span>Currently unavailable.</span> We don't know when or if this item will be back in stock.</div>
<div id="availability"><script>P.when("A").execute(function(){})</script><span class="a-color-price">Currently unavailable.</span></div></div>
<div id="similar"><span class="a-price"><span class="a-offscreen">₹829.00</span></span></div></body></html>"""

IN_STOCK_PAGE = """<html><body><span id="productTitle">Rs.2000 Sony PlayStation Store Gift Card (Email Delivery in 1 hour- Digital Voucher Code)</span>
<div id="corePriceDisplay_desktop_feature_div"><span class="a-price priceToPay"><span class="a-offscreen">₹2,000.00</span></span></div>
<div id="buybox"><div id="availability"><script>var x=1;</script><span class="a-color-success"> In stock </span></div>
<a id="sellerProfileTriggerId">Appario Retail</a>
<input id="add-to-cart-button" type="submit"><input id="buy-now-button" type="submit"></div></body></html>"""

GIFTCARD_PAGE = """<html><body><span id="productTitle">Playstation | E-Gift Card | Instant Delivery</span>
<div id="buybox"><input name="items[0.base][customerVisiblePrice][displayString]" value="₹1,000.00">
<div id="availability"><span>In stock</span></div><input id="add-to-cart-button"></div></body></html>"""

OTHER_SELLERS_PAGE = """<html><body><span id="productTitle">Rs.500 Sony PlayStation Network Wallet Top-Up</span>
<div id="buybox"><a id="buybox-see-all-buying-choices">See All Buying Options</a></div></body></html>"""

CAPTCHA_PAGE = """<html><body><form action="/errors/validateCaptcha">Type the characters you see in this image</form></body></html>"""

SEARCH_PAGE = """<html><body>
<div data-component-type="s-search-result" data-asin="B0NEWPSN01"><h2><span>Rs.3000 Sony PlayStation Network Wallet Top-Up (Email Delivery)</span></h2>
<span class="a-price"><span class="a-offscreen">₹3,000</span></span></div>
<div data-component-type="s-search-result" data-asin="B0USPSN002"><h2><span>PlayStation Store Gift Card $50 USD (US account)</span></h2>
<span class="a-price"><span class="a-offscreen">₹5,200</span></span></div>
<div data-component-type="s-search-result" data-asin="B0GAME0003"><h2><span>Grand Theft Auto VI | Standard Edition | PlayStation 5</span></h2></div>
<div data-component-type="s-search-result" data-asin="B0MEMCARD4"><h2><span>64 MB Memory Card for Sony Playstation 2</span></h2></div>
<div data-component-type="s-search-result" data-asin="B0GPLAY005"><h2><span>Google Play Gift Card | Instant Delivery</span></h2></div>
</body></html>"""


def soup(h):
    return BeautifulSoup(h, "lxml")


def test_product_states():
    assert watcher.parse_product(soup(OOS_PAGE))["status"] == "unavailable"
    assert watcher.parse_product(soup(OOS_PAGE))["price"] == ""
    p = watcher.parse_product(soup(IN_STOCK_PAGE))
    assert p["status"] == "available" and p["price"] == "₹2,000.00" and p["seller"] == "Appario Retail"
    g = watcher.parse_product(soup(GIFTCARD_PAGE))
    assert g["status"] == "available" and g["price"] == "₹1,000.00"
    assert watcher.parse_product(soup(OTHER_SELLERS_PAGE))["status"] == "available"


def test_title_filter():
    f = watcher.is_relevant_title
    assert f("Rs.500 Sony PlayStation Network Wallet Top-Up (Email Delivery in 1 hour- Digital Voucher Code)", True)
    assert f("Sony Interactive Entertainment Europe Playstation Network Wallet Top-Up", True)
    assert f("PlayStation Wallet INR 5000 (Digital Code)", True)
    assert f("Playstation | E-Gift Card | Instant Delivery | Valid for in-store", True)
    assert f("RS.15000 PSN Wallet Top-Up Redeem Key| E-MAIL DELIVERY IN 24 HRS", True)
    assert not f("PlayStation Store Gift Card $50 USD (US account)", True)
    assert f("PlayStation Store Gift Card $50 USD (US account)", False)
    assert not f("Grand Theft Auto VI | Standard Edition | PlayStation 5", True)
    assert not f("64 MB Memory Card for Sony Playstation 2", True)
    assert not f("Google Play Gift Card | Instant Delivery", True)
    assert not f("DualSense Wireless Controller for PlayStation 5", True)


def test_search_parse():
    res = watcher.parse_search(soup(SEARCH_PAGE))
    rel = [r["asin"] for r in res if watcher.is_relevant_title(r["title"], True)]
    assert rel == ["B0NEWPSN01"]


class FakeAmazon(watcher.Amazon):
    pages = {}

    def __init__(self):
        pass

    def get(self, path):
        for key, (status, body) in self.pages.items():
            if key in path:
                if any(m in body for m in watcher.CAPTCHA_MARKERS):
                    raise watcher.Blocked(path)
                return status, body
        return 404, "<html>not found</html>"

    @staticmethod
    def pause():
        pass


def run(monkeypatch, tmp_path, pages, mode="every"):
    sent = []
    FakeAmazon.pages = pages
    monkeypatch.setattr(watcher, "Amazon", FakeAmazon)
    monkeypatch.setattr(watcher, "telegram_send", lambda t, *a, **k: sent.append(t))
    monkeypatch.setattr(watcher, "STATE_FILE", tmp_path / "state.json")
    wl = tmp_path / "watchlist.txt"
    wl.write_text("B07K6RYVHR # 1000\nB093QF35KZ # 2000\n")
    monkeypatch.setattr(watcher, "WATCHLIST_FILE", wl)
    monkeypatch.setenv("ALERT_MODE", mode)
    watcher.run_check()
    return sent, json.loads((tmp_path / "state.json").read_text())


def test_full_flow(monkeypatch, tmp_path):
    base = {"/s?k=": (200, "<html></html>"), "/dp/B07K6RYVHR": (200, OOS_PAGE), "/dp/B093QF35KZ": (200, OOS_PAGE)}
    sent, st = run(monkeypatch, tmp_path, base)
    assert sent == [] and st["available"] == {}

    # one becomes available -> alert
    pages = dict(base, **{"/dp/B093QF35KZ": (200, IN_STOCK_PAGE)})
    sent, st = run(monkeypatch, tmp_path, pages)
    assert len(sent) == 1 and "B093QF35KZ" in sent[0] and "₹2,000.00" in sent[0]

    # still available, mode=every -> alert again
    sent, st = run(monkeypatch, tmp_path, pages)
    assert len(sent) == 1
    # still available, mode=change -> no alert
    sent, st = run(monkeypatch, tmp_path, pages, mode="change")
    assert sent == []

    # sold out -> sold-out notice
    sent, st = run(monkeypatch, tmp_path, base)
    assert len(sent) == 1 and "נגמר" in sent[0]

    # discovered via search and in stock
    pages = dict(base, **{"/s?k=": (200, SEARCH_PAGE), "/dp/B0NEWPSN01": (200, IN_STOCK_PAGE)})
    sent, st = run(monkeypatch, tmp_path, pages)
    assert "B0NEWPSN01" in st["discovered"] and "B0USPSN002" not in st["discovered"]
    assert len(sent) == 1 and "B0NEWPSN01" in sent[0]


def test_blocked(monkeypatch, tmp_path):
    pages = {"/": (200, CAPTCHA_PAGE)}
    for i in range(watcher.BLOCK_ALERT_AFTER - 1):
        sent, st = run(monkeypatch, tmp_path, pages)
        assert sent == []
    sent, st = run(monkeypatch, tmp_path, pages)
    assert len(sent) == 1 and "חוסם" in sent[0]
    sent, st = run(monkeypatch, tmp_path, pages)  # no repeat within 12h
    assert sent == []
    ok = {"/s?k=": (200, "<html></html>"), "/dp/": (200, OOS_PAGE)}
    sent, st = run(monkeypatch, tmp_path, ok)
    assert len(sent) == 1 and "חזרו" in sent[0] and st["blocked_streak"] == 0


def test_partial_block_keeps_found_and_no_false_soldout(monkeypatch, tmp_path):
    # first: item available
    pages = {"/s?k=": (200, "<html></html>"), "/dp/B07K6RYVHR": (200, OOS_PAGE), "/dp/B093QF35KZ": (200, IN_STOCK_PAGE)}
    sent, st = run(monkeypatch, tmp_path, pages)
    assert "B093QF35KZ" in st["available"]
    # then: product pages blocked -> no sold-out message, item kept
    pages = {"/s?k=": (200, "<html></html>"), "/dp/": (200, CAPTCHA_PAGE)}
    sent, st = run(monkeypatch, tmp_path, pages)
    assert sent == [] and "B093QF35KZ" in st["available"] and st["blocked_streak"] == 1


def test_404_is_not_captcha(monkeypatch):
    class R:
        status_code = 404
        text = "<html>Sorry! We couldn't find that page. api-services-support@amazon.com</html>"
    a = watcher.Amazon()
    monkeypatch.setattr(a.s, "get", lambda *k, **kw: R())
    assert a.get("/dp/B0FLPSFWJL")[0] == 404


def test_short_label():
    assert watcher.short_label("Rs.1000 Sony PlayStation Store Gift Card") == "₹1,000"
    assert watcher.short_label("PlayStation Wallet INR 5000 (Digital Code)") == "₹5,000"
    assert watcher.short_label("RS.15000 PSN Wallet Top-Up Redeem Key") == "₹15,000"
    assert watcher.short_label("Playstation | E-Gift Card | Instant Delivery") == "E-Gift Card"


def test_report_and_buttons(monkeypatch, tmp_path):
    pages = {"/s?k=": (200, "<html></html>"), "/dp/B07K6RYVHR": (200, OOS_PAGE), "/dp/B093QF35KZ": (200, IN_STOCK_PAGE)}
    sent, st = run(monkeypatch, tmp_path, pages)
    rep = st["last_report"]
    assert rep["items"]["B093QF35KZ"]["status"] == "available"
    assert rep["items"]["B07K6RYVHR"]["status"] == "unavailable"
    text, buttons = watcher.format_report(rep)
    assert "₹2,000" in text and "זמינים עכשיו" in text
    assert buttons[0][0]["url"].endswith("/dp/B093QF35KZ")
    # blocked run -> items marked unchecked
    sent, st = run(monkeypatch, tmp_path, {"/": (200, CAPTCHA_PAGE)})
    assert all(i["status"] == "unchecked" for i in st["last_report"]["items"].values())
    text, _ = watcher.format_report(st["last_report"])
    assert "חסם" in text


def setup_cmd(monkeypatch, tmp_path, pages):
    sent = []
    FakeAmazon.pages = pages
    monkeypatch.setattr(watcher, "Amazon", FakeAmazon)
    monkeypatch.setattr(watcher, "telegram_send", lambda t, b=None, chat=None: sent.append((t, b, chat)))
    monkeypatch.setattr(watcher, "tg", lambda *a, **k: True)
    monkeypatch.setattr(watcher, "STATE_FILE", tmp_path / "state.json")
    wl = tmp_path / "watchlist.txt"
    wl.write_text("B07K6RYVHR # Rs.1000\nB093QF35KZ # Rs.2000\n")
    monkeypatch.setattr(watcher, "WATCHLIST_FILE", wl)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setenv("ALERT_MODE", "every")
    return sent


def test_manual_check_command(monkeypatch, tmp_path):
    pages = {"/s?k=": (200, "<html></html>"), "/dp/B07K6RYVHR": (200, OOS_PAGE), "/dp/B093QF35KZ": (200, IN_STOCK_PAGE)}
    sent = setup_cmd(monkeypatch, tmp_path, pages)
    watcher.handle_update({"update_id": 1, "message": {"chat": {"id": 111}, "text": "/check"}})
    texts = [t for t, b, c in sent]
    # "checking..." + new-stock alert (to all chats) + the manual report to the asking chat
    assert "בודק" in texts[0]
    assert any("בדיקה ידנית" in t and "₹2,000" in t for t in texts)
    assert sent[-1][2] == 111
    # second /check within a minute -> cached result, no new Amazon requests
    sent.clear()
    watcher.handle_update({"update_id": 2, "callback_query": {"id": "x", "data": "check", "message": {"chat": {"id": 111}}}})
    assert len(sent) == 1 and "פחות מדקה" in sent[0][0]


def test_unknown_chat_ignored_and_menu(monkeypatch, tmp_path):
    sent = setup_cmd(monkeypatch, tmp_path, {})
    watcher.handle_update({"update_id": 1, "message": {"chat": {"id": 999}, "text": "/check"}})
    assert sent == []
    watcher.handle_update({"update_id": 2, "message": {"chat": {"id": 111}, "text": "/start"}})
    assert sent and sent[0][1] == watcher.MENU_BUTTONS
    sent.clear()
    watcher.handle_update({"update_id": 3, "message": {"chat": {"id": 111}, "text": "/status"}})
    assert "עדיין לא בוצעה בדיקה" in sent[0][0]


def test_sold_out_shows_duration(monkeypatch, tmp_path):
    pages = {"/s?k=": (200, "<html></html>"), "/dp/B07K6RYVHR": (200, OOS_PAGE), "/dp/B093QF35KZ": (200, IN_STOCK_PAGE)}
    run(monkeypatch, tmp_path, pages)
    st = json.loads((tmp_path / "state.json").read_text())
    st["available"]["B093QF35KZ"]["since"] -= 3 * 3600 + 120
    (tmp_path / "state.json").write_text(json.dumps(st))
    pages["/dp/B093QF35KZ"] = (200, OOS_PAGE)
    sent, st = run(monkeypatch, tmp_path, pages)
    assert len(sent) == 1 and "3 שעות ו-2 דקות" in sent[0]


def test_serve_listen_loop(monkeypatch, tmp_path):
    pages = {"/s?k=": (200, "<html></html>"), "/dp/": (200, OOS_PAGE)}
    sent = setup_cmd(monkeypatch, tmp_path, pages)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    calls = {"n": 0}

    def fake_tg(method, **k):
        if method == "getUpdates":
            calls["n"] += 1
            if calls["n"] == 1:
                assert k["offset"] == 0
                return [{"update_id": 41, "message": {"chat": {"id": 111}, "text": "/help"}}]
            assert k["offset"] == 42
            raise SystemExit  # stop the loop
        return True
    monkeypatch.setattr(watcher, "tg", fake_tg)
    try:
        watcher.serve(300, float("inf"))
    except SystemExit:
        pass
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["tg_offset"] == 42 and "last_report" in st
    assert any("פקודות" in t for t, b, c in sent)
