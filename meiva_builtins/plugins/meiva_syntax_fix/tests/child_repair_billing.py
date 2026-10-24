# meiva:note
# Two syntax-repair model calls in one fresh process whose real tracing is armed against a local
# collector: one that answers and one that does not. The child prints, for each call, what it
# returned and the billing spans it ended, as one JSON line.

import asyncio
import json

from base.packages.plugin_providers import build_model_catalog
from base.telemetry.tests.capture.armed_tracing import BillingRuntime, armed_billing
from meiva_builtins.plugins.meiva_syntax_fix._llm_repair import repair_once
from meiva_builtins.plugins.meiva_syntax_fix.tests.hook_support import ScriptedRepairModel


def repair(model: ScriptedRepairModel, tracer: BillingRuntime) -> dict[str, object]:
    ended = len(tracer.spans)
    repaired = asyncio.run(repair_once(model, [], catalog=build_model_catalog()))
    return {
        "repaired": repaired,
        "spans": [
            {"name": span.name, "attributes": dict(span.attributes or {})}
            for span in tracer.spans[ended:]
        ],
    }


with armed_billing() as tracer:
    outcome = {
        "answers": repair(
            ScriptedRepairModel(
                replies=["fixed = True"],
                usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            ),
            tracer,
        ),
        "unavailable": repair(ScriptedRepairModel(fails=True), tracer),
    }
print(json.dumps(outcome))
