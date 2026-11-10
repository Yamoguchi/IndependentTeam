FROM python:3.12.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends libseccomp2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY run_agent.py task_sandbox.py benchmark.py ./
ENTRYPOINT ["python", "/app/run_agent.py"]
