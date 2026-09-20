FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home trader
COPY --chown=trader:trader apps ./apps
COPY --chown=trader:trader core ./core
COPY --chown=trader:trader services ./services
COPY --chown=trader:trader infrastructure ./infrastructure
COPY --chown=trader:trader alembic.ini .
USER trader
CMD ["python", "-m", "uvicorn", "apps.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
