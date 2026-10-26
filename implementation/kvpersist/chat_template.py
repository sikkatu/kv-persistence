"""Single documented algorithm for the chat-template digest.

Identity field KVP_CHAT_TEMPLATE_DIGEST must have exactly ONE definition.
Normative algorithm (used by scripts/setup-identity.sh and preflight):

1. Template source, first match wins:
   a) chat_template.jinja in the model directory;
   b) the "chat_template" key of tokenizer_config.json (string or mapping);
   c) neither present -> UNCERTAIN -> TemplateDigestError (cache-miss policy).
2. Normalization of the template string:
   a) UTF-8 required; b) CRLF/CR -> LF; c) rstrip each line;
   d) drop trailing blank lines.
   A mapping of variants is serialized as canonical JSON (sorted keys,
   separators ,/:) FIRST, then normalized: all variants are identity-relevant
   because any of them may be the actually rendered one.
3. Digest = SHA-256 hex of the normalized payload encoded UTF-8.

pre/workflow MUST use this module instead of ad-hoc hashing. Unit-tested per
step; every step is deterministic.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Tuple


class TemplateDigestError(Exception):
    """Template cannot be resolved unambiguously -> identity is uncertain."""


def normalize_template(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def extract_template(model_dir: str) -> Tuple[str, str]:
    """Return (source_description, normalized_payload)."""
    jinja = os.path.join(model_dir, "chat_template.jinja")
    if os.path.isfile(jinja):
        with open(jinja, "rb") as fh:
            raw = fh.read()
        try:
            return "chat_template.jinja", normalize_template(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise TemplateDigestError(f"chat_template.jinja not valid UTF-8: {exc}")

    cfg_path = os.path.join(model_dir, "tokenizer_config.json")
    if not os.path.isfile(cfg_path):
        raise TemplateDigestError(
            f"neither chat_template.jinja nor tokenizer_config.json in {model_dir}")
    try:
        with open(cfg_path, "rb") as fh:
            cfg = json.loads(fh.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TemplateDigestError(f"tokenizer_config.json unreadable: {exc}")

    if "chat_template" not in cfg:
        raise TemplateDigestError(
            "no chat_template key and no chat_template.jinja: template is "
            "implicit (model default) -> UNCERTAIN by policy")
    value = cfg["chat_template"]
    if isinstance(value, str):
        return "tokenizer_config.json:chat_template", normalize_template(value)
    if isinstance(value, dict):
        canon = json.dumps(value, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
        return "tokenizer_config.json:chat_template(variants)", normalize_template(canon)
    raise TemplateDigestError(f"unsupported chat_template type: {type(value).__name__}")


def chat_template_digest(model_dir: str) -> Tuple[str, str]:
    """Return (digest_hex64, source_description); raises TemplateDigestError."""
    source, payload = extract_template(model_dir)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), source