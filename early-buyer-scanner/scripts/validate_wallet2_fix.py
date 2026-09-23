import json
import sys
import os

# Ensure project root is in sys.path
sys.path.insert(0, r"c:\Me\CRYPTO PROJECT [MEMECOIN]\screener-wallet\early-buyer-scanner")
os.chdir(r"c:\Me\CRYPTO PROJECT [MEMECOIN]\screener-wallet\early-buyer-scanner")

from api.solana_rpc import SolanaRpcClient
from storage.database import Database
from config import settings
from collectors.transactions import get_transaction_details_batch
from analyzers.transaction_classifier import classify_transaction
from analyzers.candidate_generator import (
    filter_candidate_events,
    extract_candidate_wallets,
    get_token_pool_addresses,
)
from analyzers.wallet_analyzer import build_buyer_profile
from models.schemas import TransferEvent, ClassificationEnum

def main():
    candidate_wallet = "5PnLRhNdRKURUCPezx4nNBpBGfkTe1kVFYELKWMFvRRd"
    old_sig = "2Tf2bRoQoBp7BW6CfqwskimB7Lt9umGV6P8EuLXGhhwN3mnz9S1XdSHrE12U1KHkk3dQ3fqS91VQEnqrwzryKTPm"
    token_mint = "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc"

    db = Database(settings.sqlite_db_path)
    client = SolanaRpcClient(database=db)

    print("--- 1. FETCHING TRANSACTION DETAIL ---")
    tx_list = get_transaction_details_batch([old_sig], client=client, db=db)
    if not tx_list:
        print("ERROR: Transaction could not be fetched!")
        return
    tx = tx_list[0]
    print(f"Tx Signature: {tx.signature}")
    print(f"Signer: {tx.signer}")
    print(f"Slot: {tx.slot}")
    print(f"Block Time: {tx.block_time}")
    print(f"Programs: {tx.programs}")
    
    print("\n--- TOKEN BALANCE CHANGES ---")
    for tb in tx.token_balance_changes:
        print(f"  Account: {tb.address} | Mint: {tb.mint} | Pre: {tb.pre_balance} | Post: {tb.post_balance} | Change: {tb.change}")

    print("\n--- SOL BALANCE CHANGES ---")
    for sb in tx.sol_balance_changes:
        print(f"  Account: {sb.address} | Pre: {sb.pre_balance} | Post: {sb.post_balance} | Change: {sb.change}")

    print("\n--- 2. CLASSIFYING TX FROM CANDIDATE WALLET PERSPECTIVE ---")
    classified_candidate = classify_transaction(tx, wallet_address=candidate_wallet, token_address=token_mint)
    print(f"Classification: {classified_candidate.classification.value}")
    print(f"Confidence: {classified_candidate.confidence.value}")
    print(f"Token Change: {classified_candidate.token_change}")
    print(f"Sol Change: {classified_candidate.sol_change}")
    print(f"Reasons: {classified_candidate.reasons}")

    print("\n--- 3. CLASSIFYING TX FROM ACTUAL SIGNER PERSPECTIVE ---")
    classified_signer = classify_transaction(tx, wallet_address=tx.signer, token_address=token_mint)
    print(f"Signer: {tx.signer}")
    print(f"Classification: {classified_signer.classification.value}")
    print(f"Confidence: {classified_signer.confidence.value}")
    print(f"Token Change: {classified_signer.token_change}")
    print(f"Sol Change: {classified_signer.sol_change}")
    print(f"Reasons: {classified_signer.reasons}")

    print("\n--- 4. CANDIDATE GENERATION CHECK ---")
    # Simulate a TransferEvent constructed from this transaction
    event = TransferEvent(
        signature=old_sig,
        block_time=tx.block_time,
        from_address=tx.signer,
        to_address=candidate_wallet,
        token_address=token_mint,
        amount=4012469.902709,
        decimals=6,
        slot=tx.slot,
    )
    pools = get_token_pool_addresses(token_mint)
    print(f"Derived Dynamic Pools for Mint: {pools}")
    is_in_pool = candidate_wallet in pools
    print(f"Is candidate_wallet in dynamic pools? {is_in_pool}")

    filtered = filter_candidate_events([event], token_address=token_mint)
    print(f"Events before filter: 1, after filter: {len(filtered)}")

    candidates = extract_candidate_wallets(filtered, token_address=token_mint)
    print(f"Candidate wallets extracted: {candidates}")

    print("\n--- 5. WALLET PROFILE FIRST BUY EVALUATION ---")
    profile = build_buyer_profile(
        wallet_address=candidate_wallet,
        token_address=token_mint,
        launch_time=tx.block_time,
        tx_classifications=[classified_candidate],
    )
    print(f"Wallet Profile result for {candidate_wallet}: {profile}")

if __name__ == "__main__":
    main()
