from django import template

register = template.Library()


@register.filter
def currency_symbol(currency_code):
    """
    Get the currency symbol for a given currency code.
    Args:
        currency_code: Currency code (USD, CAD, GBP)
    Returns:
        Currency symbol string ($ or £)
    """
    if currency_code == "GBP":
        return "£"
    # USD and CAD both use $
    return "$"


@register.filter
def format_price(price, currency_code):
    """
    Format a price with the appropriate currency symbol.
    Args:
        price: The price value
        currency_code: Currency code (USD, CAD, GBP)
    Returns:
        Formatted price string like "$10.00" or "£10.00"
    """
    if price is None:
        return ""
    symbol = currency_symbol(currency_code)
    try:
        return f"{symbol}{float(price):.2f}"
    except (ValueError, TypeError):
        return f"{symbol}{price}"
