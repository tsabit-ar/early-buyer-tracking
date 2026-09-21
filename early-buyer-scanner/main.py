"""Main CLI runner for Early Buyer Scanner (EBRS).

Executes the complete on-chain pipeline:
Token Input -> Historical Transfers -> Non-Buy Filtering -> Transaction Detail
-> Evidence-First Classification -> Wallet Profiling -> Scoring & Ranking
-> Terminal ASCII Report & CSV/JSON Export (PRD Section 6, 20).
"""

import argparse
import csv
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Tuple

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analyzers.candidate_generator import extract_candidate_wallets, filter_candidate_events
from analyzers.funding_analyzer import detect_funder_clusters, trace_wallet_funder
from analyzers.sell_analyzer import enrich_profile_with_sells
from analyzers.transaction_classifier import classify_transaction
from analyzers.wallet_analyzer import build_buyer_profile, format_time_delta, tag_same_block_snipers
from api.solana_rpc import SolanaRpcClient
from api.solscan import SolscanClient
from collectors.holders import get_token_holders_data
from collectors.token import fetch_token_metadata, resolve_launch_time, validate_solana_address
from collectors.transfers import collect_historical_transfers
from collectors.transactions import get_transaction_details_batch
from config import KNOWN_CEX_WALLETS, PUMP_FUN_PROGRAM_ID, RAYDIUM_AMM_V4_ID, SYSTEM_PROGRAM_ID, WSOL_MINT, settings
from models.schemas import (
    BalanceChange,
    ClassificationEnum,
    ConfidenceEnum,
    ScannerReport,
    ScoreBreakdown,
    TokenMetadata,
    TransactionClassification,
    TransactionDetail,
    TransferEvent,
    WalletProfile,
)
from scoring.scorer import score_and_rank_buyers
from storage.database import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("EBRS")


def truncate_address(address: str, head: int = 4, tail: int = 3) -> str:
    """Format an address into 7xK...abc format."""
    if not address or len(address) <= head + tail + 3:
        return address
    return f"{address[:head]}...{address[-tail:]}"


def print_executive_report(
    token: TokenMetadata,
    candidates_count: int,
    ranked_buyers: List[Tuple[WalletProfile, ScoreBreakdown]],
    top_n: int = 10,
) -> None:
    """Print the ASCII report formatted according to Section 20 of the PRD."""
    launch_str = (
        datetime.fromtimestamp(token.launch_time, tz=timezone.utc).strftime("%d %b %Y %H:%M:%S UTC")
        if token.launch_time
        else "N/A (Confidence LOW)"
    )

    token_name = token.name or "UNKNOWN"
    token_sym = token.symbol or token.token_address
    token_display = f"{token_name} ({token_sym})"

    print("\n" + "=" * 49)
    print("EARLY BUYER SCANNER")
    print("=" * 49)
    print(f"\nToken:\n{token_display}")
    print(f"\nLaunch:\n{launch_str}")
    print(f"\nCandidates:\n{candidates_count}")
    print(f"\nLikely Buyers:\n{len(ranked_buyers)}")
    print("\n" + "-" * 90)
    print(f"{'RANK':<5} {'WALLET':<12} {'FIRST BUY':<11} {'SIZE':<10} {'HOLD':<6} {'AGE':<12} {'FUNDER':<18} {'SCORE':<6} {'TAG':<8}")
    print("-" * 90)

    for rank, (p, _) in enumerate(ranked_buyers[:top_n], start=1):
        wallet_short = truncate_address(p.wallet_address, 4, 3)
        time_rel = format_time_delta(p.time_after_launch)
        # Format size as token amount or USD estimate
        size_str = f"{p.first_buy_amount:,.0f}" if p.first_buy_amount < 1e6 else f"{p.first_buy_amount/1e6:,.1f}M"
        # Hold percentage: 100% * (1 - exit_ratio) or from holder_percentage
        retention_pct = max(0, int(round((1.0 - p.exit_ratio) * 100)))
        hold_str = f"{retention_pct}%"
        tag_str = "[SNIPER]" if p.is_same_block_sniper else "-"

        # Format age
        if p.wallet_age_days is not None:
            if p.wallet_age_days < 1/24:
                m = max(1, int(p.wallet_age_days * 1440))
                age_str = f"{m}m [FRESH]"
            elif p.wallet_age_days < 1.0:
                h = max(1, int(p.wallet_age_days * 24))
                age_str = f"{h}h [FRESH]"
            else:
                age_str = f"{int(p.wallet_age_days)}d"
        elif p.funder_type == "MATURE_WALLET":
            age_str = ">7d"
        else:
            age_str = "N/A"

        # Format funder
        if p.funder_type == "CEX":
            cex_name = KNOWN_CEX_WALLETS.get(p.funder_address, "CEX")
            funder_str = f"{cex_name}"
        elif p.funder_type == "INTERNAL":
            funder_str = "Self-funded"
        elif p.cluster_id:
            short_funder = truncate_address(p.funder_address, 3, 2)
            funder_str = f"{short_funder} [{p.cluster_id}]"
        elif p.funder_address:
            funder_str = truncate_address(p.funder_address, 4, 3)
        elif p.funder_type == "MATURE_WALLET":
            funder_str = "Mature"
        else:
            funder_str = "-"

        print(
            f"{rank:<5} {wallet_short:<12} {time_rel:<11} {size_str:<10} {hold_str:<6} {age_str:<12} {funder_str:<18} {int(p.score):<6} {tag_str:<8}"
        )
    print("-" * 90)

    # Detailed view of #1 Buyer if available
    if ranked_buyers:
        top_buyer, breakdown = ranked_buyers[0]
        exit_pct = int(round(top_buyer.exit_ratio * 100))
        retention_pct = max(0, 100 - exit_pct)

        if top_buyer.wallet_age_days is not None:
            if top_buyer.wallet_age_days < 1/24:
                top_age = f"{max(1, int(top_buyer.wallet_age_days * 1440))}m (FRESH)"
            elif top_buyer.wallet_age_days < 1.0:
                top_age = f"{max(1, int(top_buyer.wallet_age_days * 24))}h (FRESH)"
            else:
                top_age = f"{int(top_buyer.wallet_age_days)} days"
        elif top_buyer.funder_type == "MATURE_WALLET":
            top_age = "> 7 days (Mature)"
        else:
            top_age = "N/A"

        if top_buyer.funder_type == "CEX":
            top_funder = KNOWN_CEX_WALLETS.get(top_buyer.funder_address, "CEX")
        elif top_buyer.funder_address:
            top_funder = top_buyer.funder_address
        else:
            top_funder = "N/A"

        print("\nWallet Detail:")
        print(top_buyer.wallet_address)
        print(f"\nClassification: BUY")
        print(f"Confidence: {top_buyer.confidence.value}")
        if top_buyer.is_same_block_sniper:
            print(f"Sniper Status: [SNIPER] (Same-Block Entry, Slot: {top_buyer.first_buy_slot or 'N/A'})")
        print(f"Wallet Age: {top_age}")
        print(f"Funding Source: {top_funder} (Type: {top_buyer.funder_type})")
        if top_buyer.funding_amount_sol is not None:
            print(f"Initial Funding: {top_buyer.funding_amount_sol} SOL")
        if top_buyer.cluster_id:
            print(f"Sybil Cluster: [{top_buyer.cluster_id}]")
        print(f"\nFirst Buy:\n{format_time_delta(top_buyer.time_after_launch)}")
        print(f"\nFirst Buy Size:\n{top_buyer.first_buy_amount:,.2f} tokens")
        print(f"\nBuy Count:\n{top_buyer.buy_count}")
        print(f"\nCurrent Holding:\n{retention_pct}%")
        print(f"\nEstimated Exit:\n{exit_pct}%")
        print("\nEvidence:")
        print(f"Transaction Signature: {top_buyer.first_buy_signature or 'N/A'}")
        if top_buyer.first_buy_slot is not None:
            print(f"Block Slot: {top_buyer.first_buy_slot}")
        if top_buyer.funding_signature:
            print(f"Funding Transaction: {top_buyer.funding_signature}")
        print(f"Score Breakdown: Early={breakdown.early_entry_score:.0f}, Size={breakdown.buy_size_score:.0f}, Acc={breakdown.accumulation_score:.0f}, Hold={breakdown.holding_score:.0f}")
        print("=" * 49 + "\n")


def export_reports(
    token: TokenMetadata,
    candidates_count: int,
    ranked_buyers: List[Tuple[WalletProfile, ScoreBreakdown]],
    output_dir: Path,
    export_csv_flag: bool,
    export_json_flag: bool,
) -> None:
    """Save ranked early buyers to CSV and JSON reports."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Export CSV
    if export_csv_flag:
        csv_path = output_dir / "buyers.csv"
        with open(csv_path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Rank",
                "Wallet Address",
                "First Buy Timestamp",
                "First Buy Slot",
                "Time After Launch",
                "First Buy Amount",
                "Total Buy Amount",
                "Buy Count",
                "Sell Count",
                "Total Sell Amount",
                "Current Holding",
                "Exit Ratio",
                "Wallet Age Days",
                "Is Fresh Wallet",
                "Funder Address",
                "Funder Type",
                "Funding Amount SOL",
                "Cluster ID",
                "Score",
                "Confidence",
                "Is Sniper",
                "First Buy Signature",
            ])
            for rank, (p, _) in enumerate(ranked_buyers, start=1):
                writer.writerow([
                    rank,
                    p.wallet_address,
                    p.first_buy_time or "",
                    p.first_buy_slot or "",
                    format_time_delta(p.time_after_launch),
                    p.first_buy_amount,
                    p.total_buy_amount,
                    p.buy_count,
                    p.sell_count,
                    p.total_sell_amount,
                    p.current_holding,
                    p.exit_ratio,
                    p.wallet_age_days if p.wallet_age_days is not None else "",
                    "YES" if p.is_fresh_wallet else "NO",
                    p.funder_address or "",
                    p.funder_type,
                    p.funding_amount_sol if p.funding_amount_sol is not None else "",
                    p.cluster_id or "",
                    p.score,
                    p.confidence.value,
                    "YES" if p.is_same_block_sniper else "NO",
                    p.first_buy_signature or "",
                ])
        logger.info(f"CSV report exported to: {csv_path.resolve()}")

    # Export JSON
    if export_json_flag:
        json_path = output_dir / "report.json"
        report_data = ScannerReport(
            token=token,
            candidates_count=candidates_count,
            likely_buyers_count=len(ranked_buyers),
            buyers=[p for p, _ in ranked_buyers],
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        with open(json_path, mode="w", encoding="utf-8") as f:
            f.write(report_data.model_dump_json(indent=2))
        logger.info(f"JSON report exported to: {json_path.resolve()}")


def run_mock_simulation(output_dir: Path, export_csv_flag: bool, export_json_flag: bool) -> None:
    """Execute end-to-end scanner using realistic offline fixture data (demonstration mode)."""
    logger.info("Executing EBRS in Offline Mock Simulation Mode...")

    token_mint = "MOJOmint111111111111111111111111111111111111"
    launch_timestamp = 1726833600  # 20 Sep 2026 12:00:00 UTC

    token = TokenMetadata(
        token_address=token_mint,
        name="Mojo Memecoin",
        symbol="MOJO",
        decimals=6,
        creator="Deployer11111111111111111111111111111111111",
        launch_time=launch_timestamp,
        launch_confidence=ConfidenceEnum.HIGH,
    )

    # 3 Realistic Early Buyers
    wallets = [
        "7xK9uN8tQAbc11111111111111111111111111111111",
        "9t2mK5vLxyz11111111111111111111111111111111",
        "4Fs8pQrTqwe11111111111111111111111111111111",
        "3Kk9aBcD111111111111111111111111111111111111",
        "8Zz2wXyV111111111111111111111111111111111111",
    ]

    # Pre-crafted Transaction Classifications
    tx_classifications = [
        # Wallet 1: Bought +2m14s (134s), 4 buys total, sold 29%, slot 300000001
        TransactionClassification(
            signature="sig_w1_buy1",
            wallet=wallets[0],
            token_address=token_mint,
            block_time=launch_timestamp + 134,
            slot=300000001,
            classification=ClassificationEnum.BUY,
            confidence=ConfidenceEnum.HIGH,
            sol_change=-1.5,
            token_change=1240000.0,
            programs=[RAYDIUM_AMM_V4_ID],
        ),
        TransactionClassification(
            signature="sig_w1_buy2",
            wallet=wallets[0],
            token_address=token_mint,
            block_time=launch_timestamp + 200,
            slot=300000010,
            classification=ClassificationEnum.BUY,
            confidence=ConfidenceEnum.HIGH,
            sol_change=-1.0,
            token_change=800000.0,
            programs=[RAYDIUM_AMM_V4_ID],
        ),
        TransactionClassification(
            signature="sig_w1_buy3",
            wallet=wallets[0],
            token_address=token_mint,
            block_time=launch_timestamp + 250,
            slot=300000020,
            classification=ClassificationEnum.BUY,
            confidence=ConfidenceEnum.HIGH,
            sol_change=-0.5,
            token_change=400000.0,
            programs=[RAYDIUM_AMM_V4_ID],
        ),
        TransactionClassification(
            signature="sig_w1_sell1",
            wallet=wallets[0],
            token_address=token_mint,
            block_time=launch_timestamp + 600,
            slot=300000100,
            classification=ClassificationEnum.SELL,
            confidence=ConfidenceEnum.HIGH,
            sol_change=2.0,
            token_change=-707600.0,  # ~29% sold
            programs=[RAYDIUM_AMM_V4_ID],
        ),
        # Wallet 2: Bought +3m02s (182s), 2 buys, sold 16%, SAME SLOT 300000001 (Sniper cluster!)
        TransactionClassification(
            signature="sig_w2_buy1",
            wallet=wallets[1],
            token_address=token_mint,
            block_time=launch_timestamp + 182,
            slot=300000001,
            classification=ClassificationEnum.BUY,
            confidence=ConfidenceEnum.HIGH,
            sol_change=-0.85,
            token_change=850000.0,
            programs=[PUMP_FUN_PROGRAM_ID],
        ),
        TransactionClassification(
            signature="sig_w2_buy2",
            wallet=wallets[1],
            token_address=token_mint,
            block_time=launch_timestamp + 240,
            classification=ClassificationEnum.BUY,
            confidence=ConfidenceEnum.HIGH,
            sol_change=-0.40,
            token_change=400000.0,
            programs=[PUMP_FUN_PROGRAM_ID],
        ),
        TransactionClassification(
            signature="sig_w2_sell1",
            wallet=wallets[1],
            token_address=token_mint,
            block_time=launch_timestamp + 900,
            classification=ClassificationEnum.SELL,
            confidence=ConfidenceEnum.HIGH,
            sol_change=0.9,
            token_change=-200000.0,  # 16% sold
            programs=[PUMP_FUN_PROGRAM_ID],
        ),
        # Wallet 3: Bought +4m31s (271s), 1 buy, sold 65%
        TransactionClassification(
            signature="sig_w3_buy1",
            wallet=wallets[2],
            token_address=token_mint,
            block_time=launch_timestamp + 271,
            classification=ClassificationEnum.BUY,
            confidence=ConfidenceEnum.HIGH,
            sol_change=-2.1,
            token_change=2100000.0,
            programs=[RAYDIUM_AMM_V4_ID],
        ),
        TransactionClassification(
            signature="sig_w3_sell1",
            wallet=wallets[2],
            token_address=token_mint,
            block_time=launch_timestamp + 1200,
            classification=ClassificationEnum.SELL,
            confidence=ConfidenceEnum.HIGH,
            sol_change=3.0,
            token_change=-1365000.0,  # 65% sold
            programs=[RAYDIUM_AMM_V4_ID],
        ),
        # Wallet 4: Regular Transfer with gas only (NOT BUY)
        TransactionClassification(
            signature="sig_w4_trans",
            wallet=wallets[3],
            token_address=token_mint,
            block_time=launch_timestamp + 50,
            classification=ClassificationEnum.TRANSFER,
            confidence=ConfidenceEnum.HIGH,
            sol_change=-0.000005,
            token_change=10000.0,
            programs=[SYSTEM_PROGRAM_ID],
        ),
        # Wallet 5: Airdrop distribution (NOT BUY)
        TransactionClassification(
            signature="sig_w5_dist",
            wallet=wallets[4],
            token_address=token_mint,
            block_time=launch_timestamp + 10,
            classification=ClassificationEnum.DISTRIBUTION,
            confidence=ConfidenceEnum.HIGH,
            sol_change=0.0,
            token_change=50000.0,
            programs=[SYSTEM_PROGRAM_ID],
        ),
    ]

    profiles: List[WalletProfile] = []
    for w in wallets:
        w_txs = [tx for tx in tx_classifications if tx.wallet == w]
        prof = build_buyer_profile(
            wallet_address=w,
            token_address=token_mint,
            launch_time=launch_timestamp,
            tx_classifications=w_txs,
        )
        if prof:
            enrich_profile_with_sells(prof, w_txs)
            profiles.append(prof)

    profiles = tag_same_block_snipers(profiles)

    # Mock funding & Sybil clustering
    if len(profiles) >= 3:
        profiles[0].funder_type = "CEX"
        profiles[0].funder_address = "5tzFkiKscMRHK5ZXkrZXZ1RChPTyVC5yFsNuPaSkWCjd"
        profiles[0].wallet_age_days = 0.08
        profiles[0].is_fresh_wallet = True
        profiles[0].funding_amount_sol = 5.0
        profiles[0].funding_signature = "sig_mock_fund_0"

        profiles[1].funder_type = "EOA"
        profiles[1].funder_address = "SybilBoss11111111111111111111111111111111111"
        profiles[1].wallet_age_days = 0.04
        profiles[1].is_fresh_wallet = True
        profiles[1].funding_amount_sol = 1.5
        profiles[1].funding_signature = "sig_mock_fund_1"

        profiles[2].funder_type = "EOA"
        profiles[2].funder_address = "SybilBoss11111111111111111111111111111111111"
        profiles[2].wallet_age_days = 0.05
        profiles[2].is_fresh_wallet = True
        profiles[2].funding_amount_sol = 2.0
        profiles[2].funding_signature = "sig_mock_fund_2"

        profiles = detect_funder_clusters(profiles)

    ranked_buyers = score_and_rank_buyers(profiles, launch_confidence=token.launch_confidence)

    print_executive_report(
        token=token,
        candidates_count=247,
        ranked_buyers=ranked_buyers,
    )

    export_reports(
        token=token,
        candidates_count=247,
        ranked_buyers=ranked_buyers,
        output_dir=output_dir,
        export_csv_flag=export_csv_flag,
        export_json_flag=export_json_flag,
    )


def run_pipeline(
    mint_address: str,
    max_transfers: int = 200,
    export_csv_flag: bool = True,
    export_json_flag: bool = True,
    output_dir: Path = Path("output"),
) -> None:
    """Execute live scanner pipeline for a given token mint."""
    valid_mint = validate_solana_address(mint_address)
    logger.info(f"Initializing EBRS scanner for token: {valid_mint}")

    db = Database(settings.sqlite_db_path)
    client = SolanaRpcClient(database=db)

    # 1. Fetch token metadata
    logger.info("Step 1: Fetching token metadata...")
    token = fetch_token_metadata(valid_mint, client=client, db=db)

    # 2. Resolve token launch time
    logger.info("Step 2: Resolving token launch timestamp...")
    launch_time, launch_conf = resolve_launch_time(valid_mint, client=client, db=db)
    token.launch_time = launch_time
    token.launch_confidence = ConfidenceEnum(launch_conf)

    # 3. Collect historical transfers
    logger.info(f"Step 3: Collecting historical token transfers (max: {max_transfers})...")
    transfers = collect_historical_transfers(
        mint_address=valid_mint,
        max_transfers=max_transfers,
        client=client,
        db=db,
    )
    logger.info(f"Retrieved {len(transfers)} historical transfer events.")

    # 4. Filter candidate events (non-buy filtering)
    logger.info("Step 4: Filtering out non-buy entities and programs...")
    filtered_events = filter_candidate_events(transfers, creator_address=token.creator)

    # 5. Extract unique candidate wallets
    candidates = extract_candidate_wallets(filtered_events, token_address=valid_mint, db=db)
    logger.info(f"Generated {len(candidates)} unique candidate buyer wallets.")

    # 6. Fetch top holders data for holding state
    logger.info("Step 6: Fetching current token holder statistics...")
    holders_map = get_token_holders_data(valid_mint, client=client, db=db)

    # 7. Collect transaction details for candidate signatures
    signatures_to_inspect = list({e.signature for e in filtered_events if e.signature})
    logger.info(f"Step 7: Inspecting on-chain transaction details for {len(signatures_to_inspect)} signatures...")
    tx_details = get_transaction_details_batch(signatures_to_inspect, client=client, db=db)
    tx_detail_map = {tx.signature: tx for tx in tx_details}

    # 8. Classify transactions (Evidence First)
    logger.info("Step 8: Classifying transactions (BUY/SELL/TRANSFER/DISTRIBUTION)...")
    all_classifications: List[TransactionClassification] = []
    for event in filtered_events:
        tx_detail = tx_detail_map.get(event.signature)
        if not tx_detail:
            continue
        classified = classify_transaction(
            tx=tx_detail,
            wallet_address=event.to_address,
            token_address=valid_mint,
        )
        all_classifications.append(classified)
        db.save_transaction(classified, raw_data=tx_detail.raw_data)

    # 9. Build buyer profiles and analyze sells
    logger.info("Step 9: Building buyer profiles and analyzing exit ratios...")
    buyer_profiles: List[WalletProfile] = []
    for wallet in candidates:
        wallet_txs = [tx for tx in all_classifications if tx.wallet == wallet]
        holder_info = holders_map.get(wallet)
        profile = build_buyer_profile(
            wallet_address=wallet,
            token_address=valid_mint,
            launch_time=token.launch_time,
            tx_classifications=wallet_txs,
            holder_data=holder_info,
        )
        if profile:
            enrich_profile_with_sells(profile, wallet_txs)
            buyer_profiles.append(profile)

    buyer_profiles = tag_same_block_snipers(buyer_profiles)
    logger.info(f"Identified {len(buyer_profiles)} verified early buyers.")

    # 10. Trace initial SOL funding & detect Sybil clusters
    logger.info(f"Step 10: Tracing initial SOL funding and detecting Sybil clusters for {len(buyer_profiles)} buyers...")
    for p in buyer_profiles:
        funder_addr, funder_type, age_days, fund_amt, fund_sig = trace_wallet_funder(
            wallet_address=p.wallet_address,
            rpc_client=client,
            db=db,
            launch_time=token.launch_time,
        )
        p.funder_address = funder_addr
        p.funder_type = funder_type
        p.wallet_age_days = age_days
        p.is_fresh_wallet = bool(age_days is not None and age_days < 1.0)
        p.funding_amount_sol = fund_amt
        p.funding_signature = fund_sig

    buyer_profiles = detect_funder_clusters(buyer_profiles)

    # 11. Scoring and ranking
    logger.info("Step 11: Calculating multi-pillar scores and ranking early buyers...")
    ranked_buyers = score_and_rank_buyers(
        buyer_profiles,
        launch_confidence=token.launch_confidence,
    )

    # Save final profiles to database
    for p, _ in ranked_buyers:
        db.save_wallet_profile(p)

    # 12. Print executive report & export
    print_executive_report(
        token=token,
        candidates_count=len(candidates),
        ranked_buyers=ranked_buyers,
    )

    export_reports(
        token=token,
        candidates_count=len(candidates),
        ranked_buyers=ranked_buyers,
        output_dir=output_dir,
        export_csv_flag=export_csv_flag,
        export_json_flag=export_json_flag,
    )


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="EarlyBuyer Scanner (EBRS) — Solana On-Chain Early Buyer Detection & Ranking System"
    )
    parser.add_argument(
        "--mint",
        type=str,
        help="Solana token mint address to scan",
    )
    parser.add_argument(
        "--max-transfers",
        type=int,
        default=200,
        help="Maximum historical token transfers to inspect (default: 200)",
    )
    parser.add_argument(
        "--export-csv",
        action="store_true",
        default=True,
        help="Export ranked buyers to output/buyers.csv (default: True)",
    )
    parser.add_argument(
        "--export-json",
        action="store_true",
        default=True,
        help="Export scanner summary report to output/report.json (default: True)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="output",
        help="Directory to save exported reports (default: output)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run offline demonstration pipeline using pre-crafted on-chain fixtures",
    )

    args = parser.parse_args()
    out_dir = Path(args.output_dir)
    if not out_dir.is_absolute():
        out_dir = PROJECT_ROOT / out_dir

    if args.mock:
        run_mock_simulation(
            output_dir=out_dir,
            export_csv_flag=args.export_csv,
            export_json_flag=args.export_json,
        )
    else:
        if not args.mint:
            parser.print_help()
            print("\nError: --mint address is required unless running in --mock mode.")
            sys.exit(1)
        run_pipeline(
            mint_address=args.mint,
            max_transfers=args.max_transfers,
            export_csv_flag=args.export_csv,
            export_json_flag=args.export_json,
            output_dir=out_dir,
        )


if __name__ == "__main__":
    main()
