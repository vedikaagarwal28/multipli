"""
Entry point. Two endpoints:

  GET /analyze/wallet/{address}    -> params #1-10, #16-18
  GET /analyze/contract/{address}  -> params #1, #2, #10-15 (phase 2)

Run with:  uvicorn app.main:app --reload
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.cache import TTLCache
from app.clients.etherscan import EtherscanClient
from app.clients.fourbyte import FourByteClient
from app.clients.rpc import RpcClient
from app.config import settings
from app.features import contract as contract_features
from app.features import wallet as wallet_features
from app.features.normalize import build_tx_frame
from app.models import ContractAnalysisResponse, WalletAnalysisResponse

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["etherscan"] = EtherscanClient(
        api_key=settings.etherscan_api_key,
        chain_id=settings.chain_id,
        max_req_per_sec=settings.etherscan_max_req_per_sec,
        page_size=settings.etherscan_page_size,
    )
    state["rpc"] = RpcClient(state["etherscan"], rpc_url=settings.rpc_url)
    state["fourbyte"] = FourByteClient()
    state["cache"] = TTLCache(ttl_seconds=settings.cache_ttl_seconds)

    yield

    await state["etherscan"].aclose()
    await state["rpc"].aclose()
    await state["fourbyte"].aclose()


app = FastAPI(title="Wallet/Contract Risk Feature API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:5500",
        "http://localhost:8000",
        # Snaps run in a sandboxed iframe, so their fetch carries Origin: null.
        "null",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

def _validate_address(address: str) -> str:
    address = address.strip().lower()
    if not address.startswith("0x") or len(address) != 42:
        raise HTTPException(400, detail="not a valid Ethereum address")
    return address


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "rpc_backend": "dedicated" if settings.rpc_url else "etherscan_proxy",
    }


@app.get("/analyze/wallet/{address}", response_model=WalletAnalysisResponse)
async def analyze_wallet(address: str):
    address = _validate_address(address)
    cache: TTLCache = state["cache"]

    cached = cache.get(f"wallet:{address}")
    if cached is not None:
        return cached

    etherscan: EtherscanClient = state["etherscan"]
    normal_rows, t1 = await etherscan.get_normal_txs(address)
    internal_rows, t2 = await etherscan.get_internal_txs(address)
    token_rows, t3 = await etherscan.get_token_txs(address)

    if not normal_rows and not internal_rows and not token_rows:
        raise HTTPException(404, detail="no on-chain history found for this address")

    records = build_tx_frame(normal_rows, internal_rows, token_rows)

    features = await wallet_features.compute_wallet_features(
        address=address,
        records=records,
        etherscan=etherscan,
        truncated=(t1 or t2 or t3),
        new_counterparty_sample_size=settings.new_counterparty_sample_size,
    )
    flags = await wallet_features.build_transaction_flags(records, address, etherscan)

    response = WalletAnalysisResponse(features=features, recent_transactions=flags)
    cache.set(f"wallet:{address}", response)
    return response


@app.get("/analyze/contract/{address}", response_model=ContractAnalysisResponse)
async def analyze_contract(address: str):
    """Phase 2 per the build plan. Implemented now so switching scope
    later is a routing decision, not a rewrite."""
    address = _validate_address(address)
    cache: TTLCache = state["cache"]

    cached = cache.get(f"contract:{address}")
    if cached is not None:
        return cached

    code = await state["etherscan"].proxy_get_code(address)
    if code in (None, "0x", "0x0"):
        raise HTTPException(400, detail="address has no code — this is an EOA, use /analyze/wallet instead")

    features = await contract_features.compute_contract_features(
        address=address,
        etherscan=state["etherscan"],
        rpc=state["rpc"],
        fourbyte=state["fourbyte"],
    )
    response = ContractAnalysisResponse(features=features)
    cache.set(f"contract:{address}", response)
    return response
