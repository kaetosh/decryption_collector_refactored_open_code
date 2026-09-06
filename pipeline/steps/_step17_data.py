"""
Mixin с загрузкой и фильтрацией данных для Шага 17.

Методы:
    _load_references: загрузка всех справочников (Меппинг_опу, ППА, ВидСвязиКА, и т.д.)
    _load_data_from_context: получение ОСВ и проводок из контекста
    _filter_91_transactions: фильтрация проводок по 91.01/91.02
    _add_income_expense_type: подтягивание вид_дохода_расхода из Меппинг_опу
    _check_unspecified: проверка наличия неучтённых доходов/расходов
    _extract_contractors: извлечение контрагентов из Субконто
"""
import pandas as pd
from loguru import logger

from pipeline.base import ProcessingContext
from pipeline.errors import MissingMappingError


class Step17DataMixin:
    """Миксин загрузки и фильтрации для Шага 17."""

    def _load_references(self, name_company: str, context: ProcessingContext) -> dict:
        """Загружает все справочники, необходимые для шага 17."""
        logger.debug("Загрузка справочников для обработки 91.01/91.02")

        mapping_opu_df = context.references['меппинг_опу']

        ppa_df = context.references['справочник_ппа']
        ppa_df = ppa_df[ppa_df['наименование_компании'] == name_company]

        group_companies_df = context.references['вид_связи_ка']

        companies_df = context.references['компании_группы']

        credit_df = context.references['кредит_обслуж']
        credit_df = credit_df[credit_df['компания'] == name_company]

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

    def _filter_91_transactions(
        self,
        transactions_all_df: pd.DataFrame,
        reference_df: pd.DataFrame
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Фильтрует проводки по 91.01 и 91.02, подтягивает вид_дохода_расхода."""
        logger.debug("Фильтрация проводок 91.01 и 91.02")

        mask_9101 = (
            transactions_all_df['Кт'].str.startswith(self.ACCOUNT_OTHER_INCOME, na=False) &
            transactions_all_df['Имя_файла'].str.contains(f"_{self.ACCOUNT_OTHER_INCOME}_", na=False)
        )

        df_9101 = transactions_all_df.loc[mask_9101].copy()

        mask_9102 = (
            transactions_all_df['Дт'].str.startswith(self.ACCOUNT_OTHER_EXPENSE, na=False) &
            transactions_all_df['Имя_файла'].str.contains(f"_{self.ACCOUNT_OTHER_EXPENSE}_", na=False)
        )

        df_9102 = transactions_all_df.loc[mask_9102].copy()

        df_9101['оборот, тыс.ед.'] = df_9101['Сумма'] / -1000
        df_9101['оборот, тыс.руб.'] = df_9101['Сумма_руб'] / -1000
        df_9102['оборот, тыс.ед.'] = df_9102['Сумма'] / 1000
        df_9102['оборот, тыс.руб.'] = df_9102['Сумма_руб'] / 1000

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

        account_col = 'Кт' if is_income else 'Дт'
        corr_col = 'Дт' if is_income else 'Кт'

        df = df.rename(columns={
            account_col: 'счет',
            corr_col: 'Корр.счет'
        })

        subconto_col = 'Субконто Кт_1' if is_income else 'Субконто Дт_1'

        df = df.rename(columns={
            subconto_col: 'доход_расход'
        })

        reference_df = reference_df.copy()

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

        df['вид_дохода_расхода'] = df['_key'].map(mapping).astype('string')

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

        df = df.drop(columns=['_key'])

        return df

    def _check_unspecified(self, df_9101: pd.DataFrame, df_9102: pd.DataFrame) -> None:
        """Проверяет наличие неучтённых доходов/расходов."""
        if not df_9102.loc[df_9102['вид_дохода_расхода'] == 'не_указано'].empty:
            logger.warning("[!] Есть неучтённые расходы в 91.02!")

        if not df_9101.loc[df_9101['вид_дохода_расхода'] == 'не_указано'].empty:
            logger.warning("[!] Есть неучтённые доходы в 91.01!")

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
