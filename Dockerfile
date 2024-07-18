###########
# BUILDER #
###########

# pull official base image
FROM python:3.11.4-slim-buster AS builder

# set work directory
WORKDIR /usr/src/app

# set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    default-libmysqlclient-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip pip-tools
COPY . /usr/src/app/

# generate an updated requirements.txt file with the latest versions of all packages
RUN pip-compile requirements.in --upgrade

# install python dependencies
# COPY ./requirements.txt . # no need to copy this, we just generated it
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /usr/src/app/wheels -r requirements.txt


#########
# FINAL #
#########

# pull official base image
FROM python:3.11.4-slim-buster

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

WORKDIR $APP_HOME

# install dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    netcat \
    build-essential \
    pkg-config \
    default-libmysqlclient-dev \
    cron \
    nano && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

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

# this is a hack to overwrite Django's broken TZ stuff that causes errors (500 page) to fail.  See https://code.djangoproject.com/ticket/33674
COPY fix_tz_hack.sh /tmp/fix_tz_hack.sh
RUN chmod +x /tmp/fix_tz_hack.sh
RUN /tmp/fix_tz_hack.sh

# volume instead
#COPY . $APP_HOME

# chown all the files to the app user
RUN chown -R app:app $APP_HOME
RUN chown -R app:app /home/user
RUN chown -R app:app /var/log/
RUN chown -R app:app /var/log/
RUN chown -R app:app /home/app/web/mediafiles
RUN chown -R app:app /home/app/web/staticfiles

USER app

EXPOSE 8000

ENTRYPOINT ["sh", "./entrypoint.sh"]
