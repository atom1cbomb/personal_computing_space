FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir 'httpx>=0.27,<1' 'jsonschema>=4.23,<5'
COPY capability_services/ark_http /app/capability_services/ark_http
USER 65534:65534
EXPOSE 8080
ENTRYPOINT ["python", "-m", "capability_services.ark_http.server"]
