# PRD — Solana Early Buyer Detection & Ranking System

**Project Name:** EarlyBuyer Scanner  
**Working Name:** EBRS (Early Buyer Ranking System)  
**Version:** 1.0 — MVP  
**Platform:** Solana  
**Primary Data Source:** Solscan Pro API  
**Document Type:** Product Requirements Document

---

## 1. Product Summary

EarlyBuyer Scanner adalah sistem analisis on-chain untuk menemukan wallet yang **kemungkinan melakukan pembelian token pada fase awal setelah sebuah token/memecoin diluncurkan**, kemudian menganalisis perilaku wallet tersebut dan menghasilkan **ranking berbasis metrik yang dapat diaudit**.

Sistem tidak hanya membaca daftar `Holders`. Sistem menggabungkan:

1. Historical token transfers
2. Detail transaksi
3. Perubahan saldo SOL/token
4. Program dan instruksi transaksi
5. Current holder state
6. Riwayat buy/sell wallet

Tujuan utamanya adalah mengubah data blockchain yang mentah menjadi daftar:

> **Early Buyer → Bukti transaksi → Perilaku wallet → Score → Ranking**

---

## 2. Problem Statement

Daftar holder hanya menunjukkan kondisi token saat ini.

Contoh:

```text
Launch
  ↓
Wallet A membeli
  ↓
Wallet A menjual seluruh token
  ↓
Wallet B membeli
  ↓
Wallet B masih hold
```

Jika sistem hanya membaca current holders, Wallet A dapat hilang dari analisis walaupun merupakan early buyer.

Selain itu, transfer token dari launchpad/authority ke wallet tidak otomatis berarti pembelian. Transfer tersebut dapat merupakan distribusi, claim, mint, atau mekanisme launch.

Karena itu, sistem harus membedakan:

- BUY
- SELL
- TRANSFER
- DISTRIBUTION/MINT
- UNKNOWN

sebelum melakukan ranking.

---

## 3. Tujuan Produk

### 3.1 Tujuan Utama

Menemukan dan merangking wallet yang teridentifikasi sebagai **early buyer** berdasarkan bukti aktivitas on-chain.

### 3.2 Tujuan Sekunder

Sistem harus dapat menjawab:

- Siapa yang membeli paling awal?
- Berapa lama setelah launch mereka membeli?
- Berapa besar pembelian awal?
- Berapa kali wallet melakukan pembelian?
- Berapa banyak token yang masih dipegang?
- Berapa banyak yang sudah dijual?
- Transaksi pembelian dilakukan melalui program/DEX apa?
- Apakah wallet melakukan akumulasi atau distribusi?
- Apa dasar score yang membuat wallet berada pada ranking tertentu?

### 3.3 Non-Goals

Versi MVP **tidak bertujuan untuk**:

- memprediksi harga token;
- menjamin sebuah wallet akan profitable;
- menyatakan wallet tertentu dimiliki oleh orang yang sama;
- memberikan rekomendasi beli/jual;
- menjadi bot trading otomatis;
- mendeteksi identitas dunia nyata pemilik wallet.

---

## 4. Target User

### Primary User

Trader/analyst yang melakukan penelitian awal terhadap memecoin Solana dan ingin mengetahui siapa yang masuk pada fase awal.

### Secondary User

Peneliti on-chain yang ingin membuat dataset historical wallet behavior.

---

## 5. Core User Story

### User Story 1 — Scan Token

> Sebagai user, saya memasukkan mint address sebuah token dan ingin mendapatkan daftar early buyer.

### User Story 2 — Verify Buyer

> Sebagai user, saya ingin melihat bukti transaksi yang menyebabkan sebuah wallet diklasifikasikan sebagai BUY.

### User Story 3 — Compare Buyers

> Sebagai user, saya ingin membandingkan beberapa early buyer berdasarkan waktu masuk, ukuran posisi, aktivitas buy/sell, dan current holding.

### User Story 4 — Rank Buyers

> Sebagai user, saya ingin mendapatkan ranking berdasarkan scoring system yang transparan.

---

# 6. Product Flow

```text
TOKEN MINT ADDRESS
        │
        ▼
GET TOKEN METADATA
        │
        ▼
GET HISTORICAL TRANSFERS
        │
        ▼
GENERATE CANDIDATE WALLETS
        │
        ▼
REMOVE / FLAG NON-BUY EVENTS
        │
        ▼
GET TRANSACTION DETAILS
        │
        ▼
CLASSIFY TRANSACTIONS
        │
        ├── BUY
        ├── SELL
        ├── TRANSFER
        ├── DISTRIBUTION
        └── UNKNOWN
        │
        ▼
BUILD WALLET PROFILE
        │
        ├── First Buy
        ├── Buy Size
        ├── Buy Count
        ├── Sell Amount
        ├── Current Holding
        └── Exit Ratio
        │
        ▼
CALCULATE SCORE
        │
        ▼
RANK EARLY BUYERS
        │
        ▼
DISPLAY REPORT
```

---

# 7. Functional Requirements

## FR-01 — Token Input

Input:

```text
mint_address
```

Validation:

- valid Solana address;
- token dapat ditemukan;
- token memiliki historical activity.

Output:

```text
token_address
name
symbol
decimals
creator
launch_time
```

---

## FR-02 — Token Metadata

Sistem mengambil metadata token dan menentukan timestamp referensi launch.

Jika timestamp launch tidak dapat ditentukan secara reliable, sistem harus memberi status:

```text
launch_time_confidence = LOW
```

dan tidak boleh menyajikan waktu relatif seolah-olah pasti.

---

## FR-03 — Historical Token Transfers

Sistem mengambil transfer token dengan:

```text
sort_by = block_time
sort_order = asc
```

Data minimum:

```text
transaction_signature
block_time
from_address
to_address
token_address
amount
decimals
activity_type
```

---

## FR-04 — Candidate Wallet Generation

Dari historical transfers, sistem membuat daftar wallet kandidat.

Contoh:

```text
Transfer #1 → Wallet A
Transfer #2 → Wallet B
Transfer #3 → Wallet C
...
```

Wallet dapat memiliki banyak event, tetapi hanya dibuat satu profile per wallet.

---

## FR-05 — Non-Buy Filtering

Sistem harus mendeteksi atau memberi flag pada alamat yang berperan sebagai:

- mint/authority;
- launchpad;
- distributor;
- liquidity pool;
- AMM/pool;
- burn address;
- known system/service address.

Event dari alamat tersebut tidak otomatis dianggap BUY.

---

## FR-06 — Transaction Detail Analysis

Untuk candidate transaction, sistem mengambil detail transaksi.

Data yang dianalisis:

```text
block_time
signer
sol_bal_change
token_bal_change
programs_involved
parsed_instructions
status
priority_fee
```

---

## FR-07 — Transaction Classification

Setiap event diklasifikasikan menjadi:

### BUY

Indikasi umum:

```text
wallet spends SOL / WSOL / quote asset
        +
wallet receives target token
        +
DEX/AMM/launch program involvement
```

### SELL

Indikasi umum:

```text
wallet sends target token
        +
wallet receives SOL / WSOL / quote asset
```

### TRANSFER

```text
wallet → wallet
```

tanpa indikasi swap.

### DISTRIBUTION

Token diterima dari authority/launchpad/distributor tanpa bukti pembayaran/swap.

### UNKNOWN

Data transaksi tidak cukup untuk klasifikasi otomatis.

---

## FR-08 — First Buy Detection

Untuk setiap wallet:

```text
first_buy_time
first_buy_signature
first_buy_amount
time_after_launch
```

Contoh:

```text
Launch:       12:00:00
First Buy:    12:03:17
Time After:   +3m17s
```

---

## FR-09 — Buy Aggregation

Untuk setiap wallet:

```text
buy_count
total_buy_amount
first_buy_amount
largest_buy
average_buy
```

Jika denominasi quote asset tidak dapat dihitung dengan reliable, sistem menyimpan nilai token/SOL mentah dan memberi confidence flag.

---

## FR-10 — Sell Analysis

Sistem menghitung:

```text
sell_count
total_sell_amount
first_sell_time
estimated_exit_amount
exit_ratio
```

Rumus dasar:

```text
exit_ratio =
total_estimated_sold /
total_estimated_acquired
```

Nilai dapat diberi status `estimated` karena transfer token tidak selalu identik dengan economic sell.

---

## FR-11 — Current Holding

Sistem mengambil current holder state.

Data:

```text
current_amount
current_value_usd
holder_rank
holder_percentage
```

---

## FR-12 — Wallet Profile

Setiap wallet memiliki profile:

```text
wallet
first_buy_time
time_after_launch
first_buy_amount
total_buy_amount
buy_count
sell_count
total_sell_amount
current_holding
exit_ratio
holder_rank
```

---

# 8. Scoring System

## 8.1 Prinsip

Score digunakan sebagai **alat pengurutan internal**, bukan ukuran "wallet terbaik".

Score harus transparan dan setiap komponen dapat ditelusuri.

---

## 8.2 Initial Score Model

MVP menggunakan empat komponen:

```text
Early Entry Score
+
Buy Size Score
+
Accumulation Score
+
Holding/Exit Score
```

Contoh bobot awal:

```text
Early Entry       40%
Buy Size          25%
Accumulation      15%
Holding/Exit      20%
```

Total:

```text
0–100
```

Bobot ini adalah **parameter desain MVP**, bukan klaim bahwa bobot tersebut terbukti optimal.

---

## 8.3 Early Entry Score

Contoh initial rule:

| Time After Launch | Score |
|---|---:|
| 0–2 min | 100 |
| >2–5 min | 90 |
| >5–10 min | 75 |
| >10–20 min | 55 |
| >20–60 min | 30 |
| >60 min | 10 |

Parameter harus configurable.

---

## 8.4 Buy Size Score

Gunakan percentile terhadap buyer pada token yang sama.

Contoh:

```text
P99 → 100
P95 → 90
P90 → 80
P75 → 60
P50 → 40
```

Tujuannya agar ukuran pembelian tidak bergantung pada nominal absolut.

---

## 8.5 Accumulation Score

Input:

```text
buy_count
```

Contoh:

```text
1 buy  → baseline
2 buy  → higher
3+ buy → higher
```

MVP tidak boleh menganggap lebih banyak buy otomatis berarti lebih baik. Komponen ini hanya menggambarkan pola akumulasi.

---

## 8.6 Holding/Exit Score

Score didasarkan pada current holding dan estimated exit ratio.

Contoh:

```text
high current holding
→ higher retention score

fully exited
→ lower retention score
```

Tetapi sistem harus menampilkan metrik mentah juga agar user tidak hanya melihat score.

---

# 9. Ranking Output

Output utama:

```text
Rank
Wallet
First Buy
Time After Launch
First Buy Size
Total Buy
Buy Count
Current Holding
Exit Ratio
Score
Confidence
```

Contoh:

```text
1  7xK...abc  +2m14s  $1,240  4 buys  71% hold  91  HIGH
2  9t2...xyz  +3m02s    $850  2 buys  84% hold  88  HIGH
3  4Fs...qwe  +4m31s  $2,100  1 buy   35% hold  82  MEDIUM
```

---

# 10. Confidence System

Karena blockchain event tidak selalu dapat diterjemahkan langsung menjadi economic intent, setiap classification memiliki confidence.

### HIGH

Bukti kuat:

```text
quote asset out
+
target token in
+
DEX/AMM interaction
```

### MEDIUM

Pola cukup kuat tetapi sebagian data tidak lengkap.

### LOW

Hanya terdapat transfer token atau bukti tidak cukup.

### UNKNOWN

Tidak dapat diklasifikasikan.

---

# 11. Data Architecture

## Table: tokens

```text
id
token_address
name
symbol
decimals
creator
launch_time
launch_confidence
created_at
```

## Table: transactions

```text
id
signature
token_address
block_time
wallet
classification
confidence
sol_change
token_change
programs
raw_data
```

## Table: wallet_profiles

```text
id
wallet_address
token_address
first_buy_time
first_buy_signature
first_buy_amount
total_buy_amount
buy_count
sell_count
total_sell_amount
current_holding
exit_ratio
holder_rank
score
confidence
```

## Table: wallet_events

```text
id
wallet_address
token_address
signature
timestamp
event_type
amount
quote_amount
confidence
```

---

# 12. API Requirements

Primary endpoints:

```text
GET /token/transfer
GET /token/holders
GET /transaction/detail
```

The token-transfer endpoint supports pagination and chronological sorting. The holder endpoint provides current holder amount, rank, percentage and value. The transaction-detail endpoint provides SOL/token balance changes, programs, parsed instructions and signers.

---

# 13. Technical Architecture

```text
Python
   │
   ├── API Client
   │
   ├── Data Collector
   │
   ├── Candidate Generator
   │
   ├── Transaction Classifier
   │
   ├── Wallet Analyzer
   │
   ├── Scoring Engine
   │
   └── Report Generator
```

Recommended MVP stack:

```text
Python
requests/httpx
Pydantic
SQLite
pandas
```

Optional later:

```text
FastAPI
PostgreSQL
Redis
React/Next.js
```

---

# 14. Project Structure

```text
early-buyer-scanner/
│
├── main.py
├── config.py
├── requirements.txt
│
├── api/
│   └── solscan.py
│
├── collectors/
│   ├── token.py
│   ├── transfers.py
│   ├── holders.py
│   └── transactions.py
│
├── analyzers/
│   ├── candidate_generator.py
│   ├── transaction_classifier.py
│   ├── wallet_analyzer.py
│   └── sell_analyzer.py
│
├── scoring/
│   └── scorer.py
│
├── models/
│   └── schemas.py
│
├── storage/
│   └── database.py
│
└── output/
    ├── buyers.csv
    └── report.json
```

---

# 15. MVP Scope

### Included

- Mint address input
- Token metadata
- Historical transfers
- Candidate wallet generation
- Transaction classification
- First-buy detection
- Buy aggregation
- Sell analysis
- Current holder lookup
- Score
- Ranking
- CSV/JSON report

### Not Included

- Automated trading
- Price prediction
- AI prediction model
- Wallet owner identification
- Cross-chain analysis
- Telegram/Discord alerts
- Real-time execution

---

# 16. Success Criteria

MVP dianggap berhasil jika:

### SC-01

User dapat memasukkan satu mint address.

### SC-02

Sistem dapat mengambil historical token transfers.

### SC-03

Sistem dapat menghasilkan candidate wallets.

### SC-04

Sistem dapat membedakan setidaknya:

```text
BUY
TRANSFER
DISTRIBUTION
UNKNOWN
```

untuk pola transaksi yang didukung.

### SC-05

Sistem dapat menentukan first detected buy untuk wallet yang terklasifikasi sebagai buyer.

### SC-06

Sistem menghasilkan ranking yang reproducible.

### SC-07

Setiap ranking memiliki bukti transaksi/signature yang dapat diperiksa.

### SC-08

User dapat mengekspor hasil ke CSV/JSON.

---

# 17. Important Limitations

## 17.1 Transfer ≠ Buy

Menerima token tidak otomatis berarti membeli.

## 17.2 Current Holder ≠ Historical Buyer

Wallet yang pernah menjadi early buyer dapat sudah menjual seluruh token.

## 17.3 Wallet ≠ Person

Satu wallet tidak membuktikan identitas seseorang.

## 17.4 Shared Funding ≠ Same Owner

Wallet yang didanai sumber yang sama tidak otomatis dimiliki entitas yang sama.

## 17.5 Score ≠ Profitability

Score hanya merangkum karakteristik on-chain yang dipilih sistem.

---

# 18. Future Development

## V2 — Wallet Intelligence

Tambahkan:

- funding source;
- wallet age;
- historical token activity;
- previous memecoin participation;
- recurring wallet patterns.

## V3 — Wallet Cluster

Mendeteksi:

```text
Funding relationship
Transfer relationship
Timing similarity
Trading similarity
```

Output:

```text
Cluster A
├── Wallet 1
├── Wallet 2
└── Wallet 3
```

Dengan label hubungan, bukan klaim kepemilikan.

## V4 — Historical Backtesting

Simpan data dari banyak token:

```text
Token A
Token B
Token C
...
```

Kemudian analisis:

```text
early buyer behavior
→ subsequent token performance
```

Tujuannya bukan membuat prediksi otomatis pada MVP, tetapi membangun dataset yang dapat digunakan untuk penelitian lebih lanjut.

---

# 19. Key Metrics

Dashboard nantinya dapat menampilkan:

```text
Token Age
Holder Count
Early Buyer Count
Median First Buy Time
Top Early Buyer Buy Size
Total Early Buyer Volume
Current Retention
Average Exit Ratio
```

---

# 20. Example Final Report

```text
=================================================
EARLY BUYER SCANNER
=================================================

Token:
MOJO

Launch:
20 Sep 2026 12:00:00

Candidates:
247

Likely Buyers:
37

-------------------------------------------------
RANK  WALLET       FIRST BUY  SIZE    HOLD  SCORE
-------------------------------------------------
1     7xK...abc    +2m14s     $1,240  71%    91
2     9t2...xyz    +3m02s       $850  84%    88
3     4Fs...qwe    +4m31s     $2,100  35%    82
-------------------------------------------------

Wallet Detail:
7xK...abc

Classification: BUY
Confidence: HIGH

First Buy:
+2m14s

First Buy Size:
$1,240

Buy Count:
4

Current Holding:
71%

Estimated Exit:
29%

Evidence:
Transaction Signature: XXXXX
```

---

# 21. Development Roadmap

```text
PHASE 1
│
├── Solscan API client
├── Token transfer collector
└── Basic database
       │
       ▼
PHASE 2
│
├── Candidate generator
├── Transaction detail collector
└── BUY classifier
       │
       ▼
PHASE 3
│
├── Wallet analyzer
├── Holder integration
└── Sell analysis
       │
       ▼
PHASE 4
│
├── Scoring engine
├── Ranking
└── CSV/JSON report
       │
       ▼
PHASE 5
│
├── Web dashboard
└── Historical database
```

---

# 22. Definition of Done — MVP

MVP selesai ketika:

- [ ] Mint address dapat dimasukkan.
- [ ] Token metadata berhasil diperoleh.
- [ ] Historical transfers berhasil dikumpulkan.
- [ ] Candidate wallets berhasil dibuat.
- [ ] Known non-buy sources dapat difilter/ditandai.
- [ ] Transaction detail dapat dianalisis.
- [ ] BUY/SELL/TRANSFER/DISTRIBUTION/UNKNOWN dapat dihasilkan.
- [ ] First buy dapat ditentukan.
- [ ] Buy/sell metrics dapat dihitung.
- [ ] Current holder state dapat diambil.
- [ ] Score dapat dihitung.
- [ ] Wallet dapat diranking.
- [ ] Setiap hasil memiliki transaction evidence.
- [ ] Report CSV/JSON dapat dibuat.
- [ ] Error/rate-limit API ditangani.
- [ ] Hasil dapat direproduksi dari input dan parameter yang sama.

---

# 23. Prinsip Desain Utama

> **Evidence first, score second.**

Sistem harus selalu menyimpan bukti transaksi terlebih dahulu.

Urutannya:

```text
RAW ON-CHAIN DATA
       ↓
TRANSACTION CLASSIFICATION
       ↓
WALLET METRICS
       ↓
SCORE
       ↓
RANK
```

Bukan:

```text
HOLDER RANK
   ↓
ASSUME EARLY BUYER
```

Karena tujuan proyek adalah menemukan **early buyer berdasarkan aktivitas historis**, bukan sekadar mencari holder terbesar.
