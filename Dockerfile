FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Nebius Serverless Job: python scripts/benchmark.py --seeds 100 --workers 8
# Nebius Serverless Endpoint / local: python server.py
ENV PORT=8000
ENV HOST=0.0.0.0
CMD ["python", "server.py"]
