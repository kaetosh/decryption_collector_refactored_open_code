# -*- coding: utf-8 -*-
"""
Смоук сводки предупреждений за прогон (logging_handling/logger_config.py).

Коллектор подключается sink-ом loguru в setup_logger, поэтому здесь
проверяем именно то, как loguru передаёт сообщение в sink:
  1. Текст собирается с подставленными аргументами ({...} разворачивается).
  2. Повторы одного предупреждения считаются, а не дублируются.
  3. Многострочное предупреждение схлопывается в одну строку.
  4. Технические предупреждения библиотек (file_only) в сводку не попадают.
  5. Сортировка — по убыванию частоты, при равенстве по алфавиту.
  6. Формат сводки: «текст (×N)» и ограничение limit.
  7. reset_collected_warnings() очищает счётчик (старт нового запуска).

Запуск: python _smoke_warnings_summary.py

Лог-файл подменяется на временный — настоящий app.log не затирается.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import logging_handling.logger_config as logger_config

PASSED = 0


def check(condition: bool, message: str) -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [OK] {message}")
    else:
        print(f"  [FAIL] {message}")


def main() -> None:
    from loguru import logger

    tmp_log = Path(tempfile.mkdtemp(prefix='smoke_log_')) / 'app.log'
    logger_config.LOG_FILE = tmp_log

    # Уровень ERROR: консольный sink молчит на предупреждениях — вывод
    # смоука остаётся читаемым, а проверяется именно файловый/коллектор.
    logger_config.setup_logger(console_level='ERROR')
    check(tmp_log.exists(), f'лог-файл создан: {tmp_log.name}')

    check(logger_config.get_collected_warnings() == [], 'после setup_logger сводка пуста')

    logger.warning('Неизвестный контрагент: {} строк заменено на «3 лица»', 12)
    logger.warning('Неизвестный контрагент: {} строк заменено на «3 лица»', 12)
    logger.warning('Ставка НДС восстановлена по аналогам группы: 7')
    logger.warning('Файл без обязательных колонок: {}\n  колонки: {}', 'выгрузка.xlsx', 'a, b, c')

    # Техническое предупреждение библиотеки — в сводку не попадает
    logger.bind(**{logger_config._FILE_ONLY_KEY: True}).warning(
        '[FutureWarning] построение DataFrame с устаревшими правилами: {}',
        'pandas 2.3',
    )

    items = logger_config.get_collected_warnings()
    texts = [text for text, _ in items]
    counts = dict(items)

    check(len(items) == 3, f'собрано уникальных предупреждений: {len(items)} (ожидалось 3)')
    check(
        any('12' in t and '3 лица' in t for t in texts),
        'аргументы подставлены, шаблон не утёк в сводку',
    )
    check(
        any(counts[t] == 2 for t in texts),
        f'повтор посчитан, а не задублирован: {counts}',
    )
    check(
        all('\n' not in t and '  ' not in t for t in texts),
        'многострочное предупреждение схлопнуто в одну строку',
    )
    check(
        not any('FutureWarning' in t for t in texts),
        'технические предупреждения библиотек (file_only) исключены',
    )
    check(
        [n for _, n, in [(t, c) for t, c in items]] == sorted([c for _, c in items], reverse=True),
        'сортировка по убыванию частоты',
    )
    check(
        items[0][1] == 2 and '3 лица' in items[0][0],
        f'самое частое предупреждение первым: {items[0][0][:40]}',
    )

    summary = logger_config.format_warnings_summary()
    check(len(summary) == 3, 'формат сводки: по строке на уникальное предупреждение')
    check(
        any(s.endswith('(×2)') for s in summary),
        f'повторы указаны в скобках: {summary}',
    )
    check(
        not any(s.endswith('(×1)') for s in summary),
        'единичные предупреждения без лишних скобок',
    )

    limited = logger_config.format_warnings_summary(limit=1)
    check(len(limited) == 1 and limited[0].endswith('(×2)'), f'limit=1 оставляет самое частое: {limited}')

    logger.warning('Пропущенные ставки НДС по группам: {} ({})', 7, ', '.join(f'Группа-{i}' * 8 for i in range(1, 9)))
    long_line = next(
        line for line in logger_config.format_warnings_summary()
        if line.startswith('Пропущенные ставки НДС')
    )
    check(
        len(long_line) <= 205 and long_line.endswith('...'),
        f'длинное предупреждение обрезано до 200 симв.: {len(long_line)} симв.',
    )
    check(
        len(logger_config.get_collected_warnings()) == 4
        and any(
            text.startswith('Пропущенные ставки НДС') and len(text) > 200
            for text, _ in logger_config.get_collected_warnings()
        ),
        'полный текст в get_collected_warnings() остаётся без обрезки',
    )
    logger_config.reset_collected_warnings()

    logger.warning('Строка один')
    logger.warning('Строка два')
    logger.warning('Строка три')
    total = sum(count for _, count in logger_config.get_collected_warnings())
    check(total == 3, f'всего срабатываний за прогон: {total}')

    logger_config.reset_collected_warnings()
    check(logger_config.get_collected_warnings() == [], 'reset очистил сводку')

    logger_config.setup_logger(console_level='ERROR')
    logger.warning('после перезапуска логирования')
    check(
        len(logger_config.get_collected_warnings()) == 1,
        'повторный setup_logger не дублирует накопленное',
    )

    logger_config.reset_collected_warnings()

    # Файловый sink держит app.log открытым (enqueue=True) — сначала
    # снимаем sink-и, иначе Windows не даст удалить временный файл
    logger.remove()
    try:
        tmp_log.unlink(missing_ok=True)
        tmp_log.parent.rmdir()
    except OSError as exc:
        print(f'  [WARN] временный лог не удалён: {exc}')

    print(f'\nSMOKE_OK ({PASSED} проверок)')


if __name__ == '__main__':
    main()
