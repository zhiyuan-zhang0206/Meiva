from tests.e2e.fakes.scenario_recording import RecordingModel, say

FIRST_REPLY = "Ready for the external session."
RESUMED_REPLY = "I resumed my work after the external session ended."


def build(model: str, *, agent_id: int | None) -> RecordingModel:
    return RecordingModel(
        agent_id=agent_id,
        script=(
            say(FIRST_REPLY),
            say(RESUMED_REPLY),
        ),
    )
