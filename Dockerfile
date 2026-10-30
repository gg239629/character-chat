FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/home/user
RUN useradd -m -u 1000 user
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user:user . .
USER user
EXPOSE 7860
CMD ["sh", "-c", "exec uvicorn deploy:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1"]
