from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class StatReset(BaseModel):
    # meiva:note
    # A labelled quota deadline; reaching it does not confirm refreshed quota.
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: str = Field(min_length=1, max_length=64)
    resets_at: AwareDatetime
