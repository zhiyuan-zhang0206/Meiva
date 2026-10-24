# meiva:note
# The scripts the web-ai driver evaluates in the logged-in page, built from a site profile's selectors and the values to type or look for. Each builder returns the full `() => ...` function text and nothing else, so a test reads exactly what the page would be asked, and the driver's one effect (evaluating it) stays elsewhere. Selector lists and strings enter the script through json.dumps, never by string splicing.
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import assert_never

from _sites import ScriptAnswer, SelectorAnswer

LOCATION_JS = "() => location.href"

IMAGE_SOURCES_JS = (
    "() => [...document.images].map((i) => i.currentSrc || i.src || '').filter(Boolean)"
)


# meiva:note
# True when any selector matches an element now.
def any_present_js(selectors: Sequence[str]) -> str:
    arr = json.dumps(list(selectors))
    return f"() => {{ for (const s of {arr}) {{ if (document.querySelector(s)) return true; }} return false; }}"


# meiva:note
# Type `text` into the first composer selector that matches, replacing what is there. It uses the native value setter for textarea and input and `execCommand insertText` for contenteditable, because both fire the input events the site's editor (React, ProseMirror, Quill) listens to, unlike setting `.value` or `.textContent` directly. It answers `{ok, reason}` or `{ok, got_len}` with the length read back from the composer.
def inject_prompt_js(composer: Sequence[str], text: str) -> str:
    return (
        "() => {"
        f"  const sels = {json.dumps(list(composer))};"
        f"  const text = {json.dumps(text)};"
        "  let el = null;"
        "  for (const s of sels) { el = document.querySelector(s); if (el) break; }"
        "  if (!el) return { ok:false, reason:'composer-not-found' };"
        "  el.focus();"
        "  const tag = el.tagName.toLowerCase();"
        "  if (tag === 'textarea' || tag === 'input') {"
        "    const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;"
        "    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;"
        "    setter.call(el, text);"
        "    el.dispatchEvent(new Event('input', { bubbles: true }));"
        "  } else {"
        "    const sel = window.getSelection();"
        "    sel.selectAllChildren(el);"
        "    document.execCommand('insertText', false, text);"
        "  }"
        "  const got = (el.innerText !== undefined ? el.innerText : el.value) || '';"
        "  return { ok:true, got_len: got.length };"
        "}"
    )


# meiva:note
# Send the composed prompt: click the first enabled send button, otherwise dispatch an Enter keystroke on the first composer that exists. It answers `{ok, via}` naming the path that fired, or `{ok: false, reason}` when neither target exists.
def submit_js(send: Sequence[str], composer: Sequence[str]) -> str:
    return (
        "() => {"
        f"  for (const s of {json.dumps(list(send))}) {{"
        "    const b = document.querySelector(s);"
        "    if (b && !b.disabled) { b.click(); return { ok:true, via:'button:'+s }; }"
        "  }"
        f"  for (const s of {json.dumps(list(composer))}) {{"
        "    const el = document.querySelector(s);"
        "    if (el) {"
        "      el.focus();"
        "      for (const t of ['keydown','keypress','keyup']) {"
        "        el.dispatchEvent(new KeyboardEvent(t, {key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true, cancelable:true}));"
        "      }"
        "      return { ok:true, via:'enter' };"
        "    }"
        "  }"
        "  return { ok:false, reason:'no-send-target' };"
        "}"
    )


# meiva:note
# Read the text of the last assistant message: a selector reader takes the innerText of the last element of the first selector that matches, and a script reader is its own function.
def read_answer_js(reader: SelectorAnswer | ScriptAnswer) -> str:
    match reader:
        case ScriptAnswer(script=script):
            return script
        case SelectorAnswer(selectors=selectors):
            return (
                "() => {"
                f"  for (const s of {json.dumps(list(selectors))}) {{"
                "    const els = document.querySelectorAll(s);"
                "    if (els.length) return els[els.length - 1].innerText || '';"
                "  }"
                "  return '';"
                "}"
            )
        case _:
            assert_never(reader)


# meiva:note
# True once the attachment `filename` shows on the page: its name in the page text, in an aria-label, or a new image whose source is not in `baseline_images`. An image attachment registers as a new thumbnail that often carries no filename anywhere, a document as its filename in text, and the probe accepts either.
def chip_visible_js(filename: str, baseline_images: Sequence[str]) -> str:
    return (
        "() => {"
        f"  const NAME = {json.dumps(filename)};"
        f"  const PRE = new Set({json.dumps(list(baseline_images))});"
        "  if ((document.body.innerText || '').includes(NAME)) return true;"
        "  if ([...document.querySelectorAll('[aria-label]')]"
        "      .some((e) => (e.getAttribute('aria-label') || '').includes(NAME))) return true;"
        "  return [...document.images].some((i) => {"
        "    const s = i.currentSrc || i.src || '';"
        "    return s && !PRE.has(s);"
        "  });"
        "}"
    )


# meiva:note
# True once some send control is present, not disabled and not aria-disabled: the signal that an upload finished on the sites that disable send mid-upload.
def send_enabled_js(send: Sequence[str]) -> str:
    return (
        "() => {"
        f"  for (const s of {json.dumps(list(send))}) {{"
        "    const b = document.querySelector(s);"
        "    if (b && !b.disabled && b.getAttribute('aria-disabled') !== 'true') return true;"
        "  }"
        "  return false;"
        "}"
    )
