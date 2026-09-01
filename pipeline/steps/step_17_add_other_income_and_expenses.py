# -*- coding: utf-8 -*-
"""
Шаг 17: Добавление прочих доходов и расходов в расшифровку ОПУ (счета 91.01/91.02)

Обработка:
- Продажа активов (запасы, ОС): корректировка на НДС, подтягивание контрагентов,
  распределение осиротевших расходов
- Кредитные линии: подтягивание контрагентов из справочника КредитОбслуж
- Процентные расходы/доходы: подтягивание контрагентов
- Изменение условий ППА: подтягивание контрагентов из справочника ППА
- Определение группа_ка, сегмент_ка, вид_связи для всех строк

Архитектурное улучшение:
- маски по продаже активов не передаются между методами;
- методы получают asset_sale_types и каждый раз строят маски заново
  по текущему состоянию DataFrame.
"""

import numpy as np
import pandas as pd
from loguru import logger

from pipeline.base import Step, ProcessingContext
from pipeline.errors import MissingMappingError
from utils.currency_utils import get_rate_deviation_limit, get_rate_for_date, get_rate_median

class Step17AddOtherIncomeExpensesToOpuStep(Step):
    """
    Шаг 17: Обработка прочих доходов и расходов (счета 91.01/91.02).

    Добавляет в расшифровку ОПУ:
     - Прочие доходы (91.01)
     - Прочие расходы (91.02)

    с детализацией по контрагентам и видам связи.
    """

    # Счета для обработки
    ACCOUNT_OTHER_INCOME = '91.01'
    ACCOUNT_OTHER_EXPENSE = '91.02'

    NDS_ACCOUNTS = '68.02'

    # Счета ППА
    PPA_ACCOUNTS = ('01.09', '02.01', '01.01')

    # Допуск для проверки сходимости с ОСВ (в тыс.ед.)
    # TOLERANCE_OSV = 1000

    def __init__(self):
        super().__init__(
            name="Шаг 17: Прочие доходы и расходы (91.01/91.02)",
            description=(
                "Добавление прочих доходов и расходов "
                "с детализацией по контрагентам и видам связи"
            )
        )

    def _process(self, context: ProcessingContext) -> ProcessingContext:
        logger.debug("Начало обработки прочих доходов и расходов")

        name_company = context.company

        # 1. Данные из контекста (ОСВ, проводки)
        osv_df, transactions_all_df = self._load_data_from_context(context)

        # 2. Все справочники одним вызовом
        refs = self._load_references(name_company, context)

        # 3. Фильтрация 91.01/91.02 + подтягивание вид_дохода_расхода
        df_9101, df_9102 = self._filter_91_transactions(
            transactions_all_df,
            refs['mapping_opu']
        )

        # Создаём 'сегмент' сразу, пока df_9101/df_9102 ещё не стали view
        segment_company = refs['companies'].loc[
            refs['companies']['сокращенное_наименование_компании'] == name_company,
            'сегмент'
        ].iloc[0]

        df_9101['сегмент'] = segment_company
        df_9102['сегмент'] = segment_company

        df_9101['сегмент'] = df_9101['сегмент'].astype('string')
        df_9102['сегмент'] = df_9102['сегмент'].astype('string')

        logger.debug(
            "Создан столбец 'сегмент': 91.01 unique={}, 91.02 unique={}",
            df_9101['сегмент'].unique().tolist(),
            df_9102['сегмент'].unique().tolist(),
        )

        # 3а. Контроль подразумеваемого курса на входе в шаг (Фаза 3.3,
        # задача «аномальный курс 92,48 RUB/AED»): ловит «ядовитые» строки,
        # пришедшие из исходных данных/конвертации шага 14.
        self._log_rate_anomalies(df_9101, df_9102, context, 'вход шага 17: после фильтрации')

        # 4. Извлечение контрагентов для обеих таблиц
        df_9101 = self._extract_contractors(
            df_9101,
            is_income=True,
            accounts_with_contractors=refs['accounts_with_contractors']
        )

        df_9102 = self._extract_contractors(
            df_9102,
            is_income=False,
            accounts_with_contractors=refs['accounts_with_contractors']
        )

        # 5. Обработка ППА (91.01 и 91.02)
        df_9101, df_9102 = self._process_ppa(
            df_9101,
            df_9102,
            refs['ppa']
        )

        # 6. Обработка продажи активов
        df_9101, df_9102 = self._process_asset_sales(
            df_9101,
            df_9102,
            asset_sale_types=refs['asset_sale_types']
        )

        # 7. Обработка кредитных линий (РБП)
        df_9101 = self._process_credit_lines(
            df_9101,
            refs['credit'],
            is_income=True
        )

        df_9102 = self._process_credit_lines(
            df_9102,
            refs['credit'],
            is_income=False
        )

        # 8. Подготовка маппингов из refs['group_companies']
        group_unique = refs['group_companies'].drop_duplicates(subset='ВариантыНазвания')

        mapping_group = (
            group_unique
            .set_index('ВариантыНазвания')['ВидСвязиКА']
            .astype('string')
        )

        mapping_segment_ka = (
            group_unique
            .set_index('ВариантыНазвания')['сегмент']
            .astype('string')
        )

        # 9. Извлечение segment_company из refs['companies']
        segment_company = refs['companies'].loc[
            refs['companies']['сокращенное_наименование_компании'] == name_company,
            'сегмент'
        ].iloc[0]

        logger.debug(
            "segment_company = '{}', type = {}, isna = {}",
            segment_company,
            type(segment_company),
            pd.isna(segment_company),
        )

        # 10. Обогащение: группа_ка, сегмент_ка, вид_связи
        df_9101 = self._enrich_with_connection_info(
            df_9101,
            mapping_group,
            mapping_segment_ka,
            segment_company
        )

        logger.debug(
            "df_9101['сегмент'] unique = {}, NA count = {}",
            df_9101['сегмент'].unique(),
            df_9101['сегмент'].isna().sum(),
        )

        df_9102 = self._enrich_with_connection_info(
            df_9102,
            mapping_group,
            mapping_segment_ka,
            segment_company
        )

        # 10а. Контроль подразумеваемого курса перед агрегацией (Фаза 3.3):
        # технические столбцы (Документ/Дата/Сумма/Сумма_руб) ещё на месте —
        # если «ядовитая» строка появилась здесь, а на входе шага её не было,
        # она рождена одним из обработчиков шага 17.
        self._log_rate_anomalies(df_9101, df_9102, context, 'выход шага 17: перед агрегацией')

        # 11. Добавление служебных столбцов
        df_9101 = self._add_service_columns(df_9101, self.ACCOUNT_OTHER_INCOME)
        df_9102 = self._add_service_columns(df_9102, self.ACCOUNT_OTHER_EXPENSE)

        # 12. Объединение с main_df
        df_final = self._merge_with_main_df(
            context.journal_df,
            df_9101,
            df_9102
        )

        context.journal_df = df_final

        logger.info(
            "[OK] Прочие доходы и расходы добавлены: 91.01 — {} позиций, 91.02 — {} позиций",
            len(df_9101),
            len(df_9102),
        )

        return context

    # =========================================================================
    # ЗАГРУЗКА ДАННЫХ
    # =========================================================================

    def _load_references(self, name_company: str, context: ProcessingContext) -> dict:
        """Загружает все справочники, необходимые для шага 17."""
        logger.debug("Загрузка справочников для обработки 91.01/91.02")

        # 1. Меппинг ОПУ
        mapping_opu_df = context.references['меппинг_опу']

        # 2. ППА
        ppa_df = context.references['справочник_ппа']
        ppa_df = ppa_df[ppa_df['наименование_компании'] == name_company]

        # 3. ВидСвязиКА
        group_companies_df = context.references['вид_связи_ка']

        # 4. КомпанииГруппы
        companies_df = context.references['компании_группы']

        # 5. КредитОбслуж
        credit_df = context.references['кредит_обслуж']
        credit_df = credit_df[credit_df['компания'] == name_company]

        # 6. План счетов БУ → список счетов с контрагентами
        chart_accounts_df = context.references['план_счетов_бу']
        chart_accounts_df = chart_accounts_df.loc[
            chart_accounts_df['компания'] == name_company
        ]

        accounts_with_contractors = tuple(
            chart_accounts_df.loc[
                chart_accounts_df['субконто_1'] == 'Контрагенты',
                'код'
            ]
        )

        if not accounts_with_contractors:
            raise ValueError(
                f"План счетов БУ для компании '{name_company}' "
                f"не содержит значение 'Контрагенты' в поле субконто_1"
            )

        logger.debug(
            "Счета с контрагентами из ПланаСчетовБУ: {}",
            accounts_with_contractors,
        )

        # 7. Справочник ПрочиеДоходыНДС → список видов доходов/расходов для обработки НДС
        other_income_vat_df = context.references['прочие_доходы_ндс']

        asset_sale_types = (
            other_income_vat_df['прочие_доходы_ндс']
            .dropna()
            .astype(str)
            .str.strip()
            .replace('', pd.NA)
            .dropna()
            .unique()
            .tolist()
        )

        if not asset_sale_types:
            logger.warning(
                "[!] В справочнике 'ПрочиеДоходыНДС' нет записей. "
                "Обработка продажи активов будет пропущена."
            )

        logger.debug(
            "Виды доходов/расходов для обработки НДС "
            "из 'ПрочиеДоходыНДС': {}",
            asset_sale_types,
        )

        return {
            'mapping_opu': mapping_opu_df,
            'ppa': ppa_df,
            'group_companies': group_companies_df,
            'companies': companies_df,
            'credit': credit_df,
            'accounts_with_contractors': accounts_with_contractors,
            'asset_sale_types': asset_sale_types,
        }

    def _load_data_from_context(
        self,
        context: ProcessingContext
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Загружает ОСВ и проводки из контекста."""
        osv_df = context.common_osv_df

        if osv_df.empty:
            raise ValueError(
                "В контексте нет общей ОСВ. "
                "Убедитесь, что предыдущие шаги (1-13) выполнены успешно."
            )

        transactions_all_df = self.get_df_from_context(
            context,
            'transactions_all_df',
            hint="Убедитесь, что предыдущий шаг (14) выполнен успешно.",
        )

        logger.debug(
            "Загружено из контекста: ОСВ={} строк, проводки={} строк",
            len(osv_df),
            len(transactions_all_df),
        )

        return osv_df, transactions_all_df

    # =========================================================================
    # ФИЛЬТРАЦИЯ ПРОВОДОК
    # =========================================================================

    def _filter_91_transactions(
        self,
        transactions_all_df: pd.DataFrame,
        reference_df: pd.DataFrame
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Фильтрует проводки по 91.01 и 91.02, подтягивает вид_дохода_расхода."""
        logger.debug("Фильтрация проводок 91.01 и 91.02")

        # 91.01 — прочие доходы (кредитовые обороты)
        mask_9101 = (
            transactions_all_df['Кт'].str.startswith(self.ACCOUNT_OTHER_INCOME, na=False) &
            transactions_all_df['Имя_файла'].str.contains(f"_{self.ACCOUNT_OTHER_INCOME}_", na=False)
        )

        df_9101 = transactions_all_df.loc[mask_9101].copy()

        # 91.02 — прочие расходы (дебетовые обороты)
        mask_9102 = (
            transactions_all_df['Дт'].str.startswith(self.ACCOUNT_OTHER_EXPENSE, na=False) &
            transactions_all_df['Имя_файла'].str.contains(f"_{self.ACCOUNT_OTHER_EXPENSE}_", na=False)
        )

        df_9102 = transactions_all_df.loc[mask_9102].copy()

        # Переводим суммы в тысячи
        df_9101['оборот, тыс.ед.'] = df_9101['Сумма'] / -1000
        df_9101['оборот, тыс.руб.'] = df_9101['Сумма_руб'] / -1000
        df_9102['оборот, тыс.ед.'] = df_9102['Сумма'] / 1000
        df_9102['оборот, тыс.руб.'] = df_9102['Сумма_руб'] / 1000

        # Подтягиваем вид_дохода_расхода из справочника Меппинг_опу
        df_9101 = self._add_income_expense_type(df_9101, reference_df, is_income=True)
        df_9102 = self._add_income_expense_type(df_9102, reference_df, is_income=False)

        logger.debug(
            "91.01: {} строк, 91.02: {} строк",
            len(df_9101),
            len(df_9102),
        )

        return df_9101, df_9102

    def _add_income_expense_type(
        self,
        df: pd.DataFrame,
        reference_df: pd.DataFrame,
        is_income: bool
    ) -> pd.DataFrame:
        """Подтягивает вид_дохода_расхода из справочника Меппинг_опу."""
        account_label = self.ACCOUNT_OTHER_INCOME if is_income else self.ACCOUNT_OTHER_EXPENSE

        # Для 91.01 Кт = наш счёт (91.01), Дт = корр.счёт
        # Для 91.02 Дт = наш счёт (91.02), Кт = корр.счёт
        account_col = 'Кт' if is_income else 'Дт'
        corr_col = 'Дт' if is_income else 'Кт'

        df = df.rename(columns={
            account_col: 'счет',
            corr_col: 'Корр.счет'
        })

        # Для 91.01 доход_расход = Субконто Кт_1
        # Для 91.02 доход_расход = Субконто Дт_1
        subconto_col = 'Субконто Кт_1' if is_income else 'Субконто Дт_1'

        df = df.rename(columns={
            subconto_col: 'доход_расход'
        })

        # Работаем с копией справочника, чтобы не менять исходный объект
        reference_df = reference_df.copy()

        # Составной ключ: счет (91.01/91.02) + доход_расход
        reference_df['_key'] = (
            reference_df['счет'].astype(str) + '_' +
            reference_df['доход_расход'].astype(str)
        )

        df['_key'] = (
            df['счет'].astype(str).str[:5] + '_' +
            df['доход_расход'].astype(str)
        )

        mapping = (
            reference_df
            .drop_duplicates(subset='_key')
            .set_index('_key')['вид_дохода_расхода']
        )

        # Оставляем NaN для проверки
        df['вид_дохода_расхода'] = df['_key'].map(mapping).astype('string')

        # Проверка: все ли значения замапились
        unmapped_mask = df['вид_дохода_расхода'].isna()

        if unmapped_mask.any():
            problem_data = (
                df.loc[unmapped_mask, ['счет', 'Корр.счет', 'доход_расход', '_key']]
                .drop_duplicates()
                .rename(columns={'_key': 'ключ_поиска'})
            )

            raise MissingMappingError(
                message=(
                    f"В справочнике Меппинг_опу отсутствуют записи для "
                    f"{unmapped_mask.sum()} строк по счёту {account_label}. "
                    f"Дополните справочник недостающими значениями."
                ),
                problem_data=problem_data,
                reference_name="Меппинг_опу",
            )

        # Убираем временный столбец
        df = df.drop(columns=['_key'])

        return df

    def _check_unspecified(self, df_9101: pd.DataFrame, df_9102: pd.DataFrame) -> None:
        """Проверяет наличие неучтённых доходов/расходов."""
        if not df_9102.loc[df_9102['вид_дохода_расхода'] == 'не_указано'].empty:
            logger.warning("[!] Есть неучтённые расходы в 91.02!")

        if not df_9101.loc[df_9101['вид_дохода_расхода'] == 'не_указано'].empty:
            logger.warning("[!] Есть неучтённые доходы в 91.01!")

    # =========================================================================
    # ОБРАБОТКА ППА
    # =========================================================================

    def _process_ppa(
        self,
        df_9101: pd.DataFrame,
        df_9102: pd.DataFrame,
        reference_ppa_df: pd.DataFrame
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Подтягивает контрагентов из справочника ППА."""
        logger.debug("Обработка ППА")

        df_9101 = df_9101.copy()
        df_9102 = df_9102.copy()

        PPA_INCOME_TYPE_9101 = (
            "Доходы от выбытия прав пользования активами, "
            "изменения условий договоров аренды"
        )

        PPA_INCOME_TYPE_9102 = (
            "Расходы от выбытия прав пользования активами, "
            "изменений условий договоров аренды"
        )

        mask_ppa_income_9101 = df_9101['вид_дохода_расхода'] == PPA_INCOME_TYPE_9101
        mask_ppa_income_9102 = df_9102['вид_дохода_расхода'] == PPA_INCOME_TYPE_9102

        if not mask_ppa_income_9101.any() and not mask_ppa_income_9102.any():
            return df_9101, df_9102

        # Добавляем столбец с объектом ППА
        mask_ppa_accounts_9101 = df_9101['Корр.счет'].astype(str).str.startswith(
            self.PPA_ACCOUNTS,
            na=False
        )

        mask_ppa_accounts_9102 = df_9102['Корр.счет'].astype(str).str.startswith(
            self.PPA_ACCOUNTS,
            na=False
        )

        df_9101['объект для изм ппа'] = df_9101['Субконто Дт_1'].where(
            mask_ppa_income_9101 & mask_ppa_accounts_9101,
            'не_указано'
        ).astype('string')

        df_9102['объект для изм ппа'] = df_9102['Субконто Кт_1'].where(
            mask_ppa_income_9102 & mask_ppa_accounts_9102,
            'не_указано'
        ).astype('string')

        # Маппинги из справочника ППА
        mapping_ppa = (
            reference_ppa_df
            .drop_duplicates(subset='ос_ппа')
            .set_index('ос_ппа')['контрагент']
        )

        mapping_transfer = (
            reference_ppa_df
            .drop_duplicates(subset='ос_после_перехода_в_собственность')
            .set_index('ос_после_перехода_в_собственность')['контрагент']
        )

        # Условие 1: Корр.счет начинается с 01.09 → поиск по ос_ппа
        mask_01_09_9101 = (
            mask_ppa_income_9101 &
            df_9101['Корр.счет'].astype(str).str.startswith('01.09', na=False)
        )

        mapped_1_9101 = df_9101['объект для изм ппа'].map(mapping_ppa)

        df_9101['контрагент'] = np.where(
            mask_01_09_9101 & mapped_1_9101.notna(),
            mapped_1_9101,
            df_9101['контрагент']
        )

        mask_01_09_9102 = (
            mask_ppa_income_9102 &
            df_9102['Корр.счет'].astype(str).str.startswith('01.09', na=False)
        )

        mapped_1_9102 = df_9102['объект для изм ппа'].map(mapping_ppa)

        df_9102['контрагент'] = np.where(
            mask_01_09_9102 & mapped_1_9102.notna(),
            mapped_1_9102,
            df_9102['контрагент']
        )

        # Условие 2: Корр.счет начинается с 02.01 или 01.01 → поиск по ос_после_перехода
        mask_02_01_or_01_01_9101 = (
            mask_ppa_income_9101 &
            df_9101['Корр.счет'].astype(str).str.startswith(('02.01', '01.01'), na=False)
        )

        mapped_2_9101 = df_9101['объект для изм ппа'].map(mapping_transfer)

        df_9101['контрагент'] = np.where(
            mask_02_01_or_01_01_9101 & mapped_2_9101.notna(),
            mapped_2_9101,
            df_9101['контрагент']
        )

        mask_02_01_or_01_01_9102 = (
            mask_ppa_income_9102 &
            df_9102['Корр.счет'].astype(str).str.startswith(('02.01', '01.01'), na=False)
        )

        mapped_2_9102 = df_9102['объект для изм ппа'].map(mapping_transfer)

        df_9102['контрагент'] = np.where(
            mask_02_01_or_01_01_9102 & mapped_2_9102.notna(),
            mapped_2_9102,
            df_9102['контрагент']
        )

        df_9101['контрагент'] = df_9101['контрагент'].astype('string')
        df_9102['контрагент'] = df_9102['контрагент'].astype('string')

        count_ppa_9101 = (
            (mask_01_09_9101 & mapped_1_9101.notna()).sum() +
            (mask_02_01_or_01_01_9101 & mapped_2_9101.notna()).sum()
        )

        count_ppa_9102 = (
            (mask_01_09_9102 & mapped_1_9102.notna()).sum() +
            (mask_02_01_or_01_01_9102 & mapped_2_9102.notna()).sum()
        )

        logger.debug(
            "Подтянуто {} контрагентов из справочника ППА",
            count_ppa_9101 + count_ppa_9102,
        )

        return df_9101, df_9102

    # =========================================================================
    # ОБРАБОТКА ПРОДАЖИ АКТИВОВ
    # =========================================================================

    def _process_asset_sales(
        self,
        df_9101: pd.DataFrame,
        df_9102: pd.DataFrame,
        asset_sale_types: list
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Обрабатывает продажу активов: НДС, контрагенты, распределение."""
        logger.debug("Обработка продажи активов")

        if not asset_sale_types:
            logger.debug("Список видов продажи активов пуст — обработка пропущена")
            return df_9101, df_9102

        mask_9101_assets, mask_9102_assets = self._build_asset_masks(
            df_9101,
            df_9102,
            asset_sale_types
        )

        if not mask_9101_assets.any() and not mask_9102_assets.any():
            logger.debug("Продажа активов не обнаружена")
            return df_9101, df_9102

        logger.debug(
            "Типы продажи активов: {} ({} строк в 91.01, {} строк в 91.02)",
            asset_sale_types,
            mask_9101_assets.sum(),
            mask_9102_assets.sum(),
        )

        # Задача 1: Корректировка выручки на НДС
        df_9101, df_9102 = self._adjust_revenue_for_vat(
            df_9101,
            df_9102,
            asset_sale_types
        )

        # Задача 2: Подтягивание контрагентов из 91.01 в 91.02
        df_9102 = self._pull_contractors_from_9101(
            df_9101,
            df_9102,
            asset_sale_types
        )

        # Задача 3: Распределение осиротевших расходов
        df_9102 = self._distribute_orphan_expenses(
            df_9101,
            df_9102,
            asset_sale_types
        )

        return df_9101, df_9102

    def _build_asset_masks(
        self,
        df_9101: pd.DataFrame,
        df_9102: pd.DataFrame,
        asset_sale_types: list
    ) -> tuple[pd.Series, pd.Series]:
        """
        Строит маски продажи активов по текущему состоянию DataFrame.

        Это архитектурно безопаснее, чем передавать маски между методами,
        потому что DataFrame могут меняться между шагами.
        """
        mask_9101_assets = df_9101['вид_дохода_расхода'].isin(asset_sale_types)
        mask_9102_assets = df_9102['вид_дохода_расхода'].isin(asset_sale_types)

        mask_9101_assets = self._ensure_aligned_bool_mask(mask_9101_assets, df_9101)
        mask_9102_assets = self._ensure_aligned_bool_mask(mask_9102_assets, df_9102)

        return mask_9101_assets, mask_9102_assets

    @staticmethod
    def _ensure_aligned_bool_mask(mask: pd.Series, df: pd.DataFrame) -> pd.Series:
        """
        Гарантирует, что булева маска выровнена по индексу DataFrame.
        """
        if not isinstance(mask, pd.Series):
            arr = np.asarray(mask, dtype=bool)

            if len(arr) != len(df):
                raise ValueError(
                    "Длина булевой маски не совпадает с длиной DataFrame"
                )

            return pd.Series(arr, index=df.index, dtype=bool)

        if not mask.index.equals(df.index):
            mask = mask.reindex(df.index, fill_value=False)

        return mask.fillna(False).astype(bool)

    def _adjust_revenue_for_vat(
        self,
        df_9101: pd.DataFrame,
        df_9102: pd.DataFrame,
        asset_sale_types: list
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Уменьшает выручку от реализации активов на сумму НДС."""
        logger.debug("Корректировка выручки на НДС")

        mask_9101_assets, mask_9102_assets = self._build_asset_masks(
            df_9101,
            df_9102,
            asset_sale_types
        )

        # Выделяем НДС из df_9102
        df_9102_assets = df_9102.loc[mask_9102_assets]

        mask_vat = df_9102_assets['Корр.счет'].astype(str).str.startswith(
            self.NDS_ACCOUNTS,
            na=False
        )

        df_9102_vat = df_9102_assets.loc[mask_vat]

        if df_9102_vat.empty:
            logger.debug("НДС по продаже активов не обнаружен")
            return df_9101, df_9102

        # Суммируем НДС по Документу (валюта и рубли)
        vat_agg = df_9102_vat.groupby('Документ', as_index=False)[['оборот, тыс.ед.', 'оборот, тыс.руб.']].sum()
        vat_by_doc = vat_agg.rename(columns={
            'оборот, тыс.ед.': 'ндс_тыс_ед',
            'оборот, тыс.руб.': 'ндс_тыс_руб',
        })

        # Применяем к df_9101
        if mask_9101_assets.any():
            asset_rows = df_9101.loc[
                mask_9101_assets,
                ['Документ', 'оборот, тыс.ед.', 'оборот, тыс.руб.']
            ].copy()

            vat_map = vat_by_doc.set_index('Документ')['ндс_тыс_ед']
            vat_map_rub = vat_by_doc.set_index('Документ')['ндс_тыс_руб']

            nds_values = asset_rows['Документ'].map(vat_map).fillna(0)
            nds_values_rub = asset_rows['Документ'].map(vat_map_rub).fillna(0)

            # Доходы со знаком МИНУС, поэтому для уменьшения
            # по модулю СКЛАДЫВАЕМ с положительным НДС.
            # Пример: было -1000, НДС=+160 → стало -1000 + 160 = -840
            updated_turnover = asset_rows['оборот, тыс.ед.'] + nds_values
            updated_turnover_rub = asset_rows['оборот, тыс.руб.'] + nds_values_rub

            # Обновляем df_9101 по индексу фактических строк
            df_9101.loc[updated_turnover.index, 'оборот, тыс.ед.'] = updated_turnover
            df_9101.loc[updated_turnover_rub.index, 'оборот, тыс.руб.'] = updated_turnover_rub

            adjusted = int((nds_values > 0).sum())
        else:
            adjusted = 0

        # Удаляем строки НДС из df_9102
        mask_vat_in_main = (
            mask_9102_assets &
            df_9102['Корр.счет'].astype(str).str.startswith(self.NDS_ACCOUNTS, na=False)
        )

        mask_vat_in_main = self._ensure_aligned_bool_mask(mask_vat_in_main, df_9102)

        df_9102 = df_9102.loc[~mask_vat_in_main].copy()

        removed_vat = int(mask_vat_in_main.sum())

        logger.debug(
            "Скорректировано {} строк выручки на НДС, удалено {} строк НДС из 91.02",
            adjusted,
            removed_vat,
        )

        return df_9101, df_9102

    def _pull_contractors_from_9101(
        self,
        df_9101: pd.DataFrame,
        df_9102: pd.DataFrame,
        asset_sale_types: list
    ) -> pd.DataFrame:
        """Подтягивает контрагентов и вид_связи из df_9101 в df_9102 по Документу."""
        logger.debug("Подтягивание контрагентов из 91.01 в 91.02")

        mask_9101_assets, mask_9102_assets = self._build_asset_masks(
            df_9101,
            df_9102,
            asset_sale_types
        )

        if not mask_9102_assets.any():
            return df_9102

        # Маппинг Документ → контрагент
        contractor_mapping = (
            df_9101.loc[mask_9101_assets, ['Документ', 'контрагент']]
            .drop_duplicates(subset='Документ', keep='first')
            .set_index('Документ')['контрагент']
        )

        # Маппинг Документ → вид_связи (если уже есть)
        connection_mapping = None

        if 'вид_связи' in df_9101.columns:
            connection_mapping = (
                df_9101.loc[mask_9101_assets, ['Документ', 'вид_связи']]
                .drop_duplicates(subset='Документ', keep='first')
                .set_index('Документ')['вид_связи']
            )

        # Маска: строки продажи активов с 'не_указано' в контрагенте
        mask_unspecified = (
            mask_9102_assets &
            df_9102['контрагент'].fillna('не_указано').astype(str).eq('не_указано')
        )

        mask_unspecified = self._ensure_aligned_bool_mask(mask_unspecified, df_9102)

        if not mask_unspecified.any():
            return df_9102

        # Подтягиваем контрагента
        mapped_contractors = df_9102.loc[mask_unspecified, 'Документ'].map(contractor_mapping)

        df_9102.loc[mask_unspecified, 'контрагент'] = (
            mapped_contractors
            .fillna('не_указано')
            .astype('string')
        )

        # Подтягиваем вид_связи (если есть маппинг)
        if connection_mapping is not None and 'вид_связи' in df_9102.columns:
            mapped_connections = df_9102.loc[mask_unspecified, 'Документ'].map(connection_mapping)
            current_connections = df_9102.loc[mask_unspecified, 'вид_связи']

            df_9102.loc[mask_unspecified, 'вид_связи'] = (
                mapped_connections
                .fillna(current_connections)
                .astype('string')
            )

        df_9102['контрагент'] = df_9102['контрагент'].astype('string')

        replaced = int(mapped_contractors.notna().sum())

        logger.debug(
            "Подтянуто {} контрагентов из 91.01 в 91.02",
            replaced,
        )

        return df_9102

    def _distribute_orphan_expenses(
        self,
        df_9101: pd.DataFrame,
        df_9102: pd.DataFrame,
        asset_sale_types: list
    ) -> pd.DataFrame:
        """Распределяет осиротевшие расходы пропорционально выручке."""
        logger.debug("Распределение осиротевших расходов")

        mask_9101_assets, mask_9102_assets = self._build_asset_masks(
            df_9101,
            df_9102,
            asset_sale_types
        )

        # Определяем документы-сироты
        docs_in_9101 = set(df_9101.loc[mask_9101_assets, 'Документ'].unique())
        docs_in_9102 = set(df_9102.loc[mask_9102_assets, 'Документ'].unique())

        orphan_docs = docs_in_9102 - docs_in_9101

        if not orphan_docs:
            logger.debug("Осиротевших документов не найдено")
            return df_9102

        mask_orphan = (
            mask_9102_assets &
            df_9102['Документ'].isin(orphan_docs)
        )

        mask_orphan = self._ensure_aligned_bool_mask(mask_orphan, df_9102)

        df_9102_orphan = df_9102.loc[mask_orphan].copy()
        df_9102_attached = df_9102.loc[~mask_orphan].copy()

        # Расчёт долей выручки по контрагентам в рамках вид_дохода_расхода
        df_9101_assets = df_9101.loc[mask_9101_assets].copy()

        if df_9101_assets.empty:
            logger.warning(
                "[!] Не удалось распределить осиротевшие расходы: "
                "нет строк выручки по продаже активов"
            )
            return df_9102

        revenue_by_type = (
            df_9101_assets
            .groupby('вид_дохода_расхода')['оборот, тыс.ед.']
            .transform('sum')
        )

        # Защита от деления на 0
        df_9101_assets['доля_контрагента'] = np.where(
            revenue_by_type != 0,
            df_9101_assets['оборот, тыс.ед.'] / revenue_by_type,
            0
        )

        contractor_share = (
            df_9101_assets
            .groupby(['вид_дохода_расхода', 'контрагент'], as_index=False)
            ['доля_контрагента']
            .sum()
        )

        # Маппинг контрагент → вид_связи
        contractor_to_connection = None

        if 'вид_связи' in df_9101_assets.columns:
            contractor_to_connection = (
                df_9101_assets[['контрагент', 'вид_связи']]
                .drop_duplicates(subset='контрагент', keep='first')
                .set_index('контрагент')['вид_связи']
            )

        # Удаляем контрагент перед merge (чтобы не было суффиксов _x/_y)
        df_9102_orphan_clean = df_9102_orphan.drop(
            columns=['контрагент'],
            errors='ignore'
        )

        # Cross-join по вид_дохода_расхода
        df_9102_orphan_dist = df_9102_orphan_clean.merge(
            contractor_share,
            on='вид_дохода_расхода',
            how='inner'
        )

        if df_9102_orphan_dist.empty:
            logger.warning(
                "[!] Не удалось распределить осиротевшие расходы (нет выручки)"
            )
            return df_9102

        # Распределяем оборот (валюта)
        df_9102_orphan_dist['оборот, тыс.ед.'] = (
            df_9102_orphan_dist['оборот, тыс.ед.'] *
            df_9102_orphan_dist['доля_контрагента']
        )
        # Распределяем рублёвый эквивалент тем же коэффициентом
        if 'оборот, тыс.руб.' in df_9102_orphan_dist.columns:
            df_9102_orphan_dist['оборот, тыс.руб.'] = (
                df_9102_orphan_dist['оборот, тыс.руб.'] *
                df_9102_orphan_dist['доля_контрагента']
            )

        df_9102_orphan_dist = df_9102_orphan_dist.drop(columns=['доля_контрагента'])

        # Подтягиваем вид_связи
        if contractor_to_connection is not None:
            df_9102_orphan_dist['вид_связи'] = (
                df_9102_orphan_dist['контрагент']
                .map(contractor_to_connection)
                .fillna('не_указано')
                .astype('string')
            )

        # Собираем обратно
        df_9102_result = pd.concat(
            [df_9102_attached, df_9102_orphan_dist],
            ignore_index=True
        )

        # Проверка сходимости
        sum_before = df_9102_orphan['оборот, тыс.ед.'].sum()
        sum_after = df_9102_orphan_dist['оборот, тыс.ед.'].sum()

        if abs(sum_before - sum_after) > 0.01:
            logger.warning(
                "[!] Расхождение при распределении: было {:,.2f}, стало {:,.2f}",
                sum_before,
                sum_after,
            )

        logger.debug(
            "Распределено {} осиротевших строк на {} строк",
            len(df_9102_orphan),
            len(df_9102_orphan_dist),
        )

        return df_9102_result

    # =========================================================================
    # ОБРАБОТКА КРЕДИТНЫХ ЛИНИЙ
    # =========================================================================

    def _process_credit_lines(
        self,
        df: pd.DataFrame,
        reference_rbp_credit: pd.DataFrame,
        is_income: bool = True
    ) -> pd.DataFrame:
        """Обрабатывает кредитные линии: подтягивает контрагентов из справочника."""
        CREDIT_TYPE = 'Кредитное обслуживание и расходы по открытию кредитных линий'

        mask_credit = (
            (df['вид_дохода_расхода'] == CREDIT_TYPE) &
            (df['Корр.счет'].astype(str).str.startswith('97', na=False))
        )

        if not mask_credit.any():
            return df

        logger.debug("Обработка кредитных линий (РБП)")

        # Для 91.01 РБП в Субконто Дт_1, для 91.02 — в Субконто Кт_1
        subconto_col = 'Субконто Дт_1' if is_income else 'Субконто Кт_1'

        df['рбп_кредитные_линии'] = df[subconto_col].where(
            mask_credit,
            'не_указано'
        ).astype('string')

        # Маппинг из справочника
        mapping_rbp = (
            reference_rbp_credit
            .drop_duplicates(subset='рбп_кредитные_линии')
            .set_index('рбп_кредитные_линии')['контрагент']
        )

        # Подтягиваем контрагентов (только для строк кредитных линий)
        mapped_values = df.loc[mask_credit, 'рбп_кредитные_линии'].map(mapping_rbp)

        mask_found = mapped_values.notna()

        if mask_found.any():
            df.loc[mask_credit, 'контрагент'] = np.where(
                mask_found,
                mapped_values,
                df.loc[mask_credit, 'контрагент']
            )

        # Диагностика отсутствующих
        missing_mask = mask_credit.copy()
        missing_mask[mask_credit] = ~mask_found

        if missing_mask.any():
            missing_list = df.loc[missing_mask, 'рбп_кредитные_линии'].unique().tolist()

            logger.warning(
                "[!] В справочнике КредитОбслуж отсутствуют {} РБП:\n{}",
                len(missing_list),
                "\n".join("  - {}".format(item) for item in missing_list[:10]),
            )

        df['контрагент'] = df['контрагент'].astype('string')

        logger.debug(
            "Кредитные линии: {} контрагентов подтянуто",
            mask_found.sum(),
        )

        return df

    # =========================================================================
    # ОБОГАЩЕНИЕ: ГРУППА_КА, СЕГМЕНТ_КА, ВИД_СВЯЗИ
    # =========================================================================

    def _enrich_with_connection_info(
        self,
        df: pd.DataFrame,
        mapping_group: pd.Series,
        mapping_segment_ka: pd.Series,
        segment_company: str,
    ) -> pd.DataFrame:
        logger.debug("Обогащение: группа_ка, сегмент_ка, вид_связи")

        df['сегмент'] = segment_company
        df['сегмент'] = df['сегмент'].astype('string')

        # Расширяем маппинги
        mapping_group_ext = {**mapping_group.to_dict(), 'не_указано': 'не_указано'}
        mapping_segment_ka_ext = {**mapping_segment_ka.to_dict(), 'не_указано': 'не_указано'}

        # Маппим контрагентов
        df['группа_ка'] = (
            df['контрагент']
            .map(mapping_group_ext)
            .fillna('3 лица')
            .astype('string')
        )

        df['сегмент_ка'] = (
            df['контрагент']
            .map(mapping_segment_ka_ext)
            .fillna('3 лица')
            .astype('string')
        )

        # Рассчитываем вид_связи
        df['вид_связи'] = self._calculate_connection_type(df, segment_company)

        # Проверка неожиданных значений
        expected_groups = {'3 лица', 'Прочие ГАП', 'ГСК', 'не_указано'}
        actual_groups = set(df['группа_ка'].unique())
        unexpected = actual_groups - expected_groups

        if unexpected:
            logger.warning("[!] Неожиданные значения в 'группа_ка': {}", unexpected)

        logger.debug(
            "вид_связи: {}",
            df['вид_связи'].value_counts().to_dict(),
        )

        return df

    def _calculate_connection_type(
        self,
        df: pd.DataFrame,
        segment_company: str
    ) -> pd.Series:
        """
        Рассчитывает вид_связи на основе группа_ка и сегмент_ка.

        Для 91 счета сегмент компании единый (segment_company),
        поэтому сравниваем сегмент_ка именно с ним.
        """
        conditions = [
            df['группа_ка'] == 'не_указано',
            df['группа_ка'] == '3 лица',
            df['группа_ка'] == 'Прочие ГАП',
            (df['группа_ка'] == 'ГСК') & (df['сегмент_ка'] == segment_company),
            (df['группа_ка'] == 'ГСК') & (df['сегмент_ка'] != segment_company),
        ]

        choices = [
            'не_указано',
            '3 лица',
            'Прочие ГАП',
            'ГСК внутрисегмент.',
            'ГСК межсегмент.',
        ]

        result = np.select(conditions, choices, default='не_указано')

        return pd.Series(result, index=df.index, dtype='string')

    # =========================================================================
    # КОНТРОЛЬ ПОДРАЗУМЕВАЕМОГО КУРСА (Фаза 3.3)
    # =========================================================================

    def _log_rate_anomalies(
        self,
        df_9101: pd.DataFrame,
        df_9102: pd.DataFrame,
        context: ProcessingContext,
        stage: str,
    ) -> None:
        """Контроль подразумеваемого курса строк 91.x (Фаза 3.3; задача
        «Диагностика аномального курса 92,48 RUB/AED» — см. AGENTS.md).

        Проверяет в разрезе проводок (до агрегации groupby-sum в
        _merge_with_main_df):
        1. Сумма_руб = Сумма × курс(дата операции) — ловит перезапись
           рублёвого эквивалента между шагом 14 и текущей точкой;
        2. «Ядовитые» строки: оборот ед.≈0 при оборот руб.≠0 — именно они
           при суммировании в группу дают аномальный курс в итоговом ОПУ.
        Все операции шага 17 над суммами линейны, поэтому расхождение между
        точками «вход» и «выход» локализует мутацию внутри шага.
        Пишет WARNING в лог (app.log), конвейер не прерывает.
        """
        try:
            median_rate = get_rate_median(context)
            deviation_limit = get_rate_deviation_limit(context)
        except (ValueError, KeyError) as e:
            logger.debug('Контроль курса ({}): пропущен — {}', stage, e)
            return

        for label, df_91 in (('91.01', df_9101), ('91.02', df_9102)):
            amount_col = 'оборот, тыс.ед.'
            rub_col = 'оборот, тыс.руб.'
            if amount_col not in df_91.columns or rub_col not in df_91.columns:
                continue

            amt = pd.to_numeric(df_91[amount_col], errors='coerce')
            rub = pd.to_numeric(df_91[rub_col], errors='coerce')
            poison_mask = (amt.abs() <= 1e-6) & (rub.abs() > 1e-6)

            mismatch_mask = pd.Series(False, index=df_91.index)
            if {'Дата', 'Сумма', 'Сумма_руб'}.issubset(df_91.columns):
                dates = pd.to_datetime(df_91['Дата'], errors='coerce')
                summ = pd.to_numeric(df_91['Сумма'], errors='coerce')
                summ_rub = pd.to_numeric(df_91['Сумма_руб'], errors='coerce')
                rate_by_date = {}
                for ts in pd.Series(dates.dropna().unique()).sort_values():
                    try:
                        rate_by_date[ts] = get_rate_for_date(context, ts)
                    except ValueError:
                        continue
                expected_rub = summ * dates.map(rate_by_date)
                mismatch_mask = (
                    (summ_rub - expected_rub).abs()
                    > deviation_limit * expected_rub.abs()
                ) & summ.notna() & summ_rub.notna() & expected_rub.notna()

            anomaly_mask = poison_mask | mismatch_mask
            if not anomaly_mask.any():
                logger.debug(
                    'Контроль курса ({}, {}): аномалий не обнаружено (строк {}) — '
                    'Сумма_руб соответствует Сумма×курс(дата), ядовитых строк нет',
                    stage, label, len(df_91),
                )
                continue

            info_cols = [c for c in (
                'Документ', 'Дата', 'Сумма', 'Сумма_руб', 'счет',
                'доход_расход', 'вид_дохода_расхода', 'контрагент',
            ) if c in df_91.columns]
            problem = df_91.loc[anomaly_mask, info_cols].copy()
            problem[amount_col] = amt[anomaly_mask]
            problem[rub_col] = rub[anomaly_mask]
            logger.warning(
                '[!] Контроль курса ({}, {}): {} строк(и) с подозрительным руб/ед '
                '(медиана листа курса {}; порог отклонения {:.0%}). Примеры:\n{}',
                stage, label, int(anomaly_mask.sum()),
                median_rate, deviation_limit,
                problem.head(10).to_string(index=False),
            )

    # =========================================================================
    # СЛУЖЕБНЫЕ СТОЛБЦЫ И ОБЪЕДИНЕНИЕ
    # =========================================================================

    def _add_service_columns(self, df: pd.DataFrame, account: str) -> pd.DataFrame:
        """Добавляет служебные столбцы для соответствия структуре main_df."""
        if 'счет' not in df.columns:
            df['счет'] = account
            df['счет'] = df['счет'].astype('string')
        else:
            df['счет'] = df['счет'].astype('string')

        # Убираем технические столбцы
        cols_to_drop = [
            'Имя_файла',
            'Дата',
            'Сумма',
            'Содержание_1',
            'Содержание_2',
            'Субконто Дт_1',
            'Субконто Дт_2',
            'Субконто Дт_3',
            'Субконто Кт_1',
            'Субконто Кт_2',
            'Субконто Кт_3',
            'Дт',
            'Кт',
            'Корр.счет',
        ]

        df = df.drop(
            columns=[c for c in cols_to_drop if c in df.columns],
            errors='ignore'
        )

        return df

    def _merge_with_main_df(
        self,
        main_df: pd.DataFrame,
        df_9101: pd.DataFrame,
        df_9102: pd.DataFrame,
    ) -> pd.DataFrame:
        """Объединяет 91.01 и 91.02 с основной расшифровкой ОПУ."""
        logger.debug("Объединение с основной расшифровкой ОПУ")

        # Объединяем 91.01 и 91.02
        df_combined = pd.concat([df_9101, df_9102], ignore_index=True)

        # Группировка по указанным столбцам
        group_cols = [
            'счет',
            'вид_дохода_расхода',
            'доход_расход',
            'контрагент',
            'объект для изм ппа',
            'группа_ка',
            'сегмент_ка',
            'сегмент',
            'вид_связи',
            'рбп_кредитные_линии',
            'рбп_проценты'
        ]

        # Оставляем только существующие столбцы
        existing_group_cols = [
            col for col in group_cols if col in df_combined.columns
        ]

        # Заполняем NaN в текстовых столбцах перед группировкой
        for col in existing_group_cols:
            df_combined[col] = df_combined[col].fillna('не_указано').astype('string')

        df_combined = df_combined.groupby(
            existing_group_cols,
            as_index=False
        )[['оборот, тыс.ед.', 'оборот, тыс.руб.']].sum()

        logger.debug(
            "Группировка: {} → {} строк",
            len(df_9101) + len(df_9102),
            len(df_combined),
        )

        # Объединяем с main_df
        df_final = pd.concat([main_df, df_combined], ignore_index=True)

        # Явное приведение всех текстовых столбцов к string
        text_cols = [
            'счет',
            'контрагент',
            'ном_группа',
            'доход_расход',
            'вид_дохода_расхода',
            'сегмент',
            'группа_ка',
            'сегмент_ка',
            'вид_связи',
            'объект для изм ппа',
            'рбп_кредитные_линии',
            'рбп_проценты'
        ]

        for col in text_cols:
            if col in df_final.columns:
                df_final[col] = df_final[col].astype('string')

        logger.debug(
            "Объединение завершено: {} + {} = {} строк",
            len(main_df),
            len(df_combined),
            len(df_final),
        )

        return df_final

    def _extract_contractors(
        self,
        df: pd.DataFrame,
        is_income: bool,
        accounts_with_contractors: tuple
    ) -> pd.DataFrame:
        """
        Извлекает контрагентов из Субконто.

        Для 91.01 (доходы): контрагент в Субконто Дт_1, корр.счет = Дт
        Для 91.02 (расходы): контрагент в Субконто Кт_1, корр.счет = Кт
        """
        contractor_col = 'Субконто Дт_1' if is_income else 'Субконто Кт_1'

        mask_contractor = df['Корр.счет'].astype(str).isin(accounts_with_contractors)

        df['контрагент'] = df[contractor_col].where(
            mask_contractor,
            'не_указано'
        ).astype('string')

        logger.debug(
            "Извлечено контрагентов: {} из {} строк",
            (df['контрагент'] != 'не_указано').sum(),
            len(df),
        )

        return df
