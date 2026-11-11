# Guarded Question Write Surface

| Route | Policy | Effect / receipt |
| --- | --- | --- |
| `POST /api/keyed/v1/agents/{agent_id}/questions/answers` | keyed | every open question of the agent is answered or skipped and the one inbound to the agent is written in one transaction |

It requires a valid caller key, explicit principal-v1 scope and verified principal on every request.
The submission names the open set the user saw (`expected_ids`) and one outcome per id; a different
set settles nothing and returns the latest one (409). Receipt replay precedes the open-set check, so
an accepted submission replays its original acceptance after the questions are gone.

Browser and CLI answering use this route; the browser's mounted form retains a key for manual
same-intent recovery after failure. A changed answer or open set gets a new key. The response is
201 with the inbound id; `status` may reflect current agent state. Replay repairs only the original
pending wake and cannot resurrect completed or deleted inbound work.

See `gateway/agents/docs/operation-receipts.md` for the transaction and event rules.
