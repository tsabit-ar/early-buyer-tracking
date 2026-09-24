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

with open('output/lifecycle_fixed_v2.csv') as f:
    csv_wallets = [r['Wallet Address'] for r in csv.DictReader(f)]

with open('output/audit_all_sells.csv') as f:
    exported_sells = {r['Signature']: r for r in csv.DictReader(f)}
with open('output/audit_all_buys.csv') as f:
    exported_buys = {r['Signature']: r for r in csv.DictReader(f)}

# Now reconstruct how audit_high_confidence ran
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

script1_sells = set()
script1_buys = set()
script1_all = []

for wallet in csv_wallets:
    w_lifecycle_sigs = set(lifecycle_map[wallet].signatures) if wallet in lifecycle_map else set()
    w_transfer_sigs = {e.signature for e in candidate_events if (e.to_address == wallet or e.from_address == wallet) and e.signature}
    combined_wallet_sigs = list(w_lifecycle_sigs | w_transfer_sigs)
    for sig in combined_wallet_sigs:
        tx = tx_detail_map.get(sig)
        if not tx: continue
        c = classify_transaction(tx=tx, wallet_address=wallet, token_address=mint)
        script1_all.append((wallet, sig, c.classification))
        if c.classification == ClassificationEnum.SELL:
            script1_sells.add(sig)
        elif c.classification == ClassificationEnum.BUY:
            script1_buys.add(sig)

print(f"script1_all total: {len(script1_all)}")
print(f"script1_sells unique sigs: {len(script1_sells)}")
print(f"script1_buys unique sigs: {len(script1_buys)}")
print(f"exported_sells unique sigs: {len(exported_sells)}")
print(f"exported_buys unique sigs: {len(exported_buys)}")

diff_sells = set(exported_sells.keys()) - script1_sells
print(f"Diff sells (in exported but not script1): {len(diff_sells)}")
for s in diff_sells:
    print(f"  Sell Sig: {s}, Wallet: {exported_sells[s]['Wallet Address']}")

diff_sells_rev = script1_sells - set(exported_sells.keys())
print(f"Diff sells (in script1 but not exported): {len(diff_sells_rev)}")

diff_buys = set(exported_buys.keys()) - script1_buys
print(f"Diff buys (in exported but not script1): {len(diff_buys)}")
for b in diff_buys:
    print(f"  Buy Sig: {b}, Wallet: {exported_buys[b]['Wallet Address']}")
