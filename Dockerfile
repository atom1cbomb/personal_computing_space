FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 docker.io docker-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY loom_v2 ./loom_v2
COPY loom_orchestration ./loom_orchestration
COPY migrations ./migrations
COPY packages/loom-orchestration ./packages/loom-orchestration
COPY skills/personal-computing-space ./skills/personal-computing-space
COPY scripts/build_agent_skill.py ./scripts/build_agent_skill.py
RUN pip install --no-cache-dir 'setuptools>=68' 'wheel>=0.45' \
    && python scripts/build_agent_skill.py --output loom_v2/web/downloads
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["uvicorn", "loom_v2.observer.app:app", "--host", "0.0.0.0", "--port", "8080"]
