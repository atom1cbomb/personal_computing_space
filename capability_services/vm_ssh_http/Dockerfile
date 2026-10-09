FROM python:3.12-slim

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY server.py /app/server.py
RUN mkdir -p /var/lib/loom/service && chown -R 65534:65534 /var/lib/loom/service
USER 65534:65534
EXPOSE 8080
ENTRYPOINT ["python", "/app/server.py"]
