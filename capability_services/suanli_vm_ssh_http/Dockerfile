FROM golang:1.27.3 AS renewal
WORKDIR /build
COPY capability_services/suanli_vm_ssh_http/renewal/go.mod capability_services/suanli_vm_ssh_http/renewal/go.sum ./
RUN go mod download
COPY capability_services/suanli_vm_ssh_http/renewal/*.go ./
RUN CGO_ENABLED=0 go build -trimpath -o /loom-opkssh .

FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir httpx==0.28.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=renewal /loom-opkssh /usr/local/bin/loom-opkssh
COPY capability_services/suanli_vm_ssh_http/server.py /app/server.py
COPY capability_services/suanli_vm_ssh_http/opkssh.yml /app/opkssh.yml
COPY capability_services/ssh_execution.py /app/capability_services/ssh_execution.py
RUN mkdir -p /var/lib/loom/service && chown -R 65534:65534 /var/lib/loom/service
USER 65534:65534
EXPOSE 8080
ENTRYPOINT ["python", "/app/server.py"]
