# Early Buyer Tracking / Scanner (EBRS)

Solana On-Chain Early Buyer Detection & Ranking System based on Product Requirements Document (PRD).

## Core Principles
1. **Evidence First, Score Second**: Never assume a transaction is a BUY without verifying quote asset (SOL/WSOL) outflows, token balance inflows, and DEX/AMM program involvement.
2. **API Resilience & Caching**: SQLite disk caching for Solscan Pro API calls, exponential backoff with jitter on HTTP 429/5xx, and disk persistence. No duplicate queries for signatures already stored in the database.
3. **Modular Architecture**: Clean separation between collectors, analyzers, scoring engine, storage, and models.

## Project Structure
```text
early-buyer-scanner/
├── api/
│   └── solscan.py                 # Resilient Solscan Pro client with SQLite caching
├── collectors/
│   ├── token.py                   # Metadata & launch time resolution
│   ├── transfers.py               # Historical transfer collector with pagination
│   └── transactions.py            # SQLite-first transaction detail collector
├── analyzers/
│   ├── candidate_generator.py     # Non-buy filtering (FR-05) & candidate extraction
│   └── transaction_classifier.py  # Evidence-first BUY/SELL/TRANSFER/DISTRIBUTION classifier
├── models/
│   └── schemas.py                 # Pure Pydantic v2 schemas & domain enums
├── storage/
│   └── database.py                # SQLite WAL mode, schema tables, SHA-256 caching
├── tests/
│   ├── test_foundation.py         # Phase 1 unit test suite
│   ├── test_phase2.py             # Phase 2 unit test suite
│   └── test_phase3.py             # Phase 3 unit test suite
├── config.py                      # Solana constants, DEX program IDs & scoring defaults
└── requirements.txt               # Dependencies
```

## Current Status
- [x] **Phase 1: Foundation** (Config, Schemas, SQLite WAL & Caching, Solscan Client)
- [x] **Phase 2: Data Collection & Candidate Generation** (Address validation, transfers, candidate filtering & extraction)
- [x] **Phase 3: Transaction Detail Collector & Classification Engine** (Batch transaction retrieval, Evidence-first classifier)
- [x] **Phase 4: Wallet Analysis, Scoring Engine, CLI Runner & Reporting** (Holders, profiler, sell analyzer, scoring, ASCII report & CSV/JSON export)

## Running Tests
To run all automated tests (40/40 passing):
```bash
python -c "import pytest, sys; sys.exit(pytest.main(['tests', '-v', '-o', 'cache_dir=.pytest_cache']))"
```

## Running the CLI Scanner
### Live Mode:
```bash
python main.py --mint <SOLANA_MINT_ADDRESS> --max-transfers 200 --export-csv --export-json
```

### Offline Demonstration (Mock Mode):
```bash
python main.py --mock
```
