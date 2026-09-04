"""``@cached_property`` on a model, and the invalidation that makes it safe.

A ``cached_property`` freezes its value for the life of the *instance*. That is exactly what a read
path wants -- a row of a lot list renders ``lot.thumbnail`` three times and reaches ``lot.bids``
four times, and each of those was a query -- and exactly what a write path must not have.

Two rules keep it correct, and :class:`CachedPropertiesMixin` implements the first of them:

* **Saving a row drops every cached value on that instance**, so nothing can answer from before its
  own write. That is automatic here rather than left to callers, because a stale read is a wrong
  page rather than an error and would be found in production instead of in review.
* **A write to a different table needs somebody to say so.** ``bid_on_lot`` saves a ``Bid`` and then
  asks the lot who the high bidder is now; no ``Lot.save()`` happened. :class:`InvalidatesRelatedCache`
  is how that is declared -- ``Bid.invalidates_cache_on = ("lot_number",)`` -- at the write, not at
  every call site.

Which properties are cached, and why each one was worth it, is in ``OPTIMIZATION.md``.
"""

from django.utils.functional import cached_property


class InvalidatesRelatedCache:
    """Drop the cached properties on the rows this one is derived from, whenever it is written.

    The other half of the problem :class:`CachedPropertiesMixin` solves. A cached value goes stale
    when a *different* table changes under it -- ``Lot.bids`` when a ``Bid`` is saved,
    ``Auction.locations`` when a ``PickupLocation`` is added, ``VolunteerJob.signups_count`` when
    somebody signs up -- and the write is the only place that cannot be forgotten. Every one of
    these was found by a test failing after the property it feeds was cached.

    Name the forward foreign keys in ``invalidates_cache_on``. Only instances the caller is already
    holding are touched (they are reached through ``fields_cache``), so this never fetches a row
    just to invalidate a copy nobody has -- which is also why it cannot help a caller holding an
    object from *before* an HTTP request; there, re-read.

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
        # read the related objects before the delete clears anything
        related = [self._state.fields_cache.get(name) for name in self.invalidates_cache_on]
        result = super().delete(*args, **kwargs)
        for obj in related:
            if obj is not None:
                obj.invalidate_cached_properties()
        return result


class CachedPropertiesMixin:
    """Adds ``invalidate_cached_properties()``, and calls it after every save.

    Mix in *before* ``models.Model`` so the ``save()`` here sits between the model's own ``save()``
    and Django's. With no arguments ``invalidate_cached_properties()`` drops every cached value;
    name properties to drop only those. Naming one that was never read is not an error, so a caller
    does not have to know which properties the request happened to touch.
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
        # refresh_from_db reloads the columns and nothing else, so without this an instance can
        # come back from the database carrying answers derived before the reload -- which is worse
        # than not refreshing at all, because the caller asked for current data and got a mix.
        self.invalidate_cached_properties()
