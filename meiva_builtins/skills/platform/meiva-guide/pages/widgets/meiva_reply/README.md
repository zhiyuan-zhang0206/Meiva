# Page replies

The shared `reply.js` helper submits the user's input from an Meiva page. The
choice, confirm, form, and compare widgets embed the same body for zero-build use.
A display-only page does not need it.

## Prepare a reply

Template `__AGENT_ID__` with the owning agent's `meiva.self.AGENT_ID`,
`__PAGE_NAME__` with the registered page name, and `__GATEWAY_ORIGIN__` with the
gateway's absolute base URL (for example `http://10.0.0.5:8000`). The first two
identify the destination and label the input; they are not credentials. Leaving
`__GATEWAY_ORIGIN__` unreplaced falls back to the page's own origin, which is
correct while pages are served from the gateway. Describe the exact question and
what each choice means before asking the user to submit it.

Call `meivaReply(kind, choices, text)` when the user submits, where `kind` is
`choice`, `confirm`, `compare`, `form` or `reply`, `choices` is the selected
option labels as the page showed them, and `text` is the free-form part (a
confirm note, a form's fields, a chat reply). Await success before displaying
"Sent"; a failed request should keep the user's input available for retry.
The result arrives as ordinary user input and wakes the destination agent.
A label such as "approved" authorizes only the specific action presented, not
unrelated operations or access to other agents.

## Existing browser transport

Open the returned Meiva page URL in the browser where the user is signed in.
The helper posts a bounded envelope to `/pages/<agent>-<name>/reply` and the
browser carries the viewer's existing login session; the gateway renders the
message. Do not insert a bearer token, machine secret, or agent credentials into
page files. There is no separate page token or frontend package.

The reply route takes the viewer's own credential and refuses a machine token,
so the record says a person answered. It accepts only `kind` / `choices` / `text`;
every other field is rejected.

Opening the file directly, browsing the raw server port, or hosting it on an
unrelated origin does not provide this authentication context. Display components
remain reusable there; submitting to Meiva requires an explicitly configured,
authenticated integration. Do not infer authorization from an agent id or the
`source` label. The current message API is a user message endpoint, not a
page-scoped permission system.

The platform message/authentication implementation owns the HTTP contract; see
`gateway/agents/` and `gateway/http/auth/` in a source checkout. The helper only
encapsulates the request used by these resources.
