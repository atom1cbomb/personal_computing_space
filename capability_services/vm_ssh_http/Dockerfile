FROM python:3.12-slim

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY loom_orchestration /app/loom_orchestration
COPY packages/loom-orchestration /app/packages/loom-orchestration
RUN pip install --no-cache-dir /app/packages/loom-orchestration 'fastapi>=0.115,<1' 'uvicorn[standard]>=0.30,<1'
COPY capability_services/vm_ssh_http/server.py /app/server.py
COPY capability_services/ssh_execution.py capability_services/ssh_app.py capability_services/ssh_files.py capability_services/ssh_files_remote.py /app/capability_services/
RUN mkdir -p /var/lib/loom/service && chown -R 65534:65534 /var/lib/loom/service
USER 65534:65534
EXPOSE 8080
ENTRYPOINT ["python", "/app/server.py"]
