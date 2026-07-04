FROM python:3.11-slim

WORKDIR /app

# Build context is the repo root (Railway resolves dockerfilePath relative to
# root but keeps root as the build context), so paths here are repo-root-relative.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

# Railway (and most PaaS platforms) inject $PORT at runtime; default to 8000 for local docker run.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
