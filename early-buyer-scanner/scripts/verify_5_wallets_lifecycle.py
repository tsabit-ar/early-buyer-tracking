"""Verification and on-chain acceptance script for 5 target wallets on FIBONACCI."""

import os, sys
from pathlib import Path

PROJECT_ROOT = Path(r"c:\Me\CRYPTO PROJECT [MEMECOIN]\screener-wallet\early-buyer-scanner")
sys.path.insert(0, str(PROJECT_ROOT))

from storage.database import Database
from api.solana_rpc import SolanaRpcClient
from collectors.candidate_lifecycle import fetch_candidate_lifecycle_signatures
from collectors.transactions import get_transaction_details_batch
from analyzers.transaction_classifier import classify_transaction
from analyzers.sell_analyzer import enrich_profile_with_sells
from models.schemas import ClassificationEnum, ConfidenceEnum, WalletProfile

db = Database(PROJECT_ROOT / "early_buyer.db")
client = SolanaRpcClient(database=db)
valid_mint = "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc"

wallets = [
    "4oMMbUFZ83T2a6MshfjZwxcsTZwYSkUCRt8FVjbmL1d3",
    "6pJXs9kq6rMZwwy6z2HDc93yXbUeu2rWQnLZURwKhk7G",
    "EWjeKsM6BT6pheDaqwXq8k3XxBPNxfAmKAhzeaDudBbd",
    "9Nhh5D1YrPWY3PRvZqgfPSrsMtpiEHPzKSEX4wdpicLj",
    "6XLbzQoWaF9TrE3MDyHKa6p8LDv685hsmw1xwr2iDzXv",
]

print("================================================================================")
print("RUNNING FORENSIC ON-CHAIN ACCEPTANCE TEST — FIBONACCI (5 WALLETS)")
print("================================================================================")

lifecycle_map = fetch_candidate_lifecycle_signatures(
    client=client,
    candidate_wallets=wallets,
    token_mint=valid_mint,
    max_signatures_per_ata=1000,
)

all_sigs = set()
for w, info in lifecycle_map.items():
    all_sigs.update(info.signatures)

print(f"Total unique ATA signatures fetched: {len(all_sigs)}")
tx_details = get_transaction_details_batch(list(all_sigs), client=client, db=db)
tx_map = {tx.signature: tx for tx in tx_details if tx}

profiles = []

for w in wallets:
    info = lifecycle_map[w]
    sigs = list(info.signatures)
    sigs.sort(key=lambda s: (tx_map[s].slot or 0, tx_map[s].block_time or 0) if s in tx_map else (0, 0))

    classified_txs = []
    first_buy_time = None
    first_buy_sig = None
    first_buy_amt = 0.0
    first_buy_slot = None
    total_buy = 0.0
    buy_count = 0

    print(f"\n--------------------------------------------------------------------------------")
    print(f"WALLET: {w} (ATA Sigs: {len(sigs)})")
    print(f"--------------------------------------------------------------------------------")

    for s in sigs:
        tx = tx_map.get(s)
        if not tx:
            continue
        cl = classify_transaction(tx, w, valid_mint)
        classified_txs.append(cl)

        if cl.classification == ClassificationEnum.BUY:
            buy_count += 1
            total_buy += cl.token_change
            if first_buy_time is None:
                first_buy_time = tx.block_time
                first_buy_sig = tx.signature
                first_buy_amt = cl.token_change
                first_buy_slot = tx.slot

        print(f"  [{cl.classification.value:12s}] {s[:16]}... Slot={tx.slot} TokChg={cl.token_change:+15.4f} SolChg={cl.sol_change:+.6f} Conf={cl.confidence.value}")

    profile = WalletProfile(
        wallet_address=w,
        token_address=valid_mint,
        first_buy_time=first_buy_time,
        first_buy_slot=first_buy_slot,
        first_buy_signature=first_buy_sig,
        first_buy_amount=first_buy_amt,
        total_buy_amount=total_buy,
        buy_count=buy_count,
        current_holding=0.0,
        lifecycle_history_complete=info.lifecycle_history_complete,
        lifecycle_signature_count=info.lifecycle_signature_count,
        lifecycle_pages_fetched=info.lifecycle_pages_fetched,
        lifecycle_truncated=info.lifecycle_truncated,
        truncation_reason=info.truncation_reason,
        last_cursor_signature=info.last_cursor_signature,
    )

    enrich_profile_with_sells(profile, classified_txs)
    profiles.append(profile)

print("\n================================================================================")
print("5-WALLET ACCEPTANCE RESULTS TABLE")
print("================================================================================")
header = (
    f"{'Wallet':<12} | {'Buy':<14} | {'Sell':<14} | {'TransIn':<12} | {'TransOut':<12} | "
    f"{'Burn':<8} | {'UnkOut':<8} | {'Holding':<8} | {'Residual':<10} | {'Status':<10} | "
    f"{'Complete':<8} | {'Trunc':<6} | {'Confidence':<10}"
)
print(header)
print("-" * len(header))

for p in profiles:
    row = (
        f"{p.wallet_address[:10]}.. | "
        f"{p.total_buy_amount:>14,.2f} | "
        f"{p.total_sell_amount:>14,.2f} | "
        f"{p.transfer_in_amount:>12,.2f} | "
        f"{p.transfer_out_amount:>12,.2f} | "
        f"{p.burn_amount:>8,.2f} | "
        f"{p.unknown_outflow_amount:>8,.2f} | "
        f"{p.current_holding:>8,.2f} | "
        f"{p.reconciliation_difference:>10,.2f} | "
        f"{p.balance_reconciliation_status:<10} | "
        f"{'YES' if p.lifecycle_history_complete else 'NO':<8} | "
        f"{'YES' if p.lifecycle_truncated else 'NO':<6} | "
        f"{p.confidence.value:<10}"
    )
    print(row)
print("================================================================================")
