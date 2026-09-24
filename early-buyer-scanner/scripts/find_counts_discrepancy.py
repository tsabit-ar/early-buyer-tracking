import csv
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.database import Database
from api.solana_rpc import SolanaRpcClient
from collectors.token import fetch_token_metadata
from collectors.transfers import collect_historical_transfers
from analyzers.candidate_generator import filter_candidate_events, extract_candidate_wallets
from collectors.candidate_lifecycle import fetch_candidate_lifecycle_signatures
from collectors.transactions import get_transaction_details_batch
from analyzers.transaction_classifier import classify_transaction
from models.schemas import ClassificationEnum

mint = '5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc'
db = Database('early_buyer.db')
client = SolanaRpcClient(database=db)

# 1. Load CSV
with open('output/lifecycle_fixed_v2.csv') as f:
    csv_rows = list(csv.DictReader(f))

csv_wallet_buys = {r['Wallet Address']: int(r['Buy Count']) for r in csv_rows}
csv_wallet_sells = {r['Wallet Address']: int(r['Sell Count']) for r in csv_rows}

# 2. Re-run pipeline classification loop exactly as in main.py
transfers = collect_historical_transfers(mint, 200, client=client, db=db)
filtered_events = filter_candidate_events(transfers, '5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc', mint)
candidates = extract_candidate_wallets(filtered_events, mint, db=db)
lifecycle_map = fetch_candidate_lifecycle_signatures(client=client, candidate_wallets=candidates, token_mint=mint)
candidate_set = set(candidates)
candidate_events = [e for e in transfers if e.to_address in candidate_set or e.from_address in candidate_set]
initial_transfer_sigs = {e.signature for e in candidate_events if e.signature}
all_lifecycle_sigs = {sig for info in lifecycle_map.values() for sig in info.signatures}
signatures_to_inspect = list(initial_transfer_sigs | all_lifecycle_sigs)

tx_details = get_transaction_details_batch(signatures_to_inspect, client=client, db=db)
tx_detail_map = {tx.signature: tx for tx in tx_details}

print(f"Total candidate wallets: {len(candidates)}")
print(f"Total signatures to inspect: {len(signatures_to_inspect)}")

# Let's inspect how main.py populated each wallet's classifications
wallet_tx_classified = {}
for wallet in candidates:
    w_lifecycle_sigs = set(lifecycle_map[wallet].signatures) if wallet in lifecycle_map else set()
    w_transfer_sigs = {e.signature for e in candidate_events if (e.to_address == wallet or e.from_address == wallet) and e.signature}
    combined_wallet_sigs = list(w_lifecycle_sigs | w_transfer_sigs)
    combined_wallet_sigs.sort(key=lambda s: (tx_detail_map[s].slot or 0, tx_detail_map[s].block_time or 0) if s in tx_detail_map else (0, 0))

    tx_list = []
    for sig in combined_wallet_sigs:
        tx = tx_detail_map.get(sig)
        if not tx: continue
        c = classify_transaction(tx=tx, wallet_address=wallet, token_address=mint)
        tx_list.append(c)
    wallet_tx_classified[wallet] = tx_list

# Now count per wallet for the 112 CSV wallets
total_buys_live = 0
total_sells_live = 0
total_unknown_live = 0
total_tr_in_live = 0
total_tr_out_live = 0
total_burn_live = 0

mismatches = []
for r in csv_rows:
    w = r['Wallet Address']
    txs = wallet_tx_classified.get(w, [])
    w_buys = [t for t in txs if t.classification == ClassificationEnum.BUY]
    w_sells = [t for t in txs if t.classification == ClassificationEnum.SELL]
    w_tr_in = [t for t in txs if t.classification in (ClassificationEnum.TRANSFER_IN, ClassificationEnum.DISTRIBUTION, ClassificationEnum.TRANSFER)]
    w_tr_out = [t for t in txs if t.classification in (ClassificationEnum.TRANSFER_OUT, ClassificationEnum.TRANSFER)]
    w_burn = [t for t in txs if t.classification == ClassificationEnum.BURN]
    w_unknown = [t for t in txs if t.classification == ClassificationEnum.UNKNOWN]

    total_buys_live += len(w_buys)
    total_sells_live += len(w_sells)
    total_unknown_live += len(w_unknown)
    total_tr_in_live += len(w_tr_in)
    total_tr_out_live += len(w_tr_out)
    total_burn_live += len(w_burn)

    csv_b = csv_wallet_buys[w]
    csv_s = csv_wallet_sells[w]
    if len(w_buys) != csv_b or len(w_sells) != csv_s:
        mismatches.append((w, csv_b, len(w_buys), csv_s, len(w_sells)))

print(f"\nLive reclassified sums across 112 CSV wallets:")
print(f"  BUYs        : {total_buys_live} (CSV sum: {sum(csv_wallet_buys.values())})")
print(f"  SELLs       : {total_sells_live} (CSV sum: {sum(csv_wallet_sells.values())})")
print(f"  TRANSFER_IN : {total_tr_in_live}")
print(f"  TRANSFER_OUT: {total_tr_out_live}")
print(f"  UNKNOWN     : {total_unknown_live}")
print(f"\nWallets with mismatch between CSV and re-classification: {len(mismatches)}")
for m in mismatches:
    print(f"  Wallet: {m[0]} | CSV buys: {m[1]}, live buys: {m[2]} | CSV sells: {m[3]}, live sells: {m[4]}")
