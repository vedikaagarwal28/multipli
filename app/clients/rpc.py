"""
RPC calls needed for contract features #12 (is_proxy) and #14
(owner_is_eoa): eth_getStorageAt, eth_call, eth_getCode. All three are
single-point state reads, not log scans, so they aren't affected by
the eth_getLogs block-range caps that killed upgrades_30d /
ownership_transfers_30d.

Two backends, same interface:
- RPC_URL set  -> standard JSON-RPC POST to that endpoint (Ankr,
  Alchemy, any provider).
- RPC_URL unset -> Etherscan's own proxy module, via the already-
  rate-limited EtherscanClient. This is the default so the app runs
  end to end on a single Etherscan key.
"""
from typing import Optional
import httpx

from app.clients.etherscan import EtherscanClient


class RpcClient:
    def __init__(self, etherscan: EtherscanClient, rpc_url: str = ""):
        self._etherscan = etherscan
        self._rpc_url = rpc_url.strip()
        self._http = httpx.AsyncClient(timeout=15.0) if self._rpc_url else None

    async def aclose(self):
        if self._http:
            await self._http.aclose()

    async def _post_rpc(self, method: str, params: list) -> Optional[str]:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        resp = await self._http.post(self._rpc_url, json=payload)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            return None
        return body.get("result")

    async def eth_call(self, to: str, data: str) -> Optional[str]:
        if self._rpc_url:
            return await self._post_rpc("eth_call", [{"to": to, "data": data}, "latest"])
        return await self._etherscan.proxy_eth_call(to, data)

    async def eth_get_storage_at(self, address: str, position: str) -> Optional[str]:
        if self._rpc_url:
            return await self._post_rpc("eth_getStorageAt", [address, position, "latest"])
        return await self._etherscan.proxy_get_storage_at(address, position)

    async def eth_get_code(self, address: str) -> Optional[str]:
        if self._rpc_url:
            return await self._post_rpc("eth_getCode", [address, "latest"])
        return await self._etherscan.proxy_get_code(address)
