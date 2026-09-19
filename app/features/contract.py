"""
Parameters #11-15 (contract-only), plus #1/#2 reused from the wallet
module since age and recent activity apply to contracts too. This is
the phase-2 addon per the build plan — wired up now so it's a config
flip away, not a rewrite, once wallet analysis is solid.

upgrades_30d and ownership_transfers_30d are deliberately NOT here —
cut from the parameter list because the only free path is scanning
OwnershipTransferred/Upgraded event logs over a 30-day window, and
free-tier eth_getLogs is capped far too tightly (10-block range on
Alchemy's free tier for Ethereum) for that to be workable without
adding BigQuery + a billing-enabled GCP account. Revisit if/when
that trade-off is worth it.
"""
from typing import Optional

from app.clients.etherscan import EtherscanClient
from app.clients.fourbyte import FourByteClient
from app.clients.rpc import RpcClient
from app.features.bytecode import classify_privileged_from_names, extract_push4_selectors
from app.models import ContractFeatures

# EIP-1967 standard storage slots — bytes32(uint256(keccak256(<slot name>)) - 1)
IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
ADMIN_SLOT = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"

# Well-known, effectively-standardized selectors (OpenZeppelin Ownable /
# TransparentUpgradeableProxy). Everything else (e.g. "blacklist") is
# resolved by name via 4byte instead of guessing a selector — see
# bytecode.py's docstring for why.
OWNER_SELECTOR = "0x8da5cb5b"   # owner()
ADMIN_SELECTOR = "0xf851a440"   # admin()

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _word_to_address(hex_word: Optional[str]) -> Optional[str]:
    if not hex_word:
        return None
    data = hex_word[2:] if hex_word.startswith("0x") else hex_word
    if len(data) < 40:
        return None
    addr = "0x" + data[-40:]
    return addr if addr.lower() != ZERO_ADDRESS else None


async def resolve_proxy(rpc: RpcClient, address: str) -> tuple[bool, Optional[str], Optional[str]]:
    """-> (is_proxy, implementation_address, owner_address)"""
    impl_word = await rpc.eth_get_storage_at(address, IMPLEMENTATION_SLOT)
    implementation = _word_to_address(impl_word)
    is_proxy = implementation is not None

    admin_word = await rpc.eth_get_storage_at(address, ADMIN_SLOT)
    admin_from_slot = _word_to_address(admin_word)

    owner_result = await rpc.eth_call(address, OWNER_SELECTOR)
    owner = _word_to_address(owner_result) or admin_from_slot

    if owner is None:
        admin_result = await rpc.eth_call(address, ADMIN_SELECTOR)
        owner = _word_to_address(admin_result)

    return is_proxy, implementation, owner


async def owner_is_eoa(rpc: RpcClient, owner_address: Optional[str]) -> Optional[bool]:
    if not owner_address:
        return None
    code = await rpc.eth_get_code(owner_address)
    if code is None:
        return None
    return code in ("0x", "0x0", "")


async def privileged_function_count(
    etherscan: EtherscanClient,
    fourbyte: FourByteClient,
    address: str,
    source_code: Optional[dict],
) -> tuple[int, list[str]]:
    names: list[str] = []

    is_verified = bool(source_code and source_code.get("SourceCode"))
    if is_verified:
        abi_raw = source_code.get("ABI", "")
        import json
        try:
            abi = json.loads(abi_raw)
            names = [item.get("name", "") for item in abi if item.get("type") == "function"]
        except (json.JSONDecodeError, TypeError):
            names = []
    else:
        code = await etherscan.proxy_get_code(address)
        if code:
            selectors = extract_push4_selectors(code)
            for sel in selectors:
                resolved = await fourbyte.lookup(sel)
                if resolved:
                    # text_signature looks like "mint(address,uint256)" — keep the name part
                    names.append(resolved.split("(")[0])

    privileged = classify_privileged_from_names(names)
    return len(privileged), privileged


async def compute_contract_features(
    address: str,
    etherscan: EtherscanClient,
    rpc: RpcClient,
    fourbyte: FourByteClient,
) -> ContractFeatures:
    source_code = await etherscan.get_source_code(address)
    is_verified = bool(source_code and source_code.get("SourceCode"))

    is_proxy, implementation, owner = await resolve_proxy(rpc, address)

    implementation_verified = None
    if implementation:
        impl_source = await etherscan.get_source_code(implementation)
        implementation_verified = bool(impl_source and impl_source.get("SourceCode"))

    is_eoa_owner = await owner_is_eoa(rpc, owner)

    priv_count, priv_names = await privileged_function_count(etherscan, fourbyte, address, source_code)

    age_days = await etherscan.get_contract_age_days(address)
    normal_txs, _ = await etherscan.get_normal_txs(address)
    import time as _time
    thirty_days_ago = _time.time() - 30 * 86400
    tx_count_30d = sum(1 for r in normal_txs if int(r.get("timeStamp", 0)) >= thirty_days_ago)

    return ContractFeatures(
        address=address.lower(),
        address_age_days=age_days,
        tx_count_30d=tx_count_30d,
        is_verified=is_verified,
        is_proxy=is_proxy,
        implementation_address=implementation,
        implementation_verified=implementation_verified,
        owner_address=owner,
        owner_is_eoa=is_eoa_owner,
        privileged_function_count=priv_count,
        privileged_functions=priv_names,
    )
