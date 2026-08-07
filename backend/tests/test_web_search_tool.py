"""The web_search tool version must follow the model.

The dynamic-filtering variant (`web_search_20260209`) is only accepted by the
Opus/Fable/Sonnet tiers. Sending it to Haiku gets the request rejected, and the
analysis path's except-branch then retries WITHOUT search — so the failure is
silent: a wasted call every cycle plus the quiet loss of the last-24h news
check the prompt explicitly asks the model to perform. Nothing surfaces except
a warning line.

Model choice is a cost lever we expect to be pulled (see config.anthropic_model),
so this pairing has to be pinned rather than left to whoever remembers.
"""
import pytest

from app import market_analysis
from app.config import settings

DYNAMIC = "web_search_20260209"
BASIC = "web_search_20250305"


@pytest.mark.parametrize("model", [
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-fable-5", "claude-sonnet-5", "claude-sonnet-4-6",
])
def test_dynamic_filtering_models_get_the_new_variant(model):
    assert market_analysis._web_search_tool(model)["type"] == DYNAMIC


@pytest.mark.parametrize("model", [
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",   # the API may echo a dated alias
])
def test_haiku_gets_the_variant_it_can_actually_accept(model):
    assert market_analysis._web_search_tool(model)["type"] == BASIC


def test_unknown_model_falls_back_to_the_widely_supported_variant():
    """Guessing the newer variant on an unrecognised id costs a rejected call;
    guessing the older one costs only dynamic filtering."""
    for model in ("some-future-model", "", None):
        assert market_analysis._web_search_tool(model)["type"] == BASIC


def test_the_configured_models_are_covered():
    """Whatever anthropic_model / llm_low_compute_model are set to, the tool
    version chosen for them must be one the API recognises."""
    for model in (settings.anthropic_model, settings.llm_low_compute_model):
        assert market_analysis._web_search_tool(model)["type"] in (DYNAMIC, BASIC)


def test_tool_block_keeps_its_name_and_use_cap():
    """The name is what the model calls and max_uses is the per-call cost cap —
    web search is billed per search, separately from tokens."""
    for model in ("claude-opus-5", "claude-haiku-4-5"):
        tool = market_analysis._web_search_tool(model)
        assert tool["name"] == "web_search"
        assert tool["max_uses"] == 3
