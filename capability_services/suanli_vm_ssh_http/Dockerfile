FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir httpx==0.28.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in amd64) OPK_ARCH=amd64 ;; arm64) OPK_ARCH=arm64 ;; *) echo "unsupported architecture" >&2; exit 1 ;; esac \
    && curl -fsSL "https://github.com/openpubkey/opkssh/releases/download/v0.14.0/opkssh-linux-${OPK_ARCH}" -o /usr/local/bin/opkssh \
    && chmod 0755 /usr/local/bin/opkssh
COPY server.py /app/server.py
COPY opkssh.yml /app/opkssh.yml
RUN mkdir -p /var/lib/loom/service && chown -R 65534:65534 /var/lib/loom/service
USER 65534:65534
EXPOSE 8080
ENTRYPOINT ["python", "/app/server.py"]
