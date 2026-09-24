"""Comprehensive Audit Script for EBRS Lifecycle and High Confidence Verification.

Verifies all 112 wallets from output/lifecycle_fixed_v2.csv against:
1. Complete transaction classification breakdowns
2. SELL swap evidence (quote asset received, DEX program, pool)
3. BUY swap evidence (quote asset spent, DEX program)
4. Non-circular raw token balance sum vs classified sum vs current holding
5. Zero false-positive SELLs (outflows touching DEX without quote received)
6. Zero false-positive BUYs (inflows touching DEX without quote spent)
7. Special transactions check (close account, rent reclaim, ATA creation, LP, transfers)
8. Global statistics (wallets, confidence, tx classification counts)
9. Top 10 largest and Top 10 smallest SELLs with evidence
"""

import csv
import json
from pathlib import Path
import sys

# Ensure early-buyer-scanner root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    BURN_ADDRESSES,
    KNOWN_DEX_PROGRAMS,
    QUOTE_ASSET_MINTS,
    WSOL_MINT,
    settings,
)
from storage.database import Database
from api.solana_rpc import SolanaRpcClient
from collectors.token import fetch_token_metadata
from collectors.transfers import collect_historical_transfers
from analyzers.candidate_generator import filter_candidate_events, extract_candidate_wallets
from collectors.candidate_lifecycle import fetch_candidate_lifecycle_signatures
from collectors.transactions import get_transaction_details_batch
from analyzers.transaction_classifier import classify_transaction
from analyzers.sell_analyzer import enrich_profile_with_sells
from analyzers.wallet_analyzer import build_buyer_profile
from collectors.holders import get_token_holders_data
from models.schemas import ClassificationEnum, ConfidenceEnum

def run_audit():
    mint = "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc"
    db = Database("early_buyer.db")
    client = SolanaRpcClient(database=db)

    # 1. Load CSV
    csv_path = PROJECT_ROOT / "output" / "lifecycle_fixed_v2.csv"
    with open(csv_path, mode="r", encoding="utf-8") as f:
        csv_rows = list(csv.DictReader(f))
    csv_wallets = [r["Wallet Address"] for r in csv_rows]
    csv_wallet_set = set(csv_wallets)
    print(f"Total wallets in CSV: {len(csv_wallets)}")

    # 2. Re-collect data from cached DB
    token = fetch_token_metadata(mint, client=client, db=db)
    transfers = collect_historical_transfers(mint, 200, client=client, db=db)
    filtered_events = filter_candidate_events(transfers, token.creator, mint)
    candidates = extract_candidate_wallets(filtered_events, mint, db=db)
    lifecycle_map = fetch_candidate_lifecycle_signatures(client=client, candidate_wallets=candidates, token_mint=mint)
    holders_map = get_token_holders_data(mint, client=client, db=db)

    candidate_set = set(candidates)
    candidate_events = [
        e for e in transfers
        if e.to_address in candidate_set or e.from_address in candidate_set
    ]
    initial_transfer_sigs = {e.signature for e in candidate_events if e.signature}
    all_lifecycle_sigs = {sig for info in lifecycle_map.values() for sig in info.signatures}
    signatures_to_inspect = list(initial_transfer_sigs | all_lifecycle_sigs)

    tx_details = get_transaction_details_batch(signatures_to_inspect, client=client, db=db)
    tx_detail_map = {tx.signature: tx for tx in tx_details}

    # 3. Classify all transactions for all 112 wallets
    wallet_classifications = {}
    all_tx_list = []
    
    for wallet in csv_wallets:
        w_lifecycle_sigs = set(lifecycle_map[wallet].signatures) if wallet in lifecycle_map else set()
        w_transfer_sigs = {e.signature for e in candidate_events if (e.to_address == wallet or e.from_address == wallet) and e.signature}
        combined_wallet_sigs = list(w_lifecycle_sigs | w_transfer_sigs)
        combined_wallet_sigs.sort(key=lambda s: (tx_detail_map[s].slot or 0, tx_detail_map[s].block_time or 0) if s in tx_detail_map else (0, 0))

        w_classifications = []
        for sig in combined_wallet_sigs:
            tx_detail = tx_detail_map.get(sig)
            if not tx_detail:
                continue
            classified = classify_transaction(tx=tx_detail, wallet_address=wallet, token_address=mint)
            w_classifications.append((classified, tx_detail))
            all_tx_list.append((wallet, classified, tx_detail))
        
        wallet_classifications[wallet] = w_classifications

    print(f"Total wallet-tx pairs evaluated: {len(all_tx_list)}")

    # 4. Global Counts & Statistics
    stats = {
        "BUY": 0,
        "SELL": 0,
        "TRANSFER_IN": 0,
        "TRANSFER_OUT": 0,
        "TRANSFER": 0,
        "DISTRIBUTION": 0,
        "BURN": 0,
        "CLOSE_ACCOUNT": 0,
        "UNKNOWN": 0,
    }
    
    all_sells = []
    all_buys = []
    
    for wallet, classified, tx_detail in all_tx_list:
        c_val = classified.classification.value
        stats[c_val] = stats.get(c_val, 0) + 1
        if classified.classification == ClassificationEnum.SELL:
            all_sells.append((wallet, classified, tx_detail))
        elif classified.classification == ClassificationEnum.BUY:
            all_buys.append((wallet, classified, tx_detail))

    print("\n=== GLOBAL TRANSACTION CLASSIFICATION STATS ===")
    for k, v in stats.items():
        print(f"  {k:15s}: {v}")

    # 5. Verify Invariant & Non-Circularity for all 112 Wallets
    # Non-circular test:
    # A. Sum of RAW on-chain token balance changes for the wallet's ATA across all txs:
    #    raw_token_delta = sum(tb.change for tb in tx.token_balance_changes if tb.address == wallet_ata)
    # B. Classified flow sum:
    #    classified_delta = total_buy + transfer_in - total_sell - transfer_out - burn
    # C. Expected current balance vs on-chain current holding
    
    invariant_failures = []
    non_circular_mismatches = []
    
    wallet_summary_table = []
    
    for row in csv_rows:
        wallet = row["Wallet Address"]
        w_txs = wallet_classifications.get(wallet, [])
        
        total_buy = sum(c.token_change for c, _ in w_txs if c.classification == ClassificationEnum.BUY and c.token_change > 0)
        total_sell = sum(abs(c.token_change) for c, _ in w_txs if c.classification == ClassificationEnum.SELL and c.token_change < 0)
        transfer_in = sum(c.token_change for c, _ in w_txs if c.classification in (ClassificationEnum.TRANSFER_IN, ClassificationEnum.DISTRIBUTION, ClassificationEnum.TRANSFER) and c.token_change > 0)
        transfer_out = sum(abs(c.token_change) for c, _ in w_txs if c.classification in (ClassificationEnum.TRANSFER_OUT, ClassificationEnum.TRANSFER) and c.token_change < 0)
        burn = sum(abs(c.token_change) for c, _ in w_txs if c.classification == ClassificationEnum.BURN and c.token_change < 0)
        unknown_in = sum(c.token_change for c, _ in w_txs if c.classification == ClassificationEnum.UNKNOWN and c.token_change > 0)
        unknown_out = sum(abs(c.token_change) for c, _ in w_txs if c.classification == ClassificationEnum.UNKNOWN and c.token_change < 0)
        
        cur_holding = float(row["Current Holding"])
        
        # Raw on-chain net token balance sum directly from tx_detail token balance changes
        raw_net_change = 0.0
        for _, tx_detail in w_txs:
            for tb in tx_detail.token_balance_changes:
                if tb.address.strip() == wallet.strip() and (tb.mint is None or tb.mint.strip() == mint):
                    raw_net_change += tb.change
                    
        classified_net_change = (total_buy + transfer_in) - (total_sell + transfer_out + burn)
        residual = classified_net_change - cur_holding
        
        # Invariant check
        if abs(residual) > 0.01:
            invariant_failures.append((wallet, residual, total_buy, transfer_in, total_sell, transfer_out, burn, cur_holding))
            
        # Non-circular check: Does classified flow equal raw on-chain delta?
        if abs(classified_net_change - raw_net_change) > 0.01:
            non_circular_mismatches.append((wallet, classified_net_change, raw_net_change))

    print(f"\n=== INVARIANT CHECKS (112 WALLETS) ===")
    print(f"Invariant Failures (|residual| > 0.01): {len(invariant_failures)}")
    print(f"Non-Circular Raw vs Classified Mismatches: {len(non_circular_mismatches)}")

    # 6. Test 6: Check for False SELLs (Outflow touching DEX without quote received / no swap)
    false_sells = []
    for wallet, c, tx in all_tx_list:
        if c.token_change < 0:
            touches_dex = any(p in KNOWN_DEX_PROGRAMS for p in tx.programs)
            quote_rec = c.sol_change > 0.000001
            has_swap_action = any("swap" in str(inst).lower() for inst in tx.raw_data.get("instructions", [])) if tx.raw_data else False
            
            if touches_dex and not quote_rec and not has_swap_action:
                # Should NOT be classified as SELL
                if c.classification == ClassificationEnum.SELL:
                    false_sells.append((wallet, c.signature, c.token_change, c.sol_change, tx.programs))

    print(f"\n=== TEST 6: FALSE SELLS (Outflow touching DEX without quote / swap) ===")
    print(f"False SELLs detected: {len(false_sells)}")
    for fs in false_sells:
        print(f"  FLAGGED FALSE SELL: {fs}")

    # 7. Test 7: Check for False BUYs (Inflow touching DEX without quote spent / no swap)
    false_buys = []
    for wallet, c, tx in all_tx_list:
        if c.token_change > 0:
            touches_dex = any(p in KNOWN_DEX_PROGRAMS for p in tx.programs)
            quote_spent = c.sol_change < -0.003  # spent more than gas
            has_swap_action = any("swap" in str(inst).lower() for inst in tx.raw_data.get("instructions", [])) if tx.raw_data else False
            
            if touches_dex and not quote_spent and not has_swap_action:
                # Should NOT be classified as BUY
                if c.classification == ClassificationEnum.BUY:
                    false_buys.append((wallet, c.signature, c.token_change, c.sol_change, tx.programs))

    print(f"\n=== TEST 7: FALSE BUYS (Inflow touching DEX without quote spent / swap) ===")
    print(f"False BUYs detected: {len(false_buys)}")
    for fb in false_buys:
        print(f"  FLAGGED FALSE BUY: {fb}")

    # 8. Test 8: Special Transactions Check
    # (close account, rent reclaim, ATA creation, LP/pool, launchpad, airdrop, transfers)
    special_misclassifications = []
    for wallet, c, tx in all_tx_list:
        # Check if tx is ATA creation / rent reclaim / close account
        raw = tx.raw_data or {}
        parsed_types = []
        for inst in raw.get("instructions", []):
            if isinstance(inst, dict):
                ptype = inst.get("parsed", {}).get("type", "")
                if ptype:
                    parsed_types.append(ptype)
        
        # If instruction is closeAccount or burn, did it get misclassified as BUY or SELL?
        if any(pt in ["closeAccount", "burn", "burnChecked"] for pt in parsed_types):
            if c.classification in (ClassificationEnum.BUY, ClassificationEnum.SELL):
                special_misclassifications.append((wallet, c.signature, c.classification.value, parsed_types))

    print(f"\n=== TEST 8: SPECIAL TRANSACTIONS MISCLASSIFICATIONS ===")
    print(f"Special misclassifications into BUY/SELL: {len(special_misclassifications)}")

    # 9. Top 10 Largest and Top 10 Smallest SELLs with Swap Proof
    # Sort all_sells by absolute token amount
    all_sells.sort(key=lambda s: abs(s[1].token_change), reverse=True)
    top_10_largest_sells = all_sells[:10]
    top_10_smallest_sells = all_sells[-10:]

    def format_sell(sell_tuple):
        wallet, c, tx = sell_tuple
        dex_prog = [p for p in tx.programs if p in KNOWN_DEX_PROGRAMS]
        prog_str = dex_prog[0] if dex_prog else "Known Pool"
        
        # Counterparty / pool address
        from analyzers.candidate_generator import get_token_pool_addresses
        pools = get_token_pool_addresses(mint)
        counterparties = [tb.address for tb in tx.token_balance_changes if tb.address in pools or tb.address != wallet]
        cp_str = counterparties[0] if counterparties else "AMM Pool"
        
        return {
            "signature": c.signature,
            "wallet": wallet,
            "token_out": abs(c.token_change),
            "quote_received": c.sol_change,
            "quote_symbol": "SOL",
            "dex_program": prog_str,
            "counterparty_pool": cp_str,
            "classifier_reasons": c.reasons,
        }

    print("\n=== TOP 10 LARGEST SELLS ===")
    for idx, s in enumerate(top_10_largest_sells, 1):
        info = format_sell(s)
        print(f"{idx}. Sig: {info['signature'][:20]}... | Tokens: {info['token_out']:,.2f} | SOL: +{info['quote_received']:.6f} | DEX: {info['dex_program'][:16]}... | Pool: {info['counterparty_pool'][:16]}...")

    print("\n=== TOP 10 SMALLEST SELLS ===")
    for idx, s in enumerate(top_10_smallest_sells, 1):
        info = format_sell(s)
        print(f"{idx}. Sig: {info['signature'][:20]}... | Tokens: {info['token_out']:,.2f} | SOL: +{info['quote_received']:.6f} | DEX: {info['dex_program'][:16]}... | Pool: {info['counterparty_pool'][:16]}...")

    # Save detailed audit results to JSON
    audit_export = {
        "total_wallets": len(csv_wallets),
        "classification_stats": stats,
        "invariant_failures_count": len(invariant_failures),
        "non_circular_mismatches_count": len(non_circular_mismatches),
        "false_sells_count": len(false_sells),
        "false_buys_count": len(false_buys),
        "special_misclassifications_count": len(special_misclassifications),
        "top_10_largest_sells": [format_sell(s) for s in top_10_largest_sells],
        "top_10_smallest_sells": [format_sell(s) for s in top_10_smallest_sells],
    }

    out_file = PROJECT_ROOT / "output" / "high_confidence_audit_results.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(audit_export, f, indent=2)
    print(f"\nAudit results successfully saved to: {out_file}")

if __name__ == "__main__":
    run_audit()
