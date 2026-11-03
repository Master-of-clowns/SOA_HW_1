import hmac
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from uuid import UUID
import grpc
import psycopg
from google.protobuf.empty_pb2 import Empty
from google.protobuf.timestamp_pb2 import Timestamp
from redis import RedisError
from redis.sentinel import Sentinel
import flight_pb2 as pb
import flight_pb2_grpc as api
from db import connect, lock

logging.basicConfig(level=logging.INFO, format='%(message)s')
redis = Sentinel([tuple(x.split(':')) for x in os.environ['REDIS_SENTINELS'].split(',')],
                 socket_timeout=.1, socket_connect_timeout=.1).master_for('flights')
TTL = 300


class Auth(grpc.ServerInterceptor):
    def intercept_service(self, continuation, details):
        handler = continuation(details)
        if handler is None:
            return None
        def call(request, context):
            supplied = dict(context.invocation_metadata()).get('x-api-key', '')
            if not hmac.compare_digest(supplied, os.environ['GRPC_API_KEY']):
                context.abort(grpc.StatusCode.UNAUTHENTICATED, 'Invalid service credentials')
            return handler.unary_unary(request, context)
        return grpc.unary_unary_rpc_method_handler(call, request_deserializer=handler.request_deserializer,
                                                   response_serializer=handler.response_serializer)


def rpc(method):
    @wraps(method)
    def call(self, request, context):
        try:
            return method(self, request, context)
        except (ValueError, InvalidOperation):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'Invalid UUID, date, price or route')
        except psycopg.OperationalError:
            context.abort(grpc.StatusCode.UNAVAILABLE, 'Flight database unavailable')
    return call


def timestamp(value):
    result = Timestamp()
    result.FromDatetime(value)
    return result


def flight_message(row):
    return pb.Flight(**{key: row[key] for key in ('flight_number','airline_code','airline_name','origin','destination','total_seats','available_seats')},
                     id=str(row['id']), price=str(row['price']), status=pb.FlightStatus.Value(row['status']),
                     departure_at=timestamp(row['departure_at']), arrival_at=timestamp(row['arrival_at']))


def reservation(row):
    return pb.Reservation(booking_id=str(row['booking_id']), flight_id=str(row['flight_id']),
                          seat_count=row['seat_count'], status=pb.ReservationStatus.Value(row['status']))


SELECT = 'SELECT f.*,a.name AS airline_name FROM flights f JOIN airlines a ON a.code=f.airline_code'


def cached(key, message, load):
    with connect() as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        version = db.execute('SELECT version FROM cache_version WHERE id=1').fetchone()['version']
        key += ':' + str(version)
        cache_ok = True
        try:
            value = redis.get(key)
            if value:
                logging.info('cache hit %s', key)
                return message.FromString(value)
        except RedisError:
            cache_ok = False
            logging.info('cache unavailable: reading PostgreSQL')
        logging.info('cache miss %s', key)
        result = load(db)
        if cache_ok:
            try:
                redis.setex(key, TTL, result.SerializeToString())
            except RedisError:
                pass
        return result


def invalidate():
    try:
        keys = list(redis.scan_iter('flight:*')) + list(redis.scan_iter('search:*'))
        if keys:
            redis.delete(*keys)
    except RedisError:
        pass  # PostgreSQL generation still makes old keys unreachable after recovery.


class FlightService(api.FlightServiceServicer):
    @rpc
    def SearchFlights(self, request, context):
        if not all(re.fullmatch('[A-Z]{3}', code) for code in (request.origin, request.destination)) or request.origin == request.destination:
            raise ValueError()
        day = None
        if request.HasField('date'):
            value = request.date.ToDatetime(tzinfo=timezone.utc)
            if value.time().isoformat() != '00:00:00':
                raise ValueError()
            day = value.date()
        def load(db):
            rows = db.execute(SELECT + " WHERE origin=%s AND destination=%s AND status='SCHEDULED'" +
                              (' AND (departure_at AT TIME ZONE \'UTC\')::date=%s' if day else '') + ' ORDER BY departure_at,id',
                              (request.origin, request.destination, day) if day else (request.origin, request.destination)).fetchall()
            return pb.FlightList(flights=[flight_message(row) for row in rows])
        return cached(f'search:{request.origin}:{request.destination}:{day or "any"}', pb.FlightList, load)

    @rpc
    def GetFlight(self, request, context):
        id = UUID(request.id)
        def load(db):
            row = db.execute(SELECT + ' WHERE f.id=%s', (id,)).fetchone()
            if not row:
                context.abort(grpc.StatusCode.NOT_FOUND, 'Flight not found')
            return flight_message(row)
        return cached('flight:' + str(id), pb.Flight, load)

    @rpc
    def ReserveSeats(self, request, context):
        id, booking = UUID(request.flight_id), UUID(request.booking_id)
        if request.seat_count < 1:
            raise ValueError()
        with connect() as db:
            lock(db, booking)
            if db.execute('SELECT 1 FROM cancelled_requests WHERE booking_id=%s', (booking,)).fetchone():
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, 'Reservation already released')
            old = db.execute('SELECT * FROM seat_reservations WHERE booking_id=%s', (booking,)).fetchone()
            if old:
                if old['flight_id'] != id or old['seat_count'] != request.seat_count:
                    context.abort(grpc.StatusCode.ALREADY_EXISTS, 'booking_id has different reservation parameters')
                if old['status'] != 'ACTIVE':
                    context.abort(grpc.StatusCode.FAILED_PRECONDITION, 'Reservation is not active')
                return reservation(old)
            row = db.execute('SELECT * FROM flights WHERE id=%s FOR UPDATE', (id,)).fetchone()
            if not row:
                context.abort(grpc.StatusCode.NOT_FOUND, 'Flight not found')
            if row['status'] != 'SCHEDULED':
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, 'Flight is not scheduled')
            if row['available_seats'] < request.seat_count:
                context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, 'Not enough seats')
            db.execute('UPDATE flights SET available_seats=available_seats-%s WHERE id=%s', (request.seat_count,id))
            result = db.execute('INSERT INTO seat_reservations(booking_id,flight_id,seat_count) VALUES (%s,%s,%s) RETURNING *',
                                (booking,id,request.seat_count)).fetchone()
            db.execute('UPDATE cache_version SET version=version+1 WHERE id=1')
        invalidate()
        return reservation(result)

    @rpc
    def ReleaseReservation(self, request, context):
        booking = UUID(request.booking_id)
        with connect() as db:
            lock(db, booking)
            row = db.execute('SELECT * FROM seat_reservations WHERE booking_id=%s', (booking,)).fetchone()
            if row and row['status'] == 'ACTIVE':
                db.execute('SELECT id FROM flights WHERE id=%s FOR UPDATE', (row['flight_id'],))
                db.execute('UPDATE flights SET available_seats=available_seats+%s WHERE id=%s', (row['seat_count'],row['flight_id']))
                db.execute("UPDATE seat_reservations SET status='RELEASED' WHERE booking_id=%s", (booking,))
                db.execute('UPDATE cache_version SET version=version+1 WHERE id=1')
            db.execute('INSERT INTO cancelled_requests VALUES (%s) ON CONFLICT DO NOTHING', (booking,))
        invalidate()
        return Empty()

    @rpc
    def UpdateFlight(self, request, context):
        id, price = UUID(request.flight_id), Decimal(request.price)
        if not price.is_finite() or price <= 0 or price > Decimal('9999999999.99') or price.quantize(Decimal('.01')) != price or request.status not in (pb.SCHEDULED,pb.DEPARTED,pb.CANCELLED,pb.COMPLETED):
            raise ValueError()
        with connect() as db:
            row = db.execute('UPDATE flights SET price=%s,status=%s WHERE id=%s RETURNING id',
                              (price,pb.FlightStatus.Name(request.status),id)).fetchone()
            if not row:
                context.abort(grpc.StatusCode.NOT_FOUND, 'Flight not found')
            db.execute('UPDATE cache_version SET version=version+1 WHERE id=1')
        invalidate()
        return self.GetFlight(pb.FlightId(id=str(id)), context)


if __name__ == '__main__':
    server = grpc.server(ThreadPoolExecutor(max_workers=10), interceptors=[Auth()])
    api.add_FlightServiceServicer_to_server(FlightService(), server)
    server.add_insecure_port('[::]:50051')
    server.start()
    logging.info('Flight gRPC listening on 50051')
    server.wait_for_termination()
