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

from config.settings import REFERENCE_SCOPE_COL, SCOPE_ALL_VALUE
from utils import build_composite_key

from pipeline.base import ProcessingContext
from pipeline.errors import MissingMappingError, ReferenceMismatchError
from pipeline.step_config import StepConstants


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

        # Виды РБП аренды/лизинга (справочник ВидыРБП_АрендаЛизинг) — для
        # определения РБП-объектов по ОСВ (счёт 97.x + вид субконто) в ветке
        # контрагентов ППА (_pull_contractors_from_ppa_by_rbp). Служебная
        # заглушка 'не_указано' не является видом РБП.
        rbp_types_df = context.references.get('виды_рбп_аренда_лизинг')
        valid_rbp_types: set[str] = set()
        if rbp_types_df is not None and 'виды_рбп_аренда_лизинг' in rbp_types_df.columns:
            types_series = rbp_types_df['виды_рбп_аренда_лизинг'].astype('string')
            types_series = types_series[~self._is_service_value(types_series)]
            valid_rbp_types = set(types_series.dropna().str.strip().tolist())

        return {
            'mapping_opu': mapping_opu_df,
            'ppa': ppa_df,
            'group_companies': group_companies_df,
            'companies': companies_df,
            'credit': credit_df,
            'accounts_with_contractors': accounts_with_contractors,
            'asset_sale_types': asset_sale_types,
            'valid_rbp_types': valid_rbp_types,
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

        Жёсткое правило: на один _key ('счет[:5] + доход_расход') должен быть
        ровно один 'вид_дохода_расхода'. Разрешение — две ступени:
        1. по ключу ровно один тип — присваивается он;
        2. несколько типов: если во «взгляде компании» на этот ключ есть ЕЁ
           собственные строки (колонка 'компания' != 'все') и они дают ровно
           один тип — берётся он (индивидуальные строки перекрывают
           универсальные); это расширяет перекрытие с ключа строки справочника
           на ключ поиска;
        3. иначе — ReferenceMismatchError с диагностикой (произвольный выбор
           первой строки запрещён).

        Ступени «тип подтверждённого объекта ППА» и «тип из блока сегмента»
        удалены (сессия 02.10.2026): подстрока «ППА» в имени объекта не является
        бизнес-признаком, а сегмент на реальном Меппинг_опу не разрешал НИ
        ОДНОГО ключа (замер 02.10.2026: все многотипные ключи имеют одинаковый
        сегмент у всех типов — 0 разрешённых сегментом). Сегмент остался только
        источником подсказки в тексте ошибки: различие, которое раньше «выручала»
        ступень 3, теперь надо разводить колонкой 'компания'.
        """
        reference = reference_df
        candidates = self._build_type_candidates(reference)
        # Индивидуальные строки компании: во «взгляде компании» остались только
        # 'все' и строки самой компании (чужие отброшены resolve_company_view)
        individual_candidates = self._build_individual_type_candidates(reference)
        # Сегмент — только источник подсказки в диагностике, решение не принимает
        segment_candidates = (
            self._build_type_candidates(reference[reference['сегмент'] == segment])
            if segment and 'сегмент' in reference.columns
            else {}
        )

        resolved = pd.Series(pd.NA, index=df.index, dtype='string')
        ambiguous_mask = pd.Series(False, index=df.index)
        for key, key_candidates in candidates.items():
            key_mask = df['_key'].eq(key)
            if len(key_candidates) == 1:
                resolved.loc[key_mask] = key_candidates[0]
                continue

            # Индивидуальные строки компании перекрывают универсальные: если на
            # ключ поиска у компании ровно один свой тип — берём его
            individual_types = individual_candidates.get(key, [])
            if len(individual_types) == 1:
                resolved.loc[key_mask] = individual_types[0]
                logger.info(
                    "Тип ОПУ по счёту {} для ключа '{}' определён по индивидуальным "
                    "строкам компании (колонка '{}'): '{}'",
                    account_label, key, REFERENCE_SCOPE_COL, individual_types[0],
                )
                continue

            ambiguous_mask.loc[key_mask] = True

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
            problem_data['типы_индивидуальных_строк'] = problem_data['ключ_поиска'].map(
                lambda key: ' | '.join(individual_candidates.get(key, [])) or '—'
            )
            problem_data['типы_по_сегменту_компании'] = problem_data['ключ_поиска'].map(
                lambda key: ' | '.join(segment_candidates.get(key, []))
            )
            problem_data['причина'] = problem_data['ключ_поиска'].map(
                lambda key: (
                    'нет индивидуальных строк компании'
                    if not individual_candidates.get(key)
                    else 'в индивидуальных строках больше одного типа'
                )
            )
            problem_data['подсказка'] = problem_data['ключ_поиска'].map(
                lambda key: self._ambiguity_hint(key, segment, segment_candidates)
            )
            raise ReferenceMismatchError(
                message=(
                    f"В справочнике Меппинг_опу для {int(ambiguous_mask.sum())} строк "
                    f"по счёту {account_label} на один ключ (счет + доход_расход) "
                    f"приходится несколько 'вид_дохода_расхода', и ни один не "
                    f"определён однозначно: нет индивидуальных строк компании "
                    f"(колонка '{REFERENCE_SCOPE_COL}') с единственным типом. "
                    f"Разведите типы колонкой '{REFERENCE_SCOPE_COL}'."
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

    @staticmethod
    def _build_individual_type_candidates(reference: pd.DataFrame) -> dict[str, list[str]]:
        """Кандидаты типов из индивидуальных строк компании (scope != 'все')."""
        if REFERENCE_SCOPE_COL not in reference.columns:
            return {}
        individual_mask = (
            reference[REFERENCE_SCOPE_COL].fillna(SCOPE_ALL_VALUE)
            .astype('string').str.strip().str.casefold()
            != SCOPE_ALL_VALUE.casefold()
        )
        if not individual_mask.any():
            return {}
        individual = reference[individual_mask]
        grouped = individual.assign(_key=individual['_key']).groupby('_key', sort=False)
        return {
            key: frame['вид_дохода_расхода'].dropna().astype(str).drop_duplicates().tolist()
            for key, frame in grouped
        }

    @staticmethod
    def _ambiguity_hint(
        key: str, segment: str | None, segment_candidates: dict
    ) -> str:
        """Подсказка для текста ReferenceMismatchError."""
        seg_types = segment_candidates.get(key, [])
        if len(seg_types) == 1 and segment:
            return (
                f"В блоке сегмента '{segment}' тип однозначен "
                f"('{seg_types[0]}') — перенесите строку меппинга "
                f"в колонку '{REFERENCE_SCOPE_COL}' этой компании."
            )
        return ""

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
