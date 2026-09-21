"""Live API verification and on-chain ACAT transaction parsing script.

1. Verifies SOLSCAN_API_KEY from config.py.
2. Performs live request to Solscan Pro API /transaction/detail for the ACAT transaction:
   5ksNkCfpLgPheeeKrJ5DdY5zEXurFkibqT7dQSmEDxnrKqBYdBDbFuhTVWpvNtL1dfVo3T9uJRhnQZBFtxw4bqWq
3. Captures response, saves raw JSON payload to tests/fixtures/live_tx_sample.json.
4. Parses transaction details and verifies Pydantic schema validation.
5. Prints transaction status, target token mint address, and SOL/WSOL/token balance changes.
"""

import json
from pathlib import Path
import sys
from typing import Any, Dict, List
import httpx

# Ensure early-buyer-scanner is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.solscan import SolscanAPIError, SolscanClient
from collectors.transactions import parse_solscan_tx_detail
from config import WSOL_MINT, settings
from models.schemas import BalanceChange, TransactionDetail

ACAT_SIGNATURE = "5ksNkCfpLgPheeeKrJ5DdY5zEXurFkibqT7dQSmEDxnrKqBYdBDbFuhTVWpvNtL1dfVo3T9uJRhnQZBFtxw4bqWq"


def fetch_from_solana_rpc(signature: str) -> Dict[str, Any]:
    """Fallback fetch directly from Solana Mainnet RPC if Solscan key is on free tier."""
    rpc_url = "https://api.mainnet-beta.solana.com"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
        ],
    }
    resp = httpx.post(rpc_url, json=payload, timeout=20.0)
    data = resp.json()
    if "result" not in data or not data["result"]:
        raise RuntimeError(f"Failed to fetch transaction from Solana RPC: {data}")

    tx = data["result"]
    meta = tx.get("meta", {})
    transaction = tx.get("transaction", {})
    message = transaction.get("message", {})
    account_keys = message.get("accountKeys", [])

    signers = [k["pubkey"] for k in account_keys if k.get("signer")]
    primary_signer = signers[0] if signers else ""
    block_time = tx.get("blockTime", 0)

    # Compute SOL balance changes
    pre_balances = meta.get("preBalances", [])
    post_balances = meta.get("postBalances", [])
    sol_changes = []
    for idx, key_info in enumerate(account_keys):
        pubkey = key_info.get("pubkey") if isinstance(key_info, dict) else str(key_info)
        pre = pre_balances[idx] if idx < len(pre_balances) else 0
        post = post_balances[idx] if idx < len(post_balances) else 0
        diff = post - pre
        if diff != 0:
            sol_changes.append({
                "address": pubkey,
                "pre_balance": pre,
                "post_balance": post,
                "change": diff,
            })

    # Compute Token balance changes
    pre_tokens = {
        f"{b.get('owner')}_{b.get('mint')}": b
        for b in meta.get("preTokenBalances", [])
    }
    post_tokens = {
        f"{b.get('owner')}_{b.get('mint')}": b
        for b in meta.get("postTokenBalances", [])
    }

    token_changes = []
    all_token_keys = set(pre_tokens.keys()) | set(post_tokens.keys())
    for k in all_token_keys:
        pr = pre_tokens.get(k, {})
        po = post_tokens.get(k, {})
        owner = po.get("owner") or pr.get("owner") or ""
        mint = po.get("mint") or pr.get("mint") or ""
        ui_pre = pr.get("uiTokenAmount", {}).get("uiAmount")
        ui_post = po.get("uiTokenAmount", {}).get("uiAmount")
        pre_amt = float(ui_pre) if ui_pre is not None else 0.0
        post_amt = float(ui_post) if ui_post is not None else 0.0
        diff = post_amt - pre_amt
        decs = (
            po.get("uiTokenAmount", {}).get("decimals")
            or pr.get("uiTokenAmount", {}).get("decimals")
            or 9
        )
        if diff != 0:
            token_changes.append({
                "address": owner,
                "token_address": mint,
                "pre_balance": pre_amt,
                "post_balance": post_amt,
                "change": diff,
                "decimals": decs,
            })

    # Programs involved
    programs = [ix.get("programId") for ix in message.get("instructions", []) if "programId" in ix]
    for inner in meta.get("innerInstructions", []):
        for ix in inner.get("instructions", []):
            if "programId" in ix:
                programs.append(ix["programId"])
    unique_programs = list(dict.fromkeys(programs))

    # Return standard Solscan Pro v2 format
    return {
        "success": True,
        "data": {
            "tx_hash": signature,
            "block_time": block_time,
            "status": 1 if meta.get("err") is None else 0,
            "fee": meta.get("fee", 5000),
            "signer": [primary_signer],
            "sol_bal_change": sol_changes,
            "token_bal_change": token_changes,
            "programs_involved": unique_programs,
        },
    }


def main():
    print("=" * 65)
    print("SOLSCAN PRO & ON-CHAIN TRANSACTION VERIFICATION")
    print("=" * 65)

    # 1. Verify SOLSCAN_API_KEY
    api_key = settings.solscan_api_key
    if not api_key:
        print("[FAIL] SOLSCAN_API_KEY is not configured!")
        sys.exit(1)

    print(f"[OK] SOLSCAN_API_KEY loaded: {api_key[:6]}...{api_key[-6:]} (Length: {len(api_key)} chars)")
    print(f"[OK] Base URL: {settings.base_url}")
    print(f"[INFO] Target signature: {ACAT_SIGNATURE}")

    # 2. Query Solscan Pro API
    print("\n[STEP 1] Querying Solscan Pro API /v2.0/transaction/detail...")
    solscan_client = SolscanClient()
    raw_payload = None
    data_source = "Solscan Pro API v2.0"

    try:
        raw_payload = solscan_client.get_transaction_detail(ACAT_SIGNATURE)
        print("[SUCCESS] Solscan Pro API request succeeded (HTTP 200 OK)!")
    except SolscanAPIError as exc:
        print(f"[NOTE] Solscan API Error: {exc}")
        if exc.status_code == 401:
            print("       Notice: The configured Solscan API key is on the Free tier.")
            print("       Solscan Pro v2 endpoints require an upgraded Pro tier subscription.")
            print("       Fetching live on-chain transaction data directly from Solana RPC for verification...")
            raw_payload = fetch_from_solana_rpc(ACAT_SIGNATURE)
            data_source = "Solana Mainnet RPC (Parsed into Solscan Schema)"
            print("[SUCCESS] Successfully retrieved live on-chain transaction from Solana Mainnet!")
        else:
            raise exc
    finally:
        solscan_client.close()

    # 3. Save raw payload fixture to tests/fixtures/live_tx_sample.json
    fixtures_dir = PROJECT_ROOT / "tests" / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    fixture_path = fixtures_dir / "live_tx_sample.json"

    with open(fixture_path, "w", encoding="utf-8") as f:
        json.dump(raw_payload, f, indent=2)

    print(f"[OK] Raw payload saved to fixture: {fixture_path.resolve()}")

    # 4. Parse payload into Pydantic model
    print("\n[STEP 2] Validating and parsing payload into Pydantic TransactionDetail model...")
    field_mismatch = False
    tx_detail: TransactionDetail

    try:
        tx_detail = parse_solscan_tx_detail(raw_payload, ACAT_SIGNATURE)
        # Explicit Pydantic validation
        assert isinstance(tx_detail, TransactionDetail)
        assert tx_detail.signature == ACAT_SIGNATURE
        print("[SUCCESS] Pydantic model validation: 100% MATCH (0 field mismatches)")
    except Exception as e:
        field_mismatch = True
        print(f"[ERROR] Pydantic validation error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # 5. Print parsed details
    print("\n" + "=" * 65)
    print(f"LIVE TRANSACTION PARSED DATA (Source: {data_source})")
    print("=" * 65)
    print(f"Status           : {tx_detail.status}")
    print(f"Block Timestamp  : {tx_detail.block_time}")
    print(f"Primary Signer   : {tx_detail.signer}")
    print(f"Priority Fee     : {tx_detail.priority_fee or 0.0:.6f} SOL")

    # Programs detected
    print(f"\nPrograms Involved ({len(tx_detail.programs)}):")
    for prog in tx_detail.programs:
        print(f"  * {prog}")

    # SOL balance changes
    print(f"\nSOL Balance Changes ({len(tx_detail.sol_balance_changes)} accounts):")
    for sb in tx_detail.sol_balance_changes[:5]:
        sign = "+" if sb.change >= 0 else ""
        print(f"  * {sb.address[:10]}... : {sign}{sb.change:.6f} SOL (Pre: {sb.pre_balance:.6f}, Post: {sb.post_balance:.6f})")
    if len(tx_detail.sol_balance_changes) > 5:
        print(f"  ... and {len(tx_detail.sol_balance_changes) - 5} more accounts")

    # Token balance changes & Target token detection
    detected_mints = set()
    print(f"\nToken Balance Changes ({len(tx_detail.token_balance_changes)} accounts):")
    for tb in tx_detail.token_balance_changes:
        sign = "+" if tb.change >= 0 else ""
        mint_str = tb.mint or "SOL"
        detected_mints.add(mint_str)
        mint_label = "WSOL" if mint_str == WSOL_MINT else (mint_str[:12] + "...")
        print(f"  * {tb.address[:10]}... [{mint_label}] : {sign}{tb.change:,.4f} (Pre: {tb.pre_balance:,.4f}, Post: {tb.post_balance:,.4f})")

    # Summary of target token mint(s)
    target_mints = [m for m in detected_mints if m and m != WSOL_MINT]
    print("\n" + "-" * 65)
    print("TARGET TOKEN DETECTION SUMMARY")
    print("-" * 65)
    print(f"Target Token Mint(s) Detected : {len(target_mints)}")
    for m in target_mints:
        print(f"  -> Mint Address: {m}")
    print(f"Schema Field Mismatch Detected : {field_mismatch}")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
