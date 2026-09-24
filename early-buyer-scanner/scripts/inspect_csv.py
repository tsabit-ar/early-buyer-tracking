import csv

wallets = [
    "4oMMbUFZ83T2a6MshfjZwxcsTZwYSkUCRt8FVjbmL1d3",
    "6pJXs9kq6rMZwwy6z2HDc93yXbUeu2rWQnLZURwKhk7G",
    "EWjeKsM6BT6pheDaqwXq8k3XxBPNxfAmKAhzeaDudBbd",
    "9Nhh5D1YrPWY3PRvZqgfPSrsMtpiEHPzKSEX4wdpicLj",
    "6XLbzQoWaF9TrE3MDyHKa6p8LDv685hsmw1xwr2iDzXv",
]

with open(r"c:\Me\CRYPTO PROJECT [MEMECOIN]\screener-wallet\early-buyer-scanner\output\lifecycle_fixed_v2.csv", mode="r", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    rows = list(reader)

print(f"Total rows in CSV: {len(rows)}")
found_wallets = {r["Wallet Address"]: r for r in rows if r["Wallet Address"] in wallets}

fields_to_print = [
    "Rank", "Wallet Address", "First Buy Amount", "Total Buy Amount", "Buy Count",
    "Sell Count", "Total Sell Amount", "Current Holding", "Exit Ratio",
    "Transfer In Amount", "Transfer Out Amount", "Net Transfer Amount", "Burn Amount",
    "Unknown In Amount", "Unknown Out Amount", "Unknown Transaction Count",
    "Reconciliation Difference", "Reconciliation Status",
    "Lifecycle Complete", "Lifecycle Truncated", "Confidence"
]

for w in wallets:
    r = found_wallets.get(w)
    if r:
        print(f"\n=======================================================")
        print(f"WALLET: {w} (Rank: {r['Rank']})")
        print(f"=======================================================")
        for fld in fields_to_print:
            print(f"  {fld:26s}: {r.get(fld, 'N/A')}")

# Check summary statistics of all 112 buyers
statuses = set(r["Reconciliation Status"] for r in rows)
completes = set(r["Lifecycle Complete"] for r in rows)
truncated = set(r["Lifecycle Truncated"] for r in rows)
confidences = set(r["Confidence"] for r in rows)

print("\n=======================================================")
print("GLOBAL CSV RECONCILIATION SUMMARY (112 BUYERS)")
print("=======================================================")
print(f"Reconciliation Statuses: {statuses}")
print(f"Lifecycle Completes: {completes}")
print(f"Lifecycle Truncated: {truncated}")
print(f"Confidences: {confidences}")

mismatches = [r for r in rows if r["Reconciliation Status"] == "MISMATCH"]
incompletes = [r for r in rows if r["Reconciliation Status"] == "INCOMPLETE"]
truncs = [r for r in rows if r["Lifecycle Truncated"] == "YES"]
unknowns = [r for r in rows if float(r.get("Unknown Out Amount", 0)) > 0 or int(r.get("Unknown Transaction Count", 0)) > 0]

print(f"Total Mismatches : {len(mismatches)}")
print(f"Total Incompletes: {len(incompletes)}")
print(f"Total Truncated  : {len(truncs)}")
print(f"Total with UNKNOWN: {len(unknowns)}")
