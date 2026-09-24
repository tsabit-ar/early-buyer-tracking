import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.solana_rpc import SolanaRpcClient
from storage.database import Database
from config import settings

def inspect_3lq7():
    db = Database(settings.sqlite_db_path)
    client = SolanaRpcClient(database=db)
    s = "3Lq7ASbKh7vtJzmQBKZi4aJXnnMshYC5aqSrARRs1eFRw7nx2JNmSzzg8G9bvQ24uLEZCstWki2Yu1nj83GQpduG"
    raw_rpc = client.get_parsed_transaction(s)
    meta = raw_rpc.get("meta", {})
    print("preTokenBalances:")
    for b in meta.get("preTokenBalances", []):
        if b.get("mint") == "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc":
            print(f"  Owner: {b.get('owner')} | uiTokenAmount: {b.get('uiTokenAmount')}")
    print("postTokenBalances:")
    for b in meta.get("postTokenBalances", []):
        if b.get("mint") == "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc":
            print(f"  Owner: {b.get('owner')} | uiTokenAmount: {b.get('uiTokenAmount')}")

if __name__ == "__main__":
    inspect_3lq7()
