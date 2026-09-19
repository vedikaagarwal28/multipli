# Blockchain Risk Parameters Reference Sheet (v2 — free-tier verified)

Cut from 20 to 18 parameters. `upgrades_30d` and `ownership_transfers_30d` removed — see note at bottom.
Sources below are verified against each service's current free-tier docs, not assumed.

| # | Parameter | Applies to | Why it matters | How to extract it | Main data source |
|---|---|---|---|---|---|
| **1** | `address_age_days` | Wallet / Contract | New addresses and newly deployed contracts have less reputation history. | Wallet: earliest `txlist` row, sort ascending, take `timeStamp`. Contract: `getcontractcreation` gives the creation tx hash; look that tx up for its `timeStamp` (one extra hop — the endpoint doesn't return a timestamp directly). | Etherscan `txlist` / `getcontractcreation` |
| **2** | `tx_count_30d` | Wallet / Contract | Detects recent activity bursts and newly active addresses. | Filter `txlist` results to the last 30 days client-side. | Etherscan `txlist` |
| **3** | `unique_counterparties_30d` | Wallet | Helps distinguish normal use from interaction with many unknown addresses. | Distinct `from`/`to` across `txlist` + `tokentx` in the window. | Etherscan `txlist`, `tokentx` |
| **4** | `new_counterparty_ratio` | Wallet | A high proportion of first-time recipients or senders can indicate phishing, sweeping, or laundering behavior. | For each counterparty in the window, pull its own `txlist` to get its first-seen date; ratio of those newer than the window start. | Etherscan `txlist` (one extra call per unique counterparty — cap to the top-K by value if a wallet has an unusually large counterparty set, to stay inside the free rate limit) |
| **5** | `pass_through_ratio` | Wallet | Measures whether funds are rapidly received and forwarded. | Match inbound and outbound value across native, internal, and token transfers within a window (e.g. 60 minutes). | Etherscan `txlist` + `txlistinternal` + `tokentx` (covers native and internal ETH movement and token transfers — not full multi-hop call traces, which need a paid archive/trace node; this is a reasonable proxy for the common case) |
| **6** | `median_hold_minutes` | Wallet | Very short holding periods are common in forwarding and sweep patterns. | Match inbound funds to the next outbound funds, compute median gap. | Etherscan `txlist` + `txlistinternal` + `tokentx` |
| **7** | `flagged_counterparty_share` | Wallet | Direct interaction with known malicious addresses is a strong risk signal. | `transfers involving labelled addresses / total observed transfers`, checked against your own labels table. | **Not Etherscan** — its tag/label export is an Enterprise-only endpoint. Build `labels.csv` from the free OFAC SDN list (direct download, no key) plus public scam-address lists; optionally cross-check against Webacy's free tier |
| **8** | `first_funder_flagged` | Wallet | The original funder may connect a seemingly new wallet to a known scam cluster. | Identify the earliest meaningful inbound transfer's sender, look it up in your labels table. | Etherscan `txlist` for the funder lookup + the same `labels.csv` as #7 |
| **9** | `recent_activity_burst` | Wallet | Sudden high-frequency activity can indicate a compromised or newly activated wallet. | Compare tx count in the last 1h/24h against the wallet's own historical daily average (z-score), not a global threshold. | Etherscan `txlist` timestamps — purely derived, no external dependency |
| **10** | `address_label_status` | Wallet / Contract | Known scam, phishing, exploit, OFAC, or trusted-protocol labels should be directly visible. | Normalize address, query `(chain_id, address)` against your labels table. | Same stack as #7: OFAC + public lists + Webacy free tier |
| **11** | `is_verified` | Contract | Source verification enables better inspection and reduces uncertainty, but is not proof of safety. | Query source-code verification status. | Etherscan `getsourcecode` |
| **12** | `is_proxy` | Contract | Proxy contracts can replace their implementation after users interact with them. | Read the EIP-1967 implementation storage slot. | `eth_getStorageAt` via any free RPC (Ankr, Alchemy) — a single-slot read, not a log scan, so it isn't affected by the range caps below |
| **13** | `implementation_verified` | Contract | A verified proxy is not sufficient if its current implementation is unverified. | Resolve implementation address from #12, then check its own verification status. | Etherscan `getsourcecode` on the resolved address |
| **14** | `owner_is_eoa` | Contract | An EOA-controlled owner or admin may represent a single-key control risk. | Call `owner()`/`admin()`, then check whether the returned address has code. | `eth_call` + `eth_getCode` via free RPC |
| **15** | `privileged_function_count` | Contract | Functions for minting, pausing, blacklisting, changing fees, or rescuing funds create central control risk. | Scan the verified ABI for known privileged selectors; when unverified, match raw bytecode selectors against a public signature database. | Etherscan ABI when verified; **4byte.directory** (free, public) when not |
| **16** | `transaction_type` | Transaction | Risk differs substantially between a native transfer, approval, permit, swap, and arbitrary contract call. | Read the decoded `methodId`/`functionName` already returned for verified contracts; fall back to 4byte.directory otherwise. | Etherscan `txlist`/`tokentx` |
| **17** | `approval_is_unlimited` | Transaction | Unlimited approvals can expose the user's full token balance. | Decode the `approve`/`permit` amount from the historical tx's `input` field, compare to max `uint256`. | Etherscan `txlist`/`tokentx` `input` field |
| **18** | `spender_or_recipient_is_new` | Transaction | A young spender/recipient wallet deserves more scrutiny than a long-established one — this measures the *counterparty's* own account age, not whether you've seen them before. | Look up the spender/recipient's own earliest transaction, same method as #1, evaluated as of the historical tx's timestamp. | Etherscan `txlist` on the counterparty address |

## Removed for now

| Parameter | Why it's cut | When to revisit |
|---|---|---|
| `upgrades_30d` | Needs event-log scanning over a 30-day window (~216,000 blocks). Free-tier `eth_getLogs` on Alchemy is capped at a 10-block range per call on Ethereum — not viable without paying or adding BigQuery's public dataset, which requires a billing-enabled GCP account. | Contract phase — pull it back in once you're willing to add BigQuery as a fourth data source, or pay for a higher RPC tier. |
| `ownership_transfers_30d` | Same constraint — filtered by the `OwnershipTransferred` event topic instead. | Same as above. |

## Labels stack, spelled out once

Parameters 7, 8, and 10 all lean on the same three-source labels table, since Etherscan's own tag export sits behind its Enterprise tier:
1. **OFAC SDN list** — free, direct download, no API key.
2. **Public scam-address lists** — GitHub-hosted phishing/scam address dumps.
3. **Webacy** (`GET /addresses/{address}`, free "Starter" tier) — as a live cross-check for anything the static lists miss.

Merge these into one local `labels.csv` keyed on `(chain_id, address)` rather than treating any single one as authoritative.
