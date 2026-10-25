FROM python:3.12-slim

ARG KUBECTL_VERSION=v1.31.4
ARG KUBELOGIN_VERSION=1.30.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod 0755 /usr/local/bin/kubectl \
    && curl -fsSLo /tmp/kubelogin.zip "https://github.com/int128/kubelogin/releases/download/v${KUBELOGIN_VERSION}/kubelogin_linux_amd64.zip" \
    && unzip -p /tmp/kubelogin.zip kubelogin > /usr/local/bin/kubectl-oidc_login \
    && chmod 0755 /usr/local/bin/kubectl-oidc_login \
    && ln -s /usr/local/bin/kubectl-oidc_login /usr/local/bin/kubelogin \
    && rm -rf /tmp/kubelogin.zip /var/lib/apt/lists/*

WORKDIR /app
COPY loom_orchestration /app/loom_orchestration
COPY packages/loom-orchestration /app/packages/loom-orchestration
RUN pip install --no-cache-dir /app/packages/loom-orchestration 'fastapi>=0.115,<1' 'uvicorn[standard]>=0.30,<1'
COPY capability_services/kubernetes_http /app/capability_services/kubernetes_http
RUN mkdir -p /var/lib/loom/service/kube && chown -R 65534:65534 /var/lib/loom/service
USER 65534:65534
EXPOSE 8080
ENTRYPOINT ["python", "-m", "capability_services.kubernetes_http.server"]
