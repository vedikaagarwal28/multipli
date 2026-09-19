"""Etherscan rows -> feature layer -> app/risk.py -> LightGBM -> HTTP response.

Everything here goes over real HTTP against a real server running the real model;
only the Etherscan rows are fixed.
"""
import httpx
import pytest

from risk_model import FEATS
from tests.integration.conftest import CONTRACT, ESTABLISHED, SUSPICIOUS, UNKNOWN

BANDS = {"ALLOW", "REVIEW", "BLOCK"}


@pytest.fixture(scope="module")
def established(api_url):
    r = httpx.get(f"{api_url}/analyze/wallet/{ESTABLISHED}", timeout=60)
    r.raise_for_status()
    return r.json()


@pytest.fixture(scope="module")
def suspicious(api_url):
    r = httpx.get(f"{api_url}/analyze/wallet/{SUSPICIOUS}", timeout=60)
    r.raise_for_status()
    return r.json()


def test_response_carries_features_and_verdict(established):
    assert set(established) == {"features", "recent_transactions", "verdict"}
    assert established["features"]["address"] == ESTABLISHED
    assert established["features"]["total_tx_count"] > 0


def test_verdict_is_well_formed(established):
    verdict = established["verdict"]
    assert 0 <= verdict["risk_score"] <= 100
    assert verdict["band"] in BANDS
    assert 0.0 <= verdict["probability"] <= 1.0
    assert {f["parameter"] for f in verdict["factors"]} == set(FEATS)


def test_model_reads_the_features_the_etherscan_layer_computed(established):
    """The mapping in app/risk.py is the easiest thing in this chain to break
    silently, so pin it against the features from the same response."""
    features, inputs = established["features"], established["verdict"]["inputs"]

    assert inputs["address_age_days"] == features["address_age_days"]
    assert inputs["tx_count_30d"] == features["tx_count_30d"]
    assert inputs["pass_through_ratio"] == features["pass_through_ratio"]
    # the two renamed ones
    assert inputs["recent_activity_burst"] == features["recent_activity_burst_zscore"]
    assert set(inputs) == set(FEATS)


def test_label_parameters_are_unknown_without_a_flagged_list(established):
    assert established["verdict"]["inputs"]["flagged_counterparty_share"] is None
    assert established["verdict"]["inputs"]["first_funder_flagged"] is None


def test_model_separates_the_two_wallets(established, suspicious):
    """A hours-old wallet that forwards every inflow to brand-new peers must not
    score below a years-old one that holds funds — if it does, the wrong values
    are reaching the model even though every field is present."""
    assert suspicious["verdict"]["risk_score"] > established["verdict"]["risk_score"]
    assert suspicious["features"]["address_age_days"] < 1
    assert suspicious["verdict"]["factors"][0]["text"]


def test_factors_are_ordered_by_influence(suspicious):
    impacts = [abs(f["impact"]) for f in suspicious["verdict"]["factors"]]
    assert impacts == sorted(impacts, reverse=True)


def test_contract_endpoint_rejects_an_eoa(api_url):
    """The snap depends on this 400 to know it should fall through to /analyze/wallet."""
    r = httpx.get(f"{api_url}/analyze/contract/{ESTABLISHED}", timeout=60)
    assert r.status_code == 400
    assert "EOA" in r.json()["detail"]


def test_contract_endpoint_answers_for_a_contract(api_url):
    r = httpx.get(f"{api_url}/analyze/contract/{CONTRACT}", timeout=60)
    assert r.status_code == 200
    body = r.json()
    assert body["features"]["is_verified"] is True
    assert "verdict" not in body          # contracts have no model yet


def test_unknown_address_is_a_404_on_both_endpoints(api_url):
    assert httpx.get(f"{api_url}/analyze/wallet/{UNKNOWN}", timeout=60).status_code == 404
    assert httpx.get(f"{api_url}/analyze/contract/{UNKNOWN}", timeout=60).status_code == 400


def test_malformed_address_is_rejected(api_url):
    assert httpx.get(f"{api_url}/analyze/wallet/0xnope", timeout=60).status_code == 400
