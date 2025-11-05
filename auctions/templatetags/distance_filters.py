from django import template

register = template.Library()

# Distance conversion constant
MILES_TO_KM = 1.60934


@register.filter
def convert_distance(miles, user):
    """
    Convert distance from miles to the user's preferred unit.
    Args:
        miles: Distance in miles (as stored in the database)
        user: The user object to check preferred unit
    Returns:
        Tuple of (converted_value, unit_string)
    """
    if not user or not user.is_authenticated:
        # Default to miles for non-authenticated users
        if miles >= 0:
            return int(round(miles)), "miles"
        return "?", "miles"

    try:
        distance_unit = user.userdata.distance_unit
    except AttributeError:
        distance_unit = "mi"

    if miles < 0:
        return "?", "km" if distance_unit == "km" else "miles"

    if distance_unit == "km":
        # Convert miles to kilometers
        km = miles * MILES_TO_KM
        return int(round(km)), "km"
    else:
        # Default to miles
        return int(round(miles)), "miles"


@register.filter
def distance_display(miles, user):
    """
    Format distance for display with appropriate unit.
    Args:
        miles: Distance in miles (as stored in the database)
        user: The user object to check preferred unit
    Returns:
        Formatted string like "10 miles" or "16 km"
    """
    value, unit = convert_distance(miles, user)
    return f"{value} {unit}"
