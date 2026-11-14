# API FastAPI + executor de migrações (mesma imagem)
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=UTC
WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt

COPY backend/ backend/
COPY connectors/ connectors/
COPY database/ database/

RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

WORKDIR /app/backend
EXPOSE 8000
# PORT é definido pela plataforma (Render); localmente usa 8000.
# RUN_MIGRATIONS_ON_START=true aplica as migrações antes de arrancar (útil em alojamento sem passo de pré-deploy).
CMD ["sh", "-c", "if [ \"$RUN_MIGRATIONS_ON_START\" = \"true\" ]; then python ../database/migrate.py up --seed || exit 1; fi; exec uvicorn --factory api.main:create_app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
