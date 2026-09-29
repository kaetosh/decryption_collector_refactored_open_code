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

from utils import build_composite_key

from pipeline.base import ProcessingContext
from pipeline.errors import MissingMappingError, ReferenceMismatchError
from pipeline.step_config import AccountConstants, StepConstants


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

        chart_accounts_all_df = context.references['план_счетов_бу']
        chart_accounts_df = chart_accounts_all_df.loc[
            chart_accounts_all_df['компания'] == name_company
        ]

        accounts_with_contractors = tuple(
            chart_accounts_df.loc[
                chart_accounts_df['субконто_1'] == 'Контрагенты',
                'код'
            ]
        )

        # ★ Единообразие с другими справочниками: вместо сырого ValueError —
        # ReferenceMismatchError с problem_data (штатная остановка [STOP],
        # отчёт сохраняется в mismatches/)
        if chart_accounts_df.empty:
            problem_data = (
                chart_accounts_all_df[['компания']]
                .drop_duplicates()
                .rename(columns={'компания': 'компания_в_справочнике'})
            )
            raise ReferenceMismatchError(
                message=(
                    f"В справочнике ПланСчетовБУ нет ни одной записи по компании "
                    f"'{name_company}'. Дополните лист ПланСчетовБУ в "
                    f"Справочники.xlsx. "
                    f"{self.hint_companies_in_reference(chart_accounts_all_df, 'компания')}"
                ),
                problem_data=problem_data,
                reference_name="ПланСчетовБУ",
                searched_company=name_company,
            )

        if not accounts_with_contractors:
            raise ReferenceMismatchError(
                message=(
                    f"В плане счетов БУ (лист ПланСчетовБУ) для компании "
                    f"'{name_company}' нет счетов со значением 'Контрагенты' "
                    f"в поле субконто_1. Дополните справочник."
                ),
                problem_data=chart_accounts_df[['компания', 'код', 'субконто_1']].copy(),
                reference_name="ПланСчетовБУ",
                searched_company=name_company,
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
        reference_df: pd.DataFrame,
        segment: str | None = None,
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

        df_9101 = self._add_income_expense_type(
            df_9101, reference_df, is_income=True, segment=segment
        )
        df_9102 = self._add_income_expense_type(
            df_9102, reference_df, is_income=False, segment=segment
        )

        logger.debug(
            "91.01: {} строк, 91.02: {} строк",
            len(df_9101),
            len(df_9102),
        )

        return df_9101, df_9102

    def _resolve_income_expense_mapping(
        self,
        df: pd.DataFrame,
        reference_df: pd.DataFrame,
        account_label: str,
        segment: str | None = None,
    ) -> pd.Series:
        """
        Разрешает тип ОПУ по ключу, не выбирая произвольную строку.

        Ключ '_key' должен быть уже построен в df и reference_df (см.
        build_composite_key) — строить его здесь дублирует логику и
        рисует расхождение, если в разных местах срезы differ.

        Порядок разрешения — от надёжного источника к менее надёжному:
        1. по ключу ровно один тип — присваивается он;
        2. несколько типов: строки с подтверждённым признаком ППА (корр.счёт из
           PPA_ACCOUNTS и объект с префиксом 'ППА') получают тип ППА;
        3. остальные строки: тип ищется среди строк меппинга СЕГМЕНТА компании
           (индивидуальные строки компании живут в блоке её сегмента) — если
           там ровно один тип, присваивается он. Благодаря этому статья, которая
           у одной компании означает другое (например «Расходы по процентам
           аренда» вместо «Аренда»), не смешивается с общим блоком справочника;
        4. иначе — ReferenceMismatchError с диагностикой (произвольный выбор
           первой строки запрещён).
        """
        reference = reference_df
        candidates = self._build_type_candidates(reference)
        segment_candidates = (
            self._build_type_candidates(reference[reference['сегмент'] == segment])
            if segment and 'сегмент' in reference.columns
            else {}
        )

        corr = df['Корр.счет'].astype(str)
        ppa_account_mask = corr.str.startswith(AccountConstants.PPA_ACCOUNTS, na=False)
        object_cols = (
            ['Субконто Кт_1', 'Субконто Кт_2']
            if account_label == self.ACCOUNT_OTHER_EXPENSE
            else ['Субконто Дт_1', 'Субконто Дт_2']
        )
        ppa_object_mask = pd.Series(False, index=df.index)
        for column in object_cols:
            if column in df.columns:
                ppa_object_mask |= df[column].astype(str).str.strip().str.casefold().str.startswith(
                    StepConstants.PPA_OBJECT_MARKER.casefold()
                )
        ppa_mask = ppa_account_mask & ppa_object_mask

        resolved = pd.Series(pd.NA, index=df.index, dtype='string')
        ambiguous_mask = pd.Series(False, index=df.index)
        for key, key_candidates in candidates.items():
            key_mask = df['_key'].eq(key)
            if len(key_candidates) == 1:
                resolved.loc[key_mask] = key_candidates[0]
                continue

            expected_type = (
                StepConstants.PPA_EXPENSE_TYPE
                if account_label == self.ACCOUNT_OTHER_EXPENSE
                else StepConstants.PPA_INCOME_TYPE
            )
            ppa_rows = key_mask & ppa_mask
            if ppa_rows.any() and expected_type in key_candidates:
                resolved.loc[ppa_rows] = expected_type

            unresolved = key_mask & resolved.isna()
            if not unresolved.any():
                continue

            segment_types = segment_candidates.get(key, [])
            if len(segment_types) == 1:
                resolved.loc[unresolved] = segment_types[0]
                logger.info(
                    "Тип ОПУ по счёту {} для ключа '{}' определён по сегменту '{}' "
                    "компании: '{}'",
                    account_label, key, segment, segment_types[0],
                )
                continue

            ambiguous_mask.loc[unresolved] = True

        df['вид_дохода_расхода'] = resolved
        if ambiguous_mask.any():
            problem_data = self.build_unmapped_problem_data(
                df.loc[ambiguous_mask],
                group_cols=['_key', 'счет', 'Корр.счет', 'доход_расход'],
                rename_map={'_key': 'ключ_поиска'},
            )
            problem_data['допустимые_типы'] = problem_data['ключ_поиска'].map(
                lambda key: ' | '.join(candidates.get(key, []))
            )
            problem_data['типы_по_сегменту_компании'] = problem_data['ключ_поиска'].map(
                lambda key: ' | '.join(segment_candidates.get(key, []))
            )
            raise ReferenceMismatchError(
                message=(
                    f"В справочнике Меппинг_опу для {int(ambiguous_mask.sum())} строк "
                    f"по счёту {account_label} найдено несколько бизнес-типов. "
                    "Строки с подтверждённым объектом ППА разрешены; остальным тип "
                    "не удалось определить и по сегменту компании — требуется "
                    "уточнение справочника."
                ),
                problem_data=problem_data,
                reference_name="Меппинг_опу",
            )
        return resolved

    @staticmethod
    def _build_type_candidates(reference: pd.DataFrame) -> dict:
        """
        Возвращает {ключ '_key': [уникальные виды дохода/расхода]}.

        Вынесено отдельно: тот же расчёт нужен и по всему справочнику, и по
        блоку сегмента компании (см. _resolve_income_expense_mapping).
        """
        return {
            key: frame['вид_дохода_расхода'].dropna().astype(str).drop_duplicates().tolist()
            for key, frame in reference.groupby('_key', sort=False)
        }

    def _add_income_expense_type(
        self,
        df: pd.DataFrame,
        reference_df: pd.DataFrame,
        is_income: bool,
        segment: str | None = None,
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

        reference_df['_key'] = build_composite_key(
            reference_df, 'счет', 'доход_расход', truncate_a=5
        )
        df['_key'] = build_composite_key(df, 'счет', 'доход_расход', truncate_a=5)

        self._resolve_income_expense_mapping(df, reference_df, account_label, segment=segment)

        unmapped_mask = df['вид_дохода_расхода'].isna()

        if unmapped_mask.any():
            problem_data = self.build_unmapped_problem_data(
                df.loc[unmapped_mask],
                group_cols=['_key', 'счет', 'Корр.счет', 'доход_расход'],
                rename_map={'_key': 'ключ_поиска'},
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
