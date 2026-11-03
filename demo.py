"""REST → gRPC → PostgreSQL, plus auth, cache invalidation and idempotency."""
import json
import os
from decimal import Decimal
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4
import grpc
import flight_pb2 as pb
import flight_pb2_grpc as api
from db import connect


def call(method, path, body=None, status=200):
    req = Request('http://localhost:8000'+path, method=method,
                  headers={'Content-Type':'application/json'}, data=json.dumps(body).encode() if body else None)
    try:
        response = urlopen(req)
    except HTTPError as error:
        response = error
    with response:
        result = json.loads(response.read())
        assert response.code == status, result
        print(method, path, '->', response.code)
        return result


id = '00000000-0000-0000-0000-000000000001'
user = str(uuid4())
call('GET', '/flights?origin=SVO&destination=LED&date=2030-01-10')
call('GET', '/flights?origin=SVO&destination=LED&date=2030-01-10')
flight = call('GET', '/flights/'+id)
body = {'user_id':user, 'flight_id':id, 'passenger_name':'Demo Passenger',
        'passenger_email':'demo@example.com', 'seat_count':2}
booking = call('POST', '/bookings', body, 201)
assert Decimal(str(booking['total_price'])) == Decimal(flight['price'])*2
assert call('GET', '/flights/'+id)['available_seats'] == flight['available_seats']-2
call('GET', '/bookings/'+booking['id'])
assert len(call('GET', '/bookings?user_id='+user)) == 1
call('POST', '/bookings', {**body,'seat_count':1000}, 409)
assert len(call('GET', '/bookings?user_id='+user)) == 1
assert call('POST', '/bookings/'+booking['id']+'/cancel')['status'] == 'CANCELLED'
assert call('GET', '/flights/'+id)['available_seats'] == flight['available_seats']
call('POST', '/bookings/'+booking['id']+'/cancel', status=409)

stub = api.FlightServiceStub(grpc.insecure_channel(os.environ['FLIGHT_ADDRESS']))
try:
    stub.GetFlight(pb.FlightId(id=id), timeout=2)
    raise AssertionError('Unauthenticated request was accepted')
except grpc.RpcError as error:
    assert error.code() == grpc.StatusCode.UNAUTHENTICATED
metadata = [('x-api-key', os.environ['GRPC_API_KEY'])]
request = pb.ReserveRequest(flight_id=id, seat_count=1, booking_id=str(uuid4()))
for _ in range(2):
    stub.ReserveSeats(request, metadata=metadata, timeout=2)
assert call('GET', '/flights/'+id)['available_seats'] == flight['available_seats']-1
for _ in range(2):
    stub.ReleaseReservation(pb.BookingId(booking_id=request.booking_id), metadata=metadata, timeout=2)
assert call('GET', '/flights/'+id)['available_seats'] == flight['available_seats']
stub.UpdateFlight(pb.UpdateRequest(flight_id=id,price=str(Decimal(flight['price'])+100),status=pb.SCHEDULED),metadata=metadata,timeout=2)
assert Decimal(call('GET', '/flights/'+id)['price']) == Decimal(flight['price'])+100
stub.UpdateFlight(pb.UpdateRequest(flight_id=id,price=flight['price'],status=pb.SCHEDULED),metadata=metadata,timeout=2)
with connect() as db:
    print('SELECT * FROM bookings WHERE id=...')
    print(db.execute('SELECT * FROM bookings WHERE id=%s',(booking['id'],)).fetchone())
print('OK: REST, gRPC, auth, snapshot, rollback, idempotency, cache invalidation, cancellation')
