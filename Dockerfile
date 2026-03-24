FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY alert_triage/ alert_triage/

RUN pip install --no-cache-dir .

EXPOSE 8099

CMD ["uvicorn", "alert_triage.app:app", "--host", "0.0.0.0", "--port", "8099"]
