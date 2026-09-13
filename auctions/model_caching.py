"""``@cached_property`` on a model, and the invalidation that makes it safe.

Two rules: :class:`CachedPropertiesMixin` drops every cached value on save, since a stale read is
worse than a missed cache. :class:`InvalidatesRelatedCache` handles writes that don't call
``save()`` on the row being read, e.g. a ``Bid`` save must invalidate the ``Lot`` it points at.

Which properties are cached is visible where declared; their cost is asserted, not described, in
``auctions/test_query_counts.py``.
"""

from django.utils.functional import cached_property


class InvalidatesRelatedCache:
    """Drop cached properties on the rows this one is derived from, whenever it is written.

    Name the forward foreign keys in ``invalidates_cache_on``. Only instances the caller already
    holds are touched (via ``fields_cache``) -- this never fetches a row just to invalidate it, and
    cannot help a caller holding an object from before this request; there, re-read.

    Mix in **before** ``models.Model``, and before ``CachedPropertiesMixin`` when a model has both.
    """

    #: Forward FK names whose target should have its cached properties dropped on write.
    invalidates_cache_on = ()

    def _invalidate_related_caches(self):
        for name in self.invalidates_cache_on:
            related = self._state.fields_cache.get(name)
            if related is not None:
                related.invalidate_cached_properties()

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self._invalidate_related_caches()

    def delete(self, *args, **kwargs):
        # Read the related objects before the delete clears anything.
        related = [self._state.fields_cache.get(name) for name in self.invalidates_cache_on]
        result = super().delete(*args, **kwargs)
        for obj in related:
            if obj is not None:
                obj.invalidate_cached_properties()
        return result


class CachedPropertiesMixin:
    """Adds ``invalidate_cached_properties()``, and calls it after every save.

    Mix in *before* ``models.Model``. With no arguments, drops every cached value; name properties
    to drop only those. Naming one that was never read is not an error.
    """

    @classmethod
    def _cached_property_names(cls):
        """The ``cached_property`` attributes on this class, worked out once per class."""
        names = cls.__dict__.get("_cached_property_name_cache")
        if names is None:
            names = frozenset(
                name
                for klass in cls.__mro__
                for name, value in vars(klass).items()
                if isinstance(value, cached_property)
            )
            cls._cached_property_name_cache = names
        return names

    def invalidate_cached_properties(self, *names):
        for name in names or self._cached_property_names():
            self.__dict__.pop(name, None)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.invalidate_cached_properties()

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        # Reloads columns only; without this, cached values could reflect the pre-reload state.
        self.invalidate_cached_properties()
