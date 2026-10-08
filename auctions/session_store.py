"""``cached_db`` sessions that fall back to the database on a Redis stall and leave Redis after two weeks idle.

Django's own guards ``load`` and ``save`` but not ``exists`` (every new session) or the refill after a
miss, so a Redis stall 500'd first visits. ``delete`` still raises: a sign-out that left the session in
the cache would leave it signed in.

Django caches a session for its whole lifetime -- a year signed in, and 3.8 years for rows from
before that was shortened -- so Redis held every session ever loaded, a million keys on prod. The
row is still the record; an idle session costs one query when it comes back.
"""

import datetime
import logging

from django.contrib.sessions.backends import cached_db
from django.contrib.sessions.backends.db import SessionStore as DBStore

logger = logging.getLogger(__name__)

CACHE_MAX_AGE = int(datetime.timedelta(days=14).total_seconds())


class SessionStore(cached_db.SessionStore):
    def exists(self, session_key):
        try:
            if session_key and (self.cache_key_prefix + session_key) in self._cache:
                return True
        except Exception as e:
            logger.warning("Session cache unreachable, checking the database: %r", e)
        return DBStore.exists(self, session_key)

    def load(self):
        try:
            data = self._cache.get(self.cache_key)
        except Exception:
            data = None
        if data is not None:
            return data
        s = self._get_session_from_db()
        if not s:
            return {}
        data = self.decode(s.session_data)
        self._cache_set(data, self.get_expiry_age(expiry=s.expire_date))
        return data

    def save(self, must_create=False):
        DBStore.save(self, must_create)
        self._cache_set(self._session, self.get_expiry_age())

    def _cache_set(self, data, expiry_age):
        try:
            self._cache.set(self.cache_key, data, min(expiry_age, CACHE_MAX_AGE))
        except Exception as e:
            logger.warning("Session cache unreachable, the database has it: %r", e)
