"""Plain MIME helpers: thread ids, text extraction, quote trimming and reply headers."""

import email.policy
import hashlib
import re
from email.message import EmailMessage, Message
from email.utils import formatdate, make_msgid
from enum import Enum

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


class QuoteTrim(Enum):
    """How much of the trailing quoted history to keep."""

    FULL = "full"  # drop it: the agent wrote the quoted mail and it is the latest in the thread
    ONE_LEVEL = "one_level"  # keep the attribution and the directly quoted mail only


_QUOTE_PREFIX = re.compile(r"[ \t]*((?:>[ \t]*)*)")


def quote_level(line: str) -> int:
    """Number of leading '>' markers, spaces between them allowed ('> > x' is level 2)."""
    return _QUOTE_PREFIX.match(line).group(1).count(">")


def _is_empty(line: str) -> bool:
    """Blank, or only quote markers."""
    return not line.replace(">", "").strip()


def trim_quotes(text: str, mode: QuoteTrim) -> str:
    """Trim the trailing quote block of a reply, independent of the client's language.

    The block is the maximal run of quoted (or blank) lines at the end, plus the unquoted line
    directly above it when it ends with ':' (the attribution, e.g. "On ... wrote:"), plus the
    blank lines above. Quotes followed by new text (interleaved answers) are never touched, and a
    text that is nothing but the block is returned unchanged.
    """
    lines = text.splitlines()
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    if not end or quote_level(lines[end - 1]) == 0:
        return text
    start = end - 1  # first quoted line of the block
    while start and (not lines[start - 1].strip() or quote_level(lines[start - 1])):
        start -= 1
    while not lines[start].strip():
        start += 1
    cut = above = start
    while above and not lines[above - 1].strip():
        above -= 1
    if above and lines[above - 1].rstrip().endswith(":"):
        cut = above - 1
    while cut and not lines[cut - 1].strip():
        cut -= 1
    if cut == 0:
        return text
    if mode is QuoteTrim.FULL:
        return "\n".join(lines[:cut])
    return "\n".join(lines[:start] + _first_level(lines[start:end]))


def _first_level(quoted: list[str]) -> list[str]:
    """The level-1 lines of a quote block, without nested quotes and their attribution."""
    levels = [quote_level(line) for line in quoted]
    nested_attribution = [False] * len(quoted)
    following = 0  # level of the next non-empty line
    for i in reversed(range(len(quoted))):
        nested_attribution[i] = (levels[i] == 1 and following >= 2
                                 and quoted[i].rstrip().endswith(":"))
        if not _is_empty(quoted[i]):
            following = levels[i]
    kept: list[str] = []
    for line, level, attribution in zip(quoted, levels, nested_attribution):
        if level >= 2 or attribution:
            continue
        if _is_empty(line) and (not kept or _is_empty(kept[-1])):
            continue  # collapse the gaps left by dropped lines
        kept.append(line)
    while kept and _is_empty(kept[-1]):
        kept.pop()
    return kept


def reply_message_id(agent_address: str) -> str:
    return make_msgid(domain=agent_address.rsplit("@", 1)[-1])


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
    message_id: str,
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
    h["Message-ID"] = message_id
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
