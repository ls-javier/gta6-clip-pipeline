FROM python:3.12-slim-trixie
# non-free needed for the iHD (Intel Gen8+/Xe) VA-API driver
RUN sed -i "s/^Components: main$/Components: main non-free non-free-firmware/" /etc/apt/sources.list.d/debian.sources \
 && apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg intel-media-va-driver-non-free vainfo sqlite3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "streamlink>=8.6" \
 && pip install --no-cache-dir torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir demucs==4.0.1 soundfile
WORKDIR /app
ENV TORCH_HOME=/data/models TMPDIR=/data/tmp PYTHONPATH=/app PYTHONUNBUFFERED=1
# Needs at least one enabled streamer in pipeline.db (see watcher/main.py)
CMD ["python", "-m", "watcher.main", "run"]
