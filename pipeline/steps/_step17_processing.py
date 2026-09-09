"""
Mixin с бизнес-обработкой для Шага 17.

Методы:
    _process_ppa: подтягивание контрагентов из справочника ППА
    _process_asset_sales: обработка продажи активов (НДС, контрагенты, распределение)
    _build_asset_masks: построение масок продажи активов
    _ensure_aligned_bool_mask: выравнивание булевых масок по индексу
    _adjust_revenue_for_vat: корректировка выручки на НДС
    _pull_contractors_from_9101: подтягивание контрагентов из 91.01 в 91.02
    _distribute_orphan_expenses: распределение осиротевших расходов
    _process_credit_lines: обработка кредитных линий (РБП)
    _enrich_with_connection_info: обогащение группа_ка, сегмент_ка, вид_связи
    _calculate_connection_type: расчёт вид_связи
"""
import numpy as np
import pandas as pd
from loguru import logger

from pipeline.base import ProcessingContext
from pipeline.errors import MissingCreditContractorError, MissingMappingError
from pipeline.step_config import StepConstants
from config.settings import STRICT_CREDIT_CONTRACTOR_CHECK


class Step17ProcessingMixin:
    """Миксин бизнес-обработки для Шага 17."""

    def _process_ppa(
        self,
        df_9101: pd.DataFrame,
        df_9102: pd.DataFrame,
        reference_ppa_df: pd.DataFrame,
        name_company: str = ''
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

        mask_ppa_accounts_9101 = df_9101['Корр.счет'].astype(str).str.startswith(
            self.PPA_ACCOUNTS, na=False
        )
        mask_ppa_accounts_9102 = df_9102['Корр.счет'].astype(str).str.startswith(
            self.PPA_ACCOUNTS, na=False
        )

        df_9101['объект для изм ппа'] = df_9101['Субконто Дт_1'].where(
            mask_ppa_income_9101 & mask_ppa_accounts_9101,
            'не_указано'
        ).astype('string')

        df_9102['объект для изм ппа'] = df_9102['Субконто Кт_1'].where(
            mask_ppa_income_9102 & mask_ppa_accounts_9102,
            'не_указано'
        ).astype('string')

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

        self._validate_ppa_mapping(
            reference_ppa_df,
            name_company,
            missing_by_mapping={
                'ос_ппа': (
                    (df_9101, mask_01_09_9101, mapped_1_9101),
                    (df_9102, mask_01_09_9102, mapped_1_9102),
                ),
                'ос_после_перехода_в_собственность': (
                    (df_9101, mask_02_01_or_01_01_9101, mapped_2_9101),
                    (df_9102, mask_02_01_or_01_01_9102, mapped_2_9102),
                ),
            },
        )

        return df_9101, df_9102

    def _validate_ppa_mapping(
        self,
        reference_ppa_df: pd.DataFrame,
        name_company: str,
        missing_by_mapping: dict,
    ) -> None:
        """
        Проверяет полноту маппинга контрагентов из справочника ППА (шаг 17).

        Единообразие с другими справочниками, данные которых подтягиваются
        по имени компании: при отсутствии записей по компании (в том числе
        когда справочник пуст) и при неполном списке формируется отчёт
        с недостающими позициями — объектами ОС, по которым не подтянулся
        контрагент. Меппинг по 'ос_ппа' обязателен, по
        'ос_после_перехода_в_собственность' — только если в справочнике
        заполнен хотя бы один такой ключ.
        """
        missing_by_type = {}
        for column, checks in missing_by_mapping.items():
            if column == 'ос_после_перехода_в_собственность':
                if 'ос_после_перехода_в_собственность' not in reference_ppa_df.columns:
                    continue
                if reference_ppa_df['ос_после_перехода_в_собственность'].dropna().empty:
                    continue
            missing_values = set()
            for df, mask, mapped in checks:
                unmapped = df.loc[mask & mapped.isna(), 'объект для изм ппа']
                missing_values.update(unmapped.dropna().astype(str))
            missing_values.discard('не_указано')
            if missing_values:
                missing_by_type[column] = sorted(missing_values)

        if not missing_by_type:
            return

        raise MissingMappingError(
            message=(
                f"В справочнике ППА отсутствуют объекты ОС для подтягивания "
                f"контрагентов при выбытии прав пользования активами / изменении "
                f"условий договоров аренды (шаг 17) по компании '{name_company}'. "
                f"Колонки без меппинга: {sorted(missing_by_type)}. Дополните лист "
                f"ППА в Справочники.xlsx. "
                + self.hint_companies_in_reference(
                    reference_ppa_df, 'наименование_компании'
                )
            ),
            problem_data=self.make_missing_values_problem_data(
                missing_by_type, name_company
            ),
            reference_name="ППА",
            searched_company=name_company,
        )

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
            df_9101, df_9102, asset_sale_types
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

        df_9101, df_9102 = self._adjust_revenue_for_vat(
            df_9101, df_9102, asset_sale_types
        )

        df_9102 = self._pull_contractors_from_9101(
            df_9101, df_9102, asset_sale_types
        )

        df_9102 = self._distribute_orphan_expenses(
            df_9101, df_9102, asset_sale_types
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

    @staticmethod
    def _distribute_vat_by_rows(
        asset_rows: pd.DataFrame,
        vat_map: pd.Series,
        amount_col: str,
    ) -> pd.Series:
        """
        Распределяет НДС документа по строкам 91.01 ровно один раз на документ.

        НДС агрегирован по 'Документ' (vat_map), но строк 91.01 у документа может
        быть несколько (типично для «Аренда»). Прямое добавление НДС каждой строке
        многократно завышает выручку. Правило распределения:
        - пропорционально доле |оборот| строки в документе;
        - при нулевом итоге документа — поровну между строками;
        - остаток от округления добавляется последней строке документа, чтобы
          сумма распределённого НДС сошлась с НДС документа точно (без дрейфа).

        :return: Series (index = asset_rows.index) — величина НДС для каждой строки.
        """
        nds_doc = asset_rows['Документ'].map(vat_map).fillna(0).astype(float)

        doc_key = asset_rows['Документ'].astype(str)
        row_abs = pd.to_numeric(asset_rows[amount_col], errors='coerce').abs().fillna(0.0)
        doc_total = row_abs.groupby(doc_key).transform('sum').astype(float)
        n_rows = asset_rows.groupby(doc_key)[amount_col].transform('size').astype(float)

        share = np.where(
            doc_total > 0,
            row_abs / doc_total.where(doc_total > 0, 1.0),
            1.0 / n_rows.where(n_rows > 0, 1.0),
        )
        share = pd.Series(share, index=asset_rows.index, dtype=float)

        distributed = nds_doc * share

        # Точная сходимость: последняя строка документа получает остаток дрейфа
        is_last = asset_rows.groupby(doc_key).cumcount(ascending=False).eq(0)
        residual = nds_doc - distributed.groupby(doc_key).transform('sum')
        distributed = distributed + residual.where(is_last, 0.0)

        return distributed

    def _adjust_revenue_for_vat(
        self,
        df_9101: pd.DataFrame,
        df_9102: pd.DataFrame,
        asset_sale_types: list
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Уменьшает выручку от реализации активов на сумму НДС."""
        logger.debug("Корректировка выручки на НДС")

        mask_9101_assets, mask_9102_assets = self._build_asset_masks(
            df_9101, df_9102, asset_sale_types
        )

        df_9102_assets = df_9102.loc[mask_9102_assets]

        mask_vat = df_9102_assets['Корр.счет'].astype(str).str.startswith(
            self.NDS_ACCOUNTS, na=False
        )
        df_9102_vat = df_9102_assets.loc[mask_vat]

        if df_9102_vat.empty:
            logger.debug("НДС по продаже активов не обнаружен")
            return df_9101, df_9102

        vat_agg = df_9102_vat.groupby('Документ', as_index=False)[['оборот, тыс.ед.', 'оборот, тыс.руб.']].sum()
        vat_by_doc = vat_agg.rename(columns={
            'оборот, тыс.ед.': 'ндс_тыс_ед',
            'оборот, тыс.руб.': 'ндс_тыс_руб',
        })

        if mask_9101_assets.any():
            asset_rows = df_9101.loc[
                mask_9101_assets,
                ['Документ', 'оборот, тыс.ед.', 'оборот, тыс.руб.']
            ].copy()

            vat_map = vat_by_doc.set_index('Документ')['ндс_тыс_ед']
            vat_map_rub = vat_by_doc.set_index('Документ')['ндс_тыс_руб']

            # НДС документа распределяется по строкам 91.01 РОВНО ОДИН РАЗ
            # (пропорционально доле строки в обороте документа). Прямое добавление
            # НДС каждой строке многократно завышало выручку «Аренда»
            # (на многострочных документах НДС накатывался на каждую строку).
            nds_values = self._distribute_vat_by_rows(
                asset_rows, vat_map, 'оборот, тыс.ед.'
            )
            nds_values_rub = self._distribute_vat_by_rows(
                asset_rows, vat_map_rub, 'оборот, тыс.руб.'
            )

            updated_turnover = asset_rows['оборот, тыс.ед.'] + nds_values
            updated_turnover_rub = asset_rows['оборот, тыс.руб.'] + nds_values_rub

            df_9101.loc[updated_turnover.index, 'оборот, тыс.ед.'] = updated_turnover
            df_9101.loc[updated_turnover_rub.index, 'оборот, тыс.руб.'] = updated_turnover_rub

            adjusted = int((nds_values.abs() > 0).sum())
        else:
            adjusted = 0

        mask_vat_in_main = (
            mask_9102_assets &
            df_9102['Корр.счет'].astype(str).str.startswith(self.NDS_ACCOUNTS, na=False)
        )
        mask_vat_in_main = self._ensure_aligned_bool_mask(mask_vat_in_main, df_9102)
        df_9102 = df_9102.loc[~mask_vat_in_main].copy()
        removed_vat = int(mask_vat_in_main.sum())

        logger.debug(
            "Скорректировано {} строк выручки на НДС, удалено {} строк НДС из 91.02",
            adjusted, removed_vat,
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
            df_9101, df_9102, asset_sale_types
        )

        if not mask_9102_assets.any():
            return df_9102

        contractor_mapping = (
            df_9101.loc[mask_9101_assets, ['Документ', 'контрагент']]
            .drop_duplicates(subset='Документ', keep='first')
            .set_index('Документ')['контрагент']
        )

        connection_mapping = None
        if 'вид_связи' in df_9101.columns:
            connection_mapping = (
                df_9101.loc[mask_9101_assets, ['Документ', 'вид_связи']]
                .drop_duplicates(subset='Документ', keep='first')
                .set_index('Документ')['вид_связи']
            )

        mask_unspecified = (
            mask_9102_assets &
            df_9102['контрагент'].fillna('не_указано').astype(str).eq('не_указано')
        )
        mask_unspecified = self._ensure_aligned_bool_mask(mask_unspecified, df_9102)

        if not mask_unspecified.any():
            return df_9102

        mapped_contractors = df_9102.loc[mask_unspecified, 'Документ'].map(contractor_mapping)
        df_9102.loc[mask_unspecified, 'контрагент'] = (
            mapped_contractors.fillna('не_указано').astype('string')
        )

        if connection_mapping is not None and 'вид_связи' in df_9102.columns:
            mapped_connections = df_9102.loc[mask_unspecified, 'Документ'].map(connection_mapping)
            current_connections = df_9102.loc[mask_unspecified, 'вид_связи']
            df_9102.loc[mask_unspecified, 'вид_связи'] = (
                mapped_connections.fillna(current_connections).astype('string')
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
            df_9101, df_9102, asset_sale_types
        )

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

        contractor_to_connection = None
        if 'вид_связи' in df_9101_assets.columns:
            contractor_to_connection = (
                df_9101_assets[['контрагент', 'вид_связи']]
                .drop_duplicates(subset='контрагент', keep='first')
                .set_index('контрагент')['вид_связи']
            )

        df_9102_orphan_clean = df_9102_orphan.drop(
            columns=['контрагент'], errors='ignore'
        )

        df_9102_orphan_dist = df_9102_orphan_clean.merge(
            contractor_share, on='вид_дохода_расхода', how='inner'
        )

        if df_9102_orphan_dist.empty:
            logger.warning(
                "[!] Не удалось распределить осиротевшие расходы (нет выручки)"
            )
            return df_9102

        df_9102_orphan_dist['оборот, тыс.ед.'] = (
            df_9102_orphan_dist['оборот, тыс.ед.'] *
            df_9102_orphan_dist['доля_контрагента']
        )
        if 'оборот, тыс.руб.' in df_9102_orphan_dist.columns:
            df_9102_orphan_dist['оборот, тыс.руб.'] = (
                df_9102_orphan_dist['оборот, тыс.руб.'] *
                df_9102_orphan_dist['доля_контрагента']
            )

        df_9102_orphan_dist = df_9102_orphan_dist.drop(columns=['доля_контрагента'])

        if contractor_to_connection is not None:
            df_9102_orphan_dist['вид_связи'] = (
                df_9102_orphan_dist['контрагент']
                .map(contractor_to_connection)
                .fillna('не_указано')
                .astype('string')
            )

        df_9102_result = pd.concat(
            [df_9102_attached, df_9102_orphan_dist], ignore_index=True
        )

        sum_before = df_9102_orphan['оборот, тыс.ед.'].sum()
        sum_after = df_9102_orphan_dist['оборот, тыс.ед.'].sum()

        if abs(sum_before - sum_after) > 0.01:
            logger.warning(
                "[!] Расхождение при распределении: было {:,.2f}, стало {:,.2f}",
                sum_before, sum_after,
            )

        logger.debug(
            "Распределено {} осиротевших строк на {} строк",
            len(df_9102_orphan), len(df_9102_orphan_dist),
        )

        return df_9102_result

    def _process_credit_lines(
        self,
        df: pd.DataFrame,
        reference_rbp_credit: pd.DataFrame,
        is_income: bool = True,
        company_name: str = ''
    ) -> pd.DataFrame:
        """Обрабатывает кредитные линии: подтягивает контрагентов из справочника КредитОбслуж."""
        CREDIT_TYPE = 'Кредитное обслуживание и расходы по открытию кредитных линий'

        mask_credit = (
            (df['вид_дохода_расхода'] == CREDIT_TYPE) &
            (df['Корр.счет'].astype(str).str.startswith('97', na=False))
        )

        if not mask_credit.any():
            return df

        logger.debug("Обработка кредитных линий (РБП)")

        subconto_col = 'Субконто Дт_1' if is_income else 'Субконто Кт_1'

        df['рбп_кредитные_линии'] = df[subconto_col].where(
            mask_credit, 'не_указано'
        ).astype('string')

        mapping_rbp = (
            reference_rbp_credit
            .drop_duplicates(subset='рбп_кредитные_линии')
            .set_index('рбп_кредитные_линии')['контрагент']
        )

        mapped_values = df.loc[mask_credit, 'рбп_кредитные_линии'].map(mapping_rbp)
        mask_found = mapped_values.notna()

        if mask_found.any():
            df.loc[mask_credit, 'контрагент'] = np.where(
                mask_found, mapped_values, df.loc[mask_credit, 'контрагент']
            )

        missing_mask = mask_credit.copy()
        missing_mask[mask_credit] = ~mask_found

        if missing_mask.any():
            missing_series = df.loc[missing_mask, 'рбп_кредитные_линии']
            missing_clean = missing_series.astype('string').fillna(StepConstants.UNSPECIFIED)
            missing_list = sorted(missing_clean.unique().tolist())

            group = (
                df.loc[missing_mask]
                .assign(рбп_кредитные_линии=missing_clean)
                .groupby('рбп_кредитные_линии', dropna=False)
            )
            if 'оборот, тыс.ед.' in df.columns:
                problem_data = group.agg(
                    количество_строк=('рбп_кредитные_линии', 'size'),
                    оборот_тыс_ед=('оборот, тыс.ед.', 'sum'),
                )
            else:
                problem_data = group.agg(
                    количество_строк=('рбп_кредитные_линии', 'size'),
                )
            problem_data = (
                problem_data
                .reset_index()
                .rename(columns={'рбп_кредитные_линии': 'отсутствующее_значение'})
                .sort_values('отсутствующее_значение')
            )
            problem_data.insert(0, 'компания', company_name)

            message = (
                f"В справочнике КредитОбслуж отсутствуют {len(missing_list)} РБП "
                f"кредитных линий для компании '{company_name}'"
            )

            if STRICT_CREDIT_CONTRACTOR_CHECK:
                raise MissingCreditContractorError(
                    message=message,
                    problem_data=problem_data,
                    reference_name="КредитОбслуж",
                    missing_rbps=missing_list,
                    company_name=company_name,
                )

            logger.warning(
                "[!] В справочнике КредитОбслуж отсутствуют {} РБП кредитных линий "
                "для компании '{}':",
                len(missing_list), company_name,
            )
            for item in missing_list:
                logger.warning("      - {}", item)
            logger.warning(
                "[!] Мягкий режим: РБП без контрагента в КредитОбслуж заменяются на '{}'",
                StepConstants.THIRD_PARTY,
            )
            df.loc[missing_mask, 'контрагент'] = StepConstants.THIRD_PARTY

            try:
                report_error = MissingCreditContractorError(
                    message=message,
                    problem_data=problem_data,
                    reference_name="КредитОбслуж",
                    missing_rbps=missing_list,
                    company_name=company_name,
                )
                self._save_reference_mismatch_report(report_error)
            except Exception as report_exc:
                logger.warning(
                    "[!] Не удалось сохранить отчёт по отсутствующим РБП: {}",
                    report_exc,
                )

        df['контрагент'] = df['контрагент'].astype('string')

        logger.debug(
            "Кредитные линии: {} контрагентов подтянуто",
            mask_found.sum(),
        )

        return df

    def _enrich_with_connection_info(
        self,
        df: pd.DataFrame,
        mapping_group: pd.Series,
        mapping_segment_ka: pd.Series,
        segment_company: str,
    ) -> pd.DataFrame:
        """Обогащает DataFrame: группа_ка, сегмент_ка, вид_связи."""
        logger.debug("Обогащение: группа_ка, сегмент_ка, вид_связи")

        df['сегмент'] = segment_company
        df['сегмент'] = df['сегмент'].astype('string')

        mapping_group_ext = {**mapping_group.to_dict(), 'не_указано': 'не_указано'}
        mapping_segment_ka_ext = {**mapping_segment_ka.to_dict(), 'не_указано': 'не_указано'}

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

        df['вид_связи'] = self._calculate_connection_type(df, segment_company)

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
