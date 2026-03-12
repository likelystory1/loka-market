import requests
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

BASE = "https://api.lokamc.com"
DB = "market_history.db"

THREADS = 2
START_PAGE = 19250


def init_db():

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")

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


def get_total_pages():

    r = requests.get(f"{BASE}/completed_market_orders?page=0")

    data = r.json()

    pages = data["page"]["totalPages"]

    print("Total pages:", pages)

    return pages


def process_page(page):

    retries = 5

    while retries > 0:

        try:

            conn = sqlite3.connect(DB)
            c = conn.cursor()

            url = f"{BASE}/completed_market_orders?page={page}"

            time.sleep(0.05)

            r = requests.get(url)

            if r.status_code == 429:

                print("Rate limited page", page)

                time.sleep(3)

                retries -= 1

                continue

            if r.status_code != 200:

                print("HTTP error", page, r.status_code)

                time.sleep(2)

                retries -= 1

                continue

            data = r.json()

            orders = data["_embedded"]["completed_market_orders"]

            rows = []

            for o in orders:

                rows.append((

                    o["id"],
                    o["type"],
                    o["price"],

                    o.get("buyerId"),
                    o.get("ownerId"),

                    None,
                    None,

                    int(time.time())

                ))

            c.executemany("""

            INSERT OR IGNORE INTO trades
            (id,item,price,buyer_uuid,seller_uuid,buyer_name,seller_name,ts)

            VALUES(?,?,?,?,?,?,?,?)

            """, rows)

            conn.commit()
            conn.close()

            print("Imported page", page)

            return

        except Exception as e:

            print("Error page", page, e)

            time.sleep(2)

            retries -= 1


def main():

    init_db()

    pages = get_total_pages()

    print("Resuming from page", START_PAGE)

    with ThreadPoolExecutor(max_workers=THREADS) as executor:

        executor.map(process_page, range(START_PAGE, pages))


if __name__ == "__main__":

    start = time.time()

    main()

    print("Finished in", round(time.time() - start, 2), "seconds")
