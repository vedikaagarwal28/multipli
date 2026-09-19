"""
#15 privileged_function_count.

When a contract is verified, its ABI already lists every function —
no need for any of this, just scan the ABI's function names directly.

When it isn't, we fall back to a heuristic: Solidity's compiled
function dispatcher pushes each 4-byte selector onto the stack
immediately before comparing it (PUSH4 <selector> ... EQ), so scanning
runtime bytecode for `PUSH4` (opcode 0x63) followed within a few bytes
by `EQ` (opcode 0x14) recovers the selector table without a full EVM
disassembler. This is a heuristic, not a guarantee — obfuscated or
non-Solidity bytecode can evade it — which is exactly why the verified-
ABI path is preferred whenever it's available.
"""
import re
from typing import Iterable

PUSH4 = 0x63
EQ = 0x14

# EIP-7702: a delegated EOA's code is `0xef0100 || <20-byte address>`.
# It is still an EOA — no creation tx, no source, no owner — so routing
# one to the contract path returns every feature as None and no verdict.
# EIP-3541 bans deploying any code beginning with 0xEF, so this prefix
# can only ever be a delegation indicator, never real contract code.
DELEGATION_PREFIX = "0xef0100"


def is_eoa_code(code: str) -> bool:
    """An eth_getCode result -> is this an externally-owned account?"""
    code = code.strip().lower()
    return code in ("", "0x", "0x0") or code.startswith(DELEGATION_PREFIX)

# Matched against 4byte.directory's resolved text signature, or
# directly against a verified ABI's function name. Deliberately
# name-based rather than a hardcoded selector table — there's no
# single standard selector for e.g. "blacklist", every project spells
# it differently (addBlackList, blacklistAddress, ...), so pattern
# matching on the resolved name generalizes better than guessing
# selectors ourselves.
PRIVILEGED_KEYWORDS = re.compile(
    r"mint|burn|pause|unpause|blacklist|blocklist|freeze|unfreeze|"
    r"seize|confiscate|setfee|settax|rescue|sweep|withdraw|drain|"
    r"kill|selfdestruct|upgrade|forcetransfer|setowner|renounceownership|"
    r"transferownership",
    re.IGNORECASE,
)


def extract_push4_selectors(bytecode_hex: str) -> set[str]:
    """bytecode_hex like '0x6080604...' -> {'0xa9059cbb', ...}"""
    data = bytecode_hex[2:] if bytecode_hex.startswith("0x") else bytecode_hex
    try:
        raw = bytes.fromhex(data)
    except ValueError:
        return set()

    selectors: set[str] = set()
    i = 0
    n = len(raw)
    while i < n:
        op = raw[i]
        if op == PUSH4 and i + 5 <= n:
            selector_bytes = raw[i + 1 : i + 5]
            # Look for EQ within the next few bytes — the standard
            # `PUSH4 <sel> EQ PUSH2 <dest> JUMPI` dispatcher pattern.
            lookahead = raw[i + 5 : i + 8]
            if EQ in lookahead:
                selectors.add("0x" + selector_bytes.hex())
            i += 5
        elif 0x60 <= op <= 0x7F:
            # Any other PUSH1..PUSH32 — skip its immediate bytes so we
            # don't misread pushed data as opcodes.
            push_len = op - 0x60 + 1
            i += 1 + push_len
        else:
            i += 1
    return selectors


def is_privileged_name(name: str) -> bool:
    return bool(PRIVILEGED_KEYWORDS.search(name))


def classify_privileged_from_names(names: Iterable[str]) -> list[str]:
    return sorted({n for n in names if is_privileged_name(n)})
