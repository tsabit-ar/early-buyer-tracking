import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.solana_rpc import SolanaRpcClient
from storage.database import Database
from config import settings
from collectors.transactions import parse_solscan_tx_detail

def inspect_tx_detail():
    db = Database(settings.sqlite_db_path)
    client = SolanaRpcClient(database=db)
    
    sigs = [
        "3yBZPD6ohPR4efTV7ngRUbY9rPv8Zsy9zSSEF5XqbJyBaySB6Mo8RUYKV5EtMkvuFH16mM3dTQmPi8t6sNyjjkCC", # BUY
        "3KxYaC3aEAuw63YzDGeDq9yTPJFktew3ugPUHeUYr8DUZknAaat4C99RAvojcWa6BXR1g3FAqd6TjHTKfuz8E6dT", # Sell 7?
        "3Lq7ASbKh7vtJzmQBKZi4aJXnnMshYC5aqSrARRs1eFRw7nx2JNmSzzg8G9bvQ24uLEZCstWki2Yu1nj83GQpduG", # Sell 8?
        "5yvg71UX7KQktRDxCd3FmQtrqmvmXPWaLvVrNDet11DGWs4rZVMSG5bQZKX4teBPTXr3cFPJxFdeFUZC4G2k62RX", # Sell 9?
    ]
    
    for s in sigs:
        print(f"\n==================== Sig: {s[:20]}... ====================")
        raw_rpc = client.get_parsed_transaction(s)
        adapter_res = client.get_transaction_detail(s)
        parsed = parse_solscan_tx_detail(adapter_res, s)
        print("Signer:", parsed.signer)
        print("SOL balance changes:")
        for sc in parsed.sol_balance_changes:
            print(f"  {sc.address}: {sc.change}")
        print("Token balance changes:")
        for tc in parsed.token_balance_changes:
            print(f"  {tc.address} ({tc.mint}): change={tc.change} (pre={tc.pre_balance}, post={tc.post_balance})")

if __name__ == "__main__":
    inspect_tx_detail()
