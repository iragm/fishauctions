###########
# BUILDER #
###########

# pull official base image
FROM python:3.11.9 AS builder

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
FROM python:3.11.9-slim AS test
COPY ./requirements-test.txt .
RUN pip install -r requirements-test.txt


#########
# Dev Container #
#########


FROM builder AS dev
COPY ./requirements*.txt .
RUN pip install -r requirements.txt

#########
# FINAL #
#########

# pull official base image
FROM python:3.11.9-slim

# create directory for the app user
RUN mkdir -p /home/app

# create the app user
#RUN addgroup --system app && adduser --system --group app
# specifying IDs to be the same as the nginx users, to set permissions correctly on images
RUN groupadd -r -g ${PUID-1000} app && \
    useradd -r -u ${PGID-1000} -g app -m -d /home/app -s /bin/bash app

# create the appropriate directories
ENV APP_HOME=/home/app/web
RUN mkdir /home/user
RUN mkdir $APP_HOME
RUN mkdir $APP_HOME/staticfiles
RUN mkdir $APP_HOME/mediafiles
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
    # libheif dependencies
    libheif-dev \
    && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN pip install pip-tools

# cron setup
COPY crontab /etc/cron.d/django-cron
RUN chmod 0644 /etc/cron.d/django-cron
RUN chmod gu+rw /var/run
RUN chmod gu+s /usr/sbin/cron
RUN crontab /etc/cron.d/django-cron
RUN touch /var/log/cron.log

COPY --from=builder /usr/src/app/wheels /wheels
COPY --from=builder /usr/src/app/requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache /wheels/*
RUN pip install mysql-connector-python

# Sometimes we need customizations made to python packages
# List changes in the .sh script, making sure it fails gracefully
COPY python_file_hack.sh /tmp/python_file_hack.sh
RUN chmod +x /tmp/python_file_hack.sh
RUN /tmp/python_file_hack.sh

# volume instead
#COPY . $APP_HOME

# chown all the files to the app user
RUN chown -R app:app $APP_HOME
RUN chown -R app:app /home/user
RUN chown -R app:app /var/log/
RUN chown -R app:app /var/log/
RUN chown -R app:app /home/app/web/mediafiles
RUN chown -R app:app /home/app/web/staticfiles
RUN chown -R app:app /home/app/.cache
USER app

EXPOSE 8000

ENTRYPOINT ["sh", "./entrypoint.sh"]
