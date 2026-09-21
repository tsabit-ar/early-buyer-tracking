"""Interactive Web GUI for Solana EarlyBuyer Scanner (EBRS).

Built with Streamlit. Provides real-time on-chain analysis, genesis launch tracking,
evidence-first transaction classification, V2 wallet intelligence (age, funding source),
Sybil cluster detection, interactive metrics, and direct CSV/JSON report downloads.
"""

import csv
from datetime import datetime, timezone
import io
import json
import logging
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Tuple

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import streamlit as st

from analyzers.candidate_generator import extract_candidate_wallets, filter_candidate_events
from analyzers.funding_analyzer import detect_funder_clusters, trace_wallet_funder
from analyzers.sell_analyzer import enrich_profile_with_sells
from analyzers.transaction_classifier import classify_transaction
from analyzers.wallet_analyzer import build_buyer_profile, format_time_delta, tag_same_block_snipers
from api.solana_rpc import SolanaRpcClient
from collectors.holders import get_token_holders_data
from collectors.token import fetch_token_metadata, resolve_launch_time, validate_solana_address
from collectors.transfers import collect_historical_transfers
from collectors.transactions import get_transaction_details_batch
from config import KNOWN_CEX_WALLETS, settings
from models.schemas import (
    ConfidenceEnum,
    ScannerReport,
    ScoreBreakdown,
    TokenMetadata,
    TransactionClassification,
    WalletProfile,
)
from scoring.scorer import score_and_rank_buyers
from storage.database import Database

# Configure page
st.set_page_config(
    page_title="Solana EarlyBuyer Scanner",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom Styling
st.markdown(
    """
    <style>
    .main-title {
        font-size: 2.2rem;
        font-weight: 800;
        letter-spacing: -0.5px;
        margin-bottom: 0px;
    }
    .sub-title {
        color: #888888;
        font-size: 1.0rem;
        margin-bottom: 20px;
    }
    .metric-card {
        background-color: #1e1e24;
        border-radius: 8px;
        padding: 15px;
        border: 1px solid #2e2e38;
    }
    .sybil-alert {
        padding: 12px 16px;
        border-radius: 8px;
        background-color: rgba(255, 75, 75, 0.1);
        border: 1px solid rgba(255, 75, 75, 0.5);
        color: #ff4b4b;
        font-weight: 600;
        margin-bottom: 15px;
    }
    .safe-alert {
        padding: 12px 16px;
        border-radius: 8px;
        background-color: rgba(0, 200, 83, 0.1);
        border: 1px solid rgba(0, 200, 83, 0.5);
        color: #00c853;
        font-weight: 600;
        margin-bottom: 15px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def truncate_address(address: str, head: int = 4, tail: int = 3) -> str:
    """Format address for clean presentation."""
    if not address or len(address) <= head + tail + 3:
        return address
    return f"{address[:head]}...{address[-tail:]}"


def format_age_str(age_days: Optional[float], funder_type: str) -> str:
    """Format wallet age cleanly."""
    if age_days is not None:
        if age_days < 1 / 24:
            m = max(1, int(age_days * 1440))
            return f"{m}m [FRESH]"
        elif age_days < 1.0:
            h = max(1, int(age_days * 24))
            return f"{h}h [FRESH]"
        else:
            return f"{int(age_days)}d"
    elif funder_type == "MATURE_WALLET":
        return ">7d"
    return "N/A"


def format_funder_str(funder_address: Optional[str], funder_type: str, cluster_id: Optional[str]) -> str:
    """Format funding entity or cluster label."""
    if funder_type == "CEX":
        return KNOWN_CEX_WALLETS.get(funder_address, "CEX")
    elif funder_type == "INTERNAL":
        return "Self-funded"
    elif cluster_id:
        return f"{truncate_address(funder_address or '', 3, 2)} [{cluster_id}]"
    elif funder_address:
        return truncate_address(funder_address, 4, 3)
    elif funder_type == "MATURE_WALLET":
        return "Mature"
    return "-"


def execute_pipeline(mint_address: str, max_transfers: int) -> Dict[str, Any]:
    """Execute EBRS on-chain pipeline with step-by-step UI progress updates."""
    valid_mint = validate_solana_address(mint_address)
    db = Database(settings.sqlite_db_path)
    client = SolanaRpcClient(database=db)

    with st.status("Sedang menjalankan pemindaian on-chain Solana...", expanded=True) as status:
        # Step 1: Token metadata
        status.update(label="1/7 Mengambil metadata token & Metaplex PDA...", state="running")
        token = fetch_token_metadata(valid_mint, client=client, db=db)

        # Step 2: Genesis launch time
        status.update(label="2/7 Menentukan waktu peluncuran genesis absolut (UTC)...", state="running")
        launch_time, launch_conf = resolve_launch_time(valid_mint, client=client, db=db)
        token.launch_time = launch_time
        token.launch_confidence = ConfidenceEnum(launch_conf)

        # Step 3: Historical transfers
        status.update(label=f"3/7 Mengumpulkan {max_transfers} transfer historis dari blok genesis...", state="running")
        transfers = collect_historical_transfers(
            mint_address=valid_mint,
            max_transfers=max_transfers,
            client=client,
            db=db,
        )

        # Step 4: Filter & candidate generation
        status.update(label="4/7 Menyaring entitas non-buy & mengekstrak kandidat pembeli...", state="running")
        filtered_events = filter_candidate_events(transfers, creator_address=token.creator)
        candidates = extract_candidate_wallets(filtered_events, token_address=valid_mint, db=db)

        # Step 5: Holder statistics & Tx details
        status.update(label=f"5/7 Memeriksa detail transaksi on-chain untuk {len(filtered_events)} transaksi...", state="running")
        holders_map = get_token_holders_data(valid_mint, client=client, db=db)
        signatures_to_inspect = list({e.signature for e in filtered_events if e.signature})
        tx_details = get_transaction_details_batch(signatures_to_inspect, client=client, db=db)
        tx_detail_map = {tx.signature: tx for tx in tx_details}

        # Step 6: Classification & Profiling
        status.update(label="6/7 Mengklasifikasikan mutasi saldo & profiling early buyers...", state="running")
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

        # Step 7: V2 Funding & Sybil Detection
        status.update(label=f"7/7 Melacak sumber dana SOL pertama & mendeteksi kluster Sybil...", state="running")
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

        # Scoring & Ranking
        ranked_buyers = score_and_rank_buyers(buyer_profiles, launch_confidence=token.launch_confidence)
        for p, _ in ranked_buyers:
            db.save_wallet_profile(p)

        status.update(label="Pemindaian On-Chain Selesai!", state="complete", expanded=False)

    return {
        "token": token,
        "candidates_count": len(candidates),
        "ranked_buyers": ranked_buyers,
    }


def generate_csv_bytes(ranked_buyers: List[Tuple[WalletProfile, ScoreBreakdown]]) -> bytes:
    """Generate CSV bytes for download."""
    output = io.StringIO()
    writer = csv.writer(output)
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
    return output.getvalue().encode("utf-8")


def generate_json_bytes(token: TokenMetadata, candidates_count: int, ranked_buyers: List[Tuple[WalletProfile, ScoreBreakdown]]) -> bytes:
    """Generate JSON bytes for download."""
    report = ScannerReport(
        token=token,
        candidates_count=candidates_count,
        likely_buyers_count=len(ranked_buyers),
        buyers=[p for p, _ in ranked_buyers],
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    return report.model_dump_json(indent=2).encode("utf-8")


# ==========================================
# SIDEBAR CONTROLS
# ==========================================

st.sidebar.markdown("### ⚙️ Parameter Pemindaian")
default_mint = "7uvLyn87LSxW2GdwdEeiwmSJwQLVrcVyo7SRVcLbbtGc"  # ACAT
mint_input = st.sidebar.text_input("Solana Token Mint Address", value=default_mint, help="Masukkan Solana Base58 Mint Address (32-44 karakter)")
max_transfers_input = st.sidebar.slider("Max Transfers to Scan", min_value=10, max_value=100, value=20, step=5, help="Jumlah transfer kronologis awal dari genesis yang dianalisis")
hide_dust_input = st.sidebar.checkbox("Sembunyikan Transaksi Debu / Dust (< 1.000 token)", value=False, help="Filter dompet dengan pembelian pertama di bawah 1.000 token")

st.sidebar.markdown("---")
scan_clicked = st.sidebar.button("🔍 Mulai Analisis On-Chain", type="primary", use_container_width=True)

st.sidebar.markdown("### ℹ️ Tentang Sistem")
st.sidebar.caption(
    "EarlyBuyer Scanner (EBRS) mengidentifikasi pembeli awal sejati (bukan sekadar holder) "
    "menggunakan Solana Native RPC gratis, verifikasi mutasi saldo SOL/token (Evidence First), "
    "pelacakan modal awal (V2), serta kluster Sybil."
)

# ==========================================
# MAIN PAGE LAYOUT
# ==========================================

st.markdown('<div class="main-title">⚡ Solana EarlyBuyer Scanner</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-title">Evidence-First Early Buyer Ranking & Sybil Cluster Detection</div>', unsafe_allow_html=True)

# Trigger scan on button click
if scan_clicked:
    try:
        results = execute_pipeline(mint_input, max_transfers_input)
        st.session_state["scan_results"] = results
        st.session_state["scanned_mint"] = mint_input
    except Exception as exc:
        st.error(f"Terjadi kesalahan saat memproses token: {exc}")

# Render results from session_state
if "scan_results" in st.session_state:
    res = st.session_state["scan_results"]
    token: TokenMetadata = res["token"]
    candidates_count: int = res["candidates_count"]
    ranked_buyers: List[Tuple[WalletProfile, ScoreBreakdown]] = res["ranked_buyers"]

    # Filter dust if option selected
    if hide_dust_input:
        displayed_buyers = [item for item in ranked_buyers if item[0].first_buy_amount >= 1000.0]
    else:
        displayed_buyers = ranked_buyers

    # Detect active Sybil clusters
    sybil_clusters = {p.cluster_id for p, _ in ranked_buyers if p.cluster_id}
    has_sybils = len(sybil_clusters) > 0

    # ------------------------------------------
    # A. KARTU METRIK UTAMA
    # ------------------------------------------
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        token_name = token.name or "UNKNOWN"
        token_symbol = token.symbol or "N/A"
        st.metric(
            label="Token Teridentifikasi",
            value=f"{token_name} ({token_symbol})",
            help=f"Mint: {token.token_address}",
        )

    with col2:
        launch_str = (
            datetime.fromtimestamp(token.launch_time, tz=timezone.utc).strftime("%d %b %Y %H:%M:%S UTC")
            if token.launch_time
            else "N/A"
        )
        st.metric(
            label="Waktu Genesis Launch",
            value=launch_str,
            help=f"Confidence: {token.launch_confidence.value}",
        )

    with col3:
        st.metric(
            label="Pembeli Awal Terverifikasi",
            value=f"{len(ranked_buyers)} Wallets",
            help=f"Dari total {candidates_count} kandidat transfer.",
        )

    with col4:
        if has_sybils:
            sybil_wallets_count = sum(1 for p, _ in ranked_buyers if p.cluster_id)
            st.metric(
                label="Status Sindikat / Sybil",
                value=f"🚨 {len(sybil_clusters)} Cluster ({sybil_wallets_count} Wallets)",
                help="Terdeteksi beberapa dompet didanai oleh penyandang dana EOA yang sama!",
            )
        else:
            st.metric(
                label="Status Sindikat / Sybil",
                value="✅ Aman (0 Cluster)",
                help="Tidak ditemukan kluster dompet yang didanai sumber EOA yang sama.",
            )

    if has_sybils:
        st.markdown(
            f'<div class="sybil-alert">⚠️ PERINGATAN SYBIL: Terdeteksi {len(sybil_clusters)} kluster '
            f'dompet yang berbagi sumber pendanaan SOL awal yang sama! Periksa kolom Cluster pada tabel di bawah.</div>',
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ------------------------------------------
    # B. TABEL DATA INTERAKTIF
    # ------------------------------------------
    st.markdown("### 📋 Daftar Peringkat Early Buyer (Terurut Skor On-Chain)")

    table_data = []
    for rank, (p, _) in enumerate(displayed_buyers, start=1):
        wallet_explorer_link = f"https://solscan.io/account/{p.wallet_address}"
        tx_explorer_link = f"https://solscan.io/tx/{p.first_buy_signature}" if p.first_buy_signature else None

        funder_display = format_funder_str(p.funder_address, p.funder_type, p.cluster_id)
        age_display = format_age_str(p.wallet_age_days, p.funder_type)

        retention_pct = max(0, int(round((1.0 - p.exit_ratio) * 100)))

        table_data.append({
            "Rank": rank,
            "Wallet": p.wallet_address,
            "Solscan": wallet_explorer_link,
            "Entry": format_time_delta(p.time_after_launch),
            "First Buy Size": round(p.first_buy_amount, 2),
            "Hold %": f"{retention_pct}%",
            "Wallet Age": age_display,
            "Funder": funder_display,
            "Cluster": p.cluster_id or "-",
            "Score": int(round(p.score)),
            "Tag": "[SNIPER]" if p.is_same_block_sniper else "-",
            "Buy Tx": tx_explorer_link,
        })

    df = pd.DataFrame(table_data)

    st.dataframe(
        df,
        column_config={
            "Rank": st.column_config.NumberColumn("Rank", width="small"),
            "Wallet": st.column_config.TextColumn("Wallet Address", width="medium"),
            "Solscan": st.column_config.LinkColumn("Explorer", display_text="🔍 Solscan", width="small"),
            "Entry": st.column_config.TextColumn("First Buy", width="small"),
            "First Buy Size": st.column_config.NumberColumn("Size (Tokens)", format="%,.2f"),
            "Hold %": st.column_config.TextColumn("Retention", width="small"),
            "Wallet Age": st.column_config.TextColumn("Wallet Age", width="small"),
            "Funder": st.column_config.TextColumn("Funder / Source", width="medium"),
            "Cluster": st.column_config.TextColumn("Sybil Cluster", width="small"),
            "Score": st.column_config.ProgressColumn("EBRS Score", min_value=0, max_value=100, format="%d"),
            "Tag": st.column_config.TextColumn("Tag", width="small"),
            "Buy Tx": st.column_config.LinkColumn("Evidence Tx", display_text="🔗 Tx", width="small"),
        },
        use_container_width=True,
        hide_index=True,
    )

    if hide_dust_input and len(displayed_buyers) < len(ranked_buyers):
        st.caption(f"Menampilkan {len(displayed_buyers)} dari {len(ranked_buyers)} pembeli (disaring transaksi < 1.000 token).")

    st.markdown("---")

    # ------------------------------------------
    # C. EVIDENCE & AUDIT EXPANDER
    # ------------------------------------------
    if ranked_buyers:
        top_buyer, breakdown = ranked_buyers[0]
        with st.expander(f"🔎 Audit & Dekonstruksi Bukti Pembeli #1: {top_buyer.wallet_address}", expanded=False):
            b_col1, b_col2, b_col3, b_col4, b_col5 = st.columns(5)
            with b_col1:
                st.metric("Total Score", f"{int(round(top_buyer.score))}/100")
            with b_col2:
                st.metric("Early Entry (40%)", f"{breakdown.early_entry_score:.0f}")
            with b_col3:
                st.metric("Buy Size (25%)", f"{breakdown.buy_size_score:.0f}")
            with b_col4:
                st.metric("Accumulation (15%)", f"{breakdown.accumulation_score:.0f}")
            with b_col5:
                st.metric("Holding/Exit (20%)", f"{breakdown.holding_score:.0f}")

            st.markdown("#### Bukti On-Chain & Intelijen Dompet:")
            det_col1, det_col2 = st.columns(2)
            with det_col1:
                st.write(f"**Waktu Masuk Pertama:** `{format_time_delta(top_buyer.time_after_launch)}` setelah peluncuran")
                st.write(f"**Jumlah Pembelian Pertama:** `{top_buyer.first_buy_amount:,.2f} tokens`")
                st.write(f"**Frekuensi Pembelian:** `{top_buyer.buy_count} kali`")
                st.write(f"**Status Sniper:** `{'[SNIPER] (Same-Block Entry)' if top_buyer.is_same_block_sniper else 'Normal'}`")
                if top_buyer.first_buy_slot is not None:
                    st.write(f"**Solana Block Slot:** `{top_buyer.first_buy_slot}`")
                if top_buyer.first_buy_signature:
                    st.markdown(f"**Signature Pembelian:** [{top_buyer.first_buy_signature}](https://solscan.io/tx/{top_buyer.first_buy_signature})")

            with det_col2:
                top_age_str = format_age_str(top_buyer.wallet_age_days, top_buyer.funder_type)
                st.write(f"**Umur Dompet:** `{top_age_str}`")
                st.write(f"**Kategori Funder:** `{top_buyer.funder_type}`")
                if top_buyer.funder_address:
                    funder_url = f"https://solscan.io/account/{top_buyer.funder_address}"
                    st.markdown(f"**Alamat Penyandang Dana:** [{top_buyer.funder_address}]({funder_url})")
                if top_buyer.funding_amount_sol is not None:
                    st.write(f"**Nominal Pendanaan Awal:** `{top_buyer.funding_amount_sol} SOL`")
                if top_buyer.funding_signature:
                    fund_tx_url = f"https://solscan.io/tx/{top_buyer.funding_signature}"
                    st.markdown(f"**Signature Pendanaan Awal:** [{top_buyer.funding_signature}]({fund_tx_url})")
                if top_buyer.cluster_id:
                    st.markdown(f"**Kluster Sybil:** `[{top_buyer.cluster_id}]` (Terhubung ke dompet lain)")

    # ------------------------------------------
    # D. TOMBOL EKSPOR LAPORAN
    # ------------------------------------------
    st.markdown("### 📥 Ekspor Laporan")
    d_col1, d_col2, _ = st.columns([1, 1, 2])

    csv_data = generate_csv_bytes(ranked_buyers)
    json_data = generate_json_bytes(token, candidates_count, ranked_buyers)

    with d_col1:
        st.download_button(
            label="📄 Unduh Laporan CSV",
            data=csv_data,
            file_name=f"early_buyers_{token.symbol or 'token'}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    with d_col2:
        st.download_button(
            label="📦 Unduh Laporan JSON",
            data=json_data,
            file_name=f"early_buyers_{token.symbol or 'token'}.json",
            mime="application/json",
            use_container_width=True,
        )

else:
    st.info("👈 Masukkan alamat mint token di sidebar kiri dan klik tombol **Mulai Analisis On-Chain** untuk memulai.")
