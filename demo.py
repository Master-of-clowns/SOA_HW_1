"""Один короткий сценарий: вход → товар → промокод → заказ → отмена → SELECT."""
import json
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from uuid import uuid4
from service.common import transaction


def call(method, path, body=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = Request('http://127.0.0.1:8000' + path, method=method, headers=headers,
                      data=json.dumps(body).encode() if body is not None else None)
    with urlopen(request) as response:
        print(method, path, '→', response.status)
        return json.loads(response.read())


def login(role):
    credentials = {'username': 'demo_' + uuid4().hex, 'password': 'demo-password-only'}
    call('POST', '/auth/register', {**credentials, 'role': role})
    return call('POST', '/auth/login', credentials)['access_token']


seller, buyer = login('SELLER'), login('USER')
product = call('POST', '/products', {'name': 'Книга', 'price': 100, 'stock': 10,
               'category': 'books', 'status': 'ACTIVE'}, seller)
now = datetime.now(timezone.utc)
promo = call('POST', '/promo-codes', {'code': 'DEMO_' + uuid4().hex[:10].upper(),
             'discount_type': 'PERCENTAGE', 'discount_value': 10, 'min_order_amount': 100,
             'max_uses': 5, 'valid_from': (now - timedelta(days=1)).isoformat(),
             'valid_until': (now + timedelta(days=1)).isoformat()}, seller)
order = call('POST', '/orders', {'items': [{'product_id': product['id'], 'quantity': 2}],
             'promo_code': promo['code']}, buyer)
print('Стоимость:', order['total_amount'], 'Скидка:', order['discount_amount'])
assert order['total_amount'] == 180
with transaction() as db:
    print('SELECT id, stock FROM products WHERE id = ...')
    print(db.execute('SELECT id,stock FROM products WHERE id=%s', (product['id'],)).fetchone())
call('POST', '/orders/' + order['id'] + '/cancel', token=buyer)
with transaction() as db:
    for table, id in [('products', product['id']), ('orders', order['id']), ('promo_codes', promo['id'])]:
        print('SELECT * FROM', table, 'WHERE id = ...')
        print(db.execute('SELECT * FROM ' + table + ' WHERE id=%s', (id,)).fetchone())
print('Готово: заказ отменён, остатки возвращены, использование промокода освобождено.')
