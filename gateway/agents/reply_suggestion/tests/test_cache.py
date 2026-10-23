from gateway.agents.reply_suggestion.cache import CACHE_ENTRIES, SuggestionCache
from gateway.agents.reply_suggestion.schemas import ReplySuggestionResponse


def answer(text: str) -> ReplySuggestionResponse:
    return ReplySuggestionResponse(suggestions=[text])


def test_an_entry_is_found_by_agent_and_last_message() -> None:
    cache = SuggestionCache(4)
    cache.remember((7, "ai-1"), answer("go"))

    assert cache.get((7, "ai-1")) == answer("go")
    assert cache.get((7, "ai-2")) is None
    assert cache.get((8, "ai-1")) is None


def test_the_cache_is_bounded_and_drops_the_least_recently_used() -> None:
    cache = SuggestionCache(2)
    cache.remember((1, "a"), answer("one"))
    cache.remember((2, "a"), answer("two"))
    # meiva:note
    # reading refreshes it
    assert cache.get((1, "a")) is not None
    cache.remember((3, "a"), answer("three"))

    assert len(cache) == 2
    assert cache.get((2, "a")) is None
    assert cache.get((1, "a")) is not None


def test_the_gateway_cache_holds_the_configured_number_of_entries() -> None:
    cache = SuggestionCache(CACHE_ENTRIES)
    for agent_id in range(CACHE_ENTRIES + 5):
        cache.remember((agent_id, "a"), answer("x"))

    assert len(cache) == CACHE_ENTRIES
