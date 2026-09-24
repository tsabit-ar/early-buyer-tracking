"""Forensic Audit of UNKNOWN Transactions for EBRS (FIBONACCI).

Reconstructs all transactions across all 112 candidate buyers,
extracts all UNKNOWN events, analyzes instructions and balance changes,
partitions them by token delta, and exports UNKNOWN_AUDIT.csv.
"""

import csv
import json
from pathlib import Path
import sys
from collections import Counter, defaultdict

# Ensure project root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from storage.database import Database
from api.solana_rpc import SolanaRpcClient
from collectors.token import fetch_token_metadata
from collectors.transfers import collect_historical_transfers
from analyzers.candidate_generator import filter_candidate_events, extract_candidate_wallets
from collectors.candidate_lifecycle import fetch_candidate_lifecycle_signatures
from collectors.transactions import get_transaction_details_batch
from analyzers.transaction_classifier import classify_transaction
from config import KNOWN_DEX_PROGRAMS, SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID
from models.schemas import ClassificationEnum

def main():
    mint = '5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc'
    db = Database('early_buyer.db')
    client = SolanaRpcClient(database=db)

    # 1. Load CSV Wallets (112 wallets)
    csv_path = PROJECT_ROOT / 'output' / 'lifecycle_fixed_v2.csv'
    with open(csv_path, mode='r', encoding='utf-8') as f:
        csv_rows = list(csv.DictReader(f))
    wallets = [r['Wallet Address'] for r in csv_rows]
    wallet_set = set(wallets)
    print(f"Loaded {len(wallets)} wallets from {csv_path.name}")

    # 2. Re-collect data from cached DB
    token = fetch_token_metadata(mint, client=client, db=db)
    transfers = collect_historical_transfers(mint, 200, client=client, db=db)
    filtered_events = filter_candidate_events(transfers, token.creator, mint)
    candidates = extract_candidate_wallets(filtered_events, mint, db=db)
    
    # Ensure all 112 CSV wallets are included in lifecycle inspection
    all_target_wallets = list(wallet_set.union(set(candidates)))
    print(f"Total target wallets for ATA lifecycle: {len(all_target_wallets)}")

    lifecycle_map = fetch_candidate_lifecycle_signatures(client=client, candidate_wallets=all_target_wallets, token_mint=mint)

    candidate_events = [
        e for e in transfers
        if e.to_address in wallet_set or e.from_address in wallet_set
    ]
    initial_transfer_sigs = {e.signature for e in candidate_events if e.signature}
    all_lifecycle_sigs = {sig for info in lifecycle_map.values() for sig in info.signatures}
    signatures_to_inspect = list(initial_transfer_sigs | all_lifecycle_sigs)
    print(f"Total signatures to inspect: {len(signatures_to_inspect)}")

    tx_details = get_transaction_details_batch(signatures_to_inspect, client=client, db=db)
    tx_detail_map = {tx.signature: tx for tx in tx_details}
    print(f"Retrieved {len(tx_detail_map)} transaction details.")

    # 3. Classify all wallet-tx events and collect UNKNOWNs
    all_wallet_events = []
    unknown_events = []

    for wallet in wallets:
        w_lifecycle_sigs = set(lifecycle_map[wallet].signatures) if wallet in lifecycle_map else set()
        w_transfer_sigs = {e.signature for e in candidate_events if (e.to_address == wallet or e.from_address == wallet) and e.signature}
        combined_wallet_sigs = list(w_lifecycle_sigs | w_transfer_sigs)
        combined_wallet_sigs.sort(key=lambda s: (tx_detail_map[s].slot or 0, tx_detail_map[s].block_time or 0) if s in tx_detail_map else (0, 0))

        for sig in combined_wallet_sigs:
            tx = tx_detail_map.get(sig)
            if not tx:
                continue
            c = classify_transaction(tx=tx, wallet_address=wallet, token_address=mint)
            all_wallet_events.append((wallet, c, tx))
            if c.classification == ClassificationEnum.UNKNOWN:
                unknown_events.append((wallet, c, tx))

    print(f"\nTotal wallet-tx events evaluated across 112 wallets: {len(all_wallet_events)}")
    print(f"Total UNKNOWN events: {len(unknown_events)}")

    # 4. Partition UNKNOWNs by token_delta
    unknown_zero_delta = []
    unknown_pos_delta = []
    unknown_neg_delta = []

    for wallet, c, tx in unknown_events:
        delta = c.token_change
        if abs(delta) < 1e-9:
            unknown_zero_delta.append((wallet, c, tx))
        elif delta > 0:
            unknown_pos_delta.append((wallet, c, tx))
        else:
            unknown_neg_delta.append((wallet, c, tx))

    print(f"\n=== UNKNOWN PARTITION ===")
    print(f"UNKNOWN TOTAL           : {len(unknown_events)}")
    print(f"UNKNOWN token_delta == 0 : {len(unknown_zero_delta)}")
    print(f"UNKNOWN token_delta > 0  : {len(unknown_pos_delta)}")
    print(f"UNKNOWN token_delta < 0  : {len(unknown_neg_delta)}")

    # 5. Deep semantic analysis of UNKNOWN with token_delta == 0
    # Categorize into:
    # 1. CLOSE_ACCOUNT
    # 2. ATA_CREATION
    # 3. RENT_RECLAIM / SOL_TRANSFER
    # 4. FAILED_TRANSACTION
    # 5. INFRASTRUCTURE / COMPUTE_BUDGET
    # 6. OTHER
    
    categories = Counter()
    categorized_records = []

    for wallet, c, tx in unknown_events:
        raw = tx.raw_data or {}
        instructions = raw.get("instructions", [])
        parsed_types = []
        programs = tx.programs or []
        
        # Check parsed instruction types
        for inst in instructions:
            if isinstance(inst, dict):
                pinfo = inst.get("parsed", {})
                if isinstance(pinfo, dict):
                    pt = pinfo.get("type")
                    if pt: parsed_types.append(pt)
                prog = inst.get("program")
                if prog: parsed_types.append(f"prog:{prog}")

        # Check transaction status
        is_failed = raw.get("status") == "Failed" or (isinstance(raw.get("meta"), dict) and raw["meta"].get("err") is not None)
        
        category = "OTHER_NON_TOKEN"
        candidate_classification = "UNKNOWN"

        if is_failed:
            category = "FAILED_TRANSACTION"
            candidate_classification = "FAILED"
        elif "closeAccount" in parsed_types:
            category = "CLOSE_ACCOUNT"
            candidate_classification = "CLOSE_ACCOUNT"
        elif "createAssociatedTokenAccount" in parsed_types or "createIdempotent" in parsed_types or ASSOCIATED_TOKEN_PROGRAM_ID in programs:
            category = "ATA_CREATION"
            candidate_classification = "ATA_CREATION"
        elif "transfer" in parsed_types and any(p == SYSTEM_PROGRAM_ID for p in programs):
            category = "SOL_TRANSFER"
            candidate_classification = "SOL_TRANSFER"
        elif any("rent" in str(inst).lower() for inst in instructions):
            category = "RENT_RECLAIM"
            candidate_classification = "RENT_RECLAIM"
        elif any(p in ["ComputeBudget111111111111111111111111111111"] for p in programs):
            category = "INFRASTRUCTURE"
            candidate_classification = "INFRASTRUCTURE"

        categories[category] += 1

        # Extract signer
        signer = "N/A"
        if raw.get("signers") and isinstance(raw["signers"], list):
            signer = raw["signers"][0]
        elif raw.get("feePayer"):
            signer = raw["feePayer"]

        categorized_records.append({
            "wallet": wallet,
            "tx_signature": c.signature,
            "slot": tx.slot or 0,
            "block_time": tx.block_time or 0,
            "token_mint": mint,
            "token_delta": f"{c.token_change:.6f}",
            "SOL_delta": f"{c.sol_change:.6f}",
            "category": category,
            "current_classification": c.classification.value,
            "candidate_classification": candidate_classification,
            "programs_involved": "; ".join(programs),
            "parsed_instructions": "; ".join(parsed_types) if parsed_types else "N/A",
            "signer": signer,
            "classification_reason": " | ".join(c.reasons),
        })

    print("\n=== SEMANTIC CATEGORIZATION OF UNKNOWN (token_delta == 0) ===")
    for cat, count in categories.most_common():
        print(f"  {cat:25s}: {count}")

    # 6. Export to output/UNKNOWN_AUDIT.csv
    out_csv = PROJECT_ROOT / "output" / "UNKNOWN_AUDIT.csv"
    with open(out_csv, mode="w", newline="", encoding="utf-8") as f:
        if categorized_records:
            writer = csv.DictWriter(f, fieldnames=list(categorized_records[0].keys()))
            writer.writeheader()
            writer.writerows(categorized_records)
    print(f"\nExported {len(categorized_records)} UNKNOWN records to: {out_csv.resolve()}")

    # 7. Check if there are any non-zero token delta records
    non_zero_records = [r for r in categorized_records if float(r["token_delta"]) != 0.0]
    print(f"\nTotal UNKNOWN with non-zero token delta: {len(non_zero_records)}")
    if non_zero_records:
        nz_csv = PROJECT_ROOT / "output" / "UNKNOWN_NONZERO_TOKEN.csv"
        with open(nz_csv, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(non_zero_records[0].keys()))
            writer.writeheader()
            writer.writerows(non_zero_records)
        print(f"Exported non-zero token delta records to: {nz_csv.resolve()}")
    else:
        print("No UNKNOWN_NONZERO_TOKEN.csv needed because non-zero UNKNOWN count is exactly 0.")

if __name__ == "__main__":
    main()
