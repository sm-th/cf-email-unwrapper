import email
import email.policy

from cf_email_unwrapper.mail import (
    a2a_message_id,
    context_id,
    extract_text,
    own_message_id,
    thread_root,
)


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
