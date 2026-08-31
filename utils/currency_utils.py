# -*- coding: utf-8 -*-
"""Currency conversion helpers: RUB conversion and rate lookups."""

import warnings

import pandas as pd
from loguru import logger

_RUB = 'RUB'

# Company currency -> reference registry key
_CURRENCY_RATE_KEYS = {
    'AED': 'курс_aed',
    'CNY': 'курс_cny',
}
_RATE_DATE_COL = 'дата'
_RATE_VALUE_COL = 'курс'
_DATE_FORMAT = '%d.%m.%Y'


def needs_conversion(context):
    currency = getattr(context, 'currency', None)
    if not currency:
        return False
    return str(currency).strip().upper() != _RUB


def get_currency(context):
    currency = getattr(context, 'currency', None)
    if not currency:
        return _RUB
    return str(currency).strip().upper()


def _parse_rate_value(value):
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    s = s.replace(' ', '')
    s = s.replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return float('nan')


def _parse_rate_dates(col: pd.Series) -> pd.Series:
    """Парсинг колонки дат листа курса без UserWarning.

    Основной путь — русский формат ДД.ММ.ГГГГ (текст в Excel).
    Настоящие даты Excel приходят ISO-строками '2025-12-31 00:00:00'
    (load_reference_data читает лист с dtype="string").
    """
    if pd.api.types.is_datetime64_any_dtype(col):
        return col
    parsed = pd.to_datetime(col, format=_DATE_FORMAT, errors='coerce')
    if parsed.isna().any():
        # fallback: ISO/прочие форматы; dayfirst=True для неоднозначных строк.
        # Ложный UserWarning pandas про ISO-формат при dayfirst=True подавляем.
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            parsed = parsed.fillna(pd.to_datetime(col, dayfirst=True, errors='coerce'))
    return parsed


def _get_rates_df(context):
    currency = get_currency(context)
    key = _CURRENCY_RATE_KEYS.get(currency, None)
    if key is None:
        raise ValueError('No rate reference configured for currency ' + repr(currency))
    rate_df = context.references.get(key) if context.references else None
    if rate_df is None or rate_df.empty:
        raise ValueError('Rate reference ' + repr(key) + ' is empty for currency ' + repr(currency))
    df = rate_df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    if _RATE_DATE_COL not in df.columns or _RATE_VALUE_COL not in df.columns:
        raise ValueError('Rate sheet must have columns ' + repr(_RATE_DATE_COL) + ' and ' + repr(_RATE_VALUE_COL))
    df = df[[_RATE_DATE_COL, _RATE_VALUE_COL]].dropna(how='all')
    df[_RATE_DATE_COL] = _parse_rate_dates(df[_RATE_DATE_COL])
    df[_RATE_VALUE_COL] = df[_RATE_VALUE_COL].map(_parse_rate_value)
    df = df.dropna(subset=[_RATE_DATE_COL, _RATE_VALUE_COL])
    df = df.sort_values(_RATE_DATE_COL).reset_index(drop=True)
    if df.empty:
        raw_sample = rate_df.head(5).astype(str).to_dict(orient='records')
        raise ValueError(
            'Rate sheet has no valid rows for currency ' + repr(currency)
            + '; check columns ' + repr(_RATE_DATE_COL) + ' / ' + repr(_RATE_VALUE_COL)
            + '; raw sample: ' + repr(raw_sample)
        )
    return df


def get_rate_for_date_with_info(context, target_date):
    """Возвращает (курс, фактическая дата курса ДД.ММ.ГГГГ) для ближайшей
    даты <= target_date. Фактическая дата может отличаться от запрошенной,
    если курса на запрошенную дату нет (берётся ближайшая предыдущая)."""
    rates_df = _get_rates_df(context)
    if isinstance(target_date, str):
        target_ts = pd.to_datetime(target_date, format=_DATE_FORMAT, errors='coerce')
    else:
        target_ts = pd.to_datetime(target_date, errors='coerce')
    if pd.isna(target_ts):
        raise ValueError('Invalid target date ' + repr(target_date))
    earlier = rates_df[rates_df[_RATE_DATE_COL] <= target_ts]
    if earlier.empty:
        raise ValueError('No exchange rate on or before ' + str(target_ts.date()) + ' for currency ' + repr(get_currency(context)))
    row = earlier.iloc[-1]
    if row[_RATE_DATE_COL].date() != target_ts.date():
        logger.info('No exact rate for {}, using nearest PREVIOUS {} (rate={}).', target_ts.date(), row[_RATE_DATE_COL].date(), row[_RATE_VALUE_COL])
    return float(row[_RATE_VALUE_COL]), row[_RATE_DATE_COL].strftime(_DATE_FORMAT)


def get_rate_for_date(context, target_date):
    """Курс на ближайшую дату <= target_date (см. get_rate_for_date_with_info)."""
    rate, _ = get_rate_for_date_with_info(context, target_date)
    return rate


def get_balance_rate(context):
    rates_df = _get_rates_df(context)
    last = rates_df.iloc[-1]
    logger.info('Balance conversion rate for {} on {}: {}', get_currency(context), last[_RATE_DATE_COL].date(), last[_RATE_VALUE_COL])
    return float(last[_RATE_VALUE_COL])


def get_last_rate_date(context):
    '''Return the latest available rate date as DD.MM.YYYY string.'''
    rates_df = _get_rates_df(context)
    last = rates_df.iloc[-1]
    return last[_RATE_DATE_COL].strftime(_DATE_FORMAT)


def convert_series(series, rate):
    '''Multiply a numeric Series by the rate. returns new Series.'''
    return series.astype(float) * float(rate)