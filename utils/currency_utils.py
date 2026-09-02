# -*- coding: utf-8 -*-
"""Currency conversion helpers: RUB conversion and rate lookups."""

import warnings

import pandas as pd
from loguru import logger

from config.defaults import DEFAULTS

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


def get_rate_for_date_with_info(context, target_date, log_miss=True):
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
    if log_miss and row[_RATE_DATE_COL].date() != target_ts.date():
        logger.info('No exact rate for {}, using nearest PREVIOUS {} (rate={}).', target_ts.date(), row[_RATE_DATE_COL].date(), row[_RATE_VALUE_COL])
    return float(row[_RATE_VALUE_COL]), row[_RATE_DATE_COL].strftime(_DATE_FORMAT)


def get_rate_for_date(context, target_date):
    """Курс на ближайшую дату <= target_date (см. get_rate_for_date_with_info)."""
    rate, _ = get_rate_for_date_with_info(context, target_date)
    return rate


def get_earliest_rate(context):
    '''Самый ранний курс листа Курс_<валюта>.

    Зеркалит fallback в add_ruble_amount_column: для дат раньше начала
    листа курса «ближайшей предыдущей» даты не существует — берётся
    самый ранний курс справочника.
    '''
    rates_df = _get_rates_df(context)
    row = rates_df.iloc[0]
    return float(row[_RATE_VALUE_COL])


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


def get_rate_median(context):
    '''Медиана курсов листа Курс_<валюта> — база автоконтроля подразумеваемого курса.

    Используется: (а) в add_ruble_amount_column (контроль применённых курсов),
    (б) в контрольных точках шагов ОПУ (поиск «ядовитых» строк ед≈0/руб≠0
    и перезаписей рублёвого эквивалента — задача про курс 92,48 RUB/AED).
    '''
    rates_df = _get_rates_df(context)
    return float(rates_df[_RATE_VALUE_COL].median())


def get_rate_deviation_limit(context):
    '''Порог отклонения курса от медианы — tolerance_rate_deviation.

    Берётся из context.tolerance_params (лист «Параметры»); при отсутствии —
    дефолт из config/defaults.py. Устойчив к контекстам без tolerance_params.
    '''
    tolerance_params = getattr(context, 'tolerance_params', None)
    if not isinstance(tolerance_params, dict):
        tolerance_params = {}
    return float(
        tolerance_params.get('tolerance_rate_deviation', DEFAULTS.get('tolerance_rate_deviation', 0.3))
    )


def convert_series(series, rate):
    '''Multiply a numeric Series by the rate. returns new Series.'''
    return series.astype(float) * float(rate)


def refresh_rub_equivalent(
    df,
    context,
    source_col='сальдо, тыс.ед.',
    rub_col='сальдо, тыс.руб.',
):
    '''Пересчитывает рублёвый эквивалент сальдо после мутаций строк сводной ОСВ.

    Шаги 6/7/10/11/12 добавляют, разбивают или заменяют строки сводной ОСВ:
    в новых строках рублёвый столбец NaN, в изменённых — устаревший.
    Курс на дату баланса един для всего баланса, поэтому инвариант
    "руб = ед × курс" всегда верен — столбец просто пересчитывается из
    исходного. Для рублёвых компаний df возвращается без изменений.
    '''
    if not needs_conversion(context):
        return df
    if source_col not in df.columns:
        return df
    rate = get_rate_for_date(context, context.balance_date)
    df[rub_col] = convert_series(df[source_col], rate).round(2)
    return df


def add_ruble_amount_column(
    df,
    context,
    date_col='Дата',
    amount_col='Сумма',
    rub_col='Сумма_руб',
):
    '''Добавляет рублёвый эквивалент сумм проводок по курсу на дату операции.

    Для валютных компаний курс берётся из листа Курс_<валюта> на каждую
    дату операции (ближайшая предшествующая дата, см. get_rate_for_date).
    Если дата операции не парсится — ValueError со списком проблемных строк.
    Для рублёвых компаний столбец равен исходному (курс 1) — единый
    код-путь без ветвлений в бизнес-шагах.

    Автоконтроль: каждый применённый курс сверяется с медианой курсов листа;
    отклонение больше tolerance_rate_deviation (лист «Параметры», дефолт в
    config/defaults.py) — WARNING. Защита от ошибочных значений в листе курса.
    '''
    if not needs_conversion(context):
        df[rub_col] = df[amount_col].astype(float)
        return df
    if date_col not in df.columns:
        raise ValueError(
            'Column ' + repr(date_col) + ' not found for currency conversion'
        )
    dates = _parse_rate_dates(df[date_col])
    if dates.isna().any():
        bad_mask = dates.isna()
        bad_sample = df.loc[bad_mask, date_col].head(5).astype(str).tolist()
        raise ValueError(
            str(int(bad_mask.sum())) + ' rows have unparseable ' + repr(date_col)
            + '; cannot convert amounts to RUB; sample: ' + repr(bad_sample)
        )
    rates_df = _get_rates_df(context)
    earliest_row = rates_df.iloc[0]
    earliest_rate = float(earliest_row[_RATE_VALUE_COL])
    earliest_date = earliest_row[_RATE_DATE_COL]

    rate_by_date = {}
    rate_date_by_date = {}
    boundary_dates = []
    nearest_prev_count = 0
    for ts in pd.Series(dates.unique()).sort_values():
        try:
            rate, actual_date_str = get_rate_for_date_with_info(context, ts, log_miss=False)
        except ValueError:
            # Дата операции раньше самой ранней даты справочника —
            # "ближайшей предыдущей" не существует. Берём самый ранний
            # курс справочника и собираем даты для сводного WARNING.
            rate = earliest_rate
            actual_date_str = earliest_date.strftime(_DATE_FORMAT)
            boundary_dates.append(ts)
        rate_by_date[ts] = rate
        rate_date_by_date[ts] = actual_date_str
        if pd.to_datetime(actual_date_str, dayfirst=True).date() != ts.date():
            nearest_prev_count += 1

    if boundary_dates:
        logger.warning(
            '[!] Для валюты {} в справочнике нет курсов на даты раньше {}: '
            '{} дат операций (с {} по {}) переведены по курсу {} от {}. '
            'Дополните лист курса, чтобы перевод ОПУ был точным.',
            get_currency(context),
            earliest_date.strftime(_DATE_FORMAT),
            len(boundary_dates),
            boundary_dates[0].strftime(_DATE_FORMAT),
            boundary_dates[-1].strftime(_DATE_FORMAT),
            earliest_rate,
            earliest_date.strftime(_DATE_FORMAT),
        )
    # ── Автоконтроль курса: отклонение применённого курса от медианы листа ──
    # Ловит ошибочные значения в листе Курс_<валюта> (кейс: курс 92,48 RUB/AED
    # в листе Курс_AED — см. AGENTS.md). Порог — tolerance_rate_deviation
    # (лист «Параметры», дефолт в config/defaults.py).
    median_rate = get_rate_median(context)
    deviation_limit = get_rate_deviation_limit(context)
    if median_rate > 0 and deviation_limit > 0:
        deviation_groups: dict[tuple[float, str], list] = {}
        for ts in sorted(rate_by_date):
            applied_rate = rate_by_date[ts]
            if abs(applied_rate / median_rate - 1.0) > deviation_limit:
                deviation_groups.setdefault((applied_rate, rate_date_by_date[ts]), []).append(ts)
        for (applied_rate, rate_date_str), op_dates in deviation_groups.items():
            logger.warning(
                '[!] Автоконтроль курса {}: применён курс {} от {}, отклонение от медианы '
                'листа курса ({}) составляет {:.0%} при пороге {:.0%}. '
                'Дат операций: {} (с {} по {}). Проверьте лист Курс_{} на ошибочные значения.',
                get_currency(context),
                applied_rate,
                rate_date_str,
                median_rate,
                abs(applied_rate / median_rate - 1.0),
                deviation_limit,
                len(op_dates),
                op_dates[0].strftime(_DATE_FORMAT),
                op_dates[-1].strftime(_DATE_FORMAT),
                get_currency(context),
            )
    logger.debug(
        'Конвертация {} -> {} ({}): уникальных дат операций {}; '
        'точно по справочнику {}; по ближайшей предыдущей {}; по раннему курсу {}. '
        'Медиана курса листа: {}; порог отклонения курса: {:.0%}.',
        amount_col, rub_col, get_currency(context),
        len(rate_by_date),
        len(rate_by_date) - nearest_prev_count,
        nearest_prev_count - len(boundary_dates),
        len(boundary_dates),
        median_rate,
        deviation_limit,
    )
    logger.debug(
        'Маппинг дата операции -> (курс, дата курса) ({}): {}',
        get_currency(context),
        ' | '.join(
            f'{ts.strftime(_DATE_FORMAT)} -> ({rate_by_date[ts]}, {rate_date_by_date[ts]})'
            for ts in sorted(rate_by_date)
        ),
    )
    df[rub_col] = df[amount_col].astype(float) * dates.map(rate_by_date)
    return df