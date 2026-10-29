"""Plain MIME helpers: thread ids, text extraction and reply headers."""

import email.policy
import hashlib
import re
from email.message import EmailMessage, Message
from email.utils import formatdate, make_msgid

import html2text

_MSG_ID = re.compile(r"<[^<>\s]+>")


def message_ids(value: str | None) -> list[str]:
    return _MSG_ID.findall(str(value or ""))


def own_message_id(msg: Message, raw: bytes) -> str:
    ids = message_ids(msg.get("Message-ID"))
    if ids:
        return ids[0]
    # Without a Message-ID the content hash is the only stable identity.
    return f"<{hashlib.sha256(raw).hexdigest()}@missing-message-id>"


def thread_root(msg: Message, message_id: str) -> str:
    """First Message-ID in References, else In-Reply-To, else the message's own id."""
    references = message_ids(msg.get("References"))
    if references:
        return references[0]
    in_reply_to = message_ids(msg.get("In-Reply-To"))
    if in_reply_to:
        return in_reply_to[0]
    return message_id


def context_id(root_message_id: str) -> str:
    return hashlib.sha256(f"thread:{root_message_id}".encode()).hexdigest()


def a2a_message_id(message_id: str) -> str:
    return hashlib.sha256(f"message:{message_id}".encode()).hexdigest()


def extract_text(entity: bytes) -> str:
    """Text of a MIME entity: text/plain preferred, else HTML converted to text."""
    msg = email.message_from_bytes(entity, policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    text = body.get_content()
    if body.get_content_subtype() == "html":
        converter = html2text.HTML2Text()
        converter.body_width = 0
        text = converter.handle(text)
    return text.strip()


def text_entity(text: str) -> bytes:
    """A text/plain MIME entity (headers + body) ready to be signed."""
    part = EmailMessage()
    part.set_content(text, charset="utf-8", cte="quoted-printable")
    del part["MIME-Version"]
    return part.as_bytes(policy=email.policy.SMTP)


def reply_subject(subject: str) -> str:
    subject = subject.strip()
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def compose_reply(
    sealed: bytes,
    *,
    agent_address: str,
    author: str,
    subject: str,
    parent_message_id: str,
    parent_references: str,
    urgent: bool,
) -> bytes:
    """Prefix the sealed MIME entity with the RFC 5322 reply headers."""
    h = EmailMessage(policy=email.policy.SMTP)
    h["From"] = agent_address
    h["To"] = author
    h["Subject"] = reply_subject(subject)
    h["Date"] = formatdate(usegmt=True)
    h["Message-ID"] = make_msgid(domain=agent_address.rsplit("@", 1)[-1])
    h["In-Reply-To"] = parent_message_id
    h["References"] = " ".join([*message_ids(parent_references), parent_message_id])
    h["Auto-Submitted"] = "auto-replied"
    if urgent:
        h["X-Priority"] = "2 (High)"
        h["Importance"] = "high"
    else:
        h["X-Priority"] = "4 (Low)"
        h["Importance"] = "low"
    head = b"".join(h.policy.fold_binary(name, value) for name, value in h.items())
    return re.sub(rb"\r?\n", b"\r\n", head + sealed)
