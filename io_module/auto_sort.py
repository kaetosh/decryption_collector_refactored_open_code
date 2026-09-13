# -*- coding: utf-8 -*-
"""
Автоматическая раскладка выгрузок 1С: папка 00_inbox -> целевые подпапки.

Пользователь выгружает файлы из 1С одним движением в папку
_INPUT_DATA/00_inbox (config.settings.INBOX_DIR), не разбираясь, что куда.
Скрипт раскладывает файлы по целевым папкам согласно списку выгрузок
«Выгрузить_<компания>_<период>.xlsx» (листы «Обязательные выгрузки» и
«Спецотчеты», колонки «Имя файла для сохранения» -> «Куда класть»).

Классификация детерминированная — по имени файла (формат
Компания_регистр_счет_Период_.ext). Содержимое файлов не читается.
Исключение — Общая ОСВ на волне 1: она узнаётся по признаку «_общаяосв_»
в имени (согласовано с шаблоном поиска DataLoader.load_general_osv).

Две волны (вызываются из пауз pipeline/executors.py):
1. Волна 1, pause_for_osv_general_export (до Фазы 0):
   - из general_osv в архив убирается всё, что не похоже на общую ОСВ;
   - из inbox в general_osv переносятся файлы с «_общаяосв_» в имени
     (их может быть несколько — штатную ошибку TooManyFilesError
     отработает DataLoader);
   - остальные файлы inbox остаются ждать волну 2.
2. Волна 2, pause_for_1c_export (после шага 1a):
   - файлы, уже лежащие в нужной папке, остаются на месте;
   - файлы целевых папок, которых нет в списке выгрузок (хвосты прошлых
     сессий), архивируются; файлы из «чужой» целевой папки перекладываются
     по назначению;
   - файлы inbox раскладываются по «куда класть»;
   - «кривые» имена (лишние пробелы, регистр, без хвостового '_') при
     совпадении со списком приводятся к эталонному имени из списка —
     иначе последующий поиск по имени (find_register_file) не распарсит
     файл;
   - нераспознанные файлы inbox переносятся в архив
     _INPUT_DATA/_archive/<run_id>/unsorted.

Нераспознанные файлы не останавливают конвейер: нехватку обязательных
выгрузок штатно сообщит шаг 1b. Каждая волна пишет отчёт sort_report.xlsx
в папку запуска и сводку в лог (INFO). Архив — вместо безвозвратного
удаления: файлы прошлых сессий и перезаписанные версии сохраняются в
_INPUT_DATA/_archive/<run_id>/<имя_папки>/.

Отключается флагом AUTO_SORT_ENABLED = False (config.settings).
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from loguru import logger

from config.settings import (
    ARCHIVE_DIR,
    BASE_DIR,
    INBOX_DIR,
    OSV_GENERAL_DIR,
)

# Признак имени файла общей ОСВ — согласован с шаблоном поиска
# DataLoader.load_general_osv: "*_общаяосв_*.xlsx"
GENERAL_OSV_TOKEN = "общаяосв"

# Служебные временные файлы Excel (открытая в Excel книга) — не трогаем:
# они исчезнут после закрытия файла пользователем
EXCEL_TEMP_PREFIX = "~$"

# Листы списка выгрузок «Выгрузить_<компания>_<период>.xlsx»
EXPECTED_FILE_SHEETS = ("Обязательные выгрузки", "Спецотчеты")

# Колонки списка выгрузок, по которым строится карта раскладки
EXPECTED_FILENAME_COL = "Имя файла для сохранения"
EXPECTED_TARGET_COL = "Куда класть"


# ═════════════════════════════════════════════════════════════════════════
# Нормализация имён и карта ожидаемых файлов
# ═════════════════════════════════════════════════════════════════════════

def normalize_name(filename: str) -> str:
    """
    Нормализует имя файла для сопоставления со списком выгрузок.

    - нижний регистр;
    - пробелы/табуляции внутри имени схлопываются в '_' (защита от
      «СМПК_осв_60_6мес2026 .xlsx»);
    - пустые части убираются, поэтому имя без завершающего '_' совпадает
      со списком, где '_' есть (и наоборот);
    - расширение сохраняется и приводится к нижнему регистру
      (xlsx vs txt — разные типы выгрузок).
    """
    path = Path(filename)
    parts = [p for p in re.split(r"[\s_]+", path.stem) if p]
    return "_".join(parts).lower() + path.suffix.lower()


def resolve_target_dir(target: Any) -> Path:
    """
    «_INPUT_DATA/accounts_osv/» -> абсолютный путь (BASE_DIR / ...).

    Поддерживает обратные слэши и абсолютные пути (используется смоуком).
    """
    cleaned = str(target).strip().replace("\\", "/").strip("/")
    path = Path(cleaned)
    if path.is_absolute():
        return path
    return BASE_DIR / path


def build_expected_map(company: str, period: str, run_dir: Path) -> dict[str, tuple[Path, str]]:
    """
    Читает «Выгрузить_<компания>_<период>.xlsx» (оба листа) и возвращает
    соответствие {нормализованное имя файла -> (целевая папка, эталонное имя)}.

    Эталонное имя — строка из колонки «Имя файла для сохранения»: файлы
    с «кривым» именем (лишние пробелы, регистр, без хвостового '_')
    при раскладке переименовываются к эталону, иначе последующий поиск
    по имени (find_register_file) их не распарсит.

    Чтение устойчиво к формату шага 1a: колонки ищутся без учёта
    регистра ('Куда класть' vs 'куда класть' из справочника «Выгрузки»),
    строка заголовка таблицы — в первых 10 строках листа (лист
    «Спецотчеты» отформатирован с двумя служебными строками сверху).

    Файл списка формирует шаг 1a в папке текущего запуска — к моменту
    волны 2 он существует всегда. Если файла нет (или листы без нужных
    колонок) — возвращается пустая карта, сортировка пропускается
    с WARNING, файлы inbox остаются на месте.
    """
    expected_path = Path(run_dir) / f"Выгрузить_{company}_{period}.xlsx"
    if not expected_path.is_file():
        logger.warning(
            "[SORT] Список выгрузок не найден: {}. Файлы из 00_inbox "
            "оставлены без изменений — разложите их по папкам вручную.",
            expected_path.name,
        )
        return {}

    expected_map: dict[str, tuple[Path, str]] = {}
    for sheet_name in EXPECTED_FILE_SHEETS:
        try:
            raw = pd.read_excel(
                expected_path, sheet_name=sheet_name, engine="openpyxl", header=None,
            )
        except ValueError:
            # Листа нет в файле — не критично (обязательный лист всегда есть)
            logger.debug("[SORT] В списке выгрузок нет листа '{}'", sheet_name)
            continue
        if raw.empty:
            continue

        # Поиск строки заголовка: обе колонки в одной строке, без учёта регистра
        fname_lc, target_lc = EXPECTED_FILENAME_COL.lower(), EXPECTED_TARGET_COL.lower()
        header_idx = None
        for i in range(min(10, len(raw))):
            vals = {str(v).strip().lower() for v in raw.iloc[i].tolist()}
            if fname_lc in vals and target_lc in vals:
                header_idx = i
                break
        if header_idx is None:
            logger.warning(
                "[SORT] В листе '{}' нет колонок '{}'/'{}' — лист пропущен",
                sheet_name, EXPECTED_FILENAME_COL, EXPECTED_TARGET_COL,
            )
            continue

        df = raw.iloc[header_idx + 1:].copy()
        df.columns = [str(c).strip().lower() for c in raw.iloc[header_idx]]
        for _, row in df.iterrows():
            filename, target = row.get(fname_lc), row.get(target_lc)
            if pd.isna(filename) or pd.isna(target):
                continue
            filename = str(filename).strip()
            if not filename or filename.startswith(EXCEL_TEMP_PREFIX):
                continue
            key = normalize_name(filename)
            target_dir = resolve_target_dir(target)
            if key in expected_map and expected_map[key][0] != target_dir:
                logger.warning(
                    "[SORT] В списке выгрузок имя '{}' встречается с разными "
                    "целевыми папками: '{}' и '{}'. Используется последняя.",
                    filename, expected_map[key][0], target_dir,
                )
            expected_map[key] = (target_dir, filename)

    logger.info("[SORT] Файлов в списке выгрузок: {}", len(expected_map))
    return expected_map


# ═════════════════════════════════════════════════════════════════════════
# Файловые операции: архив, сравнение, перенос с разрешением конфликтов
# ═════════════════════════════════════════════════════════════════════════

def _file_hash(path: Path, chunk_size: int = 1 << 20) -> str:
    """SHA-256 содержимого файла (поблочно, без загрузки в память)."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def _same_file(a: Path, b: Path) -> bool:
    """Идентичны ли файлы: сначала сравнение размера, затем хеша."""
    if a.stat().st_size != b.stat().st_size:
        return False
    return _file_hash(a) == _file_hash(b)


def _is_excel_temp(path: Path) -> bool:
    """Служебный временный файл Excel (книга открыта пользователем)."""
    return path.name.startswith(EXCEL_TEMP_PREFIX)


def _default_archive_dir(subfolder: str = "") -> Path:
    """_INPUT_DATA/_archive/<run_id>[/<subfolder>] — архив текущего запуска."""
    # Локальный импорт: защищает от циклов при инициализации пакета io_module
    from io_module.output_manager import get_run_id

    try:
        run_id = get_run_id()
    except RuntimeError:
        run_id = "manual"
    archive_dir = ARCHIVE_DIR / run_id
    if subfolder:
        archive_dir = archive_dir / subfolder
    return archive_dir


def _archive_file(path: Path, archive_dir: Path, reason: str = "") -> Path:
    """
    Переносит файл в архив (вместо удаления) с защитой от коллизии имён:
    при повторе имени добавляется суффикс _1, _2, ...
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / path.name
    if dest.exists():
        stem, suffix = path.stem, path.suffix
        counter = 1
        while dest.exists():
            dest = archive_dir / f"{stem}_{counter}{suffix}"
            counter += 1
    shutil.move(str(path), str(dest))
    if reason:
        logger.debug("[SORT] Архивирован '{}': {} -> {}", path.name, reason, dest)
    return dest


def _move_to_dir(
    src: Path,
    target_dir: Path,
    archive_dir: Path,
    actions: list,
    action: str,
    canonical_name: str = "",
) -> Path:
    """
    Перенос файла в целевую папку с разрешением конфликтов имён:

    - целевой файл отсутствует — простой перенос (при canonical_name —
      ещё и переименование к эталонному имени из списка выгрузок);
    - целевой файл идентичен по содержимому — дубликат из inbox удаляется;
    - целевой файл отличается — старая версия уходит в архив, новая
      занимает место (WARNING в лог).

    Каждое действие дописывается в actions (для отчёта).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    dest_name = canonical_name if canonical_name else src.name
    dest = target_dir / dest_name
    if dest.exists():
        if _same_file(src, dest):
            src.unlink()
            actions.append({
                "файл": src.name,
                "действие": "дубликат удалён (идентичен уже лежащему)",
                "откуда": str(src),
                "куда": str(dest),
            })
            logger.info(
                "[SORT] Дубликат '{}': идентичный файл уже в {} — копия удалена",
                src.name, target_dir.name,
            )
            return dest
        old = _archive_file(dest, archive_dir, "заменён новой версией")
        shutil.move(str(src), str(dest))
        actions.append({
            "файл": src.name,
            "действие": "перезаписан (старый в архиве)",
            "откуда": str(src),
            "куда": str(dest),
        })
        logger.warning(
            "[SORT] '{}': файл уже был в {} с другим содержимым — заменён, "
            "старая версия в архиве ({})",
            src.name, target_dir.name, old,
        )
        return dest
    shutil.move(str(src), str(dest))
    if canonical_name and src.name != canonical_name:
        logger.info(
            "[SORT] '{}': {} и приведён к эталонному имени '{}' ({})",
            src.name, action, canonical_name, target_dir.name,
        )
    else:
        logger.debug("[SORT] '{}': {} -> {}", src.name, action, dest)
    actions.append({
        "файл": src.name,
        "действие": action + (
            f" -> переименован в '{canonical_name}'" if canonical_name and src.name != canonical_name else ""
        ),
        "откуда": str(src),
        "куда": str(dest),
    })
    return dest


def _rename_to_canonical(
    f: Path,
    canonical_name: str,
    archive_dir: Path,
    actions: list,
) -> None:
    """
    Переименование файла на месте к эталонному имени из списка выгрузок
    (файл уже лежит в правильной папке). Конфликт имён разрешается как
    в _move_to_dir: идентичный по содержимому файл под эталонным именем
    означает дубликат (кривая копия удаляется), отличающийся —
    заменяется со старой версией в архиве.
    """
    dest = f.parent / canonical_name
    if dest.exists():
        if _same_file(f, dest):
            f.unlink()
            actions.append({
                "файл": f.name,
                "действие": "дубликат удалён (идентичен уже лежащему)",
                "откуда": str(f),
                "куда": str(dest),
            })
            logger.info(
                "[SORT] Дубликат '{}': идентичный файл '{}' уже в {} — копия удалена",
                f.name, canonical_name, f.parent.name,
            )
            return
        old = _archive_file(dest, archive_dir, "заменён приведённым к эталону файлом")
        f.rename(dest)
        actions.append({
            "файл": f.name,
            "действие": f"имя приведено к эталонному '{canonical_name}' (старый в архиве)",
            "откуда": str(old),
            "куда": str(dest),
        })
        logger.warning(
            "[SORT] '{}': имя приведено к эталону '{}', прежний файл под эталонным "
            "именем отличался содержимым — в архиве ({})",
            f.name, canonical_name, old,
        )
        return
    f.rename(dest)
    actions.append({
        "файл": f.name,
        "действие": f"имя приведено к эталонному '{canonical_name}'",
        "откуда": str(f),
        "куда": str(dest),
    })
    logger.info("[SORT] '{}': имя приведено к эталонному '{}'", f.name, canonical_name)


# ═════════════════════════════════════════════════════════════════════════
# Волна 1: общая ОСВ (до Фазы 0)
# ═════════════════════════════════════════════════════════════════════════

def prepare_general_osv_from_inbox(
    inbox_dir: Path = INBOX_DIR,
    target_dir: Path = OSV_GENERAL_DIR,
    archive_dir: Optional[Path] = None,
) -> list[Path]:
    """
    Волна 1 автосортировки — вызывается в pause_for_osv_general_export
    сразу после нажатия Enter (работает и в неинтерактивном режиме).

    1. Ревизия general_osv: файлы без признака «_общаяосв_» в имени
       (хвосты прошлых сессий) переносятся в архив.
    2. Из inbox в general_osv переносятся все *.xlsx с «_общаяосв_»
       в имени; если их несколько, конфликт разрешит штатная
       TooManyFilesError от DataLoader.load_general_osv.

    Возвращает список путей перенесённых общих ОСВ.
    """
    actions: list = []
    inbox_dir = Path(inbox_dir)
    target_dir = Path(target_dir)
    inbox_dir.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = Path(archive_dir) if archive_dir else _default_archive_dir("general_osv")

    # 1. Ревизия general_osv
    for f in sorted(target_dir.iterdir()):
        if not f.is_file() or _is_excel_temp(f):
            continue
        if GENERAL_OSV_TOKEN not in f.stem.lower():
            archived = _archive_file(f, archive_dir, "левый файл в general_osv")
            actions.append({
                "файл": f.name,
                "действие": "архивирован (левый файл в general_osv)",
                "откуда": str(f),
                "куда": str(archived),
            })

    # 2. Перенос общих ОСВ из inbox
    for f in sorted(inbox_dir.iterdir()):
        if not f.is_file() or _is_excel_temp(f):
            continue
        if GENERAL_OSV_TOKEN in f.stem.lower() and f.suffix.lower() == ".xlsx":
            _move_to_dir(f, target_dir, archive_dir, actions, "перенесён в general_osv")

    if actions:
        _log_and_save(actions, label="Волна 1 (общая ОСВ)", sheet_name="Волна 1")
    else:
        logger.debug("[SORT] Волна 1: переносов не было")
    return [Path(a["куда"]) for a in actions if a["действие"] == "перенесён в general_osv"]


# ═════════════════════════════════════════════════════════════════════════
# Волна 2: раскладка по списку выгрузок (после шага 1a)
# ═════════════════════════════════════════════════════════════════════════

def sort_all(
    expected_map: dict[str, tuple[Path, str]],
    inbox_dir: Path = INBOX_DIR,
    archive_dir: Optional[Path] = None,
    general_osv_dir: Path = OSV_GENERAL_DIR,
) -> list[dict]:
    """
    Волна 2 автосортировки: ревизия целевых папок + раскладка inbox
    по ожидаемому списку. Возвращает список действий (для отчёта).

    1. Ревизия целевых папок (из значений expected_map):
       - файл не из списка (хвост прошлой сессии) — в архив;
       - файл из списка, но в «чужой» папке — перекладывается по назначению;
       - файл уже в своей папке — остаётся на месте.
    2. Раскладка inbox: совпал со списком — перенос по назначению
       (с разрешением конфликтов); не распознан — в архив/unsorted.
    """
    actions: list = []
    archive_root = Path(archive_dir) if archive_dir else _default_archive_dir()
    inbox_dir = Path(inbox_dir)

    # 1. Ревизия целевых папок
    for target_dir in sorted({d for d, _ in expected_map.values()}):
        target_dir = Path(target_dir)
        if not target_dir.is_dir():
            continue
        for f in sorted(target_dir.iterdir()):
            if not f.is_file() or _is_excel_temp(f):
                continue
            mapped = expected_map.get(normalize_name(f.name))
            if mapped is None:
                archived = _archive_file(
                    f, archive_root / target_dir.name, "левый файл прошлой сессии",
                )
                actions.append({
                    "файл": f.name,
                    "действие": "архивирован (левый файл в целевой папке)",
                    "откуда": str(f),
                    "куда": str(archived),
                })
                continue
            mapped_dir, canonical = mapped
            if mapped_dir != target_dir:
                _move_to_dir(
                    f, mapped_dir, archive_root / mapped_dir.name, actions,
                    "перемещён из неверной папки", canonical_name=canonical,
                )
            elif f.name != canonical:
                # Правильная папка, но «кривое» имя — приводим к эталону,
                # иначе последующий поиск по имени (find_register_file) не сработает
                _rename_to_canonical(f, canonical, archive_root / target_dir.name, actions)
            else:
                actions.append({
                    "файл": f.name,
                    "действие": "уже на месте",
                    "откуда": str(f),
                    "куда": str(target_dir),
                })

    # 2. Раскладка inbox
    inbox_dir.mkdir(parents=True, exist_ok=True)
    for f in sorted(inbox_dir.iterdir()):
        if not f.is_file() or _is_excel_temp(f):
            continue
        # Сам файл списка выгрузок, скопированный пользователем в inbox,
        # не входит в карту — оставляем в inbox (это инструкция бухгалтера)
        if f.stem.startswith("Выгрузить_"):
            actions.append({
                "файл": f.name,
                "действие": "файл списка выгрузок — оставлен в 00_inbox",
                "откуда": str(f),
                "куда": str(f),
            })
            logger.info(
                "[SORT] Файл списка выгрузок '{}' оставлен в 00_inbox",
                f.name,
            )
            continue
        mapped = expected_map.get(normalize_name(f.name))
        # Общая ОСВ в списке выгрузок отсутствует — обрабатываем её так же,
        # как волна 1: перенос в general_osv с разрешением конфликтов
        # (идентичная копия — дубликат удаляется, отличающаяся — заменяется)
        if mapped is None and GENERAL_OSV_TOKEN in f.stem.lower() and f.suffix.lower() == ".xlsx":
            _move_to_dir(
                f, Path(general_osv_dir), archive_root / Path(general_osv_dir).name, actions,
                "перемещён из 00_inbox (общая ОСВ)",
            )
            continue
        if mapped is None:
            archived = _archive_file(
                f, archive_root / "unsorted", "не распознан по списку выгрузок",
            )
            actions.append({
                "файл": f.name,
                "действие": "не распознан -> архив/unsorted",
                "откуда": str(f),
                "куда": str(archived),
            })
            logger.warning(
                "[SORT] Файл '{}' из 00_inbox не найден в списке выгрузок — "
                "перенесён в архив/unsorted (проверьте имя файла)",
                f.name,
            )
        else:
            mapped_dir, canonical = mapped
            _move_to_dir(
                f, mapped_dir, archive_root / mapped_dir.name, actions,
                "перемещён из 00_inbox", canonical_name=canonical,
            )
    return actions


def sort_inbox_by_expected_list(
    context: Any,
    inbox_dir: Path = INBOX_DIR,
    archive_dir: Optional[Path] = None,
) -> None:
    """
    Волна 2 под ключ — вызывается в pause_for_1c_export после Enter.

    Строит карту ожидаемых файлов из списка выгрузок текущего запуска
    (Выгрузить_<компания>_<период>.xlsx) и раскладывает inbox.
    При пустой карте (список не найден) — выход с WARNING,
    файлы остаются на месте.
    """
    from io_module.output_manager import get_run_dir

    expected_map = build_expected_map(context.company, context.period, get_run_dir())
    if not expected_map:
        return
    actions = sort_all(expected_map, inbox_dir=inbox_dir, archive_dir=archive_dir)
    if actions:
        _log_and_save(
            actions,
            label=f"Волна 2 ({context.company}, {context.period})",
            sheet_name="Волна 2",
        )
    else:
        logger.debug("[SORT] Волна 2: раскладывать нечего")


# ═════════════════════════════════════════════════════════════════════════
# Отчёт и сводка
# ═════════════════════════════════════════════════════════════════════════

def _log_and_save(actions: list, label: str, sheet_name: str) -> None:
    """INFO-сводка по действиям + лист в sort_report.xlsx папки запуска."""
    result_df = pd.DataFrame(actions, columns=["файл", "действие", "откуда", "куда"])
    counts = result_df["действие"].value_counts()
    summary = ", ".join(f"{name}: {count}" for name, count in counts.items())
    logger.info("[SORT] {} — {}", label, summary)
    report_path = _save_sort_report(result_df, sheet_name=sheet_name)
    logger.info("[SORT] Отчёт о раскладке: {} (лист '{}')", report_path, sheet_name)


def _save_sort_report(result_df: pd.DataFrame, sheet_name: str = "Сортировка") -> Path:
    """Сохраняет список действий в sort_report.xlsx папки запуска.

    Каждая волна пишет свой лист (Волна 1 / Волна 2): повторный вызов
    добавляет лист и не перезаписывает отчёт предыдущей волны.
    """
    from io_module.output_manager import get_run_dir

    try:
        report_dir = get_run_dir()
    except RuntimeError:
        report_dir = ARCHIVE_DIR
    report_path = report_dir / "sort_report.xlsx"
    try:
        if report_path.exists():
            with pd.ExcelWriter(
                report_path, engine="openpyxl", mode="a", if_sheet_exists="replace"
            ) as writer:
                result_df.to_excel(writer, sheet_name=sheet_name, index=False)
        else:
            with pd.ExcelWriter(report_path, engine="openpyxl") as writer:
                result_df.to_excel(writer, sheet_name=sheet_name, index=False)
    except PermissionError:
        logger.warning(
            "[SORT] Не удалось записать '{}': файл открыт в Excel. "
            "Действия остались в логе.",
            report_path.name,
        )
    return report_path
