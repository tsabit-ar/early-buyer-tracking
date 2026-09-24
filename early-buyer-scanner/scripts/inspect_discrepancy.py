import csv
from collections import Counter

with open('output/audit_all_sells.csv', encoding='utf-8') as f:
    sells_exported = list(csv.DictReader(f))
with open('output/audit_all_buys.csv', encoding='utf-8') as f:
    buys_exported = list(csv.DictReader(f))

print('Total rows in audit_all_sells.csv:', len(sells_exported))
print('Total rows in audit_all_buys.csv :', len(buys_exported))

sells_sigs = [r['Signature'] for r in sells_exported]
buys_sigs = [r['Signature'] for r in buys_exported]

print('Unique signatures in audit_all_sells.csv:', len(set(sells_sigs)))
print('Unique signatures in audit_all_buys.csv :', len(set(buys_sigs)))

sell_sig_counts = Counter(sells_sigs)
dup_sells = {k: v for k, v in sell_sig_counts.items() if v > 1}
print('Duplicate signatures in sells:', len(dup_sells))
for sig, count in list(dup_sells.items())[:10]:
    rows = [r for r in sells_exported if r['Signature'] == sig]
    print(f'  Sig: {sig[:20]}... Count: {count}')
    for r in rows:
        print(f"    Wallet: {r['Wallet Address'][:15]}... Tokens: {r['Token Amount Out']}")

buy_sig_counts = Counter(buys_sigs)
dup_buys = {k: v for k, v in buy_sig_counts.items() if v > 1}
print('Duplicate signatures in buys:', len(dup_buys))
for sig, count in list(dup_buys.items())[:10]:
    rows = [r for r in buys_exported if r['Signature'] == sig]
    print(f'  Sig: {sig[:20]}... Count: {count}')
    for r in rows:
        print(f"    Wallet: {r['Wallet Address'][:15]}... Tokens: {r['Token Amount In']}")
