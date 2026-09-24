import json
import sqlite3

def check_cache():
    conn = sqlite3.connect("early_buyer.db")
    cur = conn.cursor()
    cur.execute("SELECT endpoint, response_json FROM api_cache WHERE endpoint LIKE '%transaction%' LIMIT 3")
    for ep, resp in cur.fetchall():
        data = json.loads(resp)
        print("Endpoint:", ep)
        if isinstance(data, dict):
            d = data.get("data", {})
            tbc = d.get("token_bal_change") or d.get("token_balance_change") or []
            print(f"  len tbc: {len(tbc)}")
            for x in tbc[:3]:
                print("    tbc item:", x)

if __name__ == "__main__":
    check_cache()
