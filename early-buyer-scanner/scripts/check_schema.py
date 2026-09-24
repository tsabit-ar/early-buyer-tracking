import sqlite3

conn = sqlite3.connect('early_buyer.db')
c = conn.cursor()
c.execute("SELECT sql FROM sqlite_master WHERE name = 'wallet_profiles'")
print(c.fetchone()[0])
