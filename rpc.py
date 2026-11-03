"""Transport middleware: API key, deadline, retry and circuit breaker."""
import logging
import os
import threading
import time
from collections import deque, namedtuple
import grpc
import flight_pb2_grpc as api

TRANSIENT = {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}
Details = namedtuple('Details', 'method timeout metadata credentials wait_for_ready compression')


class Unavailable(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return 'Flight Service circuit is OPEN; retry later'


class Resilience(grpc.UnaryUnaryClientInterceptor):
    def __init__(self):
        self.state, self.generation, self.opened = 'CLOSED', 0, 0
        self.failures, self.lock = deque(), threading.Lock()
        self.threshold = int(os.environ.get('CB_FAILURE_THRESHOLD', '5'))
        self.reset = float(os.environ.get('CB_RESET_SECONDS', '10'))
        self.window = float(os.environ.get('CB_WINDOW_SECONDS', '60'))

    def move(self, state):
        logging.info('circuit %s -> %s', self.state, state)
        self.state, self.generation = state, self.generation + 1
        if state == 'OPEN':
            self.opened = time.monotonic()

    def before(self):
        with self.lock:
            if self.state == 'OPEN':
                if time.monotonic() - self.opened < self.reset:
                    raise Unavailable()
                self.move('HALF_OPEN')
                return self.generation, True
            if self.state == 'HALF_OPEN':
                raise Unavailable()
            return self.generation, False

    def finish(self, generation, healthy):
        with self.lock:
            if generation != self.generation:
                return
            if healthy:
                self.failures.clear()
                if self.state == 'HALF_OPEN':
                    self.move('CLOSED')
            else:
                now = time.monotonic()
                self.failures.append(now)
                while self.failures and self.failures[0] < now - self.window:
                    self.failures.popleft()
                if self.state == 'HALF_OPEN' or len(self.failures) >= self.threshold:
                    self.move('OPEN')

    def intercept_unary_unary(self, continuation, details, request):
        generation, probe = self.before()
        metadata = list(details.metadata or []) + [('x-api-key', os.environ['GRPC_API_KEY'])]
        timeout = float(os.environ.get('GRPC_TIMEOUT', '1'))
        call_details = Details(details.method, min(details.timeout, timeout) if details.timeout else timeout,
                               metadata, details.credentials, details.wait_for_ready, details.compression)
        attempts = 1 if probe else 3
        for attempt in range(attempts):
            try:
                call = continuation(call_details, request)
                call.result()
                self.finish(generation, True)
                return call
            except grpc.RpcError as error:
                transient = error.code() in TRANSIENT
                if not transient or attempt == attempts - 1:
                    self.finish(generation, not transient)
                    raise
                logging.info('rpc retry %s attempt=%s code=%s', details.method, attempt + 2, error.code().name)
                time.sleep(.1 * 2 ** attempt)


class FlightClient:
    def __init__(self):
        self.policy = Resilience()

    def __getattr__(self, method):
        def call(request):
            # One channel per operation; circuit state is shared across all channels.
            with grpc.insecure_channel(os.environ.get('FLIGHT_ADDRESS', 'flight:50051'),
                                       options=[('grpc.enable_retries',0)]) as channel:
                stub = api.FlightServiceStub(grpc.intercept_channel(channel, self.policy))
                return getattr(stub, method)(request)
        return call


def client():
    return FlightClient()
