import sqlite3
import statistics

DB = "market_history.db"


def compute_fmv(item):

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    rows = c.execute("""
    SELECT price
    FROM trades
    WHERE item=?
    ORDER BY rowid DESC
    LIMIT 200
    """,(item,)).fetchall()

    conn.close()

    prices=[r[0] for r in rows]

    if len(prices)==0:
        return None

    median=statistics.median(prices)

    low=min(prices)
    high=max(prices)

    fair=round(median,2)

    return {
        "fair_value":fair,
        "low":low,
        "high":high,
        "sample_size":len(prices)
    }
