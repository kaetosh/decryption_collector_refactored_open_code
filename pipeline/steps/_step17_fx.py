"""
Mixin с курсовыми разницами для Шага 17 (Фаза 3.3).

Методы:
    _log_rate_anomalies: контроль подразумеваемого курса (поиск «ядовитых» строк)
    _prepare_fx_columns: вычисление _руб_баз и _fx
    _split_fx_difference_rows: выделение курсовых разниц в отдельные строки
"""
import pandas as pd
from loguru import logger

from pipeline.base import ProcessingContext
from utils.currency_utils import (
    get_earliest_rate,
    get_rate_deviation_limit,
    get_rate_for_date,
    get_rate_median,
    needs_conversion,
)


class Step17FXMixin:
    """Миксин курсовых разниц для Шага 17."""

    def _log_rate_anomalies(
        self,
        df_9101: pd.DataFrame,
        df_9102: pd.DataFrame,
        context: ProcessingContext,
        stage: str,
    ) -> None:
        """
        Контроль подразумеваемого курса строк 91.x (Фаза 3.3).

        Пишет WARNING в лог (app.log), конвейер не прерывает.
        Для рублёвых компаний (needs_conversion=False) контроль не выполняется.
        """
        if not needs_conversion(context):
            logger.debug(
                'Контроль курса ({}): пропущен — валюта компании RUB, '
                'перевод в рубли не выполняется, контроль подразумеваемого '
                'курса не требуется.',
                stage,
            )
            return

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
            logger.debug(
                'Контроль курса ({}, {}): {} строк(и) с подозрительным руб/ед '
                '(медиана листа курса {}; порог отклонения {:.0%}). Примеры:\n{}',
                stage, label, int(anomaly_mask.sum()),
                median_rate, deviation_limit,
                problem.head(10).to_string(index=False),
            )

    def _prepare_fx_columns(
        self,
        df_9101: pd.DataFrame,
        df_9102: pd.DataFrame,
        context: ProcessingContext,
    ) -> None:
        """
        Вычисляет служебные столбцы _руб_баз и _fx для проводок 91.01/91.02.

        _руб_баз — рублёвый эквивалент по «курсу признания» группы
        (курс на дату первой/min операции группы). _fx — хвост курсовой
        разницы = оборот_руб − _руб_баз. Мутация in-place.

        Для рублёвых компаний и строк с NaT-датой fx = 0.
        """
        if not needs_conversion(context):
            return

        if 'Дата' not in df_9101.columns and 'Дата' not in df_9102.columns:
            logger.debug('_prepare_fx_columns: столбец «Дата» отсутствует — пропуск')
            return

        for label, df_91 in (('91.01', df_9101), ('91.02', df_9102)):
            if 'Дата' not in df_91.columns:
                df_91[self.RUB_BASE_COL] = 0.0
                df_91[self.FX_COL] = 0.0
                continue

            dates = pd.to_datetime(df_91['Дата'], errors='coerce')
            valid_mask = dates.notna()

            group_keys = [c for c in self.GROUP_COLS if c in df_91.columns]

            base_date = dates.groupby(
                [df_91[c] for c in group_keys],
                sort=False,
                dropna=False,
            ).transform('min') if group_keys else dates

            base_date = pd.to_datetime(base_date, errors='coerce')

            unique_base_dates = base_date.dropna().unique()
            rate_cache: dict = {}
            for ts in sorted(unique_base_dates):
                try:
                    rate_cache[ts] = get_rate_for_date(context, ts)
                except ValueError:
                    rate_cache[ts] = get_earliest_rate(context)

            base_rate = base_date.map(rate_cache).fillna(0.0)

            amount_col = 'оборот, тыс.ед.'
            rub_col = 'оборот, тыс.руб.'
            amount = pd.to_numeric(df_91.get(amount_col, 0.0), errors='coerce').fillna(0.0)
            rub_amount = pd.to_numeric(df_91.get(rub_col, 0.0), errors='coerce').fillna(0.0)

            df_91[self.RUB_BASE_COL] = (amount * base_rate).round(6)
            df_91[self.FX_COL] = (rub_amount - df_91[self.RUB_BASE_COL]).round(6)

            df_91.loc[~valid_mask, self.FX_COL] = 0.0
            df_91.loc[~valid_mask, self.RUB_BASE_COL] = 0.0

            logger.debug(
                '_prepare_fx_columns ({}): {} строк, из них с fx≠0: {}',
                label, len(df_91),
                int((df_91[self.FX_COL].abs() > self.FX_ABS_FLOOR).sum()),
            )

    def _split_fx_difference_rows(
        self,
        df: pd.DataFrame,
        context: ProcessingContext,
    ) -> pd.DataFrame:
        """
        Выделяет курсовые разницы в отдельные строки ОПУ.

        Для групп, где |fx| > FX_ABS_FLOOR и (ед ≈ 0 ИЛИ подразумеваемый
        курс отличается от базового больше чем на deviation_limit),
        основная строка получает руб = _руб_баз, а хвост уходит в
        отдельную строку с доход_расход = FX_DIFFERENCE_LABEL и ед = 0.

        Суммарные ед/руб по ОПУ не меняются (инвариант).
        """
        amount_col = 'оборот, тыс.ед.'
        rub_col = 'оборот, тыс.руб.'

        if amount_col not in df.columns or rub_col not in df.columns:
            return df

        fx = df[self.FX_COL]
        rub_base = df[self.RUB_BASE_COL]
        amount = df[amount_col]

        try:
            deviation_limit = get_rate_deviation_limit(context)
        except (ValueError, KeyError):
            deviation_limit = 0.3

        base_implied = pd.Series(0.0, index=df.index, dtype='float64')
        implied = pd.Series(0.0, index=df.index, dtype='float64')
        nonzero_mask = amount.abs() > self.FX_ZERO_AMOUNT_EPSILON
        if nonzero_mask.any():
            base_implied[nonzero_mask] = rub_base[nonzero_mask] / amount[nonzero_mask]
            implied[nonzero_mask] = df.loc[nonzero_mask, rub_col] / amount[nonzero_mask]
        base_implied = base_implied.replace([float('inf'), float('-inf')], float('nan'))
        implied = implied.replace([float('inf'), float('-inf')], float('nan'))

        rate_ratio = pd.Series(0.0, index=df.index)
        valid_base = base_implied.abs() > 1e-12
        rate_ratio[valid_base] = implied[valid_base] / base_implied[valid_base]
        rate_deviation = (rate_ratio - 1.0).abs()

        split_mask = (
            (fx.abs() > self.FX_ABS_FLOOR)
            & (
                (amount.abs() <= self.FX_ZERO_AMOUNT_EPSILON)
                | (rate_deviation > deviation_limit)
            )
        )

        if not split_mask.any():
            logger.debug('_split_fx_difference_rows: разделение не требуется')
            return df

        df_main = df.copy()
        df_main.loc[split_mask, rub_col] = rub_base[split_mask]

        df_fx = df.loc[split_mask].copy()
        df_fx[amount_col] = 0.0
        df_fx[rub_col] = fx[split_mask]
        df_fx['доход_расход'] = self.FX_DIFFERENCE_LABEL

        fx_count = len(df_fx)
        fx_total = float(fx[split_mask].sum())

        logger.info(
            'Выделено {} строк «{}» на {:.4f} тыс.руб (порог {:.2f} тыс.руб, '
            'отклонение курса {:.0%})',
            fx_count, self.FX_DIFFERENCE_LABEL, fx_total,
            self.FX_ABS_FLOOR, deviation_limit,
        )

        return pd.concat([df_main, df_fx], ignore_index=True)
