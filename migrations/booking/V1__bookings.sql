CREATE TYPE booking_status AS ENUM ('CONFIRMED','CANCELLED');
CREATE TABLE bookings (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    flight_id UUID NOT NULL, -- External reference; deliberately no cross-database FK.
    passenger_name VARCHAR(200) NOT NULL,
    passenger_email VARCHAR(254) NOT NULL,
    seat_count INTEGER NOT NULL CHECK (seat_count > 0),
    total_price NUMERIC(14,2) NOT NULL CHECK (total_price > 0),
    status booking_status NOT NULL DEFAULT 'CONFIRMED'
);
CREATE INDEX booking_user ON bookings(user_id);
-- Durable compensation/cancellation journal, not another business status.
CREATE TABLE operations (
    booking_id UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
