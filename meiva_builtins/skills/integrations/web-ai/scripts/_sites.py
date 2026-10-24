# meiva:note
# Per-site profiles of the web-ai driver and the decisions that need nothing but a profile: which site a name means, the conversation id inside a URL, the URL of a conversation id, and whether a URL stays on the site's host. Every selector field is a tuple tried in order, so a drifted site is repaired by prepending the new selector and keeping the old one as a fallback. A profile is a frozen value; the driver reads it and never edits it. The module is strict-pure: the mutable-looking SITES mapping is a read-only view over frozen profiles.
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


# meiva:note
# The selector list reader: the answer is the innerText of the last element matched by the first selector that matches anything.
@dataclass(frozen=True)
class SelectorAnswer:
    selectors: tuple[str, ...]


# meiva:note
# The script reader, for a site whose rendered DOM is not a faithful text source: `script` is a full `() => ...` function evaluated in the page that returns the answer text.
@dataclass(frozen=True)
class ScriptAnswer:
    script: str


# meiva:note
# Everything the driver knows about one chat site. `login_indicators` are texts of a sign-in wall; `auto_login` are click paths that attempt a Google sign-in; `chat_id_patterns` capture the conversation id from a conversation URL and `chat_url_template` builds the URL back; `attach_path` is the click sequence whose final control opens the OS file chooser (it is handed the file instead of a plain click).
@dataclass(frozen=True)
class SiteProfile:
    label: str
    new_chat_url: str
    host: str
    login_indicators: tuple[str, ...]
    auto_login: tuple[tuple[str, ...], ...]
    chat_id_patterns: tuple[str, ...]
    chat_url_template: str
    composer: tuple[str, ...]
    send: tuple[str, ...]
    stop: tuple[str, ...]
    answer: SelectorAnswer | ScriptAnswer
    attach_path: tuple[tuple[str, ...], ...]


_GOOGLE_SIGN_IN = (("Continue with Google",), ("Log in with Google",), ("Sign in with Google",))

# meiva:note
# Claude's rendered markdown (`.standard-markdown`, last turn) is the answer. The old screen-reader heading is no longer a verbatim mirror (it holds a short summary), and innerText of the rendered markup loses ordered-list numbers because they are ::marker pseudo-elements, not text. So the script rebuilds ol/ul markers on a detached clone, then reads innerText off-screen (innerText needs layout, so the clone is briefly attached and removed). It grows in step with the answer during streaming, so the idle detector settles on the real text.
_CLAUDE_ANSWER_SCRIPT = (
    "() => {"
    "  const els = document.querySelectorAll('.standard-markdown');"
    "  if (!els.length) return '';"
    "  const clone = els[els.length - 1].cloneNode(true);"
    "  for (const ol of clone.querySelectorAll('ol')) {"
    "    let n = parseInt(ol.getAttribute('start') || '1', 10);"
    "    for (const li of [...ol.children]) {"
    "      if (li.tagName !== 'LI') continue;"
    "      const v = li.getAttribute('value');"
    "      if (v !== null && v !== '') n = parseInt(v, 10);"
    "      li.insertBefore(document.createTextNode(n + '. '), li.firstChild);"
    "      n++;"
    "    }"
    "  }"
    "  for (const ul of clone.querySelectorAll('ul')) {"
    "    for (const li of [...ul.children]) {"
    "      if (li.tagName === 'LI') li.insertBefore(document.createTextNode('- '), li.firstChild);"
    "    }"
    "  }"
    "  clone.style.position = 'fixed';"
    "  clone.style.left = '-99999px';"
    "  clone.style.top = '0';"
    "  document.body.appendChild(clone);"
    "  const text = clone.innerText;"
    "  clone.remove();"
    "  return text;"
    "}"
)

_CHATGPT = SiteProfile(
    label="ChatGPT",
    new_chat_url="https://chatgpt.com/",
    host="chatgpt.com",
    login_indicators=("Log in to ChatGPT", "Sign up", "Welcome back"),
    auto_login=_GOOGLE_SIGN_IN,
    chat_id_patterns=(r"/c/([a-zA-Z0-9_-]+)",),
    chat_url_template="https://chatgpt.com/c/{chat_id}",
    composer=(
        "#prompt-textarea",
        'div[contenteditable="true"]#prompt-textarea',
        "textarea#prompt-textarea",
        'main div[contenteditable="true"]',
    ),
    send=(
        'button[data-testid="send-button"]',
        "#composer-submit-button",
        'button[aria-label="Send prompt"]',
    ),
    stop=(
        'button[data-testid="stop-button"]',
        'button[aria-label="Stop streaming"]',
    ),
    answer=SelectorAnswer(('[data-message-author-role="assistant"]',)),
    attach_path=(("Add files and more",), ("Add photos & files",)),
)

_GEMINI = SiteProfile(
    label="Gemini",
    new_chat_url="https://gemini.google.com/app",
    host="gemini.google.com",
    login_indicators=("Sign in", "Sign in to continue", "Choose an account"),
    auto_login=(("Continue with Google",), ("Sign in with Google",)),
    chat_id_patterns=(r"/app/([a-zA-Z0-9_-]+)",),
    chat_url_template="https://gemini.google.com/app/{chat_id}",
    composer=(
        'div.ql-editor[contenteditable="true"]',
        'rich-textarea div[contenteditable="true"]',
        'div[role="textbox"][contenteditable="true"]',
    ),
    send=(
        'button[aria-label="Send message"]',
        "button.send-button",
        'button[mattooltip="Send message"]',
    ),
    stop=(
        'button[aria-label="Stop response"]',
        'button[aria-label*="Stop"]',
    ),
    answer=SelectorAnswer(
        (
            "message-content .model-response-text",
            "message-content",
            ".model-response-text",
        )
    ),
    attach_path=(("Upload & tools",), ("Upload files",)),
)

_CLAUDE = SiteProfile(
    label="Claude",
    new_chat_url="https://claude.ai/new",
    host="claude.ai",
    login_indicators=("Log in", "Sign up", "Sign in", "Welcome to Claude"),
    auto_login=_GOOGLE_SIGN_IN,
    chat_id_patterns=(r"/chat/([a-zA-Z0-9_-]+)",),
    chat_url_template="https://claude.ai/chat/{chat_id}",
    composer=(
        'div.ProseMirror[contenteditable="true"]',
        'div[aria-label="Write your prompt to Claude"] div[contenteditable="true"]',
        'div[contenteditable="true"][translate="no"]',
    ),
    send=(
        'button[aria-label="Send message"]',
        'button[aria-label="Send Message"]',
        'button[aria-label*="Send"]',
    ),
    stop=(
        '[data-is-streaming="true"]',
        'button[aria-label="Stop response"]',
        'button[aria-label*="Stop"]',
    ),
    answer=ScriptAnswer(_CLAUDE_ANSWER_SCRIPT),
    attach_path=(("Add files, connectors, and more",), ("Add files or photos",)),
)

# meiva:note
# Perplexity renders each answer as markdown into a div id'd markdown-content-<n> (n grows one per turn), so the last match is the newest answer; `.prose` is the same block's typography wrapper, kept as a fallback if the id scheme drifts. Attaching takes two steps: "Add files or tools" opens a popup menu (it ignores synthetic clicks, so the real-input-event click by text is required), then its "Upload files or images" item opens the OS file chooser.
_PERPLEXITY = SiteProfile(
    label="Perplexity",
    new_chat_url="https://www.perplexity.ai/",
    host="www.perplexity.ai",
    login_indicators=("Log in", "Sign up", "Sign in", "Welcome to Perplexity"),
    auto_login=_GOOGLE_SIGN_IN,
    chat_id_patterns=(r"/search/([a-zA-Z0-9_-]+)", r"/thread/([a-zA-Z0-9_-]+)"),
    chat_url_template="https://www.perplexity.ai/search/{chat_id}",
    composer=(
        "#ask-input",
        'div[contenteditable="true"]#ask-input',
        '[role="textbox"]#ask-input',
    ),
    send=('button[aria-label="Submit"]',),
    stop=(
        'button[aria-label^="Stop response"]',
        'button[aria-label*="Stop"]',
    ),
    answer=SelectorAnswer(('[id^="markdown-content"]', "div.prose")),
    attach_path=(("Add files or tools",), ("Upload files or images",)),
)

SITES: Mapping[str, SiteProfile] = MappingProxyType(
    {
        "chatgpt": _CHATGPT,
        "gemini": _GEMINI,
        "claude": _CLAUDE,
        "perplexity": _PERPLEXITY,
    }
)


# meiva:note
# The profile of `name`, or a ValueError listing the known sites.
def site(name: str) -> SiteProfile:
    try:
        return SITES[name]
    except KeyError:
        raise ValueError(f"unknown site {name!r}; known: {', '.join(SITES)}") from None


# meiva:note
# The stable conversation id of `url`: the first capture group of the first of the site's id patterns that matches, or None. Follow-up questions use it to continue the same conversation instead of opening a new one.
def chat_id_from_url(name: str, url: str) -> str | None:
    for pattern in site(name).chat_id_patterns:
        found = re.search(pattern, url)
        if found:
            return found.group(1)
    return None


# meiva:note
# The conversation URL of `chat_id` on site `name`, built from the site's template.
def chat_url(name: str, chat_id: str) -> str:
    return site(name).chat_url_template.format(chat_id=chat_id)


# meiva:note
# Refuse a URL whose host is not the site's host. The check paths take a stored conversation URL; this keeps a bad or attacker-supplied URL from reaching the logged-in browser.
def assert_same_site(url: str, name: str) -> None:
    host = site(name).host
    if not (url.startswith(f"https://{host}/") or url == f"https://{host}"):
        raise ValueError(f"refusing to navigate to non-{host} URL: {url!r}")
