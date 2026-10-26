"""Unit tests: single chat-template digest algorithm."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "implementation"))

from kvpersist.chat_template import (
    TemplateDigestError,
    chat_template_digest,
    extract_template,
    normalize_template,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_jinja_wins_over_tokenizer_config(tmp_path):
    (tmp_path / "chat_template.jinja").write_text("HELLO {{ x }}")
    (tmp_path / "tokenizer_config.json").write_text(
        json.dumps({"chat_template": "SHOULD NOT WIN"}))
    digest, source = chat_template_digest(str(tmp_path))
    assert source == "chat_template.jinja"
    assert digest == _sha("HELLO {{ x }}")


def test_uses_chat_template_key_when_no_jinja(tmp_path):
    (tmp_path / "tokenizer_config.json").write_text(json.dumps({"chat_template": "A{{ b }}"}))
    digest, source = chat_template_digest(str(tmp_path))
    assert source.startswith("tokenizer_config.json")
    assert digest == _sha("A{{ b }}")


def test_unrelated_config_keys_never_change_digest(tmp_path):
    p = tmp_path / "tokenizer_config.json"
    p.write_text(json.dumps({"chat_template": "T", "model_max_length": 100}))
    d1, _ = chat_template_digest(str(tmp_path))
    p.write_text(json.dumps({"chat_template": "T", "model_max_length": 10, "x": 1}))
    d2, _ = chat_template_digest(str(tmp_path))
    assert d1 == d2, "hashing whole tokenizer_config.json is the rejected behaviour"


def test_missing_template_is_uncertain(tmp_path):
    (tmp_path / "tokenizer_config.json").write_text(json.dumps({"model_max_length": 1}))
    with pytest.raises(TemplateDigestError):
        chat_template_digest(str(tmp_path))


def test_empty_dir_is_uncertain(tmp_path):
    with pytest.raises(TemplateDigestError):
        chat_template_digest(str(tmp_path))


def test_variants_mapping_hashed_canonically(tmp_path):
    v = {"tool_use": "A", "plain": "B"}
    (tmp_path / "tokenizer_config.json").write_text(json.dumps({"chat_template": v}))
    d1, _ = chat_template_digest(str(tmp_path))
    # key order in the file must not matter (canonical serialization)
    (tmp_path / "tokenizer_config.json").write_text(json.dumps({"chat_template": {"plain": "B", "tool_use": "A"}}))
    d2, _ = chat_template_digest(str(tmp_path))
    assert d1 == d2


def test_crlf_and_trailing_whitespace_are_normalized(tmp_path):
    (tmp_path / "chat_template.jinja").write_text("A {{ x }}  \r\nB\r\n\r\n")
    d1, _ = chat_template_digest(str(tmp_path))
    (tmp_path / "chat_template.jinja").write_text("A {{ x }}\nB")
    d2, _ = chat_template_digest(str(tmp_path))
    assert d1 == d2


def test_trailing_blank_lines_normalized():
    assert normalize_template("X\n\n\n") == normalize_template("X")


def test_inner_blank_lines_preserved():
    assert normalize_template("X\n\nY") == "X\n\nY"


def test_invalid_utf8_jinja_raises(tmp_path):
    (tmp_path / "chat_template.jinja").write_bytes(b"\xff\xfe not utf8")
    with pytest.raises(TemplateDigestError):
        chat_template_digest(str(tmp_path))


def test_supported_types_only(tmp_path):
    (tmp_path / "tokenizer_config.json").write_text(json.dumps({"chat_template": [1, 2]}))
    with pytest.raises(TemplateDigestError):
        chat_template_digest(str(tmp_path))


def test_extract_source_description(tmp_path):
    (tmp_path / "chat_template.jinja").write_text("X")
    source, payload = extract_template(str(tmp_path))
    assert source == "chat_template.jinja" and payload == "X"