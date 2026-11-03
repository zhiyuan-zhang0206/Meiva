# meiva:note
# A request whose app carries the insights state, for tests that call a route function directly.
#
# The routes read the app's state through `insights_state`, which accepts only the real state class,
# so a test builds that state and sets the members its route reads (a stand-in where the real thing
# needs a database or a reader).

from starlette.applications import Starlette
from starlette.requests import Request

from services.derived.insights.run_timeline.router import InsightsAppState


def app_request(**members: object) -> Request:
    state = InsightsAppState(dict(members))
    app = Starlette()
    app.state = state
    return Request({"type": "http", "app": app})
