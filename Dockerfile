FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "psycopg[binary]==3.2.3"
COPY . .
RUN useradd -m pln && chown -R pln /app
USER pln
ENV PLN_DRY_RUN=true PLN_PUBLISHING_ENABLED=false
EXPOSE 8000
CMD ["python", "-m", "pln_agent", "serve", "--host", "0.0.0.0", "--port", "8000"]
