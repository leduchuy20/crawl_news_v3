FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source (khi build trong context app/backend/ thì mọi thứ ở đây)
COPY . /app/backend/

# Set PYTHONPATH để import 'backend' package từ /app
ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
