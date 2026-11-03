FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/home/user OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
RUN useradd -m -u 1000 user
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY --chown=user:user . .
COPY --from=frontend --chown=user:user /static/web /app/static/web
USER user
EXPOSE 7860
CMD ["sh", "-c", "exec uvicorn deploy:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1"]
