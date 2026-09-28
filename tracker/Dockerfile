# WikiMasters — Wikipedia traffic tracker
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY tracker.py storage.py wikimedia.py alerts.py articles.txt ./

RUN useradd --system --uid 10001 --no-create-home tracker
USER tracker

CMD ["python", "tracker.py"]
