FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FINDCHIPS_NO_OPEN=1 \
    HOST=0.0.0.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       chromium \
       ca-certificates \
       fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py /app/app.py
COPY Findchips_Purchasing_Matcher.html /app/Findchips_Purchasing_Matcher.html

EXPOSE 8765

CMD ["python", "app.py"]
