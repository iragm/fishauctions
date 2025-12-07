"""Got sick of these being scattered all over the codebase,
will move more functions here as they get edited"""

from collections import Counter
from datetime import datetime, timedelta


def get_currency_symbol(currency_code):
    """
    Get the currency symbol for a given currency code.
    Args:
        currency_code: Currency code (USD, CAD, GBP, EUR, JPY, AUD, CHF, CNY)
    Returns:
        Currency symbol string ($, £, €, ¥, CHF)
    """
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


def bin_data(
    queryset,
    field_name,
    number_of_bins,
    start_bin=None,
    end_bin=None,
    add_column_for_low_overflow=False,
    add_column_for_high_overflow=False,
    generate_labels=False,
):
    """Pass a queryset and this will spit out a count of how many `field_name`s there are in each `number_of_bins`
    Pass a datetime or an int for start_bin and end_bin, the default is the min/max value in the queryset.
    Specify `add_column_for_low_overflow` and/or `add_column_for_high_overflow`, otherwise data that falls
    outside the start and end bins will be discarded.

    If `generate_labels=True`, a tuple of [labels, data] will be returned
    """
    # some cleanup and validation first
    try:
        queryset = queryset.order_by(field_name)
    except:
        if start_bin is None or end_bin is None:
            msg = f"queryset cannot be ordered by '{field_name}', so start_bin and end_bin are required"
            raise ValueError(msg)
    working_with_date = False
    if queryset.count():
        value = getattr(queryset[0], field_name)
        if isinstance(value, datetime):
            working_with_date = True
        else:
            try:
                float(value)
            except ValueError:
                msg = f"{field_name} needs to be either a datetime or an integer value, got value {value}"
                raise ValueError(msg)
    if start_bin is None:
        start_bin = value
    if end_bin is None:
        end_bin = getattr(queryset.last(), field_name)
    
    # Check for zero range to avoid division by zero
    if working_with_date:
        bin_size = (end_bin - start_bin).total_seconds() / number_of_bins
    else:
        bin_size = (end_bin - start_bin) / number_of_bins
    
    if bin_size == 0:
        msg = f"start_bin and end_bin are equal ({start_bin}), resulting in zero bin size. Cannot divide data into bins."
        raise ValueError(msg)
    
    bin_counts = Counter()
    low_overflow_count = 0
    high_overflow_count = 0
    for item in queryset:
        item_value = getattr(item, field_name)
        if item_value < start_bin:
            low_overflow_count += 1
        elif item_value >= end_bin:
            high_overflow_count += 1
        else:
            if working_with_date:
                diff = (item_value - start_bin).total_seconds()
            else:
                diff = item_value - start_bin
            bin_index = int(diff // bin_size)
            bin_counts[bin_index] += 1

    # Ensure all bins are represented (even those with 0)
    counts_list = [bin_counts[i] for i in range(number_of_bins)]

    # overflow values
    if add_column_for_low_overflow:
        counts_list = [low_overflow_count] + counts_list
    if add_column_for_high_overflow:
        counts_list = counts_list + [high_overflow_count]

    # bin labels
    if generate_labels:
        bin_labels = []
        if add_column_for_low_overflow:
            bin_labels.append("low overflow")
        for i in range(number_of_bins):
            if working_with_date:
                bin_start = start_bin + timedelta(seconds=i * bin_size)
                bin_end = start_bin + timedelta(seconds=(i + 1) * bin_size)
            else:
                bin_start = start_bin + i * bin_size
                bin_end = start_bin + (i + 1) * bin_size

            label = f"{bin_start} - {bin_end if i == number_of_bins - 1 else bin_end - 1}"
            bin_labels.append(label)

        if add_column_for_high_overflow:
            bin_labels.append("high overflow")
        return bin_labels, counts_list
    return counts_list
