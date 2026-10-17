"""The gates that keep junk off the dashboard: implausible spreads, boilerplate
split-offs, and a rejected API key."""
import pytest
from conftest import fixture

from scanner import enrich as en
from scanner import llm_extract
from scanner.build import _why


def _event(**kw):
    base = {"id": "x", "source": "BaFin", "issuer": "Test AG", "ticker": "TST.DE",
            "category": "tender", "event_type": "Takeover offer", "announce_date": "2026-10-01"}
    return {**base, **kw}


@pytest.fixture
def price(monkeypatch):
    """Set the market price enrich() will see."""
    def set_price(px):
        monkeypatch.setattr(en, "current_price", lambda ticker: px)
    return set_price


def test_plausible_spread_is_kept(price):
    price(10.0)
    e = en.enrich(_event(offer_price=11.3))
    assert e["spread_pct"] == 13.0
    assert not e.get("needs_review")


def test_implausible_spread_is_flagged_not_shown(price):
    # Cadeler-style: the regex grabbed "$1.00" against a ~$21 stock.
    price(21.36)
    e = en.enrich(_event(offer_price=1.0))
    assert e["spread_pct"] is None
    assert e["needs_review"] and e["spread_suspect"] == pytest.approx(-95.32)
    assert e["offer_price"] == 1.0  # kept so it can be checked by hand
    assert _why(e).startswith("⚠ Precio de oferta dudoso")


def test_sec_delisting_noise_price_is_dropped(price, monkeypatch):
    # 25-NSE notices have no offer at all: drop the price, don't ask for review.
    price(0.47)
    monkeypatch.setattr(en, "offer_price", lambda url: 202.5)
    e = en.enrich(_event(source="SEC", category="delisting", url="u", form="25-NSE"))
    assert e["offer_price"] is None and e["spread_pct"] is None
    assert not e.get("needs_review")


def test_boilerplate_split_off_event_is_dropped(monkeypatch):
    monkeypatch.setattr(en, "_get_text", lambda url: fixture("ndra_s4_boilerplate.txt"))
    e = en.enrich(_event(source="SEC", category="split_off", ticker="NDRA", url="u", form="S-4"))
    assert e["drop"]


class AuthenticationError(Exception):
    """Stand-in with the same class name as anthropic.AuthenticationError."""


class _RejectingClient:
    class messages:
        calls = 0

        @classmethod
        def create(cls, **kw):
            cls.calls += 1
            raise AuthenticationError("invalid x-api-key")


def test_rejected_key_stops_llm_calls(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-invalid")
    monkeypatch.setattr(llm_extract, "_client", _RejectingClient)
    assert llm_extract.status() == "ok"
    assert llm_extract.extract("Expiration Date means …") is None
    assert llm_extract.status() == "auth_error"
    assert not llm_extract.available()
    assert llm_extract.extract("again") is None  # no second request
    assert _RejectingClient.messages.calls == 1
