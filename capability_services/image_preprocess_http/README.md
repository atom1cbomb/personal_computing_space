# Image preprocess HTTP capability

This deterministic service validates, orients, resizes and JPEG-encodes an image before detection. Build it with:

```bash
docker build -t localhost:5001/loom/image-preprocess-http:0.1.0 .
```

`GET /healthz` reports readiness and `POST /invoke` accepts the contract in `schemas/`.
