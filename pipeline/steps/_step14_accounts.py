"""
Mixin с обработкой счетов 90.01/90.02 для Шага 14.

Методы:
    _process_revenue_9001: обработка выручки (90.01)
    _validate_revenue_against_osv: сверка выручки с ОСВ
    _process_cost_9002: обработка себестоимости (90.02)
    _process_reassessment: обработка переоценки (Дт 90.02 Кт 16)
    _validate_cost_against_osv: сверка себестоимости с ОСВ
    _distribute_cost_to_buyers: распределение себестоимости пропорционально выручке
"""
from collections import Counter
from typing import Tuple

import numpy as np
import pandas as pd
from loguru import logger

from io_module import DataSaver
from pipeline.errors import MissingMappingError, ReferenceMismatchError


class Step14AccountsMixin:
    """Миксин обработки счетов 90.01/90.02 для Шага 14."""

    def _process_revenue_9001(
        self,
        transactions_all_df: pd.DataFrame,
        osv_df: pd.DataFrame,
        context,
    ) -> pd.DataFrame:
        """Обрабатывает выручку по счету 90.01."""
        logger.debug("Обработка выручки (90.01)")

        mask_account = transactions_all_df['Кт'].str.startswith("90.01", na=False)
        mask_file = transactions_all_df['Имя_файла'].str.contains("_90.01_", na=False)
        df9001 = transactions_all_df.loc[mask_account & mask_file].copy()

        df9001 = df9001.rename(columns={
            'Субконто Дт_1': 'контрагент',
            'Субконто Кт_1': 'ном_группа',
            'Кт': 'счет'
        })

        df9001.loc[df9001['Дт'].str.startswith('76.15'), 'контрагент'] = 'пайщики'

        # Исходное значение субконто ставки НДС — для аудита пропущенных
        # ставок и распознавания явного «Без НДС» (текстовые маркеры).
        df9001['ндс_ставка_исх'] = df9001['Субконто Кт_2'].astype(str).str.strip()

        df9001['ндс_ставка'] = (
            df9001['Субконто Кт_2']
            .astype(str)
            .str.replace('%', '', regex=False)
            .str.strip()
            .replace('', pd.NA)
        )
        df9001['ндс_ставка'] = pd.to_numeric(df9001['ндс_ставка'], errors='coerce') / 100

        # Защитный механизм: строки с пропущенной ставкой НДС НЕ удаляются
        # (раньше NaN молча «сгорал» в groupby().sum(), завышая выручку ОПУ).
        # Ставка восстанавливается по аналогичным строкам («ном_группа»),
        # иначе берётся дефолт из листа «Параметры» (nds_missing_values).
        default_vat_rate = context.tolerance_params.get('nds_missing_values', 0.20)
        df9001, _vat_audit_df = self._restore_missing_vat_rates(
            df9001, default_vat_rate, context
        )

        df9001['выручка_без_ндс_тыс_ед'] = (df9001['Сумма'] / 1000) / (1 + df9001['ндс_ставка'])
        df9001['выручка_без_ндс_тыс_руб'] = (df9001['Сумма_руб'] / 1000) / (1 + df9001['ндс_ставка'])

        df9001 = df9001.loc[:, ['Документ', 'контрагент', 'ном_группа', 'выручка_без_ндс_тыс_ед', 'выручка_без_ндс_тыс_руб']]
        df9001 = df9001.groupby(
            ['Документ', 'контрагент', 'ном_группа'],
            as_index=False
        )[['выручка_без_ндс_тыс_ед', 'выручка_без_ндс_тыс_руб']].sum()

        self._validate_revenue_against_osv(df9001, osv_df, context)

        logger.debug("Выручка обработана: {} строк", len(df9001))

        return df9001

    def _restore_missing_vat_rates(
        self,
        df9001: pd.DataFrame,
        default_rate: float,
        context,
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Восстанавливает пропущенные ставки НДС для строк 90.01.

        Раньше строки с пустой ставкой («не_указано» в «Субконто Кт_2») молча
        «сгорали» в groupby().sum() из-за NaN, что завышало выручку ОПУ
        (характерный кейс — «Корректировка записей регистров» 1С без субконто
        ставки НДС). Теперь такие строки НЕ удаляются:

        1. ставка восстанавливается по аналогичным строкам (одинаковая
           номенклатурная группа «ном_группа» = «Субконто Кт_1»); при
           нескольких ставках берётся наиболее частая, при равенстве частот —
           дефолт;
        2. если аналогов нет — берётся дефолт из листа «Параметры»
           (параметр nds_missing_values);
        3. явные текстовые маркеры нулевой ставки («Без НДС» и т.п.)
           трактуются как ставка 0 и дефолтом не заменяются.

        Возвращает (df9001 с заполненными ставками, audit_df). При наличии
        затронутых строк аудит сохраняется в
        warnings/nds_missing_rows_<компания>_<период>.xlsx папки запуска.
        """
        rate = df9001['ндс_ставка']
        missing = rate.isna()

        if not missing.any():
            return df9001, pd.DataFrame()

        raw = df9001['ндс_ставка_исх'].astype(str).str.strip().str.lower()
        zero_marker = raw.str.contains('без', na=False) | raw.isin(
            ['0%', '0 %', '0', 'ноль']
        )
        zero_idx = missing & zero_marker
        missing_idx = missing & ~zero_marker

        df9001 = df9001.copy()
        audit_records: list = []

        if zero_idx.any():
            df9001.loc[zero_idx, 'ндс_ставка'] = 0.0
            for idx in df9001.index[zero_idx]:
                audit_records.append(self._build_vat_audit_row(
                    df9001, idx, 0.0, 'явная_0%',
                    'текстовый маркер нулевой ставки («без НДС»)',
                ))
            logger.warning(
                "НДС 90.01: {} строк помечены как «без НДС» (текстовый маркер) — "
                "использована ставка 0%. Строки не удалены. Детали: "
                "warnings/nds_missing_rows_*.xlsx",
                int(zero_idx.sum()),
            )

        if missing_idx.any():
            known = df9001.loc[rate.notna(), ['ном_группа', 'ндс_ставка']]
            # Список значений с повторами: нужен и для проверки «одна уникальная
            # ставка», и для подсчёта моды (Counter).
            group_rates = known.groupby('ном_группа')['ндс_ставка'].agg(
                lambda s: [float(x) for x in s.dropna()]
            )

            restored_n = 0
            default_n = 0
            for idx in df9001.index[missing_idx]:
                group_key = df9001.loc[idx, 'ном_группа']
                candidates = group_rates.get(group_key, None)

                rate_val = default_rate
                source = 'дефолт'
                note = 'нет аналогичных строк с известной ставкой'
                if candidates:
                    unique_rates = sorted({float(x) for x in candidates})
                    if len(unique_rates) == 1:
                        rate_val = unique_rates[0]
                        source = 'аналоги'
                        note = ''
                    else:
                        top1, top2 = Counter(candidates).most_common(2)
                        if top1[1] == top2[1]:
                            note = (
                                'неоднозначные аналоги ({}), взят дефолт'
                                .format(', '.join('{:.1%}'.format(x) for x in unique_rates))
                            )
                        else:
                            rate_val = top1[0]
                            source = 'аналоги'
                            note = 'по наиболее частой ставке аналогов'
                if not (0.0 < rate_val <= 1.0):
                    rate_val = default_rate
                    source = 'дефолт'
                    note = 'восстановленная ставка вне (0;1], взят дефолт'
                df9001.loc[idx, 'ндс_ставка'] = rate_val
                if source == 'аналоги':
                    restored_n += 1
                else:
                    default_n += 1
                audit_records.append(self._build_vat_audit_row(
                    df9001, idx, rate_val, source, note,
                ))

            logger.warning(
                "НДС 90.01: обнаружено {} строк с пропущенной ставкой "
                "(«Субконто Кт_2» пусто): восстановлено по аналогам {}, "
                "дефолтом ({:.1%}) — {}. Строки не удаляются, выручка "
                "пересчитана. Детали: warnings/nds_missing_rows_*.xlsx",
                int(missing_idx.sum()), restored_n, default_rate, default_n,
            )

        if not audit_records:
            return df9001, pd.DataFrame()

        audit_df = pd.DataFrame(audit_records)
        company = getattr(context, 'company', None) or 'unknown'
        period = (
            getattr(context, 'period', None)
            or getattr(context, 'run_id', None)
            or 'run'
        )
        try:
            DataSaver.save_to_excel(
                audit_df,
                f'nds_missing_rows_{company}_{period}.xlsx',
                subfolder='warnings',
            )
        except Exception as e:  # прагматично: аудит не должен ронять шаг
            logger.warning("Не удалось сохранить аудит НДС в Excel: {}", e)

        return df9001, audit_df

    @staticmethod
    def _build_vat_audit_row(
        df9001: pd.DataFrame,
        idx,
        restored_rate: float,
        source: str,
        note: str,
    ) -> dict:
        """Формирует строку аудита для проводки с восстановленной ставкой НДС."""
        row = df9001.loc[idx]
        return {
            'Имя_файла': row['Имя_файла'],
            'Документ': row['Документ'],
            'контрагент': row['контрагент'],
            'ном_группа': row['ном_группа'],
            'Сумма': row['Сумма'],
            'Сумма_руб': row['Сумма_руб'],
            'исходная_ставка': row['ндс_ставка_исх'],
            'восстановленная_ставка': restored_rate,
            'источник': source,
            'примечание': note,
        }

    def _validate_revenue_against_osv(
        self,
        df9001: pd.DataFrame,
        osv_df: pd.DataFrame,
        context,
    ) -> None:
        """Проверяет сходимость выручки с общей ОСВ."""
        revenue_osv_9001 = osv_df.loc[
            osv_df['Счет'].str.startswith('90.01'), 'Кредит_оборот'
        ].sum()
        revenue_osv_9003 = osv_df.loc[
            osv_df['Счет'].str.startswith('90.03'), 'Дебет_оборот'
        ].sum()
        revenue_without_vat = (revenue_osv_9001 - revenue_osv_9003) / 1000

        revenue_from_df9001 = df9001['выручка_без_ндс_тыс_ед'].sum()

        difference = abs(revenue_without_vat - revenue_from_df9001)

        if difference > context.tolerance_params['tolerance_reconciliation']:
            raise ValueError(
                f"Выручка из отчёта по проводкам ({revenue_from_df9001:,.2f} тыс.ед.) "
                f"отличается от общей ОСВ ({revenue_without_vat:,.2f} тыс.ед.) "
                f"на {difference:,.2f} тыс.ед. (допуск: {context.tolerance_params['tolerance_reconciliation']})"
            )

        logger.debug(
            "[OK] Сходимость выручки: ОСВ={:,.2f}, отчёт={:,.2f}, разница={:,.2f}",
            revenue_without_vat,
            revenue_from_df9001,
            difference,
        )

    def _process_cost_9002(
        self,
        transactions_all_df: pd.DataFrame,
        osv_df: pd.DataFrame,
        name_company: str,
        company_directory_df: pd.DataFrame,
        context,
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Обрабатывает себестоимость по счету 90.02.

        Выделяет отдельно переоценку активов (Дт 90.02 Кт 16) в df9002_16.

        Returns:
            Tuple[df9002, df9002_16]: Основная себестоимость и переоценка
        """
        logger.debug("Обработка себестоимости (90.02)")

        mask_account = transactions_all_df['Дт'].str.startswith("90.02", na=False)
        mask_file = transactions_all_df['Имя_файла'].str.contains("_90.02_", na=False)
        df9002 = transactions_all_df.loc[mask_account & mask_file].copy()

        df9002 = df9002.rename(columns={
            'Субконто Дт_1': 'ном_группа',
            'Дт': 'счет'
        })

        mask_16 = df9002['Кт'].astype(str).str.startswith('16', na=False)

        if mask_16.any():
            df9002_16 = self._process_reassessment(df9002[mask_16].copy(), name_company, company_directory_df)
            df9002 = df9002[~mask_16].copy()
            turn_df9002_16 = df9002_16['оборот, тыс.ед.'].sum()
        else:
            df9002_16 = pd.DataFrame()
            turn_df9002_16 = 0

        df9002['себестоимость_тыс_ед'] = df9002.loc[:, 'Сумма'] / 1000
        df9002['себестоимость_тыс_руб'] = df9002.loc[:, 'Сумма_руб'] / 1000
        df9002 = df9002.loc[:, ['Документ', 'ном_группа', 'себестоимость_тыс_ед', 'себестоимость_тыс_руб']]
        df9002 = df9002.groupby(
            ['Документ', 'ном_группа'],
            as_index=False
        )[['себестоимость_тыс_ед', 'себестоимость_тыс_руб']].sum()

        self._validate_cost_against_osv(df9002, osv_df, turn_df9002_16, context)

        logger.debug(
            "Себестоимость обработана: {} строк основной, {} строк переоценки",
            len(df9002),
            len(df9002_16),
        )

        return df9002, df9002_16

    def _process_reassessment(
        self,
        df9002_16: pd.DataFrame,
        name_company: str,
        company_directory_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """Обрабатывает переоценку активов (Дт 90.02 Кт 16)."""
        logger.debug("Обработка переоценки активов (Дт 90.02 Кт 16)")

        matching_rows = company_directory_df[
            company_directory_df['сокращенное_наименование_компании'] == name_company
        ]

        if matching_rows.empty:
            problem_data = (
                company_directory_df[['сокращенное_наименование_компании']]
                .drop_duplicates()
                .rename(columns={'сокращенное_наименование_компании': 'компания_в_справочнике'})
            )
            raise ReferenceMismatchError(
                message=f"Компания '{name_company}' не найдена в справочнике",
                problem_data=problem_data,
                reference_name="КомпанииГруппы",
                searched_company=name_company,
            )

        if len(matching_rows) > 1:
            raise ReferenceMismatchError(
                message=(
                    f"У компании '{name_company}' найдено {len(matching_rows)} записей. "
                    f"Ожидается одна."
                ),
                problem_data=matching_rows.copy(),
                reference_name="КомпанииГруппы",
                duplicate_count=len(matching_rows),
            )

        df9002_16['оборот, тыс.ед.'] = df9002_16.loc[:, 'Сумма'] / 1000
        df9002_16['оборот, тыс.руб.'] = df9002_16.loc[:, 'Сумма_руб'] / 1000
        df9002_16 = df9002_16.groupby(
            ['ном_группа'], as_index=False
        )[['оборот, тыс.ед.', 'оборот, тыс.руб.']].sum()

        segment = matching_rows['сегмент'].iloc[0]

        for col_name, value in [
            ('счет', '90.02'),
            ('доход_расход', 'Изменение в оценке'),
            ('сегмент', segment),
            ('вид_связи', 'не_указано'),
        ]:
            df9002_16[col_name] = pd.Series([value] * len(df9002_16), dtype='string')

        products_reassessment_type = matching_rows['вид_продукции_переоценки'].iloc[0]
        df9002_16['вид_дохода_расхода'] = products_reassessment_type

        logger.debug(
            "Переоценка: {} строк, вид_дохода_расхода='{}'",
            len(df9002_16),
            products_reassessment_type,
        )

        return df9002_16

    def _validate_cost_against_osv(
        self,
        df9002: pd.DataFrame,
        osv_df: pd.DataFrame,
        turn_df9002_16: float,
        context,
    ) -> None:
        """Проверяет сходимость себестоимости с общей ОСВ."""
        cost_price_osv_9002 = osv_df.loc[
            osv_df['Счет'].str.startswith('90.02'), 'Дебет_оборот'
        ].sum() / 1000

        cost_price_from_df9002 = df9002['себестоимость_тыс_ед'].sum() + turn_df9002_16

        difference = abs(cost_price_osv_9002 - cost_price_from_df9002)

        if difference > context.tolerance_params['tolerance_reconciliation']:
            raise ValueError(
                f"Себестоимость из отчёта по проводкам ({cost_price_from_df9002:,.2f} тыс.ед.) "
                f"отличается от общей ОСВ ({cost_price_osv_9002:,.2f} тыс.ед.) "
                f"на {difference:,.2f} тыс.ед. (допуск: {context.tolerance_params['tolerance_reconciliation']})"
            )

        logger.debug(
            "[OK] Сходимость себестоимости: ОСВ={:,.2f}, отчёт={:,.2f}, разница={:,.2f}",
            cost_price_osv_9002,
            cost_price_from_df9002,
            difference,
        )

    def _distribute_cost_to_buyers(
        self,
        df9001: pd.DataFrame,
        df9002: pd.DataFrame,
    ) -> pd.DataFrame:
        """Распределяет себестоимость на контрагентов пропорционально выручке.

        Каждая единица себестоимости попадает ровно в одну строку результата:
        - кост ключа (Документ, ном_группа) делится между контрагентами этого
          документа пропорционально их доле выручки (без задвоения, когда в
          одном документе несколько контрагентов по одной номенклатуре);
        - кост документов без выручки распределяется по номенклатурной группе
          пропорционально выручке группы;
        - кост групп, у которых нет выручки вовсе, не сжигается, а остаётся
          в отчёте с контрагентом '3 лица'.
        """
        logger.debug("Распределение себестоимости на контрагентов")

        df_buyers = df9001.copy()

        revenue_key = df9001.groupby(
            ['Документ', 'ном_группа'], as_index=False
        )[['выручка_без_ндс_тыс_ед', 'выручка_без_ндс_тыс_руб']].sum().rename(
            columns={
                'выручка_без_ндс_тыс_ед': 'выручка_ключа_ед',
                'выручка_без_ндс_тыс_руб': 'выручка_ключа_руб',
            }
        )

        df_alloc = df_buyers.merge(revenue_key, on=['Документ', 'ном_группа'], how='left')
        df_alloc = df_alloc.merge(df9002, on=['Документ', 'ном_группа'], how='left')

        rev_key_pos = df_alloc['выручка_ключа_ед'] > 0
        df_alloc['доля_строки'] = np.where(
            rev_key_pos,
            df_alloc['выручка_без_ндс_тыс_ед'] / df_alloc['выручка_ключа_ед'],
            0.0,
        )

        # Прямой кост: кост ключа × доля строки в выручке ключа (без задвоения)
        df_alloc['кост_прямой'] = (
            df_alloc['себестоимость_тыс_ед'].fillna(0) * df_alloc['доля_строки']
        )
        df_alloc['кост_прямой_руб'] = (
            df_alloc['себестоимость_тыс_руб'].fillna(0) * df_alloc['доля_строки']
        )

        # Остаток коста после прямого распределения по контрагентам ключа
        allocated_key = df_alloc.groupby(['Документ', 'ном_группа'], as_index=False)[
            ['кост_прямой', 'кост_прямой_руб']
        ].sum().rename(columns={
            'кост_прямой': 'кост_распределён_ед',
            'кост_прямой_руб': 'кост_распределён_руб',
        })
        df9002_rem = df9002.merge(allocated_key, on=['Документ', 'ном_группа'], how='left')
        df9002_rem['кост_остаток'] = (
            df9002_rem['себестоимость_тыс_ед']
            - df9002_rem['кост_распределён_ед'].fillna(0)
        )
        df9002_rem['кост_остаток_руб'] = (
            df9002_rem['себестоимость_тыс_руб']
            - df9002_rem['кост_распределён_руб'].fillna(0)
        )

        # Остатки по номенклатурной группе — распределяются по выручке группы
        group_pot = df9002_rem.groupby('ном_группа')[['кост_остаток', 'кост_остаток_руб']].sum()
        group_pot = group_pot[group_pot['кост_остаток'] != 0]

        revenue_group = df9001.groupby('ном_группа', as_index=False)[
            ['выручка_без_ндс_тыс_ед', 'выручка_без_ндс_тыс_руб']
        ].sum().rename(columns={
            'выручка_без_ндс_тыс_ед': 'выручка_группы_ед',
            'выручка_без_ндс_тыс_руб': 'выручка_группы_руб',
        })
        df_alloc = df_alloc.merge(revenue_group, on='ном_группа', how='left')
        rev_group_pos = df_alloc['выручка_группы_ед'] > 0
        df_alloc['доля_группы'] = np.where(
            rev_group_pos,
            df_alloc['выручка_без_ндс_тыс_ед'] / df_alloc['выручка_группы_ед'],
            0.0,
        )

        grp_cost = df_alloc['ном_группа'].map(group_pot['кост_остаток']).fillna(0)
        grp_cost_rub = df_alloc['ном_группа'].map(group_pot['кост_остаток_руб']).fillna(0)
        df_alloc['кост_группы'] = grp_cost * df_alloc['доля_группы']
        df_alloc['кост_группы_руб'] = grp_cost_rub * df_alloc['доля_группы']

        df_alloc['Итоговая_себестоимость'] = df_alloc['кост_прямой'] + df_alloc['кост_группы']
        df_alloc['Итоговая_себестоимость_руб'] = (
            df_alloc['кост_прямой_руб'] + df_alloc['кост_группы_руб']
        )

        # Группы, у которых остался кост без выручки, — контрагент '3 лица'
        orphans = group_pot[
            ~group_pot.index.isin(revenue_group['ном_группа'])
            | (
                revenue_group.set_index('ном_группа')['выручка_группы_ед']
                .reindex(group_pot.index).fillna(0) == 0
            )
        ]

        df_result = df_alloc[['контрагент', 'ном_группа',
                              'выручка_без_ндс_тыс_ед', 'Итоговая_себестоимость',
                              'выручка_без_ндс_тыс_руб', 'Итоговая_себестоимость_руб']]

        if not orphans.empty:
            df_result = pd.concat([
                df_result,
                pd.DataFrame({
                    'контрагент': ['3 лица'] * len(orphans),
                    'ном_группа': orphans.index,
                    'выручка_без_ндс_тыс_ед': 0.0,
                    'Итоговая_себестоимость': orphans['кост_остаток'].to_numpy(),
                    'выручка_без_ндс_тыс_руб': 0.0,
                    'Итоговая_себестоимость_руб': orphans['кост_остаток_руб'].to_numpy(),
                }),
            ], ignore_index=True)

        df_result = df_result.groupby(['контрагент', 'ном_группа'], as_index=False)[
            ['выручка_без_ндс_тыс_ед', 'Итоговая_себестоимость',
             'выручка_без_ндс_тыс_руб', 'Итоговая_себестоимость_руб']
        ].sum()

        df_result = df_result.rename(columns={
            'Итоговая_себестоимость': 'себестоимость_тыс_ед',
            'Итоговая_себестоимость_руб': 'себестоимость_тыс_руб',
        })

        logger.debug(
            "Себестоимость распределена: {} строк с контрагентами ({} строк сиротских)",
            len(df_result),
            len(orphans),
        )

        return df_result
