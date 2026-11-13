FROM python:3.12-slim
# rclone: the nightly off-site backup to Cloudflare R2 through rclone crypt (backup.py, tools/restore.sh).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl rclone \
    && rm -rf /var/lib/apt/lists/* && rclone version
WORKDIR /srv/rs
COPY requirements.txt .
# requirements.txt is the locked, hashed list (compiled from requirements.in): pip installs
# exactly those files or the build fails (SEC-3).
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
# The auto-edit's speech model (motion_edit.py, AUTO_EDIT): baked into the image so no container downloads it at
# run time. AUTO_EDIT_MODEL picks another size; that one is downloaded on first use.
ENV HF_HOME=/opt/hf HF_HUB_DISABLE_TELEMETRY=1
RUN (python -c "from faster_whisper import WhisperModel; WhisperModel('base.en', compute_type='int8')" \
     || echo "speech model not baked in: the auto-edit times captions from the take's pauses until it downloads") \
    && mkdir -p /opt/hf && chmod -R a+rX /opt/hf
COPY . .
# BUILD_ID: one random id per image. The web and worker containers of a release share it, so the web's /health
# waits for its own release's worker heartbeat after a deploy (alerts.worker_state, roadmap M7.2).
RUN mkdir -p data && python -c "import secrets; open('BUILD_ID', 'w').write(secrets.token_hex(8))"
ENV PYTHONUNBUFFERED=1 RS_DATA=/srv/rs/data
EXPOSE 8080
# The web process. With docker-compose (RS_WORKER_MODE=process) the background loop runs in the separate "worker"
# service from this same image (python worker_main.py); without it, as a thread in here (RS_WORKER=1).
CMD ["gunicorn", "-w", "1", "-k", "gthread", "--threads", "16", "--timeout", "120", \
     "--graceful-timeout", "30", "-b", "0.0.0.0:8080", "app:app"]
