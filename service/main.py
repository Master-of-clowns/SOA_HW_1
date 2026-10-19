import json
import logging
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4
import psycopg
import simplejson
from fastapi import FastAPI, Request, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from service import auth, business
from service.common import transaction, ApiError
from service.generated.routes import create_router


def json_default(value):
    if isinstance(value, UUID): return str(value)
    if isinstance(value, (datetime,date)): return value.isoformat()
    raise TypeError('Unsupported response value')


def response_json(value, status=200):
    return Response(simplejson.dumps(value,use_decimal=True,default=json_default,ensure_ascii=False), status_code=status,media_type='application/json')


def invalid_constant(value):
    raise ValueError('Non-finite JSON number')


def parse_model(model, value, source):
    try:
        return model.model_validate(value)
    except ValidationError as error:
        fields=[{'field':source+'.'+'.'.join(str(x) for x in e['loc']), 'message':e['msg']} for e in error.errors(include_input=False,include_url=False)]
        raise ApiError('VALIDATION_ERROR','Некорректные входные данные',400,{'fields':fields})


def execute(operation, request, body, query, path, roles):
    user=auth.authenticate(request,roles) if roles else None
    id=path.id if path else None
    if operation=='health':
        with transaction() as conn: conn.execute('SELECT 1')
        return {'status':'ok'}
    if operation in ('register','login','refresh'): return getattr(auth,operation)(body)
    if operation=='create_product': return business.create_product(body,user)
    if operation=='list_products': return business.list_products(query)
    if operation=='get_product': return business.get_product(id)
    if operation=='update_product': return business.update_product(id,body,user)
    if operation=='delete_product': return business.delete_product(id,user)
    if operation=='create_promo': return business.create_promo(body)
    if operation=='create_order': return business.create_order(body,user)
    if operation=='get_order': return business.get_order(id,user)
    if operation=='update_order': return business.update_order(id,body,user)
    if operation=='cancel_order': return business.cancel_order(id,user)
    if operation=='transition_order': return business.transition_order(id,body,user)
    raise RuntimeError('Unknown generated operation')


async def dispatch(request, operation, roles, status, body_model=None, query_model=None, path_model=None):
    body=None
    if body_model:
        try:
            raw=json.loads(await request.body(),parse_float=Decimal,parse_constant=invalid_constant)
        except (ValueError,UnicodeError):
            raise ApiError('VALIDATION_ERROR','Тело должно содержать корректный JSON',400,{'fields':[{'field':'body','message':'Invalid JSON'}]})
        body=parse_model(body_model,raw,'body')
    query=parse_model(query_model,dict(request.query_params),'query') if query_model else None
    path=parse_model(path_model,request.path_params,'path') if path_model else None
    result=await run_in_threadpool(execute,operation,request,body,query,path,roles)
    if status==204: return Response(status_code=204)
    return response_json(result,status)


app=FastAPI(title='Marketplace',docs_url='/docs',redoc_url=None)
SPEC=Path(__file__).resolve().parents[1]/'openapi.json'
app.openapi=lambda:json.loads(SPEC.read_text())
logger=logging.getLogger('marketplace.api')
logger.setLevel(logging.INFO)
handler=logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(handler)
logger.propagate=False


def redact(value):
    if isinstance(value,dict):
        return {key:'***' if any(word in key.lower() for word in ('password','token','secret','authorization')) else redact(v) for key,v in value.items()}
    if isinstance(value,list): return [redact(v) for v in value]
    return value


@app.middleware('http')
async def request_log(request: Request, call_next):
    started=time.perf_counter(); request_id=str(uuid4())
    request.state.user_id=None
    entry={'request_id':request_id,'method':request.method,'endpoint':request.url.path,'timestamp':datetime.now(timezone.utc).isoformat()}
    if request.method in ('POST','PUT','PATCH','DELETE'):
        raw=await request.body()
        try: entry['body']=redact(json.loads(raw)) if raw else None
        except (ValueError,UnicodeError): entry['body']='[invalid JSON omitted]'
    try:
        response=await call_next(request)
    except Exception:
        # No request headers, raw exceptions, tokens or database credentials in logs.
        response=response_json({'error_code':'INTERNAL_ERROR','message':'Внутренняя ошибка сервиса'},500)
    response.headers['X-Request-Id']=request_id
    entry.update(status_code=response.status_code,duration_ms=round((time.perf_counter()-started)*1000,2),user_id=request.state.user_id)
    logger.info(json.dumps(entry,ensure_ascii=False))
    return response


@app.exception_handler(ApiError)
async def business_error(request, error):
    body={'error_code':error.code,'message':error.message}
    if error.details is not None: body['details']=error.details
    response=response_json(body,error.status)
    if error.status==401: response.headers['WWW-Authenticate']='Bearer'
    if error.code=='ORDER_LIMIT_EXCEEDED': response.headers['Retry-After']=str(error.details['retry_after_seconds'])
    return response


@app.exception_handler(HTTPException)
async def http_error(request, error):
    code='METHOD_NOT_ALLOWED' if error.status_code==405 else 'ROUTE_NOT_FOUND'
    return response_json({'error_code':code,'message':'Метод не поддерживается' if error.status_code==405 else 'Endpoint не найден'},error.status_code)


@app.exception_handler(psycopg.OperationalError)
async def database_unavailable(request, error):
    return response_json({'error_code':'SERVICE_UNAVAILABLE','message':'База данных временно недоступна'},503)


app.include_router(create_router(dispatch))
