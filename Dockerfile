###########
# BUILDER #
###########

# pull official base image
# Pin the 3.11 minor series (not a frozen .9 patch) so `docker compose build --pull`
# picks up CPython security patch releases. Stays on 3.11 -- the project targets
# 3.11.x, not 3.12.
FROM python:3.11 AS builder

# set work directory
WORKDIR /usr/src/app

# set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    # mysqlclient dependencies
    pkg-config \
    default-libmysqlclient-dev \
    # TODO: Remove libheif dependencies. They are only necssary because
    # pyheif doesn't yet release an ARM compatible wheel. Once a compatible
    # wheel is published on pypi, these dependencies should be removed from
    # both the builder and final images.
    libheif-dev \
    # end libheif dependencies
    && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip pip-tools

# This is a lot of stuff, not really needed
# COPY . /usr/src/app/

COPY ./requirements.in .
# generate an updated requirements.txt file with the latest versions of all packages
#RUN pip-compile requirements.in --upgrade

# install python dependencies
COPY ./requirements.txt .
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /usr/src/app/wheels -r requirements.txt

#########
# Test and CI #
#########

# pull official base image
FROM python:3.11-slim AS test
COPY ./requirements-test.txt .
RUN pip install -r requirements-test.txt


#########
# Dev Container #
#########


FROM builder AS dev
COPY ./requirements*.txt .
RUN pip install -r requirements.txt
RUN pip install -r requirements-test.txt

#########
# FINAL #
#########

# pull official base image
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# App user. PUID/PGID are build args (default 1000), kept equal to the swag/nginx
# container's PUID/PGID so bind-mounted media/static share ownership across
# containers. The USER gets PUID and the GROUP gets PGID -- these were previously
# swapped (group<-PUID, user<-PGID), silently mismatching ownership whenever the
# two differed. Declared as ARG so a deployer can wire them via compose build.args.
ARG PUID=1000
ARG PGID=1000
ENV APP_HOME=/home/app/web

RUN groupadd -r -g ${PGID} app && \
    useradd -r -u ${PUID} -g app -m -d /home/app -s /bin/bash app && \
    mkdir -p /home/logs /home/app/logs /home/user /home/app/.cache /home/app/.gunicorn \
             "$APP_HOME/staticfiles" "$APP_HOME/mediafiles/images" && \
    touch /home/app/logs/django.log

WORKDIR $APP_HOME

# System packages. build-essential + pkg-config + default-libmysqlclient-dev are
# intentionally kept in this "slim" final image (not just runtime libs): update-
# packages.sh runs `pip-compile` INSIDE this container, and mysqlclient ships no
# manylinux wheel, so resolving/building it needs the compiler + MySQL headers.
# (To slim the prod image later, move that pip-compile step to the builder/dev
# stage.) libheif-dev + libpango* back pyheif/weasyprint; default-mysql-client and
# nano are ops conveniences; netcat is a lightweight connectivity check.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    netcat-traditional \
    build-essential \
    pkg-config \
    default-libmysqlclient-dev \
    default-mysql-client \
    nano \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libheif-dev \
    && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
RUN pip install pip-tools

COPY --from=builder /usr/src/app/wheels /wheels
COPY --from=builder /usr/src/app/requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache /wheels/*

# No `COPY . $APP_HOME`: this deploy bind-mounts the repo into every service (see
# docker-compose.yaml) and updates via `git pull` + restart, so app code is
# intentionally not baked into the image.

# chown app-owned trees. Everything the app writes lives under /home/app (incl.
# $APP_HOME, staticfiles, mediafiles, .cache, .gunicorn), so one recursive chown
# covers them; /home/logs and /var/log are separate. Bind-mounted paths get their
# real ownership from the host mount at runtime (entrypoint.sh warns if unwritable).
RUN chown -R app:app /home/app /home/logs /home/user /var/log
USER app

EXPOSE 8000

ENTRYPOINT ["sh", "./entrypoint.sh"]
