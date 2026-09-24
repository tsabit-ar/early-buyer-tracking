"""Verification script for candidate ATA coverage (Read-only inspection)."""

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from storage.database import Database
from api.solana_rpc import SolanaRpcClient
from collectors.candidate_lifecycle import derive_candidate_atas, fetch_candidate_lifecycle_signatures

wallets = [
    '4oMMbUFZ83T2a6MshfjZwxcsTZwYSkUCRt8FVjbmL1d3',
    '6pJXs9kq6rMZwwy6z2HDc93yXbUeu2rWQnLZURwKhk7G',
    'EWjeKsM6BT6pheDaqwXq8k3XxBPNxfAmKAhzeaDudBbd',
    '9Nhh5D1YrPWY3PRvZqgfPSrsMtpiEHPzKSEX4wdpicLj',
    '6XLbzQoWaF9TrE3MDyHKa6p8LDv685hsmw1xwr2iDzXv',
]

mint = '5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc'
db = Database('early_buyer.db')
client = SolanaRpcClient(database=db)

for w in wallets:
    atas = derive_candidate_atas(w, mint)
    print(f"=======================================================")
    print(f"WALLET: {w}")
    print(f"=======================================================")
    print(f"  Token-2022 ATA  : {atas[0]}")
    print(f"  Standard SPL ATA: {atas[1]}")
    
    sigs_2022 = client.get_signatures_for_address(atas[0], limit=100) if hasattr(client, 'get_signatures_for_address') else []
    sigs_spl = client.get_signatures_for_address(atas[1], limit=100) if hasattr(client, 'get_signatures_for_address') else []
    
    unique_sigs = set()
    for s in (sigs_2022 or []):
        sig = s.get('signature') if isinstance(s, dict) else s
        if sig: unique_sigs.add(sig)
    for s in (sigs_spl or []):
        sig = s.get('signature') if isinstance(s, dict) else s
        if sig: unique_sigs.add(sig)
        
    print(f"  Signatures on Token-2022 ATA  : {len(sigs_2022)}")
    print(f"  Signatures on Standard SPL ATA : {len(sigs_spl)}")
    print(f"  Unique Combined Signatures     : {len(unique_sigs)}")
    
    # Query on-chain token accounts via getTokenAccountsByOwner
    try:
        resp = client._call_rpc('getTokenAccountsByOwner', [
            w,
            {'mint': mint},
            {'encoding': 'jsonParsed'}
        ])
        on_chain_accounts = [acc['pubkey'] for acc in resp.get('value', [])] if resp and 'value' in resp else []
    except Exception as exc:
        on_chain_accounts = []
        print(f"  RPC Error: {exc}")
        
    print(f"  On-chain Token Accounts found  : {on_chain_accounts}")
    extra = [acc for acc in on_chain_accounts if acc not in atas]
    if extra:
        print(f"  Additional Non-ATA Accounts    : {extra}")
    else:
        print(f"  Additional Non-ATA Accounts    : None (100% ATA Coverage)")
    print()
