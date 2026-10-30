import email
import email.policy

from cf_email_unwrapper.mail import (
    QuoteTrim,
    a2a_message_id,
    context_id,
    extract_text,
    own_message_id,
    quote_level,
    thread_root,
    trim_quotes,
)

FULL, ONE_LEVEL = QuoteTrim.FULL, QuoteTrim.ONE_LEVEL


def ctx(headers: str) -> str:
    raw = (headers + "\r\n\r\nbody\r\n").encode()
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return context_id(thread_root(msg, own_message_id(msg, raw)))


def test_context_id_is_stable_across_a_thread():
    root = ctx("Message-ID: <root@example.com>")
    reply = ctx("Message-ID: <r1@x>\r\nIn-Reply-To: <root@example.com>\r\n"
                "References: <root@example.com>")
    deeper = ctx("Message-ID: <r3@x>\r\nIn-Reply-To: <r2@x>\r\n"
                 "References: <root@example.com>\r\n <r1@x> <r2@x>")
    in_reply_to_only = ctx("Message-ID: <r4@x>\r\nIn-Reply-To: <root@example.com>")
    assert root == reply == deeper == in_reply_to_only
    assert ctx("Message-ID: <other@example.com>") != root


def test_message_id_is_deterministic_and_distinct_from_context():
    assert a2a_message_id("<a@x>") == a2a_message_id("<a@x>") != a2a_message_id("<b@x>")
    assert a2a_message_id("<a@x>") != context_id("<a@x>")


def test_extract_text_prefers_plain_and_falls_back_to_html():
    alternative = (
        b"Content-Type: multipart/alternative; boundary=b\r\n\r\n"
        b"--b\r\nContent-Type: text/html\r\n\r\n<p>html</p>\r\n"
        b"--b\r\nContent-Type: text/plain\r\n\r\nplain\r\n--b--\r\n"
    )
    assert extract_text(alternative) == "plain"
    html_only = b"Content-Type: text/html\r\n\r\n<p>Hello <b>world</b></p><ul><li>one</li></ul>\r\n"
    text = extract_text(html_only)
    assert "Hello **world**" in text and "one" in text and "<" not in text


IOS_REPLY = """Ja, gerne morgen.

Am 30.10.2026 um 15:46 schrieb Hermes <hermes@example.org>:

> Treffen am Freitag?
> 
> > Am 30.10.2026 um 15:40 schrieb Author <author@example.com>:
> > 
> > Wann treffen wir uns?
>> Noch älter

"""

THUNDERBIRD_REPLY = """Sounds good.
Second line.

On 29.10.26 08:00, Hermes wrote:
> Shall we meet?
>
> On 28.10.26 07:00, Author wrote:
>> When?
>>
"""


def test_quote_level_counts_markers_with_spaces_between():
    assert [quote_level(s) for s in ["x", "> x", ">> x", "> > x", "  >x", "x > y", ">"]] == [
        0, 1, 2, 2, 1, 0, 1]


def test_ios_reply_with_localized_attribution():
    assert trim_quotes(IOS_REPLY, FULL) == "Ja, gerne morgen."
    assert trim_quotes(IOS_REPLY, ONE_LEVEL) == (
        "Ja, gerne morgen.\n\n"
        "Am 30.10.2026 um 15:46 schrieb Hermes <hermes@example.org>:\n\n"
        "> Treffen am Freitag?"
    )


def test_thunderbird_reply():
    assert trim_quotes(THUNDERBIRD_REPLY, FULL) == "Sounds good.\nSecond line."
    assert trim_quotes(THUNDERBIRD_REPLY, ONE_LEVEL) == (
        "Sounds good.\nSecond line.\n\nOn 29.10.26 08:00, Hermes wrote:\n> Shall we meet?"
    )


def test_quote_without_attribution_keeps_the_text_above():
    text = "Agreed: let us go.\nSee you\n> quoted\n> > older"
    assert trim_quotes(text, FULL) == "Agreed: let us go.\nSee you"
    assert trim_quotes(text, ONE_LEVEL) == "Agreed: let us go.\nSee you\n> quoted"


def test_interleaved_answers_are_kept():
    text = "On Thu, Hermes wrote:\n> First question?\nYes.\n\n> > nested\n> Second?\nNo.\n"
    assert trim_quotes(text, FULL) == text
    assert trim_quotes(text, ONE_LEVEL) == text


def test_message_that_is_only_a_quote_is_kept():
    for text in ["On Thu, Hermes wrote:\n> hi\n> > older", "> just\n>> quoted", ""]:
        assert trim_quotes(text, FULL) == text
        assert trim_quotes(text, ONE_LEVEL) == text
