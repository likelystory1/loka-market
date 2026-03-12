import sqlite3

DB="market_history.db"

def connect():

    conn=sqlite3.connect(DB)

    conn.row_factory=sqlite3.Row

    return conn


def init_db():

    conn=connect()

    c=conn.cursor()

    c.execute("""

    CREATE TABLE IF NOT EXISTS trades(

        id TEXT PRIMARY KEY,
        item TEXT,
        price REAL,
        buyer_uuid TEXT,
        seller_uuid TEXT,
        buyer_name TEXT,
        seller_name TEXT,
        ts INTEGER

    )

    """)

    conn.commit()

    conn.close()
