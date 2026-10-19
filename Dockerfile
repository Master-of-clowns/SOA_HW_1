FROM flyway/flyway:10.20.1 AS migrations
COPY migrations /flyway/sql
CMD ["migrate"]

FROM python:3.12-slim AS api
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY openapi.json generate.py demo.py ./
COPY service service
RUN python generate.py
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=10 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
