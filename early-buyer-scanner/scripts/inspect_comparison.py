import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.solana_rpc import SolanaRpcClient
from storage.database import Database
from config import settings

def compare():
    db = Database(settings.sqlite_db_path)
    client = SolanaRpcClient(database=db)
    
    cases = [
        ("DN19 Buy", "5ffLcdDsKh5qkjr7JXLiQT6Zsn4h4VsFKLudwfwFY16h4EyYrXZmE7K9Ax8MHqDET5rXNmhmtG9GmFYW8iKRQ2pg", "DN19MDuZRKsj4RG26sXtUaoPuQenHkxyQmGQDbgVgmzv"),
        ("BLm7 Buy", "4vxJJXpsUjXJjvDhL3UqSWwSMcxWAgw75xhiKU2vdDvnW6i3UK6NCTkTdn6e29rpkRt9tZmgTFRQQ14iyA75WfNk", "BLm7PT4iUYgRFum1LkrhJofN3tkHzjjDsYY6QjbwRBaS"),
        ("2iDAb Buy", "3yBZPD6ohPR4efTV7ngRUbY9rPv8Zsy9zSSEF5XqbJyBaySB6Mo8RUYKV5EtMkvuFH16mM3dTQmPi8t6sNyjjkCC", "2iDAbmU7i5bUiCkLr7GKHJrwbS2FANQbZJ81fXTbwjap"),
        ("2iDAb Sell 8", "3Lq7ASbKh7vtJzmQBKZi4aJXnnMshYC5aqSrARRs1eFRw7nx2JNmSzzg8G9bvQ24uLEZCstWki2Yu1nj83GQpduG", "2iDAbmU7i5bUiCkLr7GKHJrwbS2FANQbZJ81fXTbwjap"),
        ("2iDAb Sell 9", "5yvg71UX7KQktRDxCd3FmQtrqmvmXPWaLvVrNDet11DGWs4rZVMSG5bQZKX4teBPTXr3cFPJxFdeFUZC4G2k62RX", "2iDAbmU7i5bUiCkLr7GKHJrwbS2FANQbZJ81fXTbwjap"),
    ]
    
    for label, sig, wallet in cases:
        tx = client.get_parsed_transaction(sig)
        meta = tx.get("meta", {})
        print(f"\n=== {label} ({sig[:15]}...) ===")
        print("preTokenBalances for wallet:")
        for b in meta.get("preTokenBalances", []):
            if b.get("owner") == wallet and b.get("mint") == "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc":
                print("  PRE:", b.get("uiTokenAmount"))
        print("postTokenBalances for wallet:")
        for b in meta.get("postTokenBalances", []):
            if b.get("owner") == wallet and b.get("mint") == "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc":
                print("  POST:", b.get("uiTokenAmount"))

if __name__ == "__main__":
    compare()
