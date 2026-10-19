import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import jwt
from service.common import transaction, ApiError

ACCESS_MINUTES = 20
REFRESH_DAYS = 14


def password_hash(password):
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ':' + digest.hex()


def password_matches(password, stored):
    salt, expected = stored.split(':')
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return hmac.compare_digest(actual.hex(), expected)


def secret():
    value = os.environ['JWT_SECRET']
    if len(value) < 32:
        raise RuntimeError('JWT_SECRET must contain at least 32 characters')
    return value


def decode(token, kind):
    try:
        claims = jwt.decode(token, secret(), algorithms=['HS256'], issuer='marketplace', audience='marketplace-api', options={'require':['sub','role','type','exp','iat','jti']})
        if claims['type'] != kind:
            raise jwt.InvalidTokenError('Wrong token type')
        return claims
    except jwt.ExpiredSignatureError:
        code = 'TOKEN_EXPIRED' if kind == 'access' else 'REFRESH_TOKEN_INVALID'
        raise ApiError(code, 'Срок действия токена истёк', 401)
    except (jwt.InvalidTokenError, ValueError, TypeError):
        code = 'TOKEN_INVALID' if kind == 'access' else 'REFRESH_TOKEN_INVALID'
        raise ApiError(code, 'Неверный токен', 401)


def issue(conn, user):
    now = datetime.now(timezone.utc)
    def token(kind, expires, jti):
        return jwt.encode({'sub':str(user['id']), 'role':user['role'], 'type':kind, 'iat':now, 'exp':expires, 'jti':str(jti), 'iss':'marketplace', 'aud':'marketplace-api'}, secret(), algorithm='HS256')
    jti = uuid4()
    expiration = now + timedelta(days=REFRESH_DAYS)
    conn.execute('INSERT INTO refresh_tokens(jti,user_id,expires_at) VALUES (%s,%s,%s)', (jti,user['id'],expiration))
    return {'access_token':token('access',now+timedelta(minutes=ACCESS_MINUTES),uuid4()), 'refresh_token':token('refresh',expiration,jti), 'token_type':'bearer','expires_in':ACCESS_MINUTES*60}


def authenticate(request, roles):
    header = request.headers.get('authorization','')
    if not header.lower().startswith('bearer '):
        raise ApiError('TOKEN_INVALID','Требуется Bearer access token',401)
    claims = decode(header[7:], 'access')
    # Role is checked against DB too: promoting/revoking a role invalidates old claims.
    with transaction() as conn:
        user = conn.execute('SELECT id, username, role FROM users WHERE id=%s', (claims['sub'],)).fetchone()
    if not user or user['role'] != claims['role']:
        raise ApiError('TOKEN_INVALID','Пользователь или роль токена недействительны',401)
    request.state.user_id = str(user['id'])
    if user['role'] not in roles:
        raise ApiError('ACCESS_DENIED','Недостаточно прав',403)
    return user


def register(data):
    with transaction() as conn:
        row = conn.execute('INSERT INTO users(id,username,password_hash,role) VALUES (%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING RETURNING id,username,role', (uuid4(),data.username,password_hash(data.password),data.role)).fetchone()
        if not row:
            raise ApiError('USER_EXISTS','Логин уже занят',409)
        return row


def login(data):
    with transaction() as conn:
        user = conn.execute('SELECT * FROM users WHERE username=%s', (data.username,)).fetchone()
        if not user or not password_matches(data.password, user['password_hash']):
            raise ApiError('INVALID_CREDENTIALS','Неверный логин или пароль',401)
        return issue(conn, user)


def refresh(data):
    claims = decode(data.refresh_token, 'refresh')
    with transaction() as conn:
        row = conn.execute('UPDATE refresh_tokens SET used=true WHERE jti=%s AND user_id=%s AND used=false AND expires_at>now() RETURNING user_id', (claims['jti'],claims['sub'])).fetchone()
        if not row:
            raise ApiError('REFRESH_TOKEN_INVALID','Refresh token уже использован или недействителен',401)
        user = conn.execute('SELECT id,role FROM users WHERE id=%s', (row['user_id'],)).fetchone()
        return issue(conn, user)


if __name__ == '__main__':
    import sys
    with transaction() as conn:
        row = conn.execute("UPDATE users SET role='ADMIN' WHERE username=%s RETURNING id", (sys.argv[1],)).fetchone()
        if not row:
            raise SystemExit('Register this user first')
    print('Role changed to ADMIN. Log in again.')
