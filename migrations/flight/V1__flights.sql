CREATE TYPE flight_status AS ENUM ('SCHEDULED','DEPARTED','CANCELLED','COMPLETED');
CREATE TYPE reservation_status AS ENUM ('ACTIVE','RELEASED','EXPIRED');
CREATE TABLE airlines (code VARCHAR(3) PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE airports (code CHAR(3) PRIMARY KEY CHECK (code ~ '^[A-Z]{3}$'));
CREATE TABLE flights (
    id UUID PRIMARY KEY,
    flight_number VARCHAR(10) NOT NULL,
    airline_code VARCHAR(3) NOT NULL REFERENCES airlines(code),
    origin CHAR(3) NOT NULL REFERENCES airports(code),
    destination CHAR(3) NOT NULL REFERENCES airports(code),
    departure_at TIMESTAMPTZ NOT NULL,
    arrival_at TIMESTAMPTZ NOT NULL CHECK (arrival_at > departure_at),
    total_seats INTEGER NOT NULL CHECK (total_seats > 0),
    available_seats INTEGER NOT NULL CHECK (available_seats BETWEEN 0 AND total_seats),
    price NUMERIC(12,2) NOT NULL CHECK (price > 0),
    status flight_status NOT NULL DEFAULT 'SCHEDULED',
    CHECK (origin <> destination)
);
CREATE UNIQUE INDEX flight_number_date ON flights(flight_number, ((departure_at AT TIME ZONE 'UTC')::date));
CREATE INDEX flight_search ON flights(origin, destination, departure_at) WHERE status='SCHEDULED';
CREATE TABLE seat_reservations (
    booking_id UUID PRIMARY KEY,
    flight_id UUID NOT NULL REFERENCES flights(id),
    seat_count INTEGER NOT NULL CHECK (seat_count > 0),
    status reservation_status NOT NULL DEFAULT 'ACTIVE'
);
-- Cancellation barrier: an ambiguous timed-out reserve cannot arrive after compensation.
CREATE TABLE cancelled_requests (booking_id UUID PRIMARY KEY);
-- Generation prevents stale cache resurrection after concurrent reads or Redis failover.
CREATE TABLE cache_version (id INTEGER PRIMARY KEY CHECK (id=1), version BIGINT NOT NULL);
INSERT INTO cache_version VALUES (1,0);
INSERT INTO airlines VALUES ('SU','Aeroflot');
INSERT INTO airports VALUES ('SVO'),('LED'),('VKO');
INSERT INTO flights VALUES
('00000000-0000-0000-0000-000000000001','SU100','SU','SVO','LED','2030-01-10 10:00Z','2030-01-10 11:30Z',10,10,4500,'SCHEDULED'),
('00000000-0000-0000-0000-000000000002','SU101','SU','SVO','LED','2030-01-11 10:00Z','2030-01-11 11:30Z',1,1,5000,'SCHEDULED'),
('00000000-0000-0000-0000-000000000003','SU102','SU','VKO','LED','2030-01-10 12:00Z','2030-01-10 13:30Z',10,10,4000,'CANCELLED');
