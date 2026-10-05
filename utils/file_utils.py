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

    Модуль намеренно остаётся ASCII-безопасным, поэтому имена колонок 1С
    задаются кодами, а не литералами.
    """
    return ''.join(chr(c) for c in codes)


def _merge_col_codepoints():
    return _cp(0x421,0x43e,0x434,0x435,0x440,0x436,0x430,0x43d,0x438,0x435)


# Колонки, по которым проверяется, что строка «сошлась» после склейки.
# Это форматированные поля: мусор в них означает неверную позицию склейки.
_COL_DATE = _cp(0x414,0x430,0x442,0x430)        # Дата
_COL_DT = _cp(0x414,0x442)                        # Дт
_COL_KT = _cp(0x41a,0x442)                        # Кт
_COL_SUM = _cp(0x421,0x443,0x43c,0x43c,0x430)  # Сумма

_NUMERIC_SPACES = re.compile(r'[\s\u00a0\u2009\u202f]')
_ACCOUNT_RE = re.compile(r'^\d{2}(?:\.\d+)*$')
_DATE_RE = re.compile(r'^\d{2}\.\d{2}\.\d{4}')


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
    return (not v) or bool(_ACCOUNT_RE.match(v))


def _looks_like_date(value: str) -> bool:
    v = (value or '').strip()
    return (not v) or bool(_DATE_RE.match(v))


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
    checks = (
        ('amount', _looks_like_amount),
        ('account', _looks_like_account),
        ('date', _looks_like_date),
    )
    for key, checker in checks:
        for i in spec[key]:
            if i >= len(fields) or not checker(fields[i]):
                return False
    return True


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
    """Сколько неизменённых полей строки совпадает с профилем файла.

    Поле, в которое шла склейка, не учитывается: его содержимое заведомо
    новое и в профиле отсутствует.
    """
    score = 0
    for col, value in enumerate(fields):
        if col == merged_idx:
            continue
        if col in wildcard:
            score += 1
        elif not value or value in profile.get(col, ()):
            score += 1
    return score


def normalize_ragged_tab_rows(file_path, header_row, merge_column=None,
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

    Returns:
        Tuple[StringIO, int, int]: буфер, число исправленных строк,
        число строк, которые не удалось привести к корректной структуре.
    """
    if merge_column is None:
        merge_column = _merge_col_codepoints()
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
        return buff, repaired, unresolved

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

    with open(file_path,'r',encoding=encoding,errors=errors) as f:
        for raws in f:
            if raws.count('\t') > expected_tabs:




                fixed, ok = _repair_ragged_row(raws, expected_tabs, spec, has_spec,
                                            profile, wildcard)
                if ok:
                    repaired += 1
                else:
                    unresolved += 1
                buff.write(fixed if fixed != raws else raws)



            else:
                buff.write(raws)


    buff.seek(0)
    if repaired > 0:
        logger.warning(
            "Файл «{}»: строк с лишним символом табуляции исправлено: {}. "
            "Данные не потеряны.",
            file_path.name, repaired,
        )
    if unresolved > 0:
        logger.error(
            "Файл «{}»: {} строк с лишней табуляцией не удалось привести "
            "к корректной структуре — часть данных может быть потеряна. "
            "Проверьте выгрузку из 1С.",
            file_path.name, unresolved,
        )
    return buff, repaired, unresolved


def _repair_ragged_row(raw, expected_tabs, spec, has_spec, profile=None,
                       wildcard=None):
    """Склеивает лишние поля строки, подбирая позицию разрыва по данным.

    Позиция выбирается в два шага: сначала отсеиваются варианты, после которых
    строка структурно некорректна (счёт/сумма/дата поехали); среди оставшихся
    выигрывает тот, где больше прочих полей совпадает с профилем файла — иначе
    при нескольких формально подходящих позициях выбиралась первая, а не верная.

    Returns:
        Tuple[str, bool]: исправленная строка и признак успеха.
    """
    parts = raw.rstrip('\n\r').split('\t')
    ending = raw[len(raw.rstrip('\n\r')):]
    excess = len(parts) - (expected_tabs + 1)
    if excess <= 0:
        return raw, True

    if has_spec:
        best_idx = None
        best_score = -1
        for idx in range(len(parts) - excess):
            merged = _merge_at(parts, excess, idx)
            if not _row_is_valid(merged, spec):
                continue
            score = (
                _score_candidate(merged, idx, profile, wildcard)
                if profile is not None else 0
            )
            if score > best_score:
                best_idx, best_score = idx, score
        if best_idx is None:
            return raw, False
        return '\t'.join(_merge_at(parts, excess, best_idx)) + ending, True

    # Шапка не распознана — склеиваем в последнюю колонку (прежнее поведение).
    return '\t'.join(_merge_at(parts, excess, max(len(parts) - excess - 1, 0))) + ending, True


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
