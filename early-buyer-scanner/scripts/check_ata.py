import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.solana_rpc import SolanaRpcClient
from storage.database import Database
from config import settings

def check_ata():
    db = Database(settings.sqlite_db_path)
    client = SolanaRpcClient(database=db)
    ata = "AhYuWeSyRq9GMaMhfPbM2fP5ZCB252TLpF81yfRoXwv6"
    sigs = client.get_signatures_for_address(ata, limit=100)
    print(f"Signatures for 2iDAb ATA ({ata}): {len(sigs)}")
    for s in sigs:
        sig = s["signature"]
        slot = s.get("slot")
        block_time = s.get("blockTime")
        print(f"  {sig} (slot={slot}, time={block_time})")

if __name__ == "__main__":
    check_ata()
