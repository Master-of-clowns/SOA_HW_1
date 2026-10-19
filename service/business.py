"""Business rules; transport models and constraints are generated from OpenAPI."""
import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from service.common import transaction, ApiError

CENT = Decimal('0.01')
MAX_MONEY = Decimal('9999999999.99')


def fail(code, message, status=409, details=None):
    raise ApiError(code, message, status, details)


def product(conn, id, lock=False):
    row = conn.execute('SELECT * FROM products WHERE id=%s' + (' FOR UPDATE' if lock else ''), (id,)).fetchone()
    if not row:
        fail('PRODUCT_NOT_FOUND','Товар не найден',404)
    return row


def owns_product(user, row):
    if user['role'] != 'ADMIN' and row['seller_id'] != user['id']:
        fail('ACCESS_DENIED','Продавец может менять только свои товары',403)


def create_product(data, user):
    seller = data.seller_id or user['id']
    if user['role'] != 'ADMIN' and seller != user['id']:
        fail('ACCESS_DENIED','Нельзя создать товар другого продавца',403)
    with transaction() as conn:
        target = conn.execute('SELECT role FROM users WHERE id=%s',(seller,)).fetchone()
        if not target or target['role'] not in ('SELLER','ADMIN'):
            fail('VALIDATION_ERROR','seller_id должен указывать продавца или администратора',400,{'fields':[{'field':'seller_id','message':'Не найден продавец'}]})
        return conn.execute('INSERT INTO products(id,name,description,price,stock,category,status,seller_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *', (uuid4(),data.name,data.description,data.price,data.stock,data.category,data.status,seller)).fetchone()


def list_products(query):
    conditions, args = [], []
    for key in ('status','category'):
        value = getattr(query,key)
        if value is not None:
            conditions.append(key+'=%s'); args.append(value)
    where = ' WHERE '+' AND '.join(conditions) if conditions else ''
    with transaction() as conn:
        # Both count and page must see the same snapshot.
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        total = conn.execute('SELECT count(*) AS n FROM products'+where,args).fetchone()['n']
        rows = conn.execute('SELECT * FROM products'+where+' ORDER BY created_at,id LIMIT %s OFFSET %s',args+[query.size,query.page*query.size]).fetchall()
        return {'items':rows,'totalElements':total,'page':query.page,'size':query.size}


def get_product(id):
    with transaction() as conn:
        return product(conn,id)


def update_product(id, data, user):
    with transaction() as conn:
        owns_product(user,product(conn,id,True))
        return conn.execute('UPDATE products SET name=%s,description=%s,price=%s,stock=%s,category=%s,status=%s WHERE id=%s RETURNING *', (data.name,data.description,data.price,data.stock,data.category,data.status,id)).fetchone()


def delete_product(id, user):
    with transaction() as conn:
        owns_product(user,product(conn,id,True))
        conn.execute("UPDATE products SET status='ARCHIVED' WHERE id=%s", (id,))


def create_promo(data):
    violations=[]
    if data.valid_until <= data.valid_from:
        violations.append({'field':'valid_until','message':'Должно быть позже valid_from'})
    if data.discount_type == 'PERCENTAGE' and data.discount_value > 100:
        violations.append({'field':'discount_value','message':'Процент не может превышать 100'})
    if violations:
        fail('VALIDATION_ERROR','Некорректные условия промокода',400,{'fields':violations})
    with transaction() as conn:
        row=conn.execute('INSERT INTO promo_codes(id,code,discount_type,discount_value,min_order_amount,max_uses,valid_from,valid_until,active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(code) DO NOTHING RETURNING *', (uuid4(),data.code,data.discount_type,data.discount_value,data.min_order_amount,data.max_uses,data.valid_from,data.valid_until,data.active)).fetchone()
        if not row:
            fail('PROMO_CODE_EXISTS','Промокод уже существует',409)
        return row


def order(conn, id):
    row = conn.execute('SELECT * FROM orders WHERE id=%s FOR UPDATE',(id,)).fetchone()
    if not row:
        fail('ORDER_NOT_FOUND','Заказ не найден',404)
    return row


def owns_order(user, row):
    if user['role'] != 'ADMIN' and row['user_id'] != user['id']:
        fail('ORDER_OWNERSHIP_VIOLATION','Заказ принадлежит другому пользователю',403)


def order_result(conn, id):
    row=conn.execute('SELECT * FROM orders WHERE id=%s',(id,)).fetchone()
    row['items']=conn.execute('SELECT id,product_id,quantity,price_at_order FROM order_items WHERE order_id=%s ORDER BY product_id',(id,)).fetchall()
    return row


def get_order(id, user):
    with transaction() as conn:
        row=order(conn,id); owns_order(user,row)
        return order_result(conn,id)


def user_lock(conn, user_id):
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(user_id),))


def check_rate(conn, user_id, operation):
    interval=Decimal(os.environ.get('ORDER_RATE_MINUTES','1'))
    if interval < 0:
        raise RuntimeError('ORDER_RATE_MINUTES must be nonnegative')
    last=conn.execute('SELECT created_at FROM user_operations WHERE user_id=%s AND operation_type=%s ORDER BY created_at DESC LIMIT 1',(user_id,operation)).fetchone()
    if last:
        elapsed=Decimal(str((datetime.now(timezone.utc)-last['created_at']).total_seconds()))
        if elapsed < interval*60:
            fail('ORDER_LIMIT_EXCEEDED','Слишком частые операции с заказом',429,{'retry_after_seconds':max(1,int(interval*60-elapsed)+1)})


def record_operation(conn, user_id, operation):
    conn.execute('INSERT INTO user_operations(id,user_id,operation_type) VALUES (%s,%s,%s)',(uuid4(),user_id,operation))


def item_map(items):
    values={item.product_id:item.quantity for item in items}
    if len(values)!=len(items):
        fail('VALIDATION_ERROR','product_id не должен повторяться',400,{'fields':[{'field':'items','message':'Повтор product_id запрещён'}]})
    return values


def lock_products(conn, ids):
    # Stable global ordering prevents deadlocks between overlapping baskets.
    return {r['id']:r for r in conn.execute('SELECT * FROM products WHERE id=ANY(%s::uuid[]) ORDER BY id FOR UPDATE',(sorted(ids,key=str),)).fetchall()}


def reserve(conn, quantities, products):
    for id in quantities:
        if id not in products:
            fail('PRODUCT_NOT_FOUND','Товар не найден',404,{'product_id':str(id)})
        if products[id]['status']!='ACTIVE':
            fail('PRODUCT_INACTIVE','Товар неактивен',409,{'product_id':str(id)})
    missing=[{'product_id':str(id),'requested':q,'available':products[id]['stock']} for id,q in quantities.items() if products[id]['stock']<q]
    if missing:
        fail('INSUFFICIENT_STOCK','Недостаточно товара на складе',409,{'products':missing})
    for id,q in quantities.items():
        conn.execute('UPDATE products SET stock=stock-%s WHERE id=%s',(q,id))


def gross_amount(quantities, products):
    amount=sum((products[id]['price']*q for id,q in quantities.items()),Decimal(0))
    if amount>MAX_MONEY:
        fail('VALIDATION_ERROR','Стоимость заказа превышает допустимую',400,{'fields':[{'field':'items','message':'Сумма должна быть не больше 9999999999.99'}]})
    return amount


def promo_valid(promo, reuse=False):
    now=datetime.now(timezone.utc)
    return promo and promo['active'] and promo['valid_from']<=now<=promo['valid_until'] and (reuse or promo['current_uses']<promo['max_uses'])


def discount_for(promo, gross):
    if promo['discount_type']=='PERCENTAGE':
        # Assignment: if percentage exceeds 70%, price remains 100%.
        amount=gross*promo['discount_value']/100 if promo['discount_value']<=70 else Decimal(0)
    else:
        amount=min(promo['discount_value'],gross)
    return amount.quantize(CENT,rounding=ROUND_HALF_UP)


def write_items(conn, id, quantities, products):
    for product_id,q in quantities.items():
        conn.execute('INSERT INTO order_items(id,order_id,product_id,quantity,price_at_order) VALUES (%s,%s,%s,%s,%s)',(uuid4(),id,product_id,q,products[product_id]['price']))


def create_order(data, user):
    with transaction() as conn:
        user_lock(conn,user['id'])
        check_rate(conn,user['id'],'CREATE_ORDER')
        if conn.execute("SELECT id FROM orders WHERE user_id=%s AND status IN ('CREATED','PAYMENT_PENDING')",(user['id'],)).fetchone():
            fail('ORDER_HAS_ACTIVE','У пользователя уже есть активный заказ')
        quantities=item_map(data.items)
        products=lock_products(conn,quantities)
        reserve(conn,quantities,products)
        gross=gross_amount(quantities,products)
        promo=None; discount=Decimal(0)
        if data.promo_code:
            promo=conn.execute('SELECT * FROM promo_codes WHERE code=%s FOR UPDATE',(data.promo_code,)).fetchone()
            if not promo_valid(promo):
                fail('PROMO_CODE_INVALID','Промокод недоступен',422)
            if gross<promo['min_order_amount']:
                fail('PROMO_CODE_MIN_AMOUNT','Сумма меньше минимума промокода',422,{'min_order_amount':promo['min_order_amount']})
            discount=discount_for(promo,gross)
            conn.execute('UPDATE promo_codes SET current_uses=current_uses+1 WHERE id=%s',(promo['id'],))
        id=uuid4()
        conn.execute('INSERT INTO orders(id,user_id,promo_code_id,total_amount,discount_amount) VALUES (%s,%s,%s,%s,%s)',(id,user['id'],promo['id'] if promo else None,gross-discount,discount))
        write_items(conn,id,quantities,products)
        record_operation(conn,user['id'],'CREATE_ORDER')
        return order_result(conn,id)


def update_order(id, data, user):
    with transaction() as conn:
        row=order(conn,id); owns_order(user,row)
        if row['status']!='CREATED':
            fail('INVALID_STATE_TRANSITION','Менять позиции можно только в CREATED')
        user_lock(conn,user['id']); check_rate(conn,user['id'],'UPDATE_ORDER')
        quantities=item_map(data.items)
        old=conn.execute('SELECT * FROM order_items WHERE order_id=%s',(id,)).fetchall()
        products=lock_products(conn,set(quantities)|{r['product_id'] for r in old})
        for item in old:
            conn.execute('UPDATE products SET stock=stock+%s WHERE id=%s',(item['quantity'],item['product_id']))
            products[item['product_id']]['stock']+=item['quantity']
        reserve(conn,quantities,products)
        gross=gross_amount(quantities,products); discount=Decimal(0)
        promo_id=row['promo_code_id']
        if promo_id:
            promo=conn.execute('SELECT * FROM promo_codes WHERE id=%s FOR UPDATE',(promo_id,)).fetchone()
            if not promo_valid(promo,reuse=True) or gross<promo['min_order_amount']:
                conn.execute('UPDATE promo_codes SET current_uses=current_uses-1 WHERE id=%s',(promo_id,))
                promo_id=None
            else:
                discount=discount_for(promo,gross)
        conn.execute('DELETE FROM order_items WHERE order_id=%s',(id,))
        write_items(conn,id,quantities,products)
        conn.execute('UPDATE orders SET total_amount=%s,discount_amount=%s,promo_code_id=%s WHERE id=%s',(gross-discount,discount,promo_id,id))
        record_operation(conn,user['id'],'UPDATE_ORDER')
        return order_result(conn,id)


def cancel_order(id, user):
    with transaction() as conn:
        row=order(conn,id); owns_order(user,row)
        if row['status'] not in ('CREATED','PAYMENT_PENDING'):
            fail('INVALID_STATE_TRANSITION','Отменить можно CREATED или PAYMENT_PENDING')
        items=conn.execute('SELECT * FROM order_items WHERE order_id=%s',(id,)).fetchall()
        lock_products(conn,{item['product_id'] for item in items})
        for item in items:
            conn.execute('UPDATE products SET stock=stock+%s WHERE id=%s',(item['quantity'],item['product_id']))
        if row['promo_code_id']:
            conn.execute('UPDATE promo_codes SET current_uses=current_uses-1 WHERE id=%s',(row['promo_code_id'],))
        conn.execute("UPDATE orders SET status='CANCELED' WHERE id=%s",(id,))
        return order_result(conn,id)


def transition_order(id, data, user):
    allowed={'CREATED':'PAYMENT_PENDING','PAYMENT_PENDING':'PAID','PAID':'SHIPPED','SHIPPED':'COMPLETED'}
    with transaction() as conn:
        row=order(conn,id)
        if allowed.get(row['status'])!=data.status:
            fail('INVALID_STATE_TRANSITION','Переход состояния запрещён; для отмены используйте /cancel')
        conn.execute('UPDATE orders SET status=%s WHERE id=%s',(data.status,id))
        return order_result(conn,id)
