# -*- coding: utf-8 -*-
"""
Смоук: восстановление «рваных» строк TXT-выгрузок ОПУ (utils/file_utils.py).

Регрессия (компания ШПФ, 8мес2026): normalize_ragged_tab_rows склеивала лишние
табуляции на позиции колонки «Содержание» без проверки, где разрыв на самом
деле. В выгрузке ШПФ разрыв оказался в «Субконто Кт» (наименование объекта ОС
с инвентарным номером), поэтому все поля сдвигались влево: «Дт» получал текст,
«Сумма» — слово «Руководство», строка уходила в NaN и молча терялась при разборе.
На счёте 26 терялось 704 571,85 руб. (704,57 тыс.) — ровно расхождение реконциляции.

Теперь позиция разрыва определяется по данным: перебираются кандидаты и
выбирается тот, после которого счёт/сумма/дата остаются на своих местах.

Смоук обязан падать при провале: check() увеличивает счётчик FAILED, финальная
строка — SMOKE_OK (PASSED/total) либо SMOKE_FAIL со списком провалов и код
возврата 1.

Запуск: python -u _smoke_ragged_tabs.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from utils.file_utils import normalize_ragged_tab_rows

# Иначе на обычной консоли Windows (cp1251) символ «№» роняет прогон
# UnicodeEncodeError — без финальной строки SMOKE_FAIL.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace')

PASSED = 0
FAILED = 0


def check(name, condition, detail=''):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f'[OK]   {name}')
    else:
        FAILED += 1
        print(f'[FAIL] {name} {detail}')


TAB = chr(9)
NL = chr(10)

# Шапка реальной выгрузки ШПФ (УПП-формат): 12 полей, 11 табуляций
HEADER_LINE = TAB.join(['', 'Дата', 'Документ', 'Содержание', 'Дт', '', 'Кт', '',
                        'Сумма', 'Субконто Дт', 'Субконто Кт', 'Номер журнала'])

# Нормальные строки: 12 полей. Их несколько — как в реальном файле, где
# профиль значений колонок и берётся для выбора позиции склейки.
GOOD_ROW = TAB.join(['', '31.01.2026 23:59:59', 'Амортизация ОС 00002', 'Амортизация',
                     '26.01', '', '02.01', '', '4 367,36', 'Руководство',
                     'Здание склада инв № 280', 'ОС'])
GOOD_ROW_2 = TAB.join(['', '28.02.2026 12:00:00', 'Поступление 00044', 'Товары',
                       '60.01', '', '41.01', '', '12 500,00', 'Основной склад',
                       'Партия № 77', 'ТД'])
GOOD_ROW_3 = TAB.join(['', '15.03.2026 09:30:00', 'Оплата 00051', 'Услуги связи',
                       '26.01', '', '60.02', '', '999,90', 'Администрация',
                       'Договор № 12', 'ОС'])
# Тот же документ, что и в RAGGED_CONTENT: в реальных выгрузках документы
# встречаются многократно, и именно повтор позволяет профилю колонок
# однозначно указать позицию разрыва в «Содержании».
GOOD_ROW_4 = TAB.join(['', '20.03.2026 10:00:00', 'Поступление 00003', 'Оплата',
                       '60.01', '', '51', '', '800,00', 'Поставщик',
                       'Договор № 1', ''])
GOOD_ROWS = [GOOD_ROW, GOOD_ROW_2, GOOD_ROW_3, GOOD_ROW_4]

# РЕГРЕССИЯ ШПФ: лишний таб внутри «Субконто Кт» (наименование объекта ОС).
# Прежняя склейка по «Содержанию» давала: Дт='Амортизация 26.01', Сумма='Руководство'.
RAGGED_SUBKONTO = TAB.join(['', '31.01.2026 23:59:59', 'Амортизация ОС 00002',
                            'Амортизация', '26.01', '', '02.01', '',
                            '4 367,36', 'Руководство',
                            'Здание склада инв', '№ 280', 'ОС'])

# Старый кейс СТБ: разрыв в «Содержании» — тоже должен чиниться
RAGGED_CONTENT = TAB.join(['', '31.01.2026 23:59:59', 'Поступление 00003',
                           'Оплата', 'по договору', '60.01', '', '51', '',
                           '1 000,00', 'Поставщик', 'Договор № 1', ''])

# Два лишних таба подряд
RAGGED_TWICE = TAB.join(['', '31.01.2026 23:59:59', 'Амортизация ОС 00002',
                         'Амортизация', '26.01', '', '02.01', '',
                         '4 367,36', 'Руководство',
                         'Здание', 'склада', 'инв', '№ 280', 'ОС'])
def build_file(lines, tmpdir, name='test_отчпровод_26_.txt'):
    path = Path(tmpdir) / name
    path.write_text(NL.join(lines) + NL, encoding='utf-8')
    return path


def run(lines, tmpdir, name='test_отчпровод_26_.txt'):
    """Прогоняет нормализацию и возвращает (текст, repaired, unresolved)."""
    path = build_file(lines, tmpdir, name)
    buff, repaired, unresolved = normalize_ragged_tab_rows(
        path, header_row=0, encoding='utf-8'
    )
    return buff.getvalue(), repaired, unresolved


def field_of(line, idx):
    parts = line.rstrip(NL).split(TAB)
    return parts[idx] if idx < len(parts) else None


def count_fields(line):
    return len(line.rstrip(NL).split(TAB))


def to_num(value):
    return float(value.replace('\xa0', '').replace(' ', '').replace(',', '.'))


def main():
    tmpdir = tempfile.mkdtemp(prefix='smoke_ragged_')

    # --- 1. Разрыв в «Субконто Кт» (регрессия ШПФ) ---
    text, repaired, unresolved = run([HEADER_LINE] + GOOD_ROWS + [RAGGED_SUBKONTO], tmpdir)
    lines = text.split(NL)
    # Индексы полей: 3=Содержание, 4=Дт, 6=Кт, 8=Сумма, 10=Субконто Кт, 11=Номер
    check('ШПФ: строка восстановлена', repaired == 1, f'repaired={repaired}')
    check('ШПФ: нерешённых строк нет', unresolved == 0, f'unresolved={unresolved}')
    check('ШПФ: число полей = 12', count_fields(lines[-2]) == 12,
          f'got={count_fields(lines[-2])}')
    check('ШПФ: Содержание цело', field_of(lines[-2], 3) == 'Амортизация',
          f'got={field_of(lines[-2], 3)!r}')
    check('ШПФ: Дт не поехал', field_of(lines[-2], 4) == '26.01',
          f'got={field_of(lines[-2], 4)!r}')
    check('ШПФ: Кт не поехал', field_of(lines[-2], 6) == '02.01',
          f'got={field_of(lines[-2], 6)!r}')
    check('ШПФ: Сумма — число, а не текст', field_of(lines[-2], 8) == '4 367,36',
          f'got={field_of(lines[-2], 8)!r}')
    check('ШПФ: субконто склеено в один фрагмент',
          field_of(lines[-2], 10) == 'Здание склада инв № 280',
          f'got={field_of(lines[-2], 10)!r}')
    check('ШПФ: Номер журнала на месте', field_of(lines[-2], 11) == 'ОС',
          f'got={field_of(lines[-2], 11)!r}')

    # --- 2. Разрыв в «Содержании» (старый кейс СТБ) ---
    text, repaired, unresolved = run([HEADER_LINE] + GOOD_ROWS + [RAGGED_CONTENT], tmpdir)
    lines = text.split(NL)
    check('СТБ: строка восстановлена', repaired == 1, f'repaired={repaired}')
    check('СТБ: нерешённых строк нет', unresolved == 0, f'unresolved={unresolved}')
    check('СТБ: Содержание склеено', field_of(lines[-2], 3) == 'Оплата по договору',
          f'got={field_of(lines[-2], 3)!r}')
    check('СТБ: Дт не поехал', field_of(lines[-2], 4) == '60.01',
          f'got={field_of(lines[-2], 4)!r}')
    check('СТБ: Сумма — число', field_of(lines[-2], 8) == '1 000,00',
          f'got={field_of(lines[-2], 8)!r}')

    # --- 3. Два лишних таба подряд ---
    text, repaired, unresolved = run([HEADER_LINE] + GOOD_ROWS + [RAGGED_TWICE], tmpdir)
    lines = text.split(NL)
    check('Два разрыва: строка восстановлена', repaired == 1, f'repaired={repaired}')
    check('Два разрыва: нерешённых нет', unresolved == 0, f'unresolved={unresolved}')
    check('Два разрыва: число полей = 12', count_fields(lines[-2]) == 12,
          f'got={count_fields(lines[-2])}')
    check('Два разрыва: Сумма — число', field_of(lines[-2], 8) == '4 367,36',
          f'got={field_of(lines[-2], 8)!r}')
    check('Два разрыва: субконто склеено',
          field_of(lines[-2], 10) == 'Здание склада инв № 280',
          f'got={field_of(lines[-2], 10)!r}')
# --- 4. Чистый файл не трогаем ---
    text, repaired, unresolved = run([HEADER_LINE, GOOD_ROW, GOOD_ROW], tmpdir)
    check('Чистый файл: ничего не исправлено', repaired == 0, f'repaired={repaired}')
    check('Чистый файл: строки без изменений',
          text.split(NL)[1] == GOOD_ROW and text.split(NL)[2] == GOOD_ROW)

    # --- 5. Суммы сходятся до копейки (главный критерий регрессии) ---
    # Моделируем шаг 1в: суммируем Сумму по Дт=26 в сыром файле и после разбора.
    many_ragged = [HEADER_LINE] + GOOD_ROWS + [RAGGED_SUBKONTO] * 5
    raw_sum = sum(
        to_num(field_of(line, 8)) for line in many_ragged
        if field_of(line, 4) == '26.01'
    )
    text, repaired, unresolved = run(many_ragged, tmpdir, 'many_отчпровод_26_.txt')
    check('Массовый случай: все строки восстановлены',
          repaired == 5 and unresolved == 0, f'repaired={repaired} unresolved={unresolved}')
    parsed_sum = sum(
        to_num(field_of(line, 8)) for line in text.split(NL)
        if line and field_of(line, 4) == '26.01'
    )
    check('Суммы сходятся: сырой файл == разбор', abs(raw_sum - parsed_sum) < 1e-9,
          f'raw={raw_sum} parsed={parsed_sum}')
    # 4367,36 (GOOD_ROW) + 999,90 (GOOD_ROW_3) + 5 × 4367,36 (рваные строки)
    check('Суммы сходятся: ожидаемое значение', abs(raw_sum - 27204.06) < 1e-6,
          f'got={raw_sum}')

    # --- 6. Конец файла сохранён (перевод строки не теряется) ---
    text, repaired, unresolved = run([HEADER_LINE] + GOOD_ROWS + [RAGGED_SUBKONTO], tmpdir)
    check('Перевод строки в конце сохранён', text.endswith(NL))

    # --- 7. Неразрешимая строка честно помечается, а не молча теряется ---
    # 13 полей, и ни одна позиция склейки не делает Дт счётом: после любого
    # сдвига «НЕ СЧЕТ» оказывается либо в Дт, либо в Сумме.
    broken = TAB.join(['', '31.01.2026 23:59:59', 'Док', 'Содержание',
                       'НЕ СЧЕТ', '', '02.01', '', '4 367,36',
                       'Руководство', 'Здание', 'склада', 'ОС'])
    text, repaired, unresolved = run([HEADER_LINE] + GOOD_ROWS + [broken], tmpdir)
    check('Битая строка помечена как нерешённая', unresolved == 1, f'unresolved={unresolved}')
    check('Битая строка не объявлена исправленной', repaired == 0, f'repaired={repaired}')
    # Строка осталась как есть — данные не искажены «на всякий случай»
    check('Битая строка не искажена', text.split(NL)[-2] == broken)

    # --- 8. Шапка нестандартного формата (регресс старого кейя) ---
    # Раньше при отсутствии колонки «Содержание» в шапке файл не чинился вовсе.
    # Шапка здесь: Сумма распознаётся, но Содержание/Дата отсутствуют.
    odd_header = TAB.join(['Регистратор', 'Счет', 'Сумма', 'Содержание'])
    odd_row = TAB.join(['Документ 1', '60.01', '1 500,00', 'Оплата'])
    odd_bad = TAB.join(['Документ 1', '60.01', '1 500,00', 'Оплата', 'по договору'])
    text, repaired, unresolved = run([odd_header, odd_row, odd_bad], tmpdir,
                                     'odd_отчпровод_60_.txt')
    check('Нестандартная шапка: строка восстановлена', repaired == 1, f'repaired={repaired}')
    check('Нестандартная шапка: нерешённых нет', unresolved == 0, f'unresolved={unresolved}')
    check('Нестандартная шапка: число полей = 4',
          count_fields(text.split(NL)[-2]) == 4,
          f'got={count_fields(text.split(NL)[-2])}')
    check('Нестандартная шапка: Сумма — число',
          field_of(text.split(NL)[-2], 2) == '1 500,00',
          f'got={field_of(text.split(NL)[-2], 2)!r}')

    # --- 9. Насыщение профиля: правды не хватает — молчать, а не гадать ---
    # Регресс на сам алгоритм выбора позиции. В реальных выгрузках 1С текстовые
    # колонки («Субконто Кт» с наименованием объекта ОС, «Содержание») почти
    # всегда содержат больше 200 разных значений — порог из
    # _collect_column_profile. Такие колонки становятся «вольными», и профиль
    # перестаёт различать кандидатов: ВСЕ дают одинаковый максимум.
    #
    # Прежний код при равенстве оставлял первый индекс (`score > best_score`),
    # то есть склеивал как можно раньше — тот же хардкод смещения, только со
    # знаком «+». Разрыв в «Субконто Кт» уезжал в «Субконто Дт»: контрагент и
    # объект ОС менялись местами, сумма/дата/счёт оставались валидными, строка
    # НЕ отбрасывалась фильтром — и прогон отчитывался «Данные не потеряны».
    #
    # Теперь такой строке возвращается unresolved: данные не искажаются, а
    # normalize_ragged_tab_rows пишет явный WARNING/ERROR.
    huge_values = [f'Объект ОС № {i:04d}' for i in range(260)]
    bulk_good = [
        TAB.join(['', f'{10 + (i % 20):02d}.0{i % 9 + 1}.2026 10:00:00',
                  f'Документ {i:04d}', f'Операция {i}',
                  '26.01', '', '02.01', '', f'{100 + i},50',
                  f'Руководство {i % 7}',
                  huge_values[i], 'ОС'])
        for i in range(260)
    ]
    ragged_in_hicol = TAB.join(['', '31.01.2026 23:59:59', 'Амортизация ОС 00002',
                                'Амортизация', '26.01', '', '02.01', '',
                                '4 367,36', 'Руководство',
                                'Здание склада инв', '№ 280', 'ОС'])
    text, repaired, unresolved = run(
        [HEADER_LINE] + bulk_good + [ragged_in_hicol], tmpdir, 'hicol_отчпровод_26_.txt'
    )
    lines = text.split(NL)
    check('Высококардинальная колонка: строка НЕ объявлена исправленной',
          repaired == 0, f'repaired={repaired}')
    check('Высококардинальная колонка: помечена как нерешённая',
          unresolved == 1, f'unresolved={unresolved}')
    # Главное: данные не искажены «на всякий случай» — строка осталась как есть,
    # субконто не переехали в чужую колонку.
    check('Высококардинальная колонка: строка не искажена',
          lines[-2] == ragged_in_hicol)
    check('Высококардинальная колонка: Субконто Дт не захватил чужой фрагмент',
          field_of(lines[-2], 9) == 'Руководство',
          f'got={field_of(lines[-2], 9)!r}')
    # Профиль построен по 260 строкам — порог «вольной колонки» достигнут,
    # иначе сценарий был бы структурно невоспроизводим (как было в этом смоуке).
    from utils.file_utils import _collect_column_profile
    prof, wild = _collect_column_profile(
        build_file([HEADER_LINE] + bulk_good + [ragged_in_hicol], tmpdir,
                   'hicol_отчпровод_26_.txt'),
        0, 11, 'utf-8', 'strict',
    )
    check('Высококардинальная колонка: профиль помечает её вольной (порог 200)',
          10 in wild, f'wildcard={sorted(wild)}')

    print()
    total = PASSED + FAILED
    if FAILED:
        print(f'SMOKE_FAIL ({total - FAILED}/{total})')
        return 1
    print(f'SMOKE_OK ({PASSED}/{total})')
    return 0


if __name__ == '__main__':
    sys.exit(main())