# -*- coding: utf-8 -*-
"""Currency conversion helpers: RUB conversion and rate lookups."""

import warnings
from dataclasses import dataclass
from datetime import date as date_cls

import pandas as pd
from loguru import logger

from config.defaults import DEFAULTS
from pipeline.errors import MissingCurrencyRateError
from pipeline.step_config import CurrencyConstants

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
        raise MissingCurrencyRateError(
            message=(
                f"В справочнике отсутствует курс для валюты '{currency}'. "
                f"Необходимо добавить лист с курсом валюты '{currency}' в файл 'Справочники.xlsx' "
                f"(формат имени листа: 'Курс_{currency}') и перезапустить программу."
            ),
            currency=currency,
            reference_name=f"курс_{currency.lower()}",
        )
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


@dataclass(frozen=True)
class RateCoverage:
    """Результат поиска курса: какой курс применён и насколько он «отстал».

    Разница между двумя промахами принципиальна, поэтому и вынесена в
    отдельное поле:

    - ``covered=True``, ``gap_days`` 1–4 — лист курса дотягивается до
      запрошенной даты, просто на неё выпал выходной или праздник. Это
      нормальная календарная поправка, WARNING здесь был бы шумом;
    - ``covered=False`` — лист курса **заканчивается раньше** запрошенной
      даты: справочник неактуален, и суммы переводятся по курсу месячной
      (или более) давности.

    Именно по ``covered`` решается уровень сообщения, поэтому проверка
    актуальности не может быть «молча» зашита в форматирование текста.
    """

    requested_date: date_cls
    rate_date: date_cls
    rate: float
    gap_days: int
    covered: bool
    currency: str


def _parse_target_date(target_date) -> pd.Timestamp:
    """Приводит дату к Timestamp (из строки ДД.ММ.ГГГГ или из Timestamp)."""
    if isinstance(target_date, str):
        parsed = pd.to_datetime(target_date, format=_DATE_FORMAT, errors='coerce')
    else:
        parsed = pd.to_datetime(target_date, errors='coerce')
    if pd.isna(parsed):
        raise ValueError('Invalid target date ' + repr(target_date))
    return parsed


def resolve_rate(context, target_date) -> RateCoverage:
    """Курс на ближайшую дату <= target_date + признак актуальности листа.

    Логирование здесь намеренно отсутствует: вызывающий код сам решает,
    что сообщать. Одна и та же находка читается по-разному при выборе
    даты баланса (pipeline/executors.py, одно сообщение на прогон) и при
    переводе проводок ОПУ (add_ruble_amount_column, где промахов много
    и они агрегируются в один WARNING).
    """
    rates_df = _get_rates_df(context)
    target_ts = _parse_target_date(target_date)

    earlier = rates_df[rates_df[_RATE_DATE_COL] <= target_ts]
    if earlier.empty:
        raise ValueError('No exchange rate on or before ' + str(target_ts.date()) + ' for currency ' + repr(get_currency(context)))

    row = earlier.iloc[-1]
    rate_date = row[_RATE_DATE_COL].date()
    requested_date = target_ts.date()
    return RateCoverage(
        requested_date=requested_date,
        rate_date=rate_date,
        rate=float(row[_RATE_VALUE_COL]),
        gap_days=(requested_date - rate_date).days,
        # Лист покрывает дату, если он дотягивается хотя бы до неё.
        # Тогда промах календарный (выходной, праздник, дыра в справочнике),
        # а если лист заканчивается раньше — это просрочка справочника.
        covered=requested_date <= rates_df[_RATE_DATE_COL].max().date(),
        currency=get_currency(context),
    )


def format_rate_date(value) -> str:
    """Дата в формате листа курса (ДД.ММ.ГГГГ). Принимает str/date/Timestamp."""
    return pd.Timestamp(value).strftime(_DATE_FORMAT)


def warn_stale_rate(coverage: RateCoverage, what: str) -> None:
    """[!] WARNING: лист курса не дотягивает до запрошенной даты.

    Отдельное предупреждение, а не INFO, потому что суммы отчёта посчитаны
    по курсу не той даты. Префикс «[!]» не декоративен: он же служит ключом
    группировки в сводке предупреждений (logging_handling/logger_config.py),
    поэтому сообщения попадут и на титульный лист отчёта.

    Два сообщения, а не одно: консольный формат логгера режет строки, а сводка
    предупреждений — до 200 символов. Первое — факт, второе — что делать.
    """
    logger.warning(
        '[!] {}: лист курса {} заканчивается датой {} — на запрошенную дату {} '
        'курса нет, применён курс {} от {} (отставание {} дн.).',
        what,
        coverage.currency,
        format_rate_date(coverage.rate_date),
        format_rate_date(coverage.requested_date),
        coverage.rate,
        format_rate_date(coverage.rate_date),
        coverage.gap_days,
    )
    logger.warning(
        '[!] Дополните лист Курс_{} актуальными датами, иначе суммы переведены '
        'по устаревшему курсу.',
        coverage.currency,
    )


def get_rate_for_date_with_info(context, target_date, log_miss=True):
    """Возвращает (курс, фактическая дата курса ДД.ММ.ГГГГ) для ближайшей
    даты <= target_date. Фактическая дата может отличаться от запрошенной,
    если курса на запрошенную дату нет (берётся ближайшая предыдущая).

    Уровень сообщения зависит от природы промаха (см. RateCoverage):
    календарный — INFO, просрочка справочника — [!] WARNING.
    """
    coverage = resolve_rate(context, target_date)
    if log_miss and coverage.gap_days:
        if coverage.covered:
            logger.info(
                'No exact rate for {}, using nearest PREVIOUS {} (rate={}).',
                coverage.requested_date, coverage.rate_date, coverage.rate,
            )
        else:
            warn_stale_rate(coverage, 'Дата перевода остатков')
    return coverage.rate, format_rate_date(coverage.rate_date)


def get_rate_for_date(context, target_date):
    """Курс на ближайшую дату <= target_date (см. get_rate_for_date_with_info).

    Молчаливый вариант намеренно: функцию зовут на каждую уникальную дату
    проводки (шаг 17, курсовые разницы), и INFO на каждый промах превращался
    бы в десятки строк, которые забьют настоящее предупреждение о
    неактуальном справочнике. Диагностику собирает вызывающий код.
    """
    return resolve_rate(context, target_date).rate


def _store_rate_summary(context, section: str, payload: dict) -> None:
    """Кладёт сведения о применённом курсе в context.data для титульного листа.

    Сводка нужна получателю файла: запрошенную дату он не видит —
    context.balance_date хранит уже фактическую дату курса, — поэтому без
    сводки просрочка справочника видна только в логе у оператора.
    """
    data = getattr(context, 'data', None)
    if not isinstance(data, dict):
        return
    summary = data.get(CurrencyConstants.SUMMARY_KEY)
    if not isinstance(summary, dict):
        summary = {}
        data[CurrencyConstants.SUMMARY_KEY] = summary
    summary[section] = payload


def record_balance_rate(context, coverage: RateCoverage) -> None:
    """Сводка по курсу перевода остатков баланса (см. _store_rate_summary)."""
    _store_rate_summary(context, CurrencyConstants.SUMMARY_SECTION_BALANCE, {
        CurrencyConstants.SUMMARY_COVERED: coverage.covered,
        CurrencyConstants.SUMMARY_CURRENCY: coverage.currency,
        CurrencyConstants.SUMMARY_REQUESTED_DATE: format_rate_date(coverage.requested_date),
        CurrencyConstants.SUMMARY_RATE_DATE: format_rate_date(coverage.rate_date),
        CurrencyConstants.SUMMARY_RATE: coverage.rate,
        CurrencyConstants.SUMMARY_GAP_DAYS: coverage.gap_days,
    })


def record_opu_rate(
    context,
    currency: str,
    dates_count: int,
    first_date,
    last_date,
    rate_date,
    gap_days: int,
) -> None:
    """Сводка по курсу, применённому к проводкам ОПУ.

    Агрегат, а не одна дата: дат операций много, и получателю важно знать,
    сколько именно сумм посчитано по неактуальному курсу.
    """
    _store_rate_summary(context, CurrencyConstants.SUMMARY_SECTION_OPU, {
        CurrencyConstants.SUMMARY_COVERED: False,
        CurrencyConstants.SUMMARY_CURRENCY: currency,
        CurrencyConstants.SUMMARY_TX_DATES_COUNT: int(dates_count),
        CurrencyConstants.SUMMARY_TX_FIRST_DATE: format_rate_date(first_date),
        CurrencyConstants.SUMMARY_TX_LAST_DATE: format_rate_date(last_date),
        CurrencyConstants.SUMMARY_RATE_DATE: format_rate_date(rate_date),
        CurrencyConstants.SUMMARY_GAP_DAYS: int(gap_days),
    })


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
    дату операции (ближайшая предшествующая дата, см. resolve_rate).
    Если дата операции не парсится — ValueError со списком проблемных строк.
    Для рублёвых компаний столбец равен исходному (курс 1) — единый
    код-путь без ветвлений в бизнес-шагах.

    Две проверки актуальности справочника, симметричные относительно
    границ листа курса:

    - даты раньше первой даты листа (boundary_dates) — «ближайшей
      предыдущей» не существует, берётся самый ранний курс;
    - даты позже последней даты листа (stale_dates) — берётся последний
      курс, и это уже неактуальный справочник: все проводки месяца
      молча уезжают по курсу прошлого месяца.

    Вторую проверку медиана курсов не ловит (месяц просрочки — не
    выброс, а константа), поэтому о ней раньше не было сигнала вовсе.

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
    last_date = rates_df[_RATE_DATE_COL].max()

    rate_by_date = {}
    rate_date_by_date = {}
    boundary_dates = []
    stale_dates = []
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
        else:
            # Сравниваем по календарной дате, а не по полному Timestamp:
            # даты операций могут нести время (31.08.2026 12:00), и тогда
            # «31.08.2026 12:00 > 31.08.2026 00:00» ложно считало бы операцию
            # последнего дня листа просроченной (отставание 0 дн.).
            if ts.date() > last_date.date():
                stale_dates.append(ts)
        rate_by_date[ts] = rate
        rate_date_by_date[ts] = actual_date_str
        if pd.to_datetime(actual_date_str, dayfirst=True).date() != ts.date():
            nearest_prev_count += 1

    if boundary_dates:
        logger.warning(
            '[!] Для валюты {} в справочнике нет курсов на даты раньше {}: '
            '{} дат операций (с {} по {}) переведены по курсу {} от {}.',
            get_currency(context),
            earliest_date.strftime(_DATE_FORMAT),
            len(boundary_dates),
            boundary_dates[0].strftime(_DATE_FORMAT),
            boundary_dates[-1].strftime(_DATE_FORMAT),
            earliest_rate,
            earliest_date.strftime(_DATE_FORMAT),
        )
        logger.warning(
            '[!] Дополните лист Курс_{} датами, предшествующими {}, чтобы перевод '
            'ОПУ был точным.',
            get_currency(context),
            earliest_date.strftime(_DATE_FORMAT),
        )
    if stale_dates:
        # Последняя дата листа — единственный курс, которым переводится всё
        # после неё, поэтому агрегат один: сколько дат и насколько отстал курс.
        stale_rate = float(rates_df[_RATE_VALUE_COL].iloc[-1])
        max_gap = max((ts.date() - last_date.date()).days for ts in stale_dates)
        logger.warning(
            '[!] Лист курса {} заканчивается датой {} — {} дат операций (с {} по {}) '
            'переведены по последнему курсу {} от {} (отставание до {} дн.).',
            get_currency(context),
            last_date.strftime(_DATE_FORMAT),
            len(stale_dates),
            stale_dates[0].strftime(_DATE_FORMAT),
            stale_dates[-1].strftime(_DATE_FORMAT),
            stale_rate,
            last_date.strftime(_DATE_FORMAT),
            max_gap,
        )
        logger.warning(
            '[!] Дополните лист Курс_{} актуальными датами, иначе часть ОПУ '
            'посчитана по устаревшему курсу.',
            get_currency(context),
        )
        record_opu_rate(
            context,
            currency=get_currency(context),
            dates_count=len(stale_dates),
            first_date=stale_dates[0],
            last_date=stale_dates[-1],
            rate_date=last_date,
            gap_days=max_gap,
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
        'точно по справочнику {}; по ближайшей предыдущей (выходной) {}; '
        'по устаревшему курсу (лист не дотягивает) {}; по раннему курсу {}. '
        'Медиана курса листа: {}; порог отклонения курса: {:.0%}.',
        amount_col, rub_col, get_currency(context),
        len(rate_by_date),
        len(rate_by_date) - nearest_prev_count,
        nearest_prev_count - len(boundary_dates) - len(stale_dates),
        len(stale_dates),
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