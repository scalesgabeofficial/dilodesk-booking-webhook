FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py gcal.py ./
# Railway: mount a volume at /data so token.json survives redeploys
ENV DILODESK_SECRETS_DIR=/data
EXPOSE 8000
CMD ["sh","-c","uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --forwarded-allow-ips='*'"]
