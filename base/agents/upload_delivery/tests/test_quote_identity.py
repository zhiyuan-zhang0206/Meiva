# meiva:note
# Quoted uploads freeze context in the same durable identity as their text and files.

import hashlib
import json

from base.agents.messages.quote import Quote
from base.agents.upload_delivery.models import FrozenMessage, request_hash


def test_quote_changes_upload_identity_and_survives_frozen_message() -> None:
    quote = Quote(source_agent_id=7, source_item_id="1.0", author="Agent #7", text="selection")
    frozen = FrozenMessage(text="Explain", source="user", quote=quote)
    assert FrozenMessage.model_validate_json(frozen.model_dump_json()).quote == quote
    assert request_hash(7, ["file"], "Explain", quote) != request_hash(7, ["file"], "Explain")
    assert request_hash(7, ["file"], "Explain", quote) != request_hash(
        7, ["file"], "Explain", quote.model_copy(update={"text": "changed"})
    )


def test_existing_upload_identity_is_unchanged_without_quote() -> None:
    prior = hashlib.sha256(
        json.dumps([7, ["file"], "Explain"], ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert request_hash(7, ["file"], "Explain") == prior
    assert FrozenMessage.model_validate({"text": "Explain", "source": "user"}).quote is None
