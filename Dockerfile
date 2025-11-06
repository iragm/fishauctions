###########
# BUILDER #
###########

# pull official base image
FROM python:3.11.9 AS builder

# Build argument to optionally disable SSL verification
ARG DISABLE_PIP_SSL_VERIFY=0

# set work directory
WORKDIR /usr/src/app

# set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Configure pip to bypass SSL if needed (affects all pip operations including build dependencies)
# Also increase timeout significantly for very slow connections
RUN if [ "$DISABLE_PIP_SSL_VERIFY" = "1" ]; then \
        pip config set global.trusted-host "pypi.org pypi.python.org files.pythonhosted.org"; \
    fi && \
    pip config set global.timeout 600 && \
    pip config set global.retries 5

# install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    # mysqlclient dependencies
    pkg-config \
    default-libmysqlclient-dev \
    # TODO: Remove libheif dependencies. They are only necessary because
    # pyheif doesn't yet release an ARM compatible wheel. Once a compatible
    # wheel is published on pypi, these dependencies should be removed from
    # both the builder and final images.
    libheif-dev \
    # end libheif dependencies
    && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install pip and pip-tools
RUN pip install --upgrade pip pip-tools

# This is a lot of stuff, not really needed
# COPY . /usr/src/app/

COPY ./requirements.in .
# generate an updated requirements.txt file with the latest versions of all packages
#RUN pip-compile requirements.in --upgrade

# install python dependencies
COPY ./requirements.txt .
# Try to use pre-built wheels from PyPI, only building from source if necessary
# Split into smaller batches to avoid timeout on any single package
# Increase buffer size to handle slow connections
ENV PIP_DEFAULT_TIMEOUT=600
RUN pip wheel --wheel-dir /usr/src/app/wheels -r requirements.txt || \
    (pip wheel --wheel-dir /usr/src/app/wheels --prefer-binary -r requirements.txt)

#########
# Test and CI #
#########

# pull official base image
FROM python:3.11.9-slim AS test

# Build argument to optionally disable SSL verification (for corporate/CI environments with SSL inspection)
# Set DISABLE_PIP_SSL_VERIFY=1 during build if needed: docker compose build --build-arg DISABLE_PIP_SSL_VERIFY=1
ARG DISABLE_PIP_SSL_VERIFY=0

# Configure pip to bypass SSL if needed (affects all pip operations including build dependencies)
RUN if [ "$DISABLE_PIP_SSL_VERIFY" = "1" ]; then \
        pip config set global.trusted-host "pypi.org pypi.python.org files.pythonhosted.org"; \
    fi

# Update CA certificates to handle SSL certificate issues
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY ./requirements-test.txt .
RUN pip install -r requirements-test.txt


#########
# Dev Container #
#########


FROM builder AS dev

# Inherit build arg
ARG DISABLE_PIP_SSL_VERIFY=0

# Pip config is inherited from builder stage

COPY ./requirements*.txt .
RUN pip install -r requirements.txt && \
    pip install -r requirements-test.txt

#########
# FINAL #
#########

# pull official base image
FROM python:3.11.9-slim

# Build argument to optionally disable SSL verification
ARG DISABLE_PIP_SSL_VERIFY=0

# Configure pip to bypass SSL if needed
RUN if [ "$DISABLE_PIP_SSL_VERIFY" = "1" ]; then \
        pip config set global.trusted-host "pypi.org pypi.python.org files.pythonhosted.org"; \
    fi

# create directory for the app user
RUN mkdir -p /home/app

# create the app user
#RUN addgroup --system app && adduser --system --group app
# specifying IDs to be the same as the nginx users, to set permissions correctly on images
RUN groupadd -r -g ${PUID-1000} app && \
    useradd -r -u ${PGID-1000} -g app -m -d /home/app -s /bin/bash app

# create the appropriate directories
ENV APP_HOME=/home/app/web
RUN mkdir /home/logs
RUN mkdir -p /home/app/logs
RUN touch /home/app/logs/django.log
RUN mkdir /home/user
RUN mkdir -p $APP_HOME
RUN mkdir -p $APP_HOME/staticfiles
RUN mkdir -p $APP_HOME/mediafiles
RUN mkdir -p $APP_HOME/mediafiles/images
RUN mkdir /home/app/.cache

WORKDIR $APP_HOME

# install dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    netcat-traditional \
    build-essential \
    pkg-config \
    default-libmysqlclient-dev \
    cron \
    nano \
    # python3-pip \
    # python3-cffi \
    # python3-brotli \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    # libheif dependencies
    libheif-dev \
    && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install pip-tools
RUN pip install pip-tools

# cron setup
COPY crontab /etc/cron.d/django-cron
RUN chmod 0644 /etc/cron.d/django-cron
RUN chmod gu+rw /var/run
RUN chmod gu+s /usr/sbin/cron
RUN crontab /etc/cron.d/django-cron
RUN touch /var/log/cron.log

COPY --from=builder /usr/src/app/requirements.txt .

# Install packages using only pre-built binary wheels to avoid compilation timeouts
# Falls back to allowing source builds if binary wheels aren't available
ENV PIP_PREFER_BINARY=1
RUN pip install --upgrade pip && \
    (pip install --only-binary :all: -r requirements.txt || pip install -r requirements.txt) && \
    pip install mysql-connector-python

# Sometimes we need customizations made to python packages
# List changes in the .sh script, making sure it fails gracefully
COPY python_file_hack.sh /tmp/python_file_hack.sh
RUN chmod +x /tmp/python_file_hack.sh
RUN /tmp/python_file_hack.sh

# volume instead
#COPY . $APP_HOME

# chown all the files to the app user
# I am not sure that these are actually doing anything
# Docker bind mount permissions are from the host and need to be set there
# There's a test in entrypoint.sh that will show the user and permissions
RUN chown -R app:app $APP_HOME
RUN chown -R app:app /home/logs
RUN chown -R app:app /home/app/logs/
RUN chown -R app:app /home/user
RUN chown -R app:app /var/log/
RUN chown -R app:app /var/log/
RUN chown -R app:app /home/app/web/mediafiles
RUN chown -R app:app /home/app/web/mediafiles/images
RUN chown -R app:app /home/app/web/staticfiles
RUN chown -R app:app /home/app/.cache
USER app

EXPOSE 8000

ENTRYPOINT ["sh", "./entrypoint.sh"]
