import sqlite3
import json

conn = sqlite3.connect('early_buyer.db')
c = conn.cursor()
c.execute("""
    SELECT cache_key, params_json, response_json, created_at 
    FROM api_cache 
    WHERE endpoint = 'rpc:getSignaturesForAddress' 
    ORDER BY created_at DESC 
    LIMIT 1
""")
row = c.fetchone()
params = json.loads(row[1])['params']
mint_addr = params[0]
cursor_opts = params[1] if len(params) > 1 else {}
resp = json.loads(row[2])

print('=== DIAGNOSIS OF ACTIVE PAGINATION ===')
print('Mint Address           :', mint_addr)
print('Current Cursor (before):', cursor_opts.get('before'))
print('Latest Cached Request  :', row[3], 'UTC')
if resp and isinstance(resp, list) and len(resp) > 0:
    first_sig = resp[0]
    last_sig = resp[-1]
    print('Batch Size             :', len(resp))
    print('Batch Newest Slot      :', first_sig.get('slot'))
    print('Batch Newest Signature :', first_sig.get('signature'))
    print('Batch Oldest Slot      :', last_sig.get('slot'))
    print('Batch Oldest Signature :', last_sig.get('signature'))
    print('Batch Oldest BlockTime :', last_sig.get('blockTime'))

# Total count for this mint
c.execute("SELECT count(*) FROM api_cache WHERE params_json LIKE ?", (f'%{mint_addr}%',))
total_reqs = c.fetchone()[0]
print('Total Iterations/Pages :', total_reqs)
print('Total Signatures Scanned:', total_reqs * 1000)
