import pathlib

import pytest

from scanner import llm_extract

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Tests never call the Anthropic API: no key, fresh call counter."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm_extract, "_calls", 0)
    monkeypatch.setattr(llm_extract, "_client", None)
    monkeypatch.setattr(llm_extract, "_auth_failed", False)
