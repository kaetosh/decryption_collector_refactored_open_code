# -*- coding: utf-8 -*-
"""
Смоук-тест очистки архива _INPUT_DATA/_archive (io_module/auto_sort.py).

Реальные папки _INPUT_DATA не затрагиваются — работа во временном каталоге
(параметр archive_root).

Проверяет:
1. Папок меньше keep_last — ничего не удаляется;
2. Папок больше keep_last — удаляются старейшие ПО ВРЕМЕНИ ИЗМЕНЕНИЯ (mtime),
   а не по имени;
3. папка 'manual' (запуск без run_id) не вытесняет настоящие прогоны:
   по имени она «новее» всех, по mtime оценивается честно;
4. файлы в корне архива (sort_report.xlsx от запуска без configure_run)
   не трогаются;
5. keep_last вне диапазона 1..5 — фолбэк на 5 с предупреждением;
6. несуществующий корень — тихий выход, ничего не бросается;
7. папка текущего запуска (get_run_id) не удаляется.

Запуск: python -u _smoke_archive_cleanup.py
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from io_module.auto_sort import cleanup_old_archive
from io_module.output_manager import configure_run, get_run_id, get_run_dir

failures: list = []
PASSED = 0


def check(condition: bool, message: str) -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"[OK] {message}")
    else:
        failures.append(message)
        print(f"[FAIL] {message}")


def make_run_dir(root: Path, name: str, age_days: float = 0.0) -> Path:
    """Папка запуска в архиве с заданным возрастом (mtime)."""
    run_dir = root / name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "файл.xlsx").write_bytes(b"x" * 1024)
    stamp = time.time() - age_days * 86400
    os.utime(run_dir, (stamp, stamp))
    return run_dir


def names_in(root: Path) -> set:
    if not root.exists():
        return set()
    return {d.name for d in root.iterdir() if d.is_dir()}


def test_nothing_to_delete() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="_smoke_arch_"))
    try:
        root = tmp / "_archive"
        for i in range(3):
            make_run_dir(root, f"2026092{i}_120000", age_days=3 - i)

        removed = cleanup_old_archive(keep_last=5, archive_root=root)

        check(removed == [], "папок меньше keep_last — удалять нечего")
        check(
            names_in(root) == {"20260920_120000", "20260921_120000", "20260922_120000"},
            "все папки на месте",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_oldest_by_mtime_removed() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="_smoke_arch_"))
    try:
        root = tmp / "_archive"
        for i in range(6):
            make_run_dir(root, f"2026092{i}_120000", age_days=6 - i)

        removed = cleanup_old_archive(keep_last=3, archive_root=root)

        check(
            set(removed) == {"20260920_120000", "20260921_120000", "20260922_120000"},
            f"удалены три старейшие по mtime: {sorted(removed)}",
        )
        check(
            names_in(root)
            == {"20260923_120000", "20260924_120000", "20260925_120000"},
            "остались три новейшие папки",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_manual_not_privileged_by_name() -> None:
    """'manual' по имени новее цифровых папок, по mtime оценивается честно."""
    tmp = Path(tempfile.mkdtemp(prefix="_smoke_arch_"))
    try:
        root = tmp / "_archive"
        for i in range(4):
            make_run_dir(root, f"2026092{i}_120000", age_days=1)
        make_run_dir(root, "manual", age_days=10)  # архив запуска без run_id

        removed = cleanup_old_archive(keep_last=4, archive_root=root)

        check(removed == ["manual"], f"удалён старый manual: {removed}")
        check("manual" not in names_in(root), "папка manual убрана")
        check(len(names_in(root)) == 4, "четыре прогона остались")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_root_files_kept() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="_smoke_arch_"))
    try:
        root = tmp / "_archive"
        for i in range(4):
            make_run_dir(root, f"2026092{i}_120000", age_days=4 - i)
        report = root / "sort_report.xlsx"
        report.write_bytes(b"report")

        cleanup_old_archive(keep_last=1, archive_root=root)

        check(report.exists(), "файл в корне архива не тронут")
        check(len(names_in(root)) == 1, "удалены только лишние папки")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_keep_last_out_of_range() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="_smoke_arch_"))
    try:
        root = tmp / "_archive"
        for i in range(7):
            make_run_dir(root, f"2026092{i}_120000", age_days=7 - i)

        removed = cleanup_old_archive(keep_last=99, archive_root=root)

        check(len(removed) == 2, f"keep_last=99 заменён на 5 — удалено {len(removed)}")
        check(len(names_in(root)) == 5, "осталось 5 папок")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_missing_root_is_silent() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="_smoke_arch_"))
    try:
        removed = cleanup_old_archive(keep_last=5, archive_root=tmp / "_archive_нет")
        check(removed == [], "несуществующий архив — тихий выход")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_current_run_dir_kept() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="_smoke_arch_"))
    try:
        run_id = configure_run("smoke_arch_current")
        try:
            root = tmp / "_archive"
            # текущая папка запуска — самая старая, но её удалять нельзя
            make_run_dir(root, run_id, age_days=99)
            for i in range(3):
                make_run_dir(root, f"2026092{i}_120000", age_days=1)

            cleanup_old_archive(keep_last=1, archive_root=root)

            check(run_id in names_in(root), "папка текущего запуска не удалена")
            check(
                (root / run_id / "файл.xlsx").exists(),
                "файлы текущего запуска целы",
            )
        finally:
            shutil.rmtree(get_run_dir(), ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    test_nothing_to_delete()
    test_oldest_by_mtime_removed()
    test_manual_not_privileged_by_name()
    test_root_files_kept()
    test_keep_last_out_of_range()
    test_missing_root_is_silent()
    test_current_run_dir_kept()

    print("=" * 60)
    total = PASSED + len(failures)
    label = "очистка архива _INPUT_DATA/_archive"
    if failures:
        print(f"SMOKE_FAIL ({len(failures)}/{total}) — {label}")
        for message in failures:
            print(f"  [FAIL] {message}")
        sys.exit(1)
    print(f"SMOKE_OK ({PASSED}/{total}) — {label}")
    sys.exit(0)


if __name__ == "__main__":
    main()
