# -*- coding: utf-8 -*-
"""
Взгляд компании на справочники меппинга (Меппинг_опу / Меппинг_бб).

В листах меппинга есть колонка «область действия»
(config.settings.REFERENCE_SCOPE_COL = 'компания'):
  * 'все' (или пусто) — строка действует для всех компаний (универсальный блок);
  * имя компании      — строка действует только для неё (индивидуальный блок).

Индивидуальная строка ПЕРЕКРЫВАЕТ универсальную с тем же ключом листа: одна и
та же статья/счёт в разных компаниях может означать разное (пример —
«Доходы (расходы), связанные со сдачей имущества в аренду (субаренду)»:
у одной компании это проценты ППА, у остальных — аренда).

Модуль чистый (только pandas): импортируется из pipeline/executors.py до старта
конвейера и не зависит от шагов — иначе получился бы цикл pipeline <-> utils.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import pandas as pd
from loguru import logger

from config.settings import REFERENCE_SCOPE_COL, SCOPE_ALL_VALUE

# Разделитель частей составного ключа строки меппинга
KEY_SEPARATOR = '\x1f'


@dataclass
class ScopeDiagnostics:
    """Итог применения индивидуального меппинга одной компании."""

    reference_name: str = ''
    company: str = ''
    column_missing: bool = False
    universal_rows: int = 0
    individual_rows: int = 0
    overridden_rows: int = 0
    # Строки, адресованные другим компаниям (в т.ч. с опечаткой в имени) и
    # отброшенные из кадра. Их отбрасывание — не побочный эффект наличия
    # индивидуальных строк, а самостоятельное правило (см. resolve_company_view),
    # поэтому счётчик нужен даже тогда, когда индивидуальных строк нет.
    dropped_foreign_rows: int = 0
    unknown_scope_values: list = field(default_factory=list)
    overridden_frame: Optional[pd.DataFrame] = None
    individual_frame: Optional[pd.DataFrame] = None
    individual_only_frame: Optional[pd.DataFrame] = None

    @property
    def applied(self) -> bool:
        """True, если для компании реально применились индивидуальные строки."""
        return self.individual_rows > 0


def _normalized_scope(series: pd.Series) -> pd.Series:
    """Нормализует колонку области действия: string, strip, нижний регистр."""
    return (
        series.astype('string')
        .fillna(SCOPE_ALL_VALUE)
        .str.strip()
        .str.casefold()
    )


def individual_scope_mask(
    df: pd.DataFrame,
    scope_col: str = REFERENCE_SCOPE_COL,
) -> pd.Series:
    """
    Маска строк, адресованных конкретным компаниям (scope != 'все').

    Единственная точка правды для признака «строка — индивидуальная»: и
    resolve_company_view, и шаг 17 (выбор типа ОПУ по индивидуальным строкам)
    обязаны понимать одинаково. Нет колонки области действия — все строки
    универсальные (маска из False), обратная совместимость со старыми
    Справочники.xlsx.
    """
    if scope_col not in df.columns:
        return pd.Series(False, index=df.index)
    return _normalized_scope(df[scope_col]).ne(SCOPE_ALL_VALUE.casefold())


def _row_keys(frame: pd.DataFrame, key_cols: Sequence[str]) -> list[str]:
    """Составные ключи строк (в порядке строк кадра)."""
    if frame.empty:
        return []
    parts = [frame[col].astype('string').fillna('') for col in key_cols]
    return [KEY_SEPARATOR.join(values) for values in zip(*parts)]


def resolve_company_view(
    df: Optional[pd.DataFrame],
    company: Optional[str],
    key_cols: Sequence[str],
    scope_col: str = REFERENCE_SCOPE_COL,
    reference_name: str = '',
    known_companies: Optional[Iterable[str]] = None,
) -> tuple[Optional[pd.DataFrame], ScopeDiagnostics]:
    """
    Возвращает справочник во «взгляде для компании» и диагностику.

    Правила:
    1. Нет колонки области действия (или нет ключевых колонок) — кадр
       возвращается без изменений: совместимость со старыми Справочники.xlsx.
    2. 'все'/пусто — универсальные строки, строки текущей компании —
       индивидуальные, строки остальных компаний отбрасываются.
    3. Индивидуальные строки, совпавшие по key_cols с универсальными,
       перекрывают их (универсальная строка в результат не попадает).
    4. Кадр не меняется ТОЛЬКО если в справочнике нет ни строк текущей
       компании, ни строк других компаний (обычный случай — все строки
       «все»). Обратное тоже верно и важно: у компании БЕЗ индивидуальных
       строк чужие строки всё равно отбрасываются (см. доктринг ниже).
    5. Порядок результата: универсальные (без перекрытых), затем индивидуальные —
       важно для потребителей с keep='first' и dict(zip(...)).

    Почему правило 4 нельзя читать как «нет своих строк → вернуть как есть»:
    правило 2 самостоятельно. Раньше здесь стоял ранний выход
    `if individual.empty: return df`, и справочник для любой компании группы
    протекал в прогон целиком вместе с индивидуальными строками остальных.
    Реальный кейс (29.09.2026): прогон «ТимПФ» останавливался на шаге 17 с
    «найдено несколько бизнес-типов» по статье аренды 91.02 — «Аренда» (своя
    строка) против «Расходы по процентам аренда» (индивидуальная строка
    «ГиагКХП», у неё тоже сегмент «Птицеводство», как у ТимПФ).
    Справочник был корректен, протекала фильтрация.

    Args:
        df: Справочник (Меппинг_опу / Меппинг_бб) или None.
        company: Имя компании из контекста.
        key_cols: Колонки-ключ строки меппинга (для ОПУ —
            OpuReportConstants.REFERENCE_ROW_KEY, для баланса —
            BalanceReportConstants.MAPPING_KEYS).
        scope_col: Колонка области действия (по умолчанию REFERENCE_SCOPE_COL).
        reference_name: Имя листа — для логов и отчёта mismatches/.
        known_companies: Имена компаний из КомпанииГруппы — чтобы отличить
            опечатку в колонке от строки другой компании.

    Returns:
        (кадр для компании, диагностика). Исходный кадр не изменяется.
    """
    diag = ScopeDiagnostics(reference_name=reference_name, company=str(company or ''))

    if df is None or df.empty:
        return df, diag

    if scope_col not in df.columns:
        diag.column_missing = True
        logger.warning(
            "[!] В справочнике '{}' нет колонки '{}' — индивидуальный меппинг "
            "компаний не применяется (действуют все строки '{}').",
            reference_name or scope_col, scope_col, SCOPE_ALL_VALUE,
        )
        return df, diag

    missing_key_cols = [col for col in key_cols if col not in df.columns]
    if missing_key_cols:
        diag.column_missing = True
        logger.warning(
            "[!] В справочнике '{}' нет ключевых колонок {} — индивидуальный "
            "меппинг компании '{}' не применяется.",
            reference_name or scope_col, missing_key_cols, company,
        )
        return df, diag

    scope = _normalized_scope(df[scope_col])
    universal_mask = scope.eq(SCOPE_ALL_VALUE.casefold())
    company_key = str(company or '').strip().casefold()
    if company_key:
        individual_mask = scope.eq(company_key)
    else:
        individual_mask = pd.Series(False, index=df.index)

    universal = df.loc[universal_mask]
    individual = df.loc[individual_mask]
    # Строки, адресованные другим компаниям (в т.ч. опечатки в имени) — их
    # не должно быть в кадре компании НИКОГДА, независимо от наличия
    # индивидуальных строк (правило 2 самостоятельно).
    foreign_mask = ~universal_mask & ~individual_mask

    diag.universal_rows = int(len(universal))
    diag.individual_rows = int(len(individual))
    diag.dropped_foreign_rows = int(foreign_mask.sum())

    # Именно `is not None`, а не `or ()`: known_companies — это Iterable, и
    # pandas.Series в `or` уходит в __bool__ → ValueError («truth value of a
    # Series is ambiguous»). Список/Series из листа КомпанииГруппы — обычный
    # способ передать сюда компании, молча ломать его нельзя.
    known = {
        str(name).strip().casefold()
        for name in (known_companies if known_companies is not None else ())
    }

    other_values = df.loc[foreign_mask, scope_col].dropna()
    diag.unknown_scope_values = sorted(
        value
        for value in {str(value).strip() for value in other_values}
        if value and value.casefold() not in known
    )
    if diag.unknown_scope_values:
        logger.warning(
            "[!] Справочник '{}': в колонке '{}' есть значения, не совпадающие "
            "ни с '{}', ни с компаниями листа КомпанииГруппы: {}. Такие строки "
            "пропущены — проверьте написание имени компании.",
            reference_name or scope_col, scope_col, SCOPE_ALL_VALUE,
            ', '.join(diag.unknown_scope_values[:10]),
        )

    if diag.dropped_foreign_rows:
        logger.info(
            "[i] Справочник '{}': отброшены строки других компаний — {} "
            "(для компании '{}' они не действуют).",
            reference_name or scope_col, diag.dropped_foreign_rows, company,
        )

    # Ни своих строк, ни чужих — в справочнике нет индивидуального меппинга
    # вовсе: кадр не трогаем (обратная совместимость, объект идентичен).
    if individual.empty and not diag.dropped_foreign_rows:
        return df, diag

    individual_key_set = set(_row_keys(individual, key_cols))
    universal_key_list = _row_keys(universal, key_cols)
    universal_key_set = set(universal_key_list)

    keep_universal = pd.Series(
        [key not in individual_key_set for key in universal_key_list],
        index=universal.index,
    )
    overridden = universal.loc[keep_universal]
    removed = universal.loc[~keep_universal]
    diag.overridden_rows = int(len(removed))
    if diag.overridden_rows:
        diag.overridden_frame = removed

    individual_only = individual.loc[
        [key not in universal_key_set for key in _row_keys(individual, key_cols)]
    ]
    if not individual_only.empty:
        diag.individual_only_frame = individual_only

    # Все индивидуальные строки (в т.ч. перекрывающие универсальные) — для
    # аудита: в отчёте лист «Применённые строки» показывает, что реально
    # действует для компании, а не только что было вытеснено.
    if not individual.empty:
        diag.individual_frame = individual

    if individual.empty:
        # Своих строк нет, отбрасывать нечего — остаётся только отфильтрованный
        # универсальный блок (без строк других компаний).
        result = overridden
    else:
        result = pd.concat([overridden, individual], axis=0)
        logger.info(
            "[i] Индивидуальный меппинг '{}': для компании '{}' применено строк — {}, "
            "перекрыто универсальных — {}",
            reference_name or scope_col, company,
            diag.individual_rows, diag.overridden_rows,
        )

    return result, diag

