# -*- coding: utf-8 -*-
"""
Created on Mon Jun 22 09:30:02 2026

@author: a.karabedyan
"""

# utils/file_utils.py
import re
import pandas as pd
from io import StringIO
from pathlib import Path
from typing import List, Optional, Tuple
from loguru import logger
from config.settings import to_relative

# ---------------------------------------------------------------------------
# Определение кодировки текстовых (TXT) выгрузок 1С
# ---------------------------------------------------------------------------

# Порядок важен: utf-8-sig проверяется первым — cp1251 почти всегда "декодирует"
# UTF-8-байты без ошибки (кракозябрами), поэтому строгую пробу UTF-8 надо делать раньше.
TXT_ENCODING_CANDIDATES = ('utf-8-sig', 'cp1251', 'cp866')

# Объём начала файла (байт), используемый для пробы декодирования
ENCODING_SAMPLE_SIZE = 5 * 1024 * 1024  # 5 МБ


def detect_txt_encoding(
    file_path: Path,
    sample_size: int = ENCODING_SAMPLE_SIZE,
) -> Tuple[str, str]:
    """Определяет кодировку текстового файла по его началу.

    Порядок определения:
      1. BOM (utf-8-sig / utf-16) — однозначная идентификация;
      2. строгая проба декодирования сэмпла по TXT_ENCODING_CANDIDATES;
      3. если ни одна не подошла — cp1251 с заменой неопределимых байтов
         (errors='replace') и предупреждением в лог.

    Args:
        file_path: Путь к текстовому файлу.
        sample_size: Сколько байт от начала файла использовать для пробы.

    Returns:
        Tuple[str, str]: пара (кодировка, режим ошибок) — пригодна для
        open(..., encoding=..., errors=...) и
        pd.read_csv(..., encoding=..., encoding_errors=...).
    """
    file_path = Path(file_path)

    with open(file_path, 'rb') as f:
        sample = f.read(sample_size)
    head = sample[:4]

    # 1. BOM (utf-8-sig заодно убирает BOM при последующем текстовом чтении)
    if head.startswith(b'\xef\xbb\xbf'):
        logger.debug("Кодировка {}: utf-8-sig (обнаружен BOM)", file_path.name)
        return 'utf-8-sig', 'strict'
    if head.startswith((b'\xff\xfe', b'\xfe\xff')):
        logger.debug("Кодировка {}: utf-16 (обнаружен BOM)", file_path.name)
        return 'utf-16', 'strict'

    # 2. Строгая проба по кандидатам
    for encoding in TXT_ENCODING_CANDIDATES:
        try:
            sample.decode(encoding)
        except UnicodeDecodeError:
            continue
        logger.debug("Кодировка {}: {} (строгая проба сэмпла)", file_path.name, encoding)
        return encoding, 'strict'

    # 3. Последний рубеж — читаем с заменой неопределимых байтов
    logger.warning(
        "Кодировка {} не определена однозначно — читаем как cp1251 "
        "с заменой неопределимых байтов (errors='replace')",
        file_path.name,
    )
    return 'cp1251', 'replace'


def _cp(*codes: int) -> str:
    """Собирает кириллическое имя колонки из кодовых точек.

    Имена колонок 1С заданы кодами — так имена читаются в любой кодировке
    исходника файла и не зависят от настроек редактора.
    """
    return ''.join(chr(c) for c in codes)


# Колонки, по которым проверяется, что строка «сошлась» после склейки.
# Это форматированные поля: мусор в них означает неверную позицию склейки.
_COL_DATE = _cp(0x414,0x430,0x442,0x430)        # Дата
_COL_DT = _cp(0x414,0x442)                        # Дт
_COL_KT = _cp(0x41a,0x442)                        # Кт
_COL_SUM = _cp(0x421,0x443,0x43c,0x43c,0x430)  # Сумма

_NUMERIC_SPACES = re.compile(r'[\s\u00a0\u2009\u202f]')
# Полное совпадение, а не префикс (регресс 12.10.2026, файл СТБ 26_2025).
# С match() значение «01.03.2025 23:59:59 Поступление товаров…» считалось датой,
# и неверная позиция склейки проходила структурную проверку наравне с верной.
# В колонке «Дата» должно быть только дата, в «Дт»/«Кт» — только счёт:
# всё лишнее означает, что поля съехали.
_ACCOUNT_RE = re.compile(r'\d{2}(?:\.\d+)*')
_DATE_RE = re.compile(r'\d{2}\.\d{2}\.\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?')
# Часы/минуты в выгрузках 1С встречаются с точкой с запятой и без ведущих нулей
_DATE_TIME_RE = re.compile(r'^\s*\d{1,2}[.:]\d{2}(?:[.:]\d{2})?\s*$')


def _looks_like_amount(value: str) -> bool:
    """Пустое значение допустимо (не у всех проводок есть сумма)."""
    cleaned = _NUMERIC_SPACES.sub('', value or '').replace(',', '.')
    if not cleaned:
        return True
    try:
        float(cleaned)
    except ValueError:
        return False
    return True


def _looks_like_account(value: str) -> bool:
    v = (value or '').strip()
    return (not v) or bool(_ACCOUNT_RE.fullmatch(v))


def _looks_like_date(value: str) -> bool:
    """Дата в формате 1С: ДД.ММ.ГГГГ, возможно с временем.

    Время допускается и через точку с запятой, и с двоеточием, и без ведущих
    нулей — в выгрузках встречаются все три. Всё остальное после даты
    (текст проводки, наименование) означает, что поле съехало: это и есть
    признак неверной позиции склейки.
    """
    v = (value or '').strip()
    if not v:
        return True
    if _DATE_TIME_RE.match(v):
        return bool(_DATE_RE.fullmatch(v.replace(';', ':')))
    return bool(_DATE_RE.fullmatch(v))


def _row_validation_spec(hdr_parts: List[str]) -> dict:
    """Индексы колонок, по которым проверяется структура строки."""
    spec = {'amount': [], 'account': [], 'date': []}
    for i, name in enumerate(hdr_parts):
        key = name.strip()
        if key == _COL_SUM:
            spec['amount'].append(i)
        elif key in (_COL_DT, _COL_KT):
            spec['account'].append(i)
        elif key == _COL_DATE:
            spec['date'].append(i)
    return spec


def _row_is_valid(fields: List[str], spec: dict) -> bool:
    """Строка структурно корректна, если форматированные поля не поехали."""
    return _formatted_filled(fields, spec) == _expected_filled(spec)


def _expected_filled(spec: dict) -> int:
    """Сколько форматированных колонок вообще есть в строке этого файла."""
    return len(spec['amount']) + len(spec['account']) + len(spec['date'])


def _formatted_filled(fields: List[str], spec: dict) -> int:
    """Сколько форматированных полей заполнено НЕПУСТЫМ корректным значением.

    Это главный разделитель между верной и неверной позицией склейки на
    реальных выгрузках. Пустое форматированное поле допустимо само по себе
    (не у каждой проводки есть сумма), но если соседние кандидаты держат
    счёт/сумму/дату, а этот — нет, значит склейка сдвинула строку: значения
    уехали в соседние колонки.

    Регресс 12.10.2026 (файл СТБ 26_2025, строка 126579): разрыв пришёлся на
    «Содержание», склеивать надо fragments 3..11. Кандидаты 7/9/10 оставляли
    Дт/Кт/Сумму пустыми, но проходили проверку, потому что пустое значение
    считается допустимым. Подсчёт непустых форматированных полей отличает
    их от верного кандидата без обращения к профилю колонок (который для
    текстовых колонок этого файла бесполезен — см. _collect_column_profile).
    """
    filled = 0
    checks = (
        ('amount', _looks_like_amount),
        ('account', _looks_like_account),
        ('date', _looks_like_date),
    )
    for key, checker in checks:
        for i in spec[key]:
            if i >= len(fields):
                continue
            value = fields[i]
            if value and value.strip() and checker(value):
                filled += 1
    return filled


def _collect_column_profile(file_path, header_row, expected_tabs, encoding, errors,
                            max_values: int = 200):
    """Собирает профиль значений колонок по корректным строкам файла.

    Нужен как якорь при выборе позиции склейки: счёт/сумма/дата одинаково
    «выглядят» правильно при нескольких позициях, а вот значения соседних
    колонок известны из остальных строк файла. Колонка, у которой значений
    больше `max_values`, помечается как вольная (все значения допустимы).
    """
    wildcard = set()
    profile: dict = {}
    with open(file_path, 'r', encoding=encoding, errors=errors) as f:
        for i, line in enumerate(f):
            if i <= header_row:
                continue
            if line.count('\t') != expected_tabs:
                continue
            fields = line.rstrip('\n\r').split('\t')
            for col, value in enumerate(fields):
                if col in wildcard:
                    continue
                values = profile.setdefault(col, set())
                values.add(value)
                if len(values) > max_values:
                    wildcard.add(col)
                    profile.pop(col, None)
    return profile, wildcard


def _score_candidate(fields: List[str], merged_idx: int, profile: dict,
                     wildcard: set) -> int:
    """Сколько неизменённых полей строки согласуется с профилем файла.

    Поле, в которое шла склейка, не учитывается: его содержимое заведомо
    новое и в профиле отсутствует.

    Очко даётся только за СВИДЕТЕЛЬСТВО, а не за отсутствие противоречия:

    - значение найдено в профиле этой колонки → согласуется, +1;
    - значение пустое → не противоречит (в колонке бывают пустые), +1;
    - колонка вольная (значений больше порога, значения колонки не различаются)
      → сказать нечего: НЕ даём очко. Раньше вольная колонка давала +1 за любое
      содержимое, из-за чего осколок разрыва, случайно попавший в неё,
      перевешивал настоящее совпадение по соседней колонке — склейка уезжала
      на позицию левее верной;
    - значения в профиле нет → противоречит, 0.

    Именно поэтому при высококардинальных текстовых колонках (наименования
    объектов ОС) кандидаты сравниваются по остальным полям строки, а при
    полном равенстве счёта _repair_ragged_row отдаёт строку как есть.
    """
    score = 0
    for col, value in enumerate(fields):
        if col == merged_idx:
            continue
        if col in wildcard:
            continue
        if not value or value in profile.get(col, ()):
            score += 1
    return score


def normalize_ragged_tab_rows(file_path, header_row,
                                encoding='cp1251', errors='strict'):
    """Normalize ragged ('рваные') rows in 1C TXT posting exports.

    A literal TAB inside a free-text column makes pandas C-engine fail with
    ParserError. This re-joins the excess fragments back into the column where
    the break actually happened, so all lines match the header field count.

    Позиция разрыва не предполагается, а определяется по данным: перебираются
    все допустимые позиции склейки и выбирается та, после которой строка
    остаётся структурно корректной (счёт/сумма/дата на своих местах). Раньше
    позиция жёстко бралась по колонке «Содержание», из-за чего разрыв в любой
    другой текстовой колонке (например, «Субконто Кт» — наименование объекта ОС)
    сдвигал все поля влево: «Сумма» получала текст вместо числа, строка уходила
    в NaN и молча терялась при разборе.

    Параметра «колонка склейки» здесь намеренно нет: он был мёртвым остатком
    того самого хардкода и оставлял ловушку — вернуть его можно было бы,
    тихо вернув регрессию ШПФ.

    ВАЖНО: функция ВСЕГДА возвращает буфер, пригодный для чтения. Строка,
    которую не удалось привести к корректной структуре, не пишется в буфер
    вообще: оставленная как есть, она роняет разбор ВСЕГО файла
    (ParserError на строке 126579 из 126579 — теряется весь отчёт по
    проводкам из-за одной строки). Потеря одной проводки вместо всего
    файла — меньшее зло, и оно заметно: число исключённых строк пишется
    в лог вместе с их номерами.

    Returns:
        Tuple[StringIO, int, int, List[dict]]: буфер, число исправленных строк,
        число строк, исключённых из разбора, и список этих строк с подсказками
        для поиска (см. _describe_problem_row). Четвёртый элемент нужен
        бухгалтерии: номера строки в блокноте нет, а «ищите вручную» без
        указания что искать — не помощь, а отписка.
    """
    file_path = Path(file_path)

    expected_tabs = None
    hdr_parts = None
    with open(file_path, 'r', encoding=encoding, errors=errors) as f:
        for i, line in enumerate(f):
            if i == header_row:
                expected_tabs = line.count('\t')
                hdr_parts = line.rstrip('\n\r').split('\t')
                break

    buff = StringIO()
    repaired = 0
    unresolved = 0

    if expected_tabs is None:
        with open(file_path, 'r', encoding=encoding, errors=errors) as f:
            buff.write(f.read())
        buff.seek(0)
        return buff, repaired, unresolved, []

    spec = _row_validation_spec(hdr_parts)
    # Есть что проверять? Иначе (шапка не распознана) — склеиваем в последнюю
    # колонку, как это делалось раньше.
    has_spec = bool(spec['amount'] or spec['account'] or spec['date'])

    # Профиль значений колонок по корректным строкам — якорь для выбора
    # позиции склейки. Без него при нескольких формально подходящих
    # позициях выбиралась первая, а не верная.
    profile = wildcard = None
    if has_spec:
        profile, wildcard = _collect_column_profile(
            file_path, header_row, expected_tabs, encoding, errors
        )

    approximate = 0
    unresolved_lines: List[str] = []
    problem_rows: List[dict] = []

    with open(file_path,'r',encoding=encoding,errors=errors) as f:
        for line_no, raws in enumerate(f, start=1):
            if raws.count('\t') > expected_tabs:
                fixed, ok, approx = _repair_ragged_row(
                    raws, expected_tabs, spec, has_spec, profile, wildcard
                )
                if not ok:
                    # Строку НЕ пишем: иначе она роняет разбор всего файла
                    # (ParserError). Потеряется одна проводка вместо всего
                    # отчёта по проводкам — и это видно по счётчику в логе.
                    unresolved += 1
                    if len(unresolved_lines) < 20:
                        unresolved_lines.append(str(line_no))
                    problem_rows.append(_describe_problem_row(raws, line_no))
                    continue
                repaired += 1
                if approx:
                    approximate += 1
                    if len(unresolved_lines) < 20:
                        unresolved_lines.append(str(line_no))
                buff.write(fixed if fixed != raws else raws)
            else:
                buff.write(raws)

    buff.seek(0)
    _report_ragged(file_path.name, repaired, approximate, unresolved,
                   unresolved_lines)
    return buff, repaired, unresolved, problem_rows


# Длина подсказки для поиска. Это ПРЕФИКС самого длинного поля строки, то есть
# подстрока исходной строки — её копируют в «Найти» блокнота, и поиск
# срабатывает. 120 символов достаточно, чтобы попасть в конкретный документ, и
# достаточно мало, чтобы поместиться в ячейку Excel и на экран.
_SEARCH_HINT_MAX_LEN = 120


def _describe_problem_row(raw: str, line_no: int) -> dict:
    """Описание исключённой строки для диагностического Excel.

    Главное здесь — колонка «что искать»: бухгалтер открывает TXT в блокноте,
    где номеров строк нет, поэтому «исправьте строку 28094» бесполезно.
    Отдаём готовую строку для поиска Ctrl+F.

    Как строится подсказка: берём САМОЕ ДЛИННОЕ поле разбитой по табу строки.
    В отчёте по проводках это всегда свободнотекстовое поле (наименование
    документа, содержание, наименование объекта), а не счёт и не сумма. Оно не
    содержит табов, поэтому вставляется в «Найти» как есть.

    Инвариант, который обязан выполняться всегда: подсказка — ПОДСТРОКА исходной
    строки, иначе Ctrl+F её не найдёт. Поэтому берём самое длинное поле даже
    тогда, когда оно короткое, и НЕ подставляем вместо него строку с
    показанными табами: табы, превращённые в «→», подстрокой файла уже не
    являются (регрессия на синтетической строке из одних коротких полей).
    Разбивка строки может быть неверной — отсюда «подсказка», а не «точное
    содержимое»: даже если позиция не та, префикс длинного поля почти
    наверняка найдётся, а найденная строка уже покажет, где правда.
    """
    fields = [f.strip() for f in raw.rstrip('\n\r').split('\t')]
    longest = max(fields, key=len, default='')
    if not longest:
        # Совсем пустая строка — искать нечем, отдаём исходник как есть.
        hint = raw.rstrip('\n\r')
    else:
        hint = longest[:_SEARCH_HINT_MAX_LEN]
    return {
        'строка': line_no,
        'причина': (
            'лишняя табуляция, структуру строки привести не удалось — '
            'строка исключена из разбора'
        ),
        'что_искать': hint,
        'строка_файла': _show_tabs(raw.rstrip('\n\r')),
    }


def _show_tabs(text: str) -> str:
    """Показывает невидимые табы — в Excel они иначе выглядят как пробелы."""
    return text.replace('\t', ' → ')


def _report_ragged(file_name: str, repaired: int, approximate: int,
                   unresolved: int, sample_lines: List[str]) -> None:
    """Сообщает о восстановлении рваных строк — факт и что делать.

    Сообщения разбиты на «что произошло» и «что делать»: консольный формат
    логгера режет длинные строки, а сводка предупреждений на титульном
    листе — до 200 символов (см. logging_handling/logger_config.py).

    Префикс «[!]» обязателен для WARNING: он же служит ключом группировки
    в сводке предупреждений.
    """
    if repaired > 0:
        logger.warning(
            "Файл «{}»: исправлено строк с лишней табуляцией: {}. "
            "Данные не потеряны.",
            file_name, repaired,
        )
    if approximate > 0:
        lines = ', '.join(sample_lines[:10])
        logger.warning(
            "[!] Файл «{}»: {} строк восстановлено ПРИБЛИЗИТЕЛЬНО — позиция "
            "разрыва не определяется однозначно, проверьте их вручную. "
            "Строки: {}.",
            file_name, approximate, lines,
        )
        logger.warning(
            "[!] Выгрузка содержит табуляцию внутри текста. Устраните её в 1С "
            "(например, убрав табуляцию из наименований) — тогда строки будут "
            "приведены точно.",
        )
    if unresolved > 0:
        lines = ', '.join(sample_lines[:10])
        logger.error(
            "Файл «{}»: {} строк с лишней табуляцией не удалось привести к "
            "корректной структуре — они исключены из разбора. Остальные строки "
            "файла прочитаны. Строки: {}.",
            file_name, unresolved, lines,
        )


def _repair_ragged_row(raw, expected_tabs, spec, has_spec, profile=None,
                       wildcard=None):
    """Склеивает лишние поля строки, подбирая позицию разрыва по данным.

    Позиция выбирается в три уровня, по убыванию надёжности:

    1. **Сколько форматированных полей заполнено** (`_formatted_filled`).
       Это главный критерий: у верной склейки счёт/сумма/дата стоят на своих
       местах и заполнены, у сдвинутой — часть из них пуста или поехала.
       Работает всегда, даже когда профиль колонок бесполезен (текстовые
       колонки выгрузки 1С содержат тысячи разных значений).
    2. **Совпадение с профилем файла** (`_score_candidate`) — различает
       кандидаты, у которых форматированные поля одинаково заполнены.
    3. Если уровни 1 и 2 не различают позиции — берём ПЕРВУЮ (склейка как
       можно раньше) и помечаем строку как восстановленную приблизительно.
       Почему именно так, а не «в последнюю колонку»: у поздней склейки
       форматированные поля съезжают вправо и оказываются пустыми. Регресс
       12.10.2026 (СТБ 26_2025, строка 126579): при склейке в последнюю
       колонку «Сумма» становилась пустой — терялась сумма проводки, то
       есть ровно то, из-за чего файл и чинится. Ранняя склейка при
       неоднозначности портит только текстовые реквизиты (наименование
       документа, содержание), а сумма/счёт/дата остаются на месте.
       Отказ от склейки здесь означал бы ParserError и потерю ВСЕГО файла.

    Returns:
        Tuple[str, bool, bool]: строка, признак успеха, признак того, что
        позиция выбрана приблизительно.
    """
    parts = raw.rstrip('\n\r').split('\t')
    ending = raw[len(raw.rstrip('\n\r')):]
    excess = len(parts) - (expected_tabs + 1)
    if excess <= 0:
        return raw, True, False

    if has_spec:
        candidates: List[tuple] = []
        for idx in range(len(parts) - excess):
            merged = _merge_at(parts, excess, idx)
            if not _row_is_valid(merged, spec):
                continue
            candidates.append((
                _formatted_filled(merged, spec),
                _score_candidate(merged, idx, profile, wildcard),
                idx,
                merged,
            ))
        if not candidates:
            return raw, False, False

        # Сначала максимум заполненных форматированных полей, потом — профиль,
        # потом — более ранняя позиция.
        candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
        best_filled, best_score, best_idx, best_merged = candidates[0]

        tied = [
            item for item in candidates[1:]
            if (item[0], item[1]) == (best_filled, best_score)
        ]
        if tied:
            # Если все равнозначные кандидаты дают ОДИНАКОВЫЙ результат,
            # неоднозначности нет: отличаются лишь пустые фрагменты между
            # ними, а на выходе строки совпадают (реальный кейс СТБ 91.02,
            # строка 17101: склейка в 2 и в 3 дают одну и ту же строку).
            # Предупреждать о «приблизительном» восстановлении здесь не
            # о чем — строки собираются точно.
            if all(item[3] == best_merged for item in tied):
                return '\t'.join(best_merged) + ending, True, False
            # Данные различают позиции, но не дают преимущества ни одному
            # кандидату: склеиваем как можно раньше (так форматированные
            # поля остаются на своих местах) и ПРЕДУПРЕЖДАЕМ.
            return '\t'.join(best_merged) + ending, True, True

        return '\t'.join(best_merged) + ending, True, False

    # Шапка не распознана — склеиваем в последнюю колонку (прежнее поведение).
    return '\t'.join(_merge_at(parts, excess, max(len(parts) - excess - 1, 0))) + ending, True, True


def _merge_at(parts, excess, idx):
    """Склеивает `excess` лишних полей начиная с позиции `idx`."""
    content = ' '.join(p for p in parts[idx: idx + excess + 1] if p)
    return parts[:idx] + [content] + parts[idx + excess + 1:]


def format_filename_vectorized(df: pd.DataFrame) -> list:
    """Векторизованное формирование имен файлов (работает в разы быстрее apply)"""
    return (
        df['Сокращенное Наименование компании'].astype(str) + '_' +
        df['регистр'].astype(str) + '_' +
        df['счет'].astype(str) + '_' +
        df['Период Отчетности'].astype(str) + '_.xlsx'
    ).tolist()

def find_missing_files(filenames: List[str], folder_path: str = 'INPUT_DATA') -> List[str]:
    """Возвращает список файлов из filenames, которых нет в указанной папке."""
    folder = Path(folder_path)
    if not folder.exists() or not folder.is_dir():
        logger.error(
            "Папка '{}' не найдена или не является директорией",
            to_relative(folder),
        )
        return filenames.copy()
        
    existing_files = {f.name for f in folder.iterdir() if f.is_file()}
    return list(set(filenames) - existing_files)

def find_register_file(
    folder_path: Path,
    type_register: Optional[str] = None,
    account_number: Optional[str] = None,
    company_name: Optional[str] = None,
    period: Optional[str] = None
) -> Optional[Path]:
    """
    Находит единственный файл по критериям в указанной папке.
    Формат имени: CompanyName_typeRegister_accountNumber_period_.xlsx
    """
    folder = Path(folder_path)
    if not folder.exists() or not folder.is_dir():
        raise FileNotFoundError(f"Папка '{folder_path}' не найдена")
        
    files = [f for f in folder.glob('*.xlsx') if not f.name.startswith('~$')]
    
    if not files:
        return None
        
    for file_path in files:
        name = file_path.stem.rstrip('_')
        parts = name.split('_')
        if len(parts) < 4:
            continue
            
        file_company = parts[0]
        file_type = parts[1]
        file_account = parts[2]
        file_period = parts[3]
        
        if company_name and file_company != company_name:
            continue
        if type_register and file_type != type_register:
            continue
        if account_number and file_account != account_number:
            continue
        if period and file_period != period:
            continue
            
        return file_path
        
    criteria = []
    if company_name: criteria.append(f"компания='{company_name}'")
    if type_register: criteria.append(f"тип='{type_register}'")
    if account_number: criteria.append(f"счет='{account_number}'")
    if period: criteria.append(f"период='{period}'")
    
    logger.warning("Файл не найден по критериям: {}", ', '.join(criteria))
    return None
