"""The snap's own fetch and routing code, run against the live API.

snap/src/api.mjs is imported by the harness rather than reimplemented, so a
change to the snap's routing (or to the API's status codes) fails here.
"""
import json
import subprocess
from pathlib import Path

import pytest

from tests.integration.conftest import CONTRACT, ESTABLISHED, SUSPICIOUS, UNKNOWN

HARNESS = Path(__file__).parent / "snap_harness.mjs"

# the fields snap/src/index.jsx reads out of a verdict when it renders
RENDERED_VERDICT_FIELDS = {"band", "risk_score", "factors"}
RENDERED_FACTOR_FIELDS = {"direction", "text"}


def run_snap(node, api_url, address):
    done = subprocess.run(
        [node, str(HARNESS), api_url, address],
        capture_output=True, text=True, timeout=180,
    )
    assert done.returncode == 0, f"snap harness failed:\n{done.stderr}"
    return json.loads(done.stdout)


@pytest.fixture(scope="module")
def wallet_result(node, api_url):
    return run_snap(node, api_url, ESTABLISHED)


def test_snap_falls_through_to_the_wallet_endpoint_for_an_eoa(wallet_result):
    """It asks /analyze/contract first; only the 400 sends it to /analyze/wallet."""
    assert wallet_result["kind"] == "wallet"
    assert wallet_result["features"]["address"] == ESTABLISHED


def test_snap_receives_a_verdict_it_can_render(wallet_result):
    verdict = wallet_result["verdict"]
    assert RENDERED_VERDICT_FIELDS <= set(verdict)
    assert verdict["band"] in {"ALLOW", "REVIEW", "BLOCK"}

    for factor in verdict["factors"]:
        assert RENDERED_FACTOR_FIELDS <= set(factor)
    # index.jsx filters on this exact string to pick the risk drivers it lists
    assert {f["direction"] for f in verdict["factors"]} <= {
        "raises risk", "lowers risk", "neutral"
    }


def test_snap_would_show_the_warning_banner_for_a_risky_wallet(node, api_url):
    """index.jsx sets SeverityLevel.Critical on any band other than ALLOW."""
    result = run_snap(node, api_url, SUSPICIOUS)
    assert result["verdict"]["band"] != "ALLOW"
    assert any(f["direction"] == "raises risk" for f in result["verdict"]["factors"])


def test_snap_stops_at_the_contract_endpoint_for_a_contract(node, api_url):
    result = run_snap(node, api_url, CONTRACT)
    assert result["kind"] == "contract"
    assert result["features"]["is_verified"] is True
    assert "verdict" not in result


def test_snap_surfaces_the_backend_message_when_nothing_is_found(node, api_url):
    result = run_snap(node, api_url, UNKNOWN)
    assert "features" not in result
    assert "no on-chain history" in result["detail"]
