"""
Etherscan API v2 client.

Rate limiting: the free tier is 3 req/sec (some plans allow 5 — set
ETHERSCAN_MAX_REQ_PER_SEC accordingly). This client serializes every
call through one asyncio.Semaphore-guarded pacer so nothing in the
app can accidentally burst past it, including the N parallel calls
new_counterparty_ratio makes.

Pagination: capped at 1,000 rows/page (Etherscan free tier, effective
2026-07-01 — was 10,000 before). A `truncated` flag is set on any
result that hits the cap, so callers know age/count features on a
very busy address may be undercounting older history.

Also exposes the `proxy` module (eth_call / eth_getStorageAt /
eth_getCode / eth_getTransactionByHash / eth_getBlockByNumber) so the
whole app can run on a single Etherscan key with no separate RPC
provider — see RpcClient for how this is used as the default backend.
"""
import asyncio
import time
from typing import Any, Optional

import httpx

BASE_URL = "https://api.etherscan.io/v2/api"


class EtherscanError(Exception):
    pass


class RateLimiter:
    """Simple pacer: guarantees at least `interval` seconds between
    calls, safe under concurrent use via a lock."""

    def __init__(self, max_per_sec: float):
        self._interval = 1.0 / max_per_sec if max_per_sec > 0 else 0
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self._interval:
                await asyncio.sleep(self._interval - elapsed)
            self._last_call = time.monotonic()


class EtherscanClient:
    def __init__(
        self,
        api_key: str,
        chain_id: int = 1,
        max_req_per_sec: float = 3.0,
        page_size: int = 1000,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.api_key = api_key
        self.chain_id = chain_id
        self.page_size = page_size
        self._limiter = RateLimiter(max_req_per_sec)
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, params: dict) -> Any:
        params = {"chainid": self.chain_id, "apikey": self.api_key, **params}
        await self._limiter.wait()
        for attempt in range(3):
            resp = await self._client.get(BASE_URL, params=params)
            resp.raise_for_status()
            body = resp.json()
            # Etherscan returns 200 with an error string on rate-limit
            # hits rather than a non-200 status — and, verified against
            # the live API, the rate-limit text lands in `result`
            # ("Max calls per sec rate limit reached (3/sec)"), not
            # `message` (which is just the generic "NOTOK"). Checking
            # `message` alone never catches it, letting the placeholder
            # string flow downstream as if it were real tx data / a
            # real eth_call result.
            result = body.get("result")
            if isinstance(result, str) and "rate limit" in result.lower():
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            return body
        raise EtherscanError(f"rate limited after retries: {params.get('action')}")

    # ---- account module -------------------------------------------------

    async def _paginated_account_list(self, address: str, action: str) -> tuple[list[dict], bool]:
        """Shared pager for txlist / txlistinternal / tokentx. Returns
        (rows, truncated)."""
        rows: list[dict] = []
        page = 1
        truncated = False
        while True:
            body = await self._get({
                "module": "account",
                "action": action,
                "address": address,
                "startblock": 0,
                "endblock": 99999999,
                "page": page,
                "offset": self.page_size,
                "sort": "asc",
            })
            result = body.get("result")
            if not isinstance(result, list) or not result:
                break
            rows.extend(result)
            if len(result) < self.page_size:
                break
            page += 1
            if page > 50:  # hard stop — 50k rows is already an extreme address
                truncated = True
                break
        return rows, truncated

    async def get_normal_txs(self, address: str) -> tuple[list[dict], bool]:
        return await self._paginated_account_list(address, "txlist")

    async def get_internal_txs(self, address: str) -> tuple[list[dict], bool]:
        return await self._paginated_account_list(address, "txlistinternal")

    async def get_token_txs(self, address: str) -> tuple[list[dict], bool]:
        return await self._paginated_account_list(address, "tokentx")

    async def get_first_seen_timestamp(self, address: str) -> Optional[int]:
        """address_age_days building block (#1, and reused for #4/#8/#18
        applied to a counterparty instead of the subject wallet)."""
        body = await self._get({
            "module": "account",
            "action": "txlist",
            "address": address,
            "startblock": 0,
            "endblock": 99999999,
            "page": 1,
            "offset": 1,
            "sort": "asc",
        })
        result = body.get("result")
        if isinstance(result, list) and result:
            return int(result[0]["timeStamp"])
        return None

    # ---- contract module --------------------------------------------------

    async def get_source_code(self, address: str) -> Optional[dict]:
        """#11 is_verified, #13 implementation_verified, #15 ABI scan."""
        body = await self._get({
            "module": "contract",
            "action": "getsourcecode",
            "address": address,
        })
        result = body.get("result")
        if isinstance(result, list) and result:
            return result[0]
        return None

    async def get_contract_creation(self, address: str) -> Optional[dict]:
        """Deployer + creation tx hash. No timestamp in the response —
        chain a proxy eth_getTransactionByHash call for that (see
        get_contract_age_days below)."""
        body = await self._get({
            "module": "contract",
            "action": "getcontractcreation",
            "contractaddresses": address,
        })
        result = body.get("result")
        if isinstance(result, list) and result:
            return result[0]
        return None

    async def get_contract_age_days(self, address: str) -> Optional[float]:
        creation = await self.get_contract_creation(address)
        if not creation:
            return None
        tx_hash = creation.get("txHash")
        if not tx_hash:
            return None
        tx = await self.proxy_get_transaction_by_hash(tx_hash)
        if not tx:
            return None
        block_num = tx.get("blockNumber")
        if not block_num:
            return None
        block = await self.proxy_get_block_by_number(block_num)
        if not block or "timestamp" not in block:
            return None
        created_ts = int(block["timestamp"], 16)
        return (time.time() - created_ts) / 86400.0

    # ---- proxy module (JSON-RPC-shaped, GET-based) -------------------------
    # Used as the default RPC backend so the app runs on one Etherscan
    # key. RpcClient swaps to a dedicated provider transparently if
    # RPC_URL is set.

    async def proxy_eth_call(self, to: str, data: str) -> Optional[str]:
        body = await self._get({
            "module": "proxy", "action": "eth_call",
            "to": to, "data": data, "tag": "latest",
        })
        return body.get("result")

    async def proxy_get_storage_at(self, address: str, position: str) -> Optional[str]:
        body = await self._get({
            "module": "proxy", "action": "eth_getStorageAt",
            "address": address, "position": position, "tag": "latest",
        })
        return body.get("result")

    async def proxy_get_code(self, address: str) -> Optional[str]:
        body = await self._get({
            "module": "proxy", "action": "eth_getCode",
            "address": address, "tag": "latest",
        })
        return body.get("result")

    async def proxy_get_transaction_by_hash(self, tx_hash: str) -> Optional[dict]:
        body = await self._get({
            "module": "proxy", "action": "eth_getTransactionByHash", "txhash": tx_hash,
        })
        return body.get("result")

    async def proxy_get_block_by_number(self, block_number_hex: str) -> Optional[dict]:
        body = await self._get({
            "module": "proxy", "action": "eth_getBlockByNumber",
            "tag": block_number_hex, "boolean": "false",
        })
        return body.get("result")
