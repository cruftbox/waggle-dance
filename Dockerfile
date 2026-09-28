FROM python:3.12-slim

# Run as a non-root user whose IDs match the host owner of the bind-mounted
# data/, config/, and instructions/ directories. Set APP_UID and APP_GID in .env.
ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# DejaVu fonts for the PDF transcript export (waggle_dance/pdf.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-dejavu-extra \
    && rm -rf /var/lib/apt/lists/*

# pytest is included so tests and the smoke script run in the same image as the bot.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --root-user-action=ignore -r requirements-dev.txt

RUN (getent group "$APP_GID" || groupadd -g "$APP_GID" app) \
    && useradd -u "$APP_UID" -g "$APP_GID" -M -d /app -s /usr/sbin/nologin app

COPY --chown=${APP_UID}:${APP_GID} . .
RUN mkdir -p /app/data && chown "$APP_UID:$APP_GID" /app/data

USER ${APP_UID}:${APP_GID}
VOLUME ["/app/data"]

CMD ["python", "-m", "waggle_dance.main"]
