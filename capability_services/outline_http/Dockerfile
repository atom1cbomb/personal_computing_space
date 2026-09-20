FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir 'httpx>=0.27,<1'
COPY server.py /app/server.py
USER 65534:65534
EXPOSE 8080
ENTRYPOINT ["python", "/app/server.py"]
