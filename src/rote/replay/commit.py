"""The commit protocol for irreversible capabilities: preview, then commit with a token.

An LLM caller would always pass ``confirm: true``, so a boolean can't carry
consent. Instead:

1. **Preview** runs up to the first irreversible step, reads the review screen
   (the capability's ``preview_outputs``), and returns those values with a
   signed **commit token**. The token binds the artifact (content hash and
   overlay hash), the tenant, the exact inputs, and the review values, and it
   expires.
2. **Commit** needs that token *and* an idempotency key. It re-runs to the review
   screen, re-reads the values, and clicks the irreversible control only if they
   still match what the token approved. Otherwise the result is PREVIEW_MISMATCH.

The token holds digests, never raw values, so it leaks nothing if logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

from rote.schema.hashing import canonical_json

TOKEN_TTL_S = 15 * 60


class TokenError(ValueError):
    pass


@dataclass(frozen=True)
class TokenClaims:
    capability: str
    content_hash: str
    overlay_hash: str | None
    tenant: str
    inputs_digest: str
    preview_digest: str
    expires: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def digest(key: bytes, value: Any) -> str:
    return hmac.new(key, canonical_json(value).encode(), hashlib.sha256).hexdigest()[:32]


def issue(key: bytes, *, capability: str, content_hash: str, overlay_hash: str | None, tenant: str,
          inputs: dict[str, str], preview: dict[str, Any], ttl_s: int = TOKEN_TTL_S) -> tuple[str, int]:
    expires = int(time.time()) + ttl_s
    claims = {"cap": capability, "hash": content_hash, "overlay": overlay_hash, "tenant": tenant,
              "inputs": digest(key, inputs), "preview": digest(key, preview), "exp": expires}
    body = canonical_json(claims).encode()
    signature = hmac.new(key, body, hashlib.sha256).digest()
    return f"{_b64(body)}.{_b64(signature)}", expires


def verify(key: bytes, token: str, *, capability: str, content_hash: str, overlay_hash: str | None, tenant: str,
           inputs: dict[str, str]) -> TokenClaims:
    try:
        body_text, signature_text = token.split(".", 1)
        body, signature = _unb64(body_text), _unb64(signature_text)
    except ValueError as exc:
        raise TokenError("the token is malformed") from exc
    if not hmac.compare_digest(hmac.new(key, body, hashlib.sha256).digest(), signature):
        raise TokenError("the token signature is invalid")
    claims = json.loads(body)
    if claims["exp"] < time.time():
        raise TokenError("the token has expired; run the preview again")
    if claims["cap"] != capability or claims["hash"] != content_hash or claims["overlay"] != overlay_hash:
        raise TokenError("the token was issued for a different version of this capability")
    if claims["tenant"] != tenant:
        raise TokenError("the token was issued for another tenant")
    if not hmac.compare_digest(claims["inputs"], digest(key, inputs)):
        raise TokenError("the inputs differ from the ones the preview was run with")
    return TokenClaims(claims["cap"], claims["hash"], claims["overlay"], claims["tenant"], claims["inputs"],
                       claims["preview"], claims["exp"])
