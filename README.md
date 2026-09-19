# Multipli

Multipli is a wallet-risk intelligence layer for crypto transactions. It helps users detect suspicious wallet behavior before they approve a transfer, so they can avoid sending funds to risky or scam-like addresses.

## The Problem

Crypto users often send funds to wallets that look normal at first glance, but may actually be:

- Scam wallets
- Laundering or mixing paths
- Phishing destinations
- Addresses with abnormal transaction behavior

Most users only see the destination address at the moment of signing, without any meaningful signal about whether that wallet is safe.

## What Multipli Does

Multipli analyzes the receiving wallet's on-chain history and calculates a risk score based on behavioral patterns.

It then gives the user a simple verdict:

```text
ALLOW
REVIEW
BLOCK
```

The goal is to identify suspicious activity before the transfer is finalized.

## Product Workflow

```text
User prepares a transaction
        |
        v
Wallet confirmation flow reads destination address
        |
        v
Backend fetches transaction history and wallet behavior data
        |
        v
Feature extraction
        |
        v
Risk model scores the wallet
        |
        v
Risk verdict + plain-language explanation
        |
        v
User chooses Allow, Review, or Block
```

## Architecture

Multipli is structured around three main layers:

### 1. API Layer

A FastAPI service responsible for:

- Wallet analysis
- Contract analysis
- Risk-scoring requests
- Returning verdicts and explanations

### 2. Feature Extraction Layer

This layer fetches and analyzes on-chain wallet activity to generate behavioral signals such as:

- Wallet age
- Recent transaction activity
- Counterparty diversity
- Share of new counterparties
- Activity spikes
- Pass-through or fund-forwarding behavior
- Interactions with known flagged addresses

### 3. Risk Model Layer

A LightGBM model combines the extracted signals and produces a wallet risk score from `0` to `100`.

The model output is then converted into a user-facing verdict and explanation.

## High-Level Flow

```text
MetaMask / Wallet Confirmation Screen
                |
                v
           Snap UI
                |
                v
          Backend API
                |
                v
Etherscan + Feature Extraction
                |
                v
          Risk Model
                |
                v
      Verdict + Explanation
```

## Main Features

### 1. Wallet Risk Scoring

Multipli evaluates wallets using behavioral signals including:

- Age of the wallet
- Number of recent transactions
- Diversity of counterparties
- Share of new counterparties
- Unusual bursts of activity
- Pass-through fund movement
- Interaction with known flagged addresses

These features are combined into a risk score from `0` to `100`.

### 2. Explainable Verdicts

Multipli does not only return a numerical score. It also explains the main reasons behind the risk assessment.

Example risk drivers include:

- High share of brand-new counterparties
- Suspiciously rapid fund forwarding
- Funding from a flagged address
- Unusually high recent activity

This makes the output easier to understand and useful in a live transaction flow.

### 3. MetaMask Integration

A MetaMask Snap is integrated into the wallet confirmation flow so users can receive a risk verdict before signing.

This makes Multipli a pre-signing security layer rather than a post-transaction alert system.

### 4. User Decision Controls

Multipli includes policy logic for:

- Whitelisting trusted addresses
- Blacklisting risky addresses
- Review or hold flows
- Re-checking trusted wallets over time

This turns the risk model into a practical transaction decision system rather than a one-off detector.

### 5. Contract Analysis

The project also supports analysis of smart-contract addresses alongside regular wallet addresses.

This makes the system extensible beyond simple externally owned accounts (EOAs).

## Risk Decision Model

A simplified interpretation of the system is:

| Verdict | Meaning |
|---|---|
| `ALLOW` | The wallet does not currently show significant risk signals. |
| `REVIEW` | The wallet has signals that require additional user attention. |
| `BLOCK` | The wallet shows strong risk indicators and the transaction should be stopped or rejected. |

The exact decision can incorporate both the model's risk score and additional policy rules such as whitelist and blacklist status.

## Example

A user is about to send crypto to:

```text
0xABC...123
```

Multipli analyzes the destination address and detects:

```text
Wallet age: Very low
Recent activity: Unusually high
New counterparties: High
Fund forwarding: Detected
Flagged address interaction: Detected
```

The system could return:

```text
Risk Score: 87/100
Verdict: BLOCK

Reasons:
- High share of new counterparties
- Rapid fund forwarding detected
- Interaction with a known flagged address
```

The user receives this information before approving the transaction.

## Why This Matters

Multipli brings transaction risk screening directly into the signing experience.

It can be useful for:

- Everyday crypto users
- Treasury and operations teams
- Wallet providers
- Compliance and risk teams
- Applications that need safer transaction approval

Instead of asking users to manually investigate a destination address, Multipli provides an automated risk signal at the point where the transaction is about to be signed.

## Simple Pitch

> Multipli detects risky wallet behavior before a transaction is signed, explains why the address looks suspicious, and gives the user a quick path to allow, review, or block.

## Tech Stack

| Technology | Purpose |
|---|---|
| Python | Core application and ML logic |
| FastAPI | Backend API |
| LightGBM | Wallet risk model |
| Etherscan | On-chain transaction and wallet data |
| MetaMask Snaps | Pre-signing wallet integration |
| PostgreSQL | Review, policy, and decision state |

## Project Structure

A high-level representation of the project architecture:

```text
multipli/
|
├── api/
|   ├── routes/
|   ├── services/
|   └── main.py
|
├── features/
|   ├── wallet_metrics.py
|   ├── transaction_analysis.py
|   └── behavior.py
|
├── model/
|   ├── training/
|   ├── inference/
|   └── risk_model.py
|
├── snap/
|   └── metamask-integration/
|
├── database/
|   └── review_logic/
|
├── tests/
|
├── requirements.txt
└── README.md
```

The exact structure may vary depending on the implementation.

## Core Design Principle

Multipli is designed around one simple idea:

```text
Don't wait for the transaction to become a problem.
Assess the destination before the user signs.
```

By combining on-chain behavioral analysis, machine-learning risk scoring, explainable signals, and wallet-level policy controls, Multipli turns blockchain transaction data into an actionable security decision.

## Status

Multipli is a prototype / product implementation focused on demonstrating pre-signing wallet risk intelligence, explainable risk signals, and MetaMask-based transaction screening.

## Disclaimer

Multipli provides risk signals based on available on-chain data and model outputs. A risk score is not a guarantee that an address is malicious or safe. Users and integrating applications should consider additional context before making transaction decisions.
