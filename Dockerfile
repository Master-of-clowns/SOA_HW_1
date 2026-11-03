FROM flyway/flyway:10.20.1 AS migrations
COPY migrations /flyway/sql

FROM redis:7-alpine AS sentinel
COPY sentinel.conf /sentinel.conf
CMD ["sh", "-c", "cp /sentinel.conf /tmp/sentinel.conf && exec redis-sentinel /tmp/sentinel.conf"]

FROM python:3.12-slim AS app
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY flight.proto .
RUN python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. flight.proto
COPY *.py ./
USER 10001:10001
CMD ["uvicorn", "booking:app", "--host", "0.0.0.0", "--port", "8000"]
