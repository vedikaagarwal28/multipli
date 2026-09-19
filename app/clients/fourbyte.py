"""
4byte.directory — free, public, no API key. Used for #15
(privileged_function_count) when a contract isn't verified on
Etherscan, and as a fallback for #16 (transaction_type) when a
historical tx's target contract has no ABI.

The directory is crowd-sourced, so a selector can resolve to more
than one plausible text signature (hash collisions in the 4-byte
space, or just multiple real functions sharing a name elsewhere).
4byte's default ordering is newest-submission-first, which tends to
surface spam/collision entries (e.g. "workMyDirefulOwner(uint256,
uint256)" ahead of the real "transfer(address,uint256)" for
0xa9059cbb) — verified against the live API. We explicitly request
`ordering=created_at` (oldest first) and take the first result, which
puts the original/canonical signature first — good enough for a
keyword match, not meant to be a source of truth for exact ABI
decoding.
"""
from typing import Optional
import httpx

BASE_URL = "https://www.4byte.directory/api/v1/signatures/"


class FourByteClient:
    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None
        self._cache: dict[str, Optional[str]] = {}

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()

    async def lookup(self, selector: str) -> Optional[str]:
        """selector like '0xa9059cbb' -> 'transfer(address,uint256)' or None."""
        selector = selector.lower()
        if selector in self._cache:
            return self._cache[selector]
        try:
            resp = await self._client.get(
                BASE_URL, params={"hex_signature": selector, "ordering": "created_at"}
            )
            resp.raise_for_status()
            body = resp.json()
            results = body.get("results", [])
            text = results[0]["text_signature"] if results else None
        except (httpx.HTTPError, KeyError, ValueError):
            text = None
        self._cache[selector] = text
        return text
