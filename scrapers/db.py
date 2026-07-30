"""
Database connection factory.
Uses psycopg2 directly against the Supabase PostgreSQL endpoint.
"""
import psycopg2
import psycopg2.extras
from loguru import logger
from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME


def get_connection():
    """Return a new psycopg2 connection. Caller is responsible for closing."""
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=DB_NAME,
        sslmode="require",
        connect_timeout=15,
    )
    conn.autocommit = False
    return conn


def execute_batch(conn, sql: str, records: list, page_size: int = 100):
    """
    Insert/upsert a list of dicts using psycopg2.extras.execute_batch.
    Commits after each page. Returns count of rows processed.
    """
    if not records:
        return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, records, page_size=page_size)
    conn.commit()
    return len(records)


def fetch_all_fingerprints(conn, table: str, key_col: str) -> dict:
    """
    Load the full fingerprint table into memory as {key: hash}.
    Used to diff against freshly fetched records.
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT {key_col}, fingerprint_hash FROM {table}")
        rows = cur.fetchall()
    return {row[0]: row[1] for row in rows}
