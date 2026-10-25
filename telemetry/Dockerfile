FROM python:3.13-slim
WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY bi ./bi
RUN useradd --system --uid 10001 atlas && mkdir -p /data && chown atlas /data
USER atlas
ENV DATABASE_URL=sqlite:////data/telemetry.db PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT}"]
