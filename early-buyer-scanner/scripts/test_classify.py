import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.solana_rpc import SolanaRpcClient
from storage.database import Database
from config import settings
from collectors.transactions import parse_solscan_tx_detail
from analyzers.transaction_classifier import classify_transaction

def test_classify():
    db = Database(settings.sqlite_db_path)
    client = SolanaRpcClient(database=db)
    wallet = "2iDAbmU7i5bUiCkLr7GKHJrwbS2FANQbZJ81fXTbwjap"
    mint = "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc"
    
    sigs = [
        "3KxYaC3aEAuw63YzDGeDq9yTPJFktew3ugPUHeUYr8DUZknAaat4C99RAvojcWa6BXR1g3FAqd6TjHTKfuz8E6dT",
        "3Lq7ASbKh7vtJzmQBKZi4aJXnnMshYC5aqSrARRs1eFRw7nx2JNmSzzg8G9bvQ24uLEZCstWki2Yu1nj83GQpduG",
        "5yvg71UX7KQktRDxCd3FmQtrqmvmXPWaLvVrNDet11DGWs4rZVMSG5bQZKX4teBPTXr3cFPJxFdeFUZC4G2k62RX",
    ]
    
    for s in sigs:
        raw_detail = client.get_transaction_detail(s)
        parsed = parse_solscan_tx_detail(raw_detail, s)
        classified = classify_transaction(parsed, wallet, mint)
        print(f"\nSig: {s[:20]}...")
        print(f"Classification: {classified.classification}")
        print(f"Token change: {classified.token_change}")
        print(f"SOL change: {classified.sol_change}")
        print(f"Reasons: {classified.reasons}")

if __name__ == "__main__":
    test_classify()
