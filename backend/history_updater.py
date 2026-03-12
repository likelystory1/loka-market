from flask import Flask, jsonify
from flask_cors import CORS
from database import connect

app=Flask(__name__)

CORS(app)


@app.route("/api/items")

def items():

    conn=connect()

    c=conn.cursor()

    rows=c.execute("""

    SELECT item,
           AVG(price) as avg_price,
           COUNT(*) as volume

    FROM trades
    GROUP BY item

    ORDER BY volume DESC

    """).fetchall()

    return jsonify([dict(r) for r in rows])


@app.route("/api/history/<item>")

def history(item):

    conn=connect()

    c=conn.cursor()

    rows=c.execute("""

    SELECT price,ts
    FROM trades
    WHERE item=?
    ORDER BY ts

    """,(item,)).fetchall()

    return jsonify([dict(r) for r in rows])


@app.route("/api/orderbook/<item>")

def orderbook(item):

    conn=connect()

    c=conn.cursor()

    rows=c.execute("""

    SELECT price,
           COUNT(*) as volume

    FROM trades
    WHERE item=?
    GROUP BY price
    ORDER BY price

    """,(item,)).fetchall()

    return jsonify([dict(r) for r in rows])


@app.route("/api/stats/<item>")

def stats(item):

    conn=connect()

    c=conn.cursor()

    row=c.execute("""

    SELECT
        MIN(price) as low,
        MAX(price) as high,
        AVG(price) as avg,
        COUNT(*) as trades

    FROM trades
    WHERE item=?

    """,(item,)).fetchone()

    return jsonify(dict(row))


app.run(port=5000)
