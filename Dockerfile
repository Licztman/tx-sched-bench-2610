FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir numpy

COPY src/ /app/src/
COPY data/ /app/data/
COPY README.md REPORT.md /app/

ENV PYTHONUNBUFFERED=1

# Default execution is strictly dry-run + on-chain verification (never spends funds unless explicitly overridden)
ENTRYPOINT ["python3", "src/verify_claims.py"]
CMD ["--mode", "dry-run", "--spot-checks", "4"]
