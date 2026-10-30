FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY loom_orchestration /app/loom_orchestration
COPY packages/loom-orchestration /app/packages/loom-orchestration
RUN pip install --no-cache-dir /app/packages/loom-orchestration 'fastapi>=0.115,<1' 'uvicorn[standard]>=0.30,<1'
COPY capability_services/suanli_overleaf_http /app/capability_services/suanli_overleaf_http
RUN chmod 755 /app/capability_services/suanli_overleaf_http/askpass.sh && mkdir -p /var/lib/loom/service && chown 65534:65534 /var/lib/loom/service
USER 65534:65534
EXPOSE 8080
ENTRYPOINT ["python", "-m", "capability_services.suanli_overleaf_http.server"]
