FROM node:22-bookworm-slim AS codex
ARG CODEX_VERSION=0.164.0
RUN npm install --global @openai/codex@${CODEX_VERSION}

FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /bin/bash agent
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex
USER 1000:1000
WORKDIR /work
CMD ["sleep", "infinity"]
