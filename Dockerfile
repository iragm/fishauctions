###########
# BUILDER #
###########

# pull official base image
FROM python:3.11.4-slim-buster AS builder

# set work directory
WORKDIR /usr/src/app

# set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

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
#RUN pip-compile requirements.in --upgrade # fixme

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
RUN addgroup --system app && adduser --system --group app

# create the appropriate directories
ENV APP_HOME=/home/app/web
RUN mkdir /home/user
RUN mkdir $APP_HOME
RUN mkdir $APP_HOME/staticfiles
RUN mkdir $APP_HOME/media

WORKDIR $APP_HOME

# install dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    netcat \
    build-essential \
    pkg-config \
    default-libmysqlclient-dev \
    cron && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# cron setup
COPY crontab /etc/cron.d/django-cron
RUN chmod 0644 /etc/cron.d/django-cron
RUN crontab /etc/cron.d/django-cron
RUN touch /var/log/cron.log

COPY --from=builder /usr/src/app/wheels /wheels
COPY --from=builder /usr/src/app/requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache /wheels/*
RUN pip install mysql-connector-python

COPY . $APP_HOME

# chown all the files to the app user
RUN chown -R app:app $APP_HOME
RUN chown -R app:app /home/user
RUN chown -R app:app /var/log/
USER app

EXPOSE 8000

ENTRYPOINT ["sh", "./entrypoint.sh"]
