from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from gateway.app import openapi_schema


# meiva:note
# One parameter of an OpenAPI operation: its name, whether it is required, and where it travels
# (`in` in the document).
class Parameter(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    required: bool = False
    location: str = Field(alias="in")


# meiva:note
# One operation of an OpenAPI path item.
class Operation(BaseModel):
    parameters: list[Parameter] = Field(default_factory=list[Parameter])


# meiva:note
# The operations of one OpenAPI path that the gateway tests read.
class PathItem(BaseModel):
    get: Operation | None = None
    post: Operation | None = None


# meiva:note
# The part of the gateway's OpenAPI document its tests read: the paths and their operations.
class Document(BaseModel):
    paths: dict[str, PathItem]


# meiva:note
# The gateway's OpenAPI document, built cold, as the tests read it.
def openapi_document() -> Document:
    return Document.model_validate(openapi_schema())
