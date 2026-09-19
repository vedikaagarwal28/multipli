"""The last hop: the page's own render code over a real API response.

Adding `verdict` nested a new object inside the payload the page renders, and
renderParameterTable recurses into nested objects — this is where that would
break, or silently drop the score.
"""
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import CONTRACT, ESTABLISHED, UNKNOWN, SUSPICIOUS

HARNESS = Path(__file__).parent / "frontend_harness.mjs"
PAGE = Path(__file__).resolve().parents[2] / "frontend" / "base.html"


def render(node, payload):
    done = subprocess.run(
        [node, str(HARNESS), str(PAGE)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=180,
    )
    assert done.returncode == 0, f"frontend harness failed:\n{done.stderr}"
    return done.stdout


@pytest.fixture(scope="module")
def payload(api_url):
    r = httpx.get(f"{api_url}/analyze/wallet/{ESTABLISHED}", timeout=60)
    r.raise_for_status()
    return r.json()


def test_page_renders_a_table_without_throwing(node, payload):
    assert "<table" in render(node, payload)


def test_etherscan_features_still_render(node, payload):
    table = render(node, payload)
    assert "features.address_age_days" in table
    assert "features.tx_count_30d" in table


def test_verdict_reaches_the_table(node, payload):
    """The nested verdict object must be flattened into rows, not stringified
    into one unreadable cell or dropped."""
    table = render(node, payload)
    assert "verdict.risk_score" in table
    assert "verdict.band" in table
    assert str(payload["verdict"]["risk_score"]) in table
    assert payload["verdict"]["band"] in table


def test_model_inputs_render_for_debugging(node, payload):
    table = render(node, payload)
    assert "verdict.inputs.address_age_days" in table
    assert "verdict.inputs.recent_activity_burst" in table


def test_a_risky_verdict_renders_its_band(node, api_url):
    r = httpx.get(f"{api_url}/analyze/wallet/{SUSPICIOUS}", timeout=60)
    r.raise_for_status()
    body = r.json()
    assert body["verdict"]["band"] in render(node, body)


def route(node, api_url, address):
    """The page's own analyzeAddress() against the live API -> the tab it picked
    and the endpoints it called on the way."""
    done = subprocess.run(
        [node, str(HARNESS), str(PAGE), api_url, address],
        capture_output=True, text=True, timeout=180,
    )
    assert done.returncode == 0, f"frontend harness failed:\n{done.stderr}"
    return json.loads(done.stdout)


def test_page_routes_an_eoa_to_the_wallet_tab(node, api_url):
    """ESTABLISHED reports EIP-7702 delegation code, so /contract has to 400
    before the page falls through — the single input can't ask the user."""
    routed = route(node, api_url, ESTABLISHED)
    assert routed["tab"] == "wallet"
    assert routed["calls"] == [
        {"endpoint": "contract", "status": 400},
        {"endpoint": "wallet", "status": 200},
    ]


def test_page_stops_at_the_contract_endpoint_for_a_contract(node, api_url):
    routed = route(node, api_url, CONTRACT)
    assert routed["tab"] == "contract"
    assert routed["calls"] == [{"endpoint": "contract", "status": 200}]


def test_page_lands_on_the_wallet_tab_when_neither_endpoint_answers(node, api_url):
    routed = route(node, api_url, UNKNOWN)
    assert routed["tab"] == "wallet"
    assert [c["status"] for c in routed["calls"]] == [400, 404]
