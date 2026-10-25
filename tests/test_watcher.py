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
    monkeypatch.setattr(watcher, "telegram_send", lambda t: sent.append(t))
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
