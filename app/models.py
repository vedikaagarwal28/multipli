"""
Shared data shapes. TxRecord is the canonical normalized transaction
used by every wallet feature — txlist, txlistinternal and tokentx all
get flattened into this one shape so the feature functions never care
which Etherscan endpoint a row came from.
"""
from enum import Enum
from typing import Optional
from pydantic import BaseModel


class TxKind(str, Enum):
    EXTERNAL = "external"      # normal tx, from txlist
    INTERNAL = "internal"      # value moved by a contract, from txlistinternal
    TOKEN = "token"            # ERC-20 transfer, from tokentx


class TxRecord(BaseModel):
    hash: str
    block_number: int
    timestamp: int             # unix seconds
    from_address: str
    to_address: str
    value_wei: int
    kind: TxKind
    is_error: bool = False
    method_id: Optional[str] = None
    function_name: Optional[str] = None
    input_data: str = "0x"
    token_symbol: Optional[str] = None
    token_decimals: Optional[int] = None


class TransactionFlags(BaseModel):
    """Params #16-18, computed per historical transaction rather than
    live at signing time, since interception is out of scope."""
    hash: str
    timestamp: int
    transaction_type: str                      # #16
    approval_is_unlimited: Optional[bool] = None  # #17, only set for approve/permit
    counterparty: Optional[str] = None
    counterparty_age_days: Optional[float] = None  # #18
    counterparty_is_new: Optional[bool] = None      # #18


class WalletFeatures(BaseModel):
    address: str
    address_age_days: Optional[float] = None            # 1
    tx_count_30d: int = 0                                 # 2
    unique_counterparties_30d: int = 0                    # 3
    new_counterparty_ratio: Optional[float] = None        # 4
    pass_through_ratio: Optional[float] = None            # 5
    median_hold_minutes: Optional[float] = None           # 6
    first_funder_address: Optional[str] = None
    recent_activity_burst_zscore: Optional[float] = None  # 9
    total_tx_count: int = 0
    truncated: bool = False   # hit Etherscan's 1000-row page cap


class ContractFeatures(BaseModel):
    address: str
    address_age_days: Optional[float] = None       # 1
    tx_count_30d: int = 0                            # 2
    is_verified: bool = False                        # 11
    is_proxy: bool = False                           # 12
    implementation_address: Optional[str] = None
    implementation_verified: Optional[bool] = None   # 13
    owner_address: Optional[str] = None
    owner_is_eoa: Optional[bool] = None               # 14
    privileged_function_count: int = 0                # 15
    privileged_functions: list[str] = []


class WalletAnalysisResponse(BaseModel):
    features: WalletFeatures
    recent_transactions: list[TransactionFlags]
    model_config = {"protected_namespaces": ()}


class ContractAnalysisResponse(BaseModel):
    features: ContractFeatures
