import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from storage.database import Database
from api.solana_rpc import SolanaRpcClient

mint = 'DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP'
db = Database('early_buyer.db')
client = SolanaRpcClient(database=db)

print(f"=== TESTING TOKEN SOURCES FOR: {mint} ===")

# 1. DexScreener API
try:
    resp = httpx.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=5.0)
    if resp.status_code == 200:
        data = resp.json()
        pairs = data.get("pairs", [])
        print(f"DexScreener pairs found: {len(pairs)}")
        if pairs:
            p0 = pairs[0]
            print(f"  DEX: {p0.get('dexId')}")
            print(f"  Pair created at (ms): {p0.get('pairCreatedAt')}")
            if p0.get('pairCreatedAt'):
                print(f"  Pair created at (sec): {p0.get('pairCreatedAt') // 1000}")
            print(f"  Base token: {p0.get('baseToken')}")
except Exception as e:
    print(f"DexScreener error: {e}")

# 2. Account info on-chain
try:
    acc = client.get_account_info(mint, encoding="jsonParsed")
    print(f"Account info owner: {acc.get('owner') if isinstance(acc, dict) else 'N/A'}")
    if isinstance(acc, dict) and 'data' in acc:
        parsed = acc['data'].get('parsed', {})
        print(f"Parsed info: {parsed.get('info')}")
except Exception as e:
    print(f"Account info error: {e}")

# 3. Solscan token meta
try:
    from api.solscan import SolscanClient
    solscan = SolscanClient(db=db)
    meta = solscan.get_token_meta(mint)
    print(f"Solscan meta: {meta}")
except Exception as e:
    print(f"Solscan meta error: {e}")
