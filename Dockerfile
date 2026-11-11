FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

RUN apt-get update \
    && apt-get install -y --no-install-recommends libseccomp2=2.5.4-1+deb12u1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN --mount=type=bind,source=requirements.txt,target=/tmp/requirements.txt \
    --mount=type=bind,source=requirements-linux.txt,target=/tmp/requirements-linux.txt \
    pip install --no-cache-dir -r /tmp/requirements.txt -c /tmp/requirements-linux.txt
COPY run_agent.py ./
COPY contest_harness/ ./contest_harness/
ENTRYPOINT ["python", "/app/run_agent.py"]
