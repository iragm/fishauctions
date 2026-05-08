from django import template

register = template.Library()


@register.filter
def get_attr(obj, attr):
    """Return getattr(obj, attr, 0) — used in leaderboard partials to fetch dynamic point fields."""
    return getattr(obj, attr, 0)
