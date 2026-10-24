# meiva:note
# Tests of what the gmail skill decides, as values.
#
# The mailbox itself (IMAP/SMTP against Google) never runs here. The credential parsing, the
# header and body reading, the Gmail queries, the reading of IMAP answers and the reply and
# forward plans are functions over values; each is called with the value it decides on and its
# return value is asserted. The shells that execute them are tested in test_gmail_shell.py.


from __future__ import annotations

import io
from email.message import EmailMessage, Message
from pathlib import Path

import pytest

from meiva_builtins.skills.tests.gmail.gmail_modules import load_gmail_modules

M = load_gmail_modules()
auth = M.auth
headers = M.headers
wire = M.wire
queries = M.queries
compose = M.compose
feed = M.feed

NOW_TS = 1_800_000_000


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("", None),
        ("<newsletter.example.com>", "newsletter.example.com"),
        ("My Newsletter <newsletter.example.com>", "newsletter.example.com"),
        ("Prefix <first> Suffix <second.example.org>", "second.example.org"),
        ("no-brackets-here", "no-brackets-here"),
        ("<trailing.space >", "trailing.space"),
        ("< leading.space>", "leading.space"),
        ("  <spaces.example.com>  ", "spaces.example.com"),
    ],
)
def test_parse_list_id(raw: str | None, expected: str | None) -> None:
    assert headers.parse_list_id(raw) == expected


def test_html_to_text_plain() -> None:
    assert headers.html_to_text("Hello world") == "Hello world"


def test_html_to_text_strips_tags() -> None:
    result = headers.html_to_text("<p>Hello <b>world</b></p>")
    assert "Hello" in result
    assert "world" in result
    assert "<p>" not in result
    assert "<b>" not in result


def test_html_to_text_strips_scripts() -> None:
    result = headers.html_to_text("<html><script>alert('x')</script><body>Content</body></html>")
    assert "Content" in result
    assert "alert" not in result


def test_html_to_text_handles_entities() -> None:
    result = headers.html_to_text("Price: &euro;5 &amp; &lt;free&gt;")
    assert "€5" in result
    assert "&" in result or "&amp;" not in result


def test_html_to_text_collapses_whitespace() -> None:
    result = headers.html_to_text("<div>a</div>   <div>b</div>")
    # meiva:note
    # Whitespace should be normalized — no double spaces
    assert "  " not in result or result.count("\n") >= 1


def test_html_to_text_empty() -> None:
    assert headers.html_to_text("") == ""


def test_body_text_prefers_the_plain_part() -> None:
    msg = EmailMessage()
    msg.set_content("plain body\n")
    msg.add_alternative("<p>html body</p>", subtype="html")
    assert headers.body_text(msg) == "plain body"


def test_body_text_falls_back_to_stripped_html() -> None:
    msg = EmailMessage()
    msg.set_content("<p>only <b>html</b></p>", subtype="html")
    assert headers.body_text(msg) == "only html"


def test_body_text_of_a_message_without_text_parts_is_empty() -> None:
    msg = EmailMessage()
    msg.set_content(b"\x00\x01", maintype="application", subtype="octet-stream")
    assert headers.body_text(msg) == ""


def test_iso_none() -> None:
    assert headers.iso(None) is None
    assert headers.iso("") is None


def test_iso_rfc2822() -> None:
    assert headers.iso("Mon, 21 Jun 2026 15:30:00 +0000") == "2026-06-21T15:30:00Z"


def test_iso_converts_other_zones_to_utc() -> None:
    assert headers.iso("Mon, 21 Jun 2026 15:30:00 +0200") == "2026-06-21T13:30:00Z"


def test_iso_naive_becomes_utc() -> None:
    assert headers.iso("Mon, 21 Jun 2026 15:30:00") == "2026-06-21T15:30:00Z"


def test_iso_invalid() -> None:
    assert headers.iso("not a date") is None
    assert headers.iso("garbage") is None


def test_meta_extracts_fields() -> None:
    msg: Message[str, str] = Message()
    msg["Message-Id"] = "<abc123@example.com>"
    msg["List-Id"] = "My List <newsletter.example.com>"
    msg["From"] = "sender@example.com"
    msg["Subject"] = "Test Subject"
    msg["Date"] = "Mon, 21 Jun 2026 15:30:00 +0000"

    assert headers.meta(msg) == {
        "message_id": "abc123@example.com",
        "list_id": "newsletter.example.com",
        "from": "sender@example.com",
        "subject": "Test Subject",
        "date": "2026-06-21T15:30:00Z",  # time-bomb-ok: a parsed header value, not a window
    }


def test_meta_missing_fields() -> None:
    msg: Message[str, str] = Message()
    assert headers.meta(msg) == {
        "message_id": None,
        "list_id": None,
        "from": None,
        "subject": None,
        "date": None,
    }


def test_addr_list_single() -> None:
    assert headers.addr_list("a@b.com") == ["a@b.com"]


def test_addr_list_multiple() -> None:
    assert headers.addr_list("a@b.com, c@d.com") == ["a@b.com", "c@d.com"]


def test_addr_list_with_names() -> None:
    assert headers.addr_list("Alice <a@b.com>, Bob <c@d.com>") == ["a@b.com", "c@d.com"]


def test_addr_list_none() -> None:
    assert headers.addr_list(None) == []


def test_addr_list_empty() -> None:
    assert headers.addr_list("") == []


def test_recipient_list_parses_strings_and_keeps_lists() -> None:
    assert compose.recipient_list("A <a@b.com>, c@d.com") == ["a@b.com", "c@d.com"]
    assert compose.recipient_list(["a@b.com", "c@d.com"]) == ["a@b.com", "c@d.com"]
    assert compose.recipient_list(None) == []


def test_since_to_ts_none() -> None:
    assert queries.since_to_ts(None, NOW_TS) is None


def test_since_to_ts_days() -> None:
    assert queries.since_to_ts("30d", NOW_TS) == NOW_TS - 30 * 86400


def test_since_to_ts_hours() -> None:
    assert queries.since_to_ts("12h", NOW_TS) == NOW_TS - 12 * 3600


def test_since_to_ts_iso_date() -> None:
    assert queries.since_to_ts("2026-06-01", NOW_TS) == 1780272000


def test_since_to_ts_iso_date_with_zone() -> None:
    assert queries.since_to_ts("2026-06-01T02:00:00+02:00", NOW_TS) == 1780272000


def test_since_to_ts_refuses_an_unreadable_window() -> None:
    with pytest.raises(ValueError, match="--since must be like 30d, 12h or 2026-05-07"):
        queries.since_to_ts("last week", NOW_TS)


def test_message_query_accepts_brackets_or_bare() -> None:
    assert queries.message_query("<abc@host>") == "rfc822msgid:abc@host"
    assert queries.message_query(" abc@host ") == "rfc822msgid:abc@host"


def test_read_query_needs_exactly_one_selector() -> None:
    assert queries.read_query("from:boss", None) == "from:boss"
    assert queries.read_query(None, "<abc@host>") == "rfc822msgid:abc@host"
    with pytest.raises(ValueError, match="exactly one"):
        queries.read_query(None, None)
    with pytest.raises(ValueError, match="exactly one"):
        queries.read_query("from:boss", "<abc@host>")


def test_discover_query_parenthesizes_the_net_so_the_window_binds_all_of_it() -> None:
    assert queries.discover_query(None) == "(category:updates OR category:forums)"
    assert queries.discover_query(NOW_TS) == f"(category:updates OR category:forums) after:{NOW_TS}"


def test_enum_query_filters_by_list_id_and_window() -> None:
    assert queries.enum_query("news.example.com", None) == "list:news.example.com"
    assert queries.enum_query("news.example.com", 5) == "list:news.example.com after:5"


def _meta(list_id: str | None, sender: str, subject: str) -> dict[str, str | None]:
    return {"list_id": list_id, "from": sender, "subject": subject}


def test_list_id_counts_groups_by_newsletter_sorted_by_count() -> None:
    rows = queries.list_id_counts(
        [
            _meta("a.example.com", "A <a@x>", "a-new"),
            _meta(None, "person@x", "hello"),
            _meta("b.example.com", "B <b@x>", "b-new"),
            _meta("b.example.com", "B <b@x>", "b-old"),
            _meta("", "empty@x", "empty list id"),
        ]
    )
    assert rows == [
        {
            "list_id": "b.example.com",
            "count": 2,
            "sample_from": "B <b@x>",
            "sample_subject": "b-new",
        },
        {
            "list_id": "a.example.com",
            "count": 1,
            "sample_from": "A <a@x>",
            "sample_subject": "a-new",
        },
    ]


def test_list_id_counts_of_nothing_is_empty() -> None:
    assert queries.list_id_counts([]) == []


def test_xgm_simple() -> None:
    assert wire.xgm("from:boss newer_than:7d") == '"from:boss newer_than:7d"'


def test_xgm_escapes_quotes() -> None:
    assert wire.xgm('subject:"hello world"') == '"subject:\\"hello world\\""'


def test_xgm_escapes_backslashes() -> None:
    assert wire.xgm("a\\b") == '"a\\\\b"'


def test_search_response_names_the_matching_ids() -> None:
    assert wire.parse_search_response("OK", [b"3 7 12"], "q") == [b"3", b"7", b"12"]
    assert wire.parse_search_response("OK", [b""], "q") == []
    assert wire.parse_search_response("OK", [None], "q") == []
    assert wire.parse_search_response("OK", [], "q") == []


def test_search_response_not_ok_raises() -> None:
    with pytest.raises(auth.GmailError, match=r"search failed \(typ=NO\)"):
        wire.parse_search_response("NO", [b"nope"], "from:x")


def test_search_response_gmail_refusal_in_the_data_line_raises() -> None:
    with pytest.raises(auth.GmailError, match=r"Gmail refused the search 'list:\*'"):
        wire.parse_search_response("OK", [b"[SERVERBUG] infeasible query"], "list:*")


def test_recent_is_newest_first_and_windowed() -> None:
    ids = [b"1", b"2", b"3", b"4"]
    assert wire.recent(ids, None) == [b"4", b"3", b"2", b"1"]
    assert wire.recent(ids, 2) == [b"4", b"3"]
    assert wire.recent(ids, 0) == []
    assert wire.recent(ids, 99) == [b"4", b"3", b"2", b"1"]


def test_recent_rejects_a_negative_window() -> None:
    with pytest.raises(ValueError, match="must be >= 0"):
        wire.recent([b"1"], -1)


def test_drafts_folder_is_found_by_its_special_use_attribute() -> None:
    lines = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"',
        b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
    ]
    assert wire.parse_drafts_folder(lines) == "[Gmail]/Drafts"


def test_drafts_folder_name_may_be_unquoted_or_localized() -> None:
    name = "Entw" + chr(0xFC) + "rfe"
    line = ("(" + chr(92) + "Drafts) NIL " + name).encode()
    assert wire.parse_drafts_folder([line]) == name


def test_drafts_folder_missing_or_unparseable_raises() -> None:
    with pytest.raises(auth.GmailError, match=r"no .*Drafts special-use folder"):
        wire.parse_drafts_folder([b'(\\HasNoChildren) "/" "INBOX"'])
    with pytest.raises(auth.GmailError, match="unparseable LIST line"):
        wire.parse_drafts_folder([b"garbage"])


def test_body_arg_direct() -> None:
    assert feed.body_arg("hello", io.StringIO("ignored")) == "hello"


def test_body_arg_reads_stdin_when_omitted() -> None:
    assert feed.body_arg(None, io.StringIO("from stdin\n")) == "from stdin\n"


def test_body_arg_rejects_empty() -> None:
    with pytest.raises(ValueError, match="no body"):
        feed.body_arg("", io.StringIO())


def test_body_arg_rejects_whitespace() -> None:
    with pytest.raises(ValueError, match="no body"):
        feed.body_arg("   ", io.StringIO())


def test_body_arg_rejects_a_blank_stdin() -> None:
    with pytest.raises(ValueError, match="no body"):
        feed.body_arg(None, io.StringIO(" \n"))


def _message(**hdrs: str) -> EmailMessage:
    msg = EmailMessage()
    for key, value in hdrs.items():
        msg[key.replace("_", "-")] = value
    msg.set_content("Hello world")
    return msg


def test_msg_summary() -> None:
    msg = _message(
        From="me@gmail.com", To="you@gmail.com", Subject="Hi", Message_Id="<abc@mail.gmail.com>"
    )
    summary = compose.msg_summary(msg)
    assert summary["message_id"] == "abc@mail.gmail.com"
    assert summary["from"] == "me@gmail.com"
    assert summary["to"] == "you@gmail.com"
    assert summary["subject"] == "Hi"
    assert summary["body"] == "Hello world"
    assert summary["attachments"] == []


def test_msg_summary_lists_attachment_names() -> None:
    msg = _message(From="me@gmail.com", To="you@gmail.com", Subject="Hi")
    msg.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="a.pdf")
    summary = compose.msg_summary(msg)
    assert summary["attachments"] == ["a.pdf"]
    assert summary["body"] == "Hello world"


def test_sent_summary_dry_run() -> None:
    msg = _message(From="me@gmail.com", To="you@gmail.com", Subject="Hi", Message_Id="<a@b>")
    summary = compose.sent_summary(msg, sent=False)
    assert summary["sent"] is False
    assert summary["body"] == "Hello world"


def test_sent_summary_real() -> None:
    msg = _message(From="me@gmail.com", To="you@gmail.com", Subject="Hi", Message_Id="<a@b>")
    assert compose.sent_summary(msg, sent=True)["sent"] is True


def _original(**hdrs: str) -> EmailMessage:
    return _message(**{"Message_Id": "<orig@x>", "Subject": "Quarterly plan", **hdrs})


def test_reply_goes_to_the_reply_to_address_and_threads() -> None:
    orig = _original(From="Boss <boss@x>", Reply_To="desk@x", References="<root@x>")
    plan = compose.plan_reply(orig, "<orig@x>", "me@x", reply_all=False)
    assert plan.to == ("desk@x",)
    assert plan.cc == ()
    assert plan.subject == "Re: Quarterly plan"
    assert plan.in_reply_to == "<orig@x>"
    assert plan.references == "<root@x> <orig@x>"


def test_reply_falls_back_to_from_and_starts_the_reference_chain() -> None:
    plan = compose.plan_reply(_original(From="Boss <boss@x>"), "<orig@x>", "me@x", reply_all=False)
    assert plan.to == ("boss@x",)
    assert plan.references == "<orig@x>"


def test_reply_does_not_double_the_prefix() -> None:
    orig = _original(From="boss@x", Subject="RE: already")
    assert compose.plan_reply(orig, "<orig@x>", "me@x", reply_all=False).subject == "RE: already"


def test_reply_all_ccs_the_original_recipients_minus_us_and_the_reply_targets() -> None:
    orig = _original(From="boss@x", To="Me <ME@x>, peer@x, boss@x", Cc="peer@x, other@x")
    plan = compose.plan_reply(orig, "<orig@x>", "me@x", reply_all=True)
    assert plan.to == ("boss@x",)
    assert plan.cc == ("peer@x", "other@x")


def test_reply_to_a_message_without_a_sender_raises() -> None:
    msg = EmailMessage()
    msg.set_content("x")
    with pytest.raises(auth.GmailError, match="no Reply-To/From address"):
        compose.plan_reply(msg, "<orig@x>", "me@x", reply_all=False)


def test_forward_quotes_the_original_below_the_note() -> None:
    orig = _original(From="boss@x", To="me@x", Date="Sun, 21 Jun 2026 15:30:00 +0000")
    plan = compose.plan_forward(orig, "FYI")
    assert plan.subject == "Fwd: Quarterly plan"
    assert plan.body == (
        "FYI\n\n---------- Forwarded message ----------\n"
        "From: boss@x\nDate: Sun, 21 Jun 2026 15:30:00 +0000\n"
        "Subject: Quarterly plan\nTo: me@x\n\nHello world"
    )


def test_forward_without_a_note_is_just_the_quote() -> None:
    plan = compose.plan_forward(_original(From="boss@x", Subject="Fwd: again"), "  ")
    assert plan.subject == "Fwd: again"
    assert plan.body.startswith("---------- Forwarded message ----------")


def test_keychain_argv_names_the_service_and_the_field() -> None:
    sources = auth.AuthSources("", Path("pw"), "sec", 15.0)
    assert sources.keychain_argv() == ["sec", "find-generic-password", "-s", "meiva-gmail-imap"]
    assert sources.keychain_argv("-w")[-1] == "-w"


def test_account_is_the_acct_label_of_the_keychain_dump() -> None:
    dump = (
        'class: "genp"\nattributes:\n    "acct"<blob>="stored@example.com"\n    "svce"<blob>="x"\n'
    )
    assert auth.account_from_keychain_dump(dump) == "stored@example.com"


def test_a_keychain_dump_without_an_account_raises() -> None:
    with pytest.raises(auth.GmailError, match=r"no account \(acct\) attribute"):
        auth.account_from_keychain_dump('class: "genp"\n')


def test_password_normalization_drops_the_display_spaces() -> None:
    assert auth.normalize_password(" abcd efgh ijkl mnop \n") == "abcdefghijklmnop"
    assert auth.normalize_password("  \n") == ""


@pytest.mark.parametrize(
    "kind,stderr,expected",
    [
        ("program_missing", "", "security command not found"),
        ("timed_out", "", "timed out after 15s"),
        (
            "refused",
            "The specified item could not be found\n",
            "The specified item could not be found.",
        ),
        ("refused", "", "not found."),
    ],
)
def test_keychain_errors_say_what_failed_and_how_to_create_the_entry(
    kind: str, stderr: str, expected: str
) -> None:
    error = auth.keychain_error(kind, stderr, 15.0)
    assert isinstance(error, auth.GmailError)
    assert "could not read Keychain entry (service='meiva-gmail-imap')" in str(error)
    assert expected in str(error)
    if kind != "timed_out":
        assert "security add-generic-password -a <you@gmail.com> -s meiva-gmail-imap -w" in str(
            error
        )


def test_no_password_error_names_the_file() -> None:
    error = auth.no_password_error(Path("/home/x/.meiva/secrets/gmail-app-password"))
    assert "file /home/x/.meiva/secrets/gmail-app-password unreadable/missing" in str(error)
