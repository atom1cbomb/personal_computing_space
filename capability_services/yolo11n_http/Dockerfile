FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 YOLO_MODEL_PATH=/models/yolo11n.pt YOLO_MODEL_NAME=yolo11n YOLO_DEVICE=cpu
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir -p /models && python -c "from ultralytics import YOLO; YOLO('/models/yolo11n.pt')"
COPY server.py .
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app /models
USER appuser
EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
