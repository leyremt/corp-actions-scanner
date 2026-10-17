"""Regression tests on real filings. Fixtures are short excerpts of the actual
documents (tests/fixtures), so the parsers are exercised offline.

Each case is one we already got wrong or right once — keep it passing when
hardening a regex."""
from conftest import fixture

from scanner import bafin, buybacks, cb
from scanner.enrich import _expiration, _otp_price, is_real_split_off


# ---- Split-offs ---------------------------------------------------------

def test_boilerplate_split_off_is_rejected():
    # ENDRA's S-4 (reverse merger with ASP Isotopes) only mentions "split-off"
    # in the equity plan's "split-up, spin-off, split-off…" list.
    assert not is_real_split_off(fixture("ndra_s4_boilerplate.txt"))


def test_boilerplate_split_off_rejected_even_with_exchange_offer_mentions():
    # Vireo's S-4 talks about exchange offers, but its only "split-off" is
    # still the boilerplate list — the window check must catch it.
    assert not is_real_split_off(fixture("vreof_s4_boilerplate.txt"))


def test_real_split_off_is_kept():
    # Medtronic's MiniMed split-off: a genuine exchange offer.
    assert is_real_split_off(fixture("mdt_minimed_split_off.txt"))


# ---- SEC tender offers ----------------------------------------------------

def test_gnk_offer_to_purchase():
    # Diana Shipping's tender for Genco, original Offer to Purchase (May 2026).
    text = fixture("gnk_offer_to_purchase.txt")
    assert _otp_price(text) == 23.50
    assert _expiration(text) == "2026-06-02"


# ---- BaFin (WpÜG offer documents) ---------------------------------------

def test_bafin_hugo_boss_cover():
    assert bafin.parse_cover(fixture("bafin_hugo_boss_cover.txt")) == (38.0, "2026-07-27")


def test_bafin_commerzbank_cover():
    # Exchange offer (no cash price on the cover) — the date must still parse.
    assert bafin.parse_cover(fixture("bafin_commerzbank_cover.txt")) == (None, "2026-06-16")


# ---- German buybacks (news titles) --------------------------------------

AUMANN = "EQS-Adhoc: Aumann beschließt öffentliches Aktienrückkaufangebot zum Preis von 16,50 € je Aktie"


def test_aumann_buyback_title():
    # The case that motivated the DE-Buyback source: not a WpÜG offer.
    assert buybacks._OFFER_RE.search(AUMANN)
    assert buybacks._company(AUMANN) == "Aumann"
    assert buybacks._price(AUMANN) == 16.50


# ---- Form CB --------------------------------------------------------------

def test_cb_equity_tender():
    assert cb._classify(fixture("cb_world_co_equity.txt")) == "equity"


def test_cb_debt_tender_despite_checkbox_labels():
    # Every CB cover prints "Tender Offer" / "Rights Offering" as checkbox
    # labels; only the securities description tells debt from equity.
    text = fixture("cb_enbridge_debt.txt")
    assert "Tender Offer" in text
    assert cb._classify(text) == "debt"
