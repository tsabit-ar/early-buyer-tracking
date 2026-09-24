import os, sys, json
from pathlib import Path

PROJECT_ROOT = Path(r"c:\Me\CRYPTO PROJECT [MEMECOIN]\screener-wallet\early-buyer-scanner")
sys.path.insert(0, str(PROJECT_ROOT))

from storage.database import Database
from api.solana_rpc import SolanaRpcClient
from collectors.candidate_lifecycle import derive_candidate_atas, fetch_candidate_lifecycle_signatures
from collectors.transactions import get_transaction_details_batch, parse_solscan_tx_detail
from analyzers.transaction_classifier import classify_transaction

db = Database(PROJECT_ROOT / "early_buyer.db")
client = SolanaRpcClient(database=db)
valid_mint = "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc"

wallets = [
    "4oMMbUFZ83T2a6MshfjZwxcsTZwYSkUCRt8FVjbmL1d3",
    "6pJXs9kq6rMZwwy6z2HDc93yXbUeu2rWQnLZURwKhk7G",
    "EWjeKsM6BT6pheDaqwXq8k3XxBPNxfAmKAhzeaDudBbd",
    "9Nhh5D1YrPWY3PRvZqgfPSrsMtpiEHPzKSEX4wdpicLj",
    "6XLbzQoWaF9TrE3MDyHKa6p8LDv685hsmw1xwr2iDzXv"
]

print("=== STARTING AUDIT OF 5 WALLETS ===")

# First, fetch lifecycle signatures for these 5 wallets
lifecycle_map = fetch_candidate_lifecycle_signatures(
    client=client,
    candidate_wallets=wallets,
    token_mint=valid_mint,
    max_signatures_per_ata=1000,
)

all_sigs = set()
for w, info in lifecycle_map.items():
    print(f"\nWallet {w[:8]}... ATAs: {info.lifecycle_atas} | Sig Count: {len(info.signatures)} | Complete: {info.lifecycle_history_complete}")
    all_sigs.update(info.signatures)

print(f"\nTotal unique signatures to inspect across 5 wallets: {len(all_sigs)}")
tx_details = get_transaction_details_batch(list(all_sigs), client=client, db=db)
tx_map = {tx.signature: tx for tx in tx_details}

for w in wallets:
    print(f"\n========================================================")
    print(f"AUDIT FOR WALLET: {w}")
    print(f"========================================================")
    info = lifecycle_map.get(w)
    sigs = info.signatures if info else []
    
    # Sort chronologically
    sigs.sort(key=lambda s: (tx_map[s].slot or 0, tx_map[s].block_time or 0) if s in tx_map else (0, 0))
    
    total_token_in = 0.0
    total_token_out = 0.0
    
    for s in sigs:
        tx = tx_map.get(s)
        if not tx:
            print(f"  [MISSING] Sig: {s}")
            continue
            
        cl = classify_transaction(tx, w, valid_mint)
        
        # Extract full balance changes for this token and wallet
        w_tb = [tb for tb in tx.token_balance_changes if tb.mint == valid_mint and tb.address == w]
        # In case address in token_balance_changes was ATA or owner
        all_token_tb = [tb for tb in tx.token_balance_changes if tb.mint == valid_mint]
        
        print(f"\n  Sig: {s}")
        print(f"    Slot: {tx.slot} | BlockTime: {tx.block_time}")
        print(f"    Signer: {tx.signer}")
        print(f"    Programs: {tx.programs}")
        print(f"    Classified: {cl.classification} (Conf: {cl.confidence})")
        print(f"    Classified TokenChange: {cl.token_change:,.6f} | SolChange: {cl.sol_change:.6f}")
        
        # Print raw token balance changes
        for tb in all_token_tb:
            print(f"    TokenBal: addr={tb.address[:10]}... pre={tb.pre_balance:,.6f} post={tb.post_balance:,.6f} chg={tb.change:,.6f} dec={tb.decimals}")
        
        # Check raw instructions if available in raw_data
        if tx.raw_data:
            d = tx.raw_data.get("data", tx.raw_data)
            # Check if there is transfer/instruction detail
            parsed_tx = d.get("parsedInstruction", [])
            
    print(f"\n--------------------------------------------------------")
