import os
from contextlib import contextmanager
from decimal import Decimal
import psycopg
from psycopg.rows import dict_row


class ApiError(Exception):
    def __init__(self, code, message, status=400, details=None):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details


@contextmanager
def transaction():
    with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row) as conn:
        yield conn


def decimal_number(value):
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
        raise ValueError('Expected a JSON number')
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Number must be finite')
    return result
