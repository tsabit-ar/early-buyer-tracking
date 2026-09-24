import sqlite3

def check_2idab():
    conn = sqlite3.connect("early_buyer.db")
    cur = conn.cursor()
    cur.execute("SELECT signature, classification, token_change, sol_change FROM transactions WHERE wallet = '2iDAbmU7i5bUiCkLr7GKHJrwbS2FANQbZJ81fXTbwjap'")
    for r in cur.fetchall():
        print(r)

if __name__ == "__main__":
    check_2idab()
