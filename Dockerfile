FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/rs
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p data
ENV PYTHONUNBUFFERED=1 RS_DATA=/srv/rs/data
EXPOSE 8080
# One process: the background worker thread lives in it (RS_WORKER=1).
CMD ["gunicorn", "-w", "1", "-k", "gthread", "--threads", "16", "--timeout", "120", \
     "--graceful-timeout", "30", "-b", "0.0.0.0:8080", "app:app"]
