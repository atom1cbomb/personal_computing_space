FROM python:3.12-slim
WORKDIR /app
COPY loom_orchestration /app/loom_orchestration
COPY packages/loom-orchestration /app/packages/loom-orchestration
RUN pip install --no-cache-dir /app/packages/loom-orchestration 'fastapi>=0.115,<1' 'uvicorn[standard]>=0.30,<1'
COPY capability_services/embedding_http /app/capability_services/embedding_http
USER 65534:65534
EXPOSE 8080 8081
ENTRYPOINT ["python", "-m", "capability_services.embedding_http.server"]
