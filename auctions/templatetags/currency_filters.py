from django import template

register = template.Library()


@register.filter
def currency_symbol(currency_code):
    """The symbol for a currency code -- ``$``, ``£``, ``€``, ``¥`` or ``CHF``."""
    symbol_map = {
        "GBP": "£",
        "EUR": "€",
        "JPY": "¥",
        "CNY": "¥",
        "CHF": "CHF",
        "USD": "$",
        "CAD": "$",
        "AUD": "$",
    }
    return symbol_map.get(currency_code, "$")


@register.filter
def format_price(price, currency_code):
    """A price with its currency symbol: '$10.00', '£10.00', '¥10', 'CHF 10.00'."""
    if price is None:
        return ""
    symbol = currency_symbol(currency_code)
    try:
        # JPY and CNY typically don't use decimal places
        if currency_code in ["JPY", "CNY"]:
            return f"{symbol}{int(float(price))}"
        # CHF typically has space between symbol and amount
        elif currency_code == "CHF":
            return f"{symbol} {float(price):.2f}"
        else:
            return f"{symbol}{float(price):.2f}"
    except (ValueError, TypeError):
        return f"{symbol}{price}"
