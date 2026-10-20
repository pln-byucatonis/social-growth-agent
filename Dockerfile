FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
# Optional build secret `ca` = extra CA bundle for builds behind a TLS-inspecting proxy (never stored in the image).
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    pip install --no-cache-dir -r requirements.txt "psycopg[binary]==3.3.6"
COPY pln_agent ./pln_agent
COPY config ./config
COPY migrations ./migrations
RUN useradd -m pln && mkdir -p /app/data && chown -R pln /app
USER pln
# Safe defaults; App Runner sets PORT. The ops proxy supplies PLN_AGENT_API_KEY-authenticated requests.
ENV PLN_DRY_RUN=true PLN_PUBLISHING_ENABLED=false PLN_WORKER=off PORT=8000
EXPOSE 8000
CMD ["python", "-m", "pln_agent", "serve", "--host", "0.0.0.0"]
