"""A real uvicorn server with Etherscan faked out, so the snap's own fetch code
and the frontend's own render code can run against the actual API over HTTP.

Everything below Etherscan is real: the feature layer, app/risk.py's mapping and
the LightGBM model all run for each request. Only the network calls are stubbed,
because a test that depends on a live address's on-chain history stops being a
test the moment someone sends that address a transaction.

Each scenario uses its own address, which also keeps the API's TTL cache from
carrying one test's result into another.
"""
import shutil
import socket
import threading
import time

import pytest
import uvicorn

DAY = 86400
NOW = int(time.time())

ESTABLISHED = "0x" + "1" * 40   # years of history, holds funds -> low risk
SUSPICIOUS = "0x" + "2" * 40    # hours old, forwards everything -> high risk
CONTRACT = "0x" + "3" * 40      # has bytecode -> /analyze/contract answers
UNKNOWN = "0x" + "4" * 40       # no history at all -> 404 from both endpoints

OLD_PEER = "0x" + "a" * 40
NEW_PEER = "0x" + "b" * 40

FIRST_SEEN = {OLD_PEER: NOW - 900 * DAY, NEW_PEER: NOW - 2 * DAY}


def _row(tag, ts, frm, to, value_wei):
    return {
        "hash": f"0x{tag}", "blockNumber": "1", "timeStamp": str(int(ts)),
        "from": frm, "to": to, "value": str(value_wei),
        "isError": "0", "input": "0x", "methodId": "", "functionName": "",
    }


def _established_history():
    """Regular two-way traffic with a long-standing peer, funds held for days."""
    rows = [_row("seed", NOW - 800 * DAY, OLD_PEER, ESTABLISHED, 5 * 10**18)]
    for i in range(12):
        rows.append(_row(f"in{i}", NOW - (60 - i * 4) * DAY, OLD_PEER, ESTABLISHED, 10**18))
        rows.append(_row(f"out{i}", NOW - (58 - i * 4) * DAY, ESTABLISHED, OLD_PEER, 10**17))
    return rows


def _suspicious_history():
    """Born 12h ago, every inflow forwarded within minutes to a brand-new peer."""
    rows = [_row("seed", NOW - int(0.5 * DAY), NEW_PEER, SUSPICIOUS, 3 * 10**18)]
    for i in range(6):
        inbound = NOW - int(0.4 * DAY) + i * 600
        rows.append(_row(f"in{i}", inbound, NEW_PEER, SUSPICIOUS, 10**18))
        rows.append(_row(f"out{i}", inbound + 120, SUSPICIOUS, NEW_PEER, 10**18))
    return rows


HISTORY = {ESTABLISHED: _established_history(), SUSPICIOUS: _suspicious_history()}


class FakeEtherscan:
    """Only the methods the wallet path and the EOA/contract split actually call."""

    async def get_normal_txs(self, address):
        return HISTORY.get(address.lower(), []), False

    async def get_internal_txs(self, address):
        return [], False

    async def get_token_txs(self, address):
        return [], False

    async def get_first_seen_timestamp(self, address):
        return FIRST_SEEN.get(address.lower())

    async def proxy_get_code(self, address):
        return "0x60806040" if address.lower() == CONTRACT else "0x"

    async def get_source_code(self, address):
        # verified with an ABI, so the privileged-function scan reads names from the
        # ABI and never reaches the 4byte lookup
        return {
            "SourceCode": "contract Token {}",
            "ABI": '[{"type": "function", "name": "mint"},'
                   ' {"type": "function", "name": "transfer"}]',
        }

    async def get_contract_age_days(self, address):
        return 365.0

    async def aclose(self):
        pass


class FakeRpc:
    """Answers "not a proxy, no owner" — enough for the contract endpoint to
    return without touching the network."""

    async def eth_get_storage_at(self, address, slot):
        return None

    async def eth_call(self, to, data):
        return None

    async def eth_get_code(self, address):
        return "0x"

    async def aclose(self):
        pass


@pytest.fixture(scope="session")
def api_url():
    from app.main import app, state

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 60
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("API server did not start")
        time.sleep(0.05)

    # swapped in after the lifespan built the real ones, so the model still loads for real
    state["etherscan"] = FakeEtherscan()
    state["rpc"] = FakeRpc()
    port = server.servers[0].sockets[0].getsockname()[1]

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="session")
def node():
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not on PATH; the snap and frontend layers need it")
    return exe


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
