import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.solana_rpc import SolanaRpcClient
from storage.database import Database
from config import settings

def inspect_dn19():
    db = Database(settings.sqlite_db_path)
    client = SolanaRpcClient(database=db)
    s = "5ffLcdDsKh5qkjr7JXLiQT6Zsn4h4VsFKLudwfwFY16h4EyYrXZmE7K9Ax8MHqDET5rXNmhmtG9GmFYW8iKRQ2pg"
    raw_rpc = client.get_parsed_transaction(s)
    meta = raw_rpc.get("meta", {})
    print("DN19 preTokenBalances:")
    for b in meta.get("preTokenBalances", []):
        if b.get("mint") == "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc":
            print(f"  Owner: {b.get('owner')} | uiTokenAmount: {b.get('uiTokenAmount')}")
    print("DN19 postTokenBalances:")
    for b in meta.get("postTokenBalances", []):
        if b.get("mint") == "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc":
            print(f"  Owner: {b.get('owner')} | uiTokenAmount: {b.get('uiTokenAmount')}")

if __name__ == "__main__":
    inspect_dn19()
