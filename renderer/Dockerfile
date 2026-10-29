# kindle-dash — renders a grayscale dashboard PNG for a jailbroken Kindle.
# Pulls time, Open-Meteo weather (forecast/rain/sun), optional Home Assistant
# calendars and optional Claude usage, and serves it at GET /dash.png for the device to fetch.
FROM python:3.14-slim

# tzdata: local time/date on the dashboard. fonts-dejavu-core: TTFs Pillow draws
# with (slim ships none). Both are tiny.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

# State (Claude OAuth tokens + usage cache) lives here; mount a volume to persist.
RUN mkdir -p /data
ENV DASH_STATE_DIR=/data

EXPOSE 8080
CMD ["python", "app.py"]
