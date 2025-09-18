# syntax=docker/dockerfile:1
FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
COPY app .

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8008

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8009"]