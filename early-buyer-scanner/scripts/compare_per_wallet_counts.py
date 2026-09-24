import csv
from collections import Counter

# Load CSV
with open('output/lifecycle_fixed_v2.csv') as f:
    csv_rows = list(csv.DictReader(f))

# Load exported buys/sells
with open('output/audit_all_buys.csv') as f:
    exported_buys = list(csv.DictReader(f))
with open('output/audit_all_sells.csv') as f:
    exported_sells = list(csv.DictReader(f))

buy_counts_by_wallet = Counter(r['Wallet Address'] for r in exported_buys)
sell_counts_by_wallet = Counter(r['Wallet Address'] for r in exported_sells)

print(f"Total exported buys: {len(exported_buys)}")
print(f"Total exported sells: {len(exported_sells)}")

buy_diffs = []
sell_diffs = []

for r in csv_rows:
    w = r['Wallet Address']
    csv_b = int(r['Buy Count'])
    csv_s = int(r['Sell Count'])
    exp_b = buy_counts_by_wallet.get(w, 0)
    exp_s = sell_counts_by_wallet.get(w, 0)
    
    if csv_b != exp_b:
        buy_diffs.append((w, csv_b, exp_b))
    if csv_s != exp_s:
        sell_diffs.append((w, csv_s, exp_s))

print(f"\nWallets with buy count differences (CSV vs exported): {len(buy_diffs)}")
for d in buy_diffs:
    print(f"  Wallet: {d[0]} | CSV Buy Count: {d[1]} | Exported Buy Count: {d[2]}")

print(f"\nWallets with sell count differences (CSV vs exported): {len(sell_diffs)}")
for d in sell_diffs:
    print(f"  Wallet: {d[0]} | CSV Sell Count: {d[1]} | Exported Sell Count: {d[2]}")
