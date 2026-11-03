import os
import psycopg
from psycopg.rows import dict_row


def connect():
    return psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row)


def lock(db, booking_id, wait=True):
    name = 'pg_advisory_lock' if wait else 'pg_try_advisory_lock'
    return db.execute(f'SELECT {name}(hashtextextended(%s,0)) AS locked', (str(booking_id),)).fetchone()['locked']
