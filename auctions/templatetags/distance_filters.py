from django import template

register = template.Library()

# Distance conversion constant
MILES_TO_KM = 1.60934


@register.filter
def convert_distance(miles, user):
    """``miles`` in the user's preferred unit, as ``(value, unit)`` -- or None when it is zero or
    unreadable. The stored value may be a number or a string.
    """
    # Zero means the user's location is unset, or the lot is part of an auction: show nothing.
    if miles is None:
        return None

    try:
        miles = float(miles)
    except (ValueError, TypeError):
        return None

    if miles == 0:
        return None

    if not user or not user.is_authenticated:
        # Default to miles for non-authenticated users
        if miles > 0:
            return int(round(miles)), "miles"
        return None

    try:
        distance_unit = user.userdata.distance_unit
    except AttributeError:
        distance_unit = "mi"

    if miles < 0:
        return None

    if distance_unit == "km":
        # Convert miles to kilometers
        km = miles * MILES_TO_KM
        return int(round(km)), "km"
    else:
        # Default to miles
        return int(round(miles)), "miles"


@register.filter
def distance_display(miles, user):
    """``miles`` ready to print -- '10 miles', '16 km' -- or '' when there is nothing to say."""
    result = convert_distance(miles, user)
    if result is None:
        return ""
    value, unit = result
    return f"{value} {unit}"
