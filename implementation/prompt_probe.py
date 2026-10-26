"""Chat request-format probe: measures the *stable* token prefix of chat-app requests.

chat apps serialize conversations to OpenAI-style /v1/chat/completions calls.
This tool rebuilds exactly that serialization, tokenizes it with the model's
tokenizer and reports how many leading tokens are stable across sends (the part
that prefix caching / the persistent cache can reuse).

Why it exists: two visually similar conversations may NOT share a tokenized
prefix (timestamps, tool-call IDs, message ordering). The task requires
verifying stability from the real serialization instead of assuming it.

Usage:
    python3 prompt_probe.py --tokenizer /path/to/model_dir --request-file req1.json --request-file req2.json

Each request file is a JSON body exactly as your chat app POSTs it. The probe
prints report JSON: per-request token count, common-prefix length in tokens,
and the SHA-256 of the common prefix token ids. No prompt text is ever printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import List


def render_chat_prompt(request: dict) -> str:
    """Approximate the chat-template rendering for prefix measurement.

    We only need consistent, deterministic text here to COMPARE prefixes across
    requests; the real model uses its own template at serving time. Tool
    definitions and system messages participate in the serialized prefix, so
    they must be included in order.
    """
    parts: List[str] = []
    system = request.get("system")
    if system:
        parts.append("<|system|>\n" + system)
    for tool in request.get("tools") or []:
        parts.append("<|tool|>\n" + json.dumps(tool, sort_keys=True, ensure_ascii=False))
    for msg in request.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, sort_keys=True, ensure_ascii=False)
        parts.append(f"<|{role}|>\n{content}")
    parts.append("<|assistant|>")
    return "\n".join(parts)


def common_prefix_len(a: List[int], b: List[int]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def load_tokenizer(path: str):
    try:
        from transformers import AutoTokenizer  # type: ignore
    except ImportError as exc:  # pragma: no cover - dependency hint only
        raise SystemExit(f"transformers is required for tokenization: {exc}")
    return AutoTokenizer.from_pretrained(path, trust_remote_code=True, local_files_only=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="chat-app stable-prefix probe")
    ap.add_argument("--tokenizer", required=True, help="local model/tokenizer directory")
    ap.add_argument("--request-file", action="append", required=True, dest="requests")
    args = ap.parse_args(argv)

    tok = load_tokenizer(args.tokenizer)
    ids_per_req: List[List[int]] = []
    for path in args.requests:
        with open(path, "r", encoding="utf-8") as fh:
            req = json.load(fh)
        text = render_chat_prompt(req)
        ids = tok.encode(text, add_special_tokens=True)
        ids_per_req.append(ids)

    shortest = min(len(ids) for ids in ids_per_req)
    stable = ids_per_req[0]
    for ids in ids_per_req[1:]:
        stable = stable[: common_prefix_len(stable, ids)]

    digest = hashlib.sha256(json.dumps(stable).encode()).hexdigest()
    report = {
        "requests": len(ids_per_req),
        "token_counts": [len(ids) for ids in ids_per_req],
        "stable_prefix_tokens": len(stable),
        "stable_prefix_sha256": digest,
        "note": "token counts only; prompt text is never printed",
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())