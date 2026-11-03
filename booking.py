import logging
import threading
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, time, timezone
from decimal import Decimal
from uuid import UUID, uuid4
import grpc
import psycopg
from fastapi import FastAPI, HTTPException, Query
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, Field
import flight_pb2 as pb
from db import connect, lock
from rpc import client

logging.basicConfig(level=logging.INFO, format='%(message)s')
flights = client()


class BookingInput(BaseModel):
    user_id: UUID
    flight_id: UUID
    passenger_name: str = Field(min_length=1, max_length=200)
    passenger_email: str = Field(max_length=254, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    seat_count: int = Field(gt=0, le=1000, strict=True)


def row(db, id):
    result = db.execute('SELECT * FROM bookings WHERE id=%s FOR UPDATE', (id,)).fetchone()
    if not result:
        raise HTTPException(404, 'Booking not found')
    return result


def cancel(db, id):
    flights.ReleaseReservation(pb.BookingId(booking_id=str(id)))
    db.execute("UPDATE bookings SET status='CANCELLED' WHERE id=%s", (id,))
    db.execute('DELETE FROM operations WHERE booking_id=%s', (id,))
    db.commit()


def recover(stop):
    while not stop.wait(1):
        try:
            with connect() as db:
                ids = db.execute('SELECT booking_id FROM operations ORDER BY created_at LIMIT 100').fetchall()
            for item in ids:
                with connect() as db:
                    id = item['booking_id']
                    if lock(db, id, wait=False) and db.execute('SELECT 1 FROM operations WHERE booking_id=%s', (id,)).fetchone():
                        cancel(db, id)
                        logging.info('recovered operation %s', id)
        except (grpc.RpcError, psycopg.Error):
            logging.info('recovery deferred: dependency unavailable')


@asynccontextmanager
async def lifespan(app):
    stop = threading.Event()
    worker = threading.Thread(target=recover, args=(stop,), daemon=True)
    worker.start()
    yield
    stop.set()
    worker.join(timeout=2)


app = FastAPI(title='Flight Booking', lifespan=lifespan)


@app.exception_handler(grpc.RpcError)
async def rpc_error(request, error):
    from fastapi.responses import JSONResponse
    status = {grpc.StatusCode.INVALID_ARGUMENT:400, grpc.StatusCode.NOT_FOUND:404,
              grpc.StatusCode.RESOURCE_EXHAUSTED:409, grpc.StatusCode.FAILED_PRECONDITION:409,
              grpc.StatusCode.ALREADY_EXISTS:409, grpc.StatusCode.UNAVAILABLE:503,
              grpc.StatusCode.DEADLINE_EXCEEDED:503}.get(error.code(),502)
    return JSONResponse({'error':error.code().name, 'message':error.details()}, status_code=status)


@app.exception_handler(psycopg.Error)
async def database_error(request, error):
    from fastapi.responses import JSONResponse
    return JSONResponse({'error':'DATABASE_UNAVAILABLE'}, status_code=503)


@app.get('/health')
def health():
    with connect() as db:
        db.execute('SELECT 1')
    return {'status':'ok'}


@app.get('/flights')
def search(origin: str = Query(pattern='^[A-Z]{3}$'), destination: str = Query(pattern='^[A-Z]{3}$'), date: Date | None = None):
    request = pb.SearchRequest(origin=origin, destination=destination)
    if date:
        request.date.FromDatetime(datetime.combine(date,time(),timezone.utc))
    return [MessageToDict(f, preserving_proto_field_name=True, always_print_fields_with_no_presence=True) for f in flights.SearchFlights(request).flights]


@app.get('/flights/{id}')
def get_flight(id: UUID):
    return MessageToDict(flights.GetFlight(pb.FlightId(id=str(id))), preserving_proto_field_name=True, always_print_fields_with_no_presence=True)


@app.post('/bookings', status_code=201)
def create(data: BookingInput):
    id = uuid4()
    with connect() as db:
        lock(db, id)
        db.execute('INSERT INTO operations(booking_id) VALUES (%s)', (id,))
        db.commit()
        try:
            flight = flights.GetFlight(pb.FlightId(id=str(data.flight_id)))
            flights.ReserveSeats(pb.ReserveRequest(flight_id=str(data.flight_id), seat_count=data.seat_count, booking_id=str(id)))
            result = db.execute('INSERT INTO bookings(id,user_id,flight_id,passenger_name,passenger_email,seat_count,total_price) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *',
                                 (id,data.user_id,data.flight_id,data.passenger_name,data.passenger_email,data.seat_count,Decimal(flight.price)*data.seat_count)).fetchone()
            db.execute('DELETE FROM operations WHERE booking_id=%s', (id,))
            return result
        except Exception:
            db.rollback()
            try:
                cancel(db, id)
            except (grpc.RpcError, psycopg.Error):
                logging.info('compensation queued %s', id)
            raise


@app.get('/bookings')
def list_bookings(user_id: UUID):
    with connect() as db:
        return db.execute('SELECT * FROM bookings WHERE user_id=%s ORDER BY id', (user_id,)).fetchall()


@app.get('/bookings/{id}')
def get_booking(id: UUID):
    with connect() as db:
        return row(db, id)


@app.post('/bookings/{id}/cancel')
def cancel_booking(id: UUID):
    with connect() as db:
        lock(db, id)
        booking = row(db, id)
        if booking['status'] != 'CONFIRMED':
            raise HTTPException(409, 'Only CONFIRMED bookings can be cancelled')
        db.execute('INSERT INTO operations(booking_id) VALUES (%s) ON CONFLICT DO NOTHING', (id,))
        db.commit()
        cancel(db, id)
        return row(db, id)
