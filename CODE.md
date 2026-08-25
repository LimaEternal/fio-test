# CODE.md — документация проекта fio-test

Автосгенерировано из docstring'ов и сигнатур исходников.

Для каждого файла перечислены все функции/классы/методы с их назначением.

---

## fio-test.py

fio-test.py — Автоматический бенчмаркинг несистемных накопителей.

Сканирует систему на несистемные диски, классифицирует их по интерфейсу
(NVMe/SAS/SATA), запускает FIO-тесты с оптимальными параметрами для каждого типа
и выводит результаты в консоль + MD-отчёт.

Использование:
    python fio-test.py              — тестирование (параллельно)
    python fio-test.py -s           — тестирование (последовательно)
    python fio-test.py -c           — тестирование с подтверждением
    python fio-test.py -c -s        — с подтверждением, последовательно
    python fio-test.py -f           — быстрый режим без предварительного заполнения
                                      (по умолчанию префилл выполняется перед тестами)
    python fio-test.py -r 60            — 60 сек на тест
    python fio-test.py -l               — подробное логирование: отчёт обновляется
                                          по мере завершения тестов (мониторинг в отчёте)
    python fio-test.py -t               — тестовый режим (пробные данные без fio)
    python fio-test.py -a 1 3-5         — протестировать только диски 1 и 3..5
                                           (номера и диапазоны из нумерованного списка)
    python fio-test.py -d 4 6-8         — протестировать все несистемные диски,
                                           кроме 4 и 6..8
    python fio-test.py -o my.md         — свой путь отчёта
    python fio-test.py --target-iops 50 — целевая нагрузка IOPS на поток для расчёта
                                           bs/numjobs/iodepth последовательных тестов

Примечания по флагам:
  - -a/--add и -d/--delete взаимоисключающие: передача обоих вызывает ошибку
    (SystemExit) ещё на этапе разбора аргументов.
  - -t/--test (тестовый режим) молча игнорирует -s/-f/-r/-b/-c:
    эти флаги не влияют на пробную таблицу и в отчёт тестового режима не попадают.
  - -r/--runtime должен быть положительным числом (>0); некорректное значение —
    ошибка разбора.
  - -o/--output должен указывать на файл внутри существующего каталога
    (сам путь не должен быть каталогом, родитель должен существовать).

### Функции модуля

### `def _load_fio_configs()`

Читает configs/<interface>.fio -> {интерфейс: {id: [аргументы]}}.

### Загрузка порогов (при импорте модуля)

Пороги читаются из utils.thresholds при старте (fail-fast): `BASE_THRESHOLDS` <- configs/base_thresholds.json (общие по интерфейсу+поколению), `PERSONAL_THRESHOLDS` <- configs/disk_thresholds.json (персональные по моделям). Ошибка чтения/парсинга останавливает запуск; структура обоих файлов дополнительно проверяется в validate_configs().

### `def _expand_short_flags(argv)`

Расширяет комбинированные короткие флаги: -sc → -s -c, -cf → -c -f.

### `def _block_gb_type(value)`

Тип для -b/--block: целое число гигабайт, 0 = весь диск.

### `def parse_args()`

_без docstring_

### `def check_threshold(test_id, res, thresholds)`

Проверяет результат теста по пороговым значениям. Возвращает 'PASS' или 'FAIL'.

### `def _expand_token(token)`

Разворачивает один токен номера/диапазона в список целых.  '5'   → [5] '1-3' → [1, 2, 3] '3-1' → [3, 2, 1] Неверный формат → ValueError.

### `def parse_disk_numbers(raw)`

Разбирает строку номеров и диапазонов '1-3 5' в список целых (пустая строка → []).

### `def _input_disk_numbers(prompt)`

Спрашивает у пользователя номера дисков (через пробел или диапазоном) и возвращает их список.  При неверном формате запрашивает ввод повторно; EOF/Ctrl-C → «Отменено».

### `def apply_disk_selection(disks, args)`

Применяет выбор дисков --add/--delete к пронумерованному списку (1..N).  --add 1 2 3    — оставить только диски 1, 2, 3; --add без номеров → []. --delete 4 5 6 — исключить диски 4, 5, 6 из полного набора; --delete без номеров → все диски. Флаг не задан (None) → список без изменений. Неверные номера (вне диапазона) считаются ошибкой и останавливают запуск.

### `def _build_run_info(args, prefill_duration, tests_duration, test_mode)`

Собирает мета-информацию о запуске для секции «Параметры запуска» отчёта.  В тестовом режиме (test_mode=True) флаги, не влияющие на вывод пробной таблицы (-s, -f, -r, пороги), в отчёт не попадают: режим показывается как «тестовый».

### `def _disk_interface(disk)`

Возвращает ключ интерфейса (nvme/sas/sata) для диска.

### `resolve_thresholds(disk, base, personal)` (из utils/thresholds.py)

Выбирает итоговые пороги диска. Приоритет: 1) персональная запись по нормализованной модели (upper-case, схлопывание пробелов) — применяется ЦЕЛИКОМ; 2) секция hdd при rotational == 1; 3) строка интерфейс+поколение из base_thresholds.json (NVMe: поколение PCIe из sysfs-линка с клампингом к доступным строкам; sas/sata/hdd: первая строка секции); 4) поколение неизвестно -> нижняя строка интерфейса. Возвращает (thresholds, source): source — {тест: "персональные (по модели)" | "общие (<интерфейс> <строка>)"}.

### `def collect_plan_info(disks, disk_plans, target_iops)`

Собирает фактические параметры тестов для отчёта.  Возвращает {имя_диска: {"interface", "ceiling_mbps", "max_sectors_kb", "target_iops", "tests": {тест: {"bs", "iodepth", "numjobs"}}, "thresholds": {тест: {min_bw_mb|min_iops}}, "threshold_source": {тест: "персональные (по модели)" | "общие (...)"}}}.  В реальном режиме параметры берутся из уже построенного плана (disk_plans) — ровно те, с которыми пойдут тесты; в тестовом режиме (disk_plans=None) — считаются из профиля диска.

### `def build_disk_plans(disks, args)`

Строит планы тестов для всех дисков.  Возвращает (disk_plans, disk_thresholds): disk_plans — список [(disk_idx, disk, plan)], где plan — [(test_id, fio_args)] с переопределёнными bs/iodepth/numjobs и добавленными --runtime/--size; disk_thresholds — {disk_idx: итоговые пороги (персональные по модели либо общие из configs/base_thresholds.json)}.

### `def _fake_profile(tran, rotational, **link)`

Профиль железа в формате utils.hw_profile.collect_hw_profile().

### `def build_fake_disks()`

Фейковые диски (5 шт., разные интерфейсы) для проверки вёрстки таблицы.

### `def build_fake_results(disks)`

Пробные результаты: во всех ячейках значение 'test'.

### `def validate_configs()`

Валидирует .fio-конфиги и пороги всех интерфейсов.

### `def _run_io_process(cmd, cancel_event)`

Запускает процесс и ждёт завершения (обёртка над utils.process.run_process).  Возвращает (proc, stdout, stderr) либо None при отмене или ошибке запуска. FileNotFoundError пробрасывается наверх для точной диагностики.

### `def _save_raw_fio_output(disk_name, test_id, stdout)`

Сохраняет сырой stdout fio в reports/raw/ для диагностики метрик.  Нужно для расследования аномальных значений (например, недостоверных перцентилей clat): по сохранённому JSON можно увидеть, что именно отдал fio, не перезапуская тесты.

### `def _max_iodepth(iodepth_level)`

Возвращает максимальную достигнутую глубину очереди из гистограммы fio.  fio помечает верхнюю (переполненную) корзину гистограммы строкой вида ">=64" — такие ключи разбираются по цифрам.

### `def _percentile_value(percentile, target)`

Возвращает значение перцентиля из fio-JSON или None.  Ключи обычно форматируются как "99.000000", но у старых версий fio могут отличаться — сначала ищем точный строковый ключ, затем числовое совпадение.

### `def _parse_fio_result(test_id, stdout)`

Разбирает stdout fio (JSON) в результат + диагностические метрики.

### `def run_fio_test(disk_info, test_id, base_args, cancel_event, diag_store, tuner, state_lock, live_store)`

Запускает fio-тест. Поддерживает отмену через cancel_event.  Параллельно с тестом всегда сэмплирует линк и температуру; сводка (максимальная температура за тест и т.п.) попадает в res["diag"] — из неё берётся колонка Tmax в консольной таблице. В диагностическом режиме (diag_store) дополнительно пишутся посекундные логи нагрузки, raw JSON и посекундные сэмплы в diag_store/live_store для единого файла отчёта.  state_lock защищает запись в общие diag_store/live_store от гонок с фоновым writer-потоком инкрементального отчёта.

### `def _pow2_down(x)`

Наибольшая степень двойки, не превосходящая x.

### `def _pow2_up(x)`

Наименьшая степень двойки, не меньшая x.

### `def _format_bs_kb(bs_kb)`

Форматирует блок в KB как строку fio (1024k → 1m).

### `def _sequential_overrides(profile, interface, target_iops)`

Считает bs/numjobs/iodepth для последовательного теста.  Потолок шины берётся из sysfs (GT/s × width × кодирование), блок — такой, чтобы нагрузка на поток не превышала target_iops. Ограничения: - не меньше минимального последовательного блока (64k); - не больше лимита ядра max_sectors_kb на один I/O; - не меньше физического блока диска; - механические диски (rotational) всегда читают блоком 1M — формула потолка шины к ним не применима.  numjobs добирает оставшиеся IOPS, iodepth — по закону Литтла (глубина очереди покрывает задержку при целевой нагрузке на поток).  Возвращает {"bs", "iodepth", "numjobs"} или {} при отсутствии потолка (фоллбек на базовые параметры .fio-конфига).

### `def _random_overrides(interface, link, test_id)`

Параметры случайных тестов (bs всегда 4k). numjobs/iodepth подбираются по правилам интерфейса/поколения: IOPS случайного доступа упираются в сам накопитель, а не в шину, поэтому формула потолка тут не применяется.

### `def compute_test_overrides(profile, interface, test_id, target_iops)`

Возвращает переопределения (bs/iodepth/numjobs) для одного теста.

### `def build_test_plan(disk, base_tests, target_iops)`

Строит динамический план тестов на основе профиля железа диска.  Возвращает список [(test_id, fio_args), ...] с переопределёнными параметрами (bs, iodepth, numjobs): - последовательные тесты: блок/потоки/глубина считаются из потолка шины (sysfs: GT/s × width) под целевую нагрузку target_iops на поток; - случайные тесты: bs=4k, numjobs/iodepth по правилам интерфейса.  Если потолок не определён (нет линка в sysfs) — параметры остаются из базового .fio-конфига.

### `def process_task_result(results, idx, disk, t, fio_args, res, state_lock)`

Обрабатывает результат задачи и записывает его в общий словарь результатов.

### `def _format_test_done(disk, test_id, result)`

Строка «тест завершён» для консольного прогресса в режиме -l.

### `def run_disk_tests(disk_idx, disk, plan, results, cancel_event, diag_store, tuner, state_lock, report_queue, live_store)`

Запускает все тесты одного диска строго последовательно.  Параллелизация идёт по дискам, а не по отдельным тестам: несколько fio, конкурирующих за один накопитель, делят его шину и занижают результаты.  После каждого завершённого теста в report_queue кладётся маркер — фоновый writer-поток перегенерирует MD-отчёт по мере поступления данных. В results[disk_idx] сохраняется длительность всех тестов диска (_wall_s).

### `def _default_report_path()`

Формирует путь отчёта по умолчанию (reports/fio_report_<timestamp>.md).

### `def _snapshot_state(results, diag_store, live_store, state_lock)`

Копирует текущее состояние для рендера отчёта (безопасно к воркерам).  Живые сэмплы идущих тестов (live_store) вливаются в диагностическую копию, чтобы посекундные таблицы текущего теста попадали в отчёт.

### `def _write_report(disks, results, diag_store, live_store, state_lock, output_path, tuner, test_names, run_info, test_plans, show_lat_p99, show_tmax)`

Перегенерирует MD-отчёт по текущему (возможно, неполному) состоянию.

### `def main()`

_без docstring_

### Классы

#### `class _ReportWriter(threading.Thread)`

Фоновый поток: перегенерирует отчёт по мере поступления данных.  Реагирует на маркер завершения теста из очереди и, по таймауту, на живые посекундные сэмплы идущих тестов (live_store).

##### ### `def __init__(self, report_queue, render, has_live, tick)`

_без docstring_

##### ### `def run(self)`

_без docstring_

##### ### `def _safe_render(self)`

_без docstring_

### Константы/переменные модуля

- `console`

- `CONFIG_DIR`

- `INTERFACES`

- `FRIENDLY_TEST_NAMES`

- `INTERFACE_CONFIGS`

- `BASE_THRESHOLDS`

- `PERSONAL_THRESHOLDS`

- `TEST_NAMES`

- `P99_RELIABILITY_FACTOR`

- `DEFAULT_TARGET_IOPS`

- `SEQUENTIAL_TESTS`

- `MIN_SEQ_BS_KB`

- `HDD_SEQ_BS_KB`

- `MAX_JOBS`

- `DEFAULT_MAX_SECTORS_KB`

- `RTT_MS`

- `HDD_RTT_MS`

- `MAX_QD`

- `_SHORT_FLAGS`

- `REPORT_TICK`

- `_STOP`


## tests/__init__.py


## tests/test_diagnostics.py

### Функции модуля

### `def _patch_paths(tmp)`

Перенаправляет /sys/class/nvme и /sys/class/block на временный каталог.

### `def _smart_log(text, returncode)`

Мок вывода `nvme smart-log`: обычный или с ошибкой.

### Классы

#### `class DiagnosticSamplerTests(unittest.TestCase)`

##### ### `def setUp(self)`

_без docstring_

##### ### `def test_link_and_temperature_are_read(self)`

_без docstring_

##### ### `def test_temp_read_from_namespace_fallback(self)`

_без docstring_

##### ### `def test_temp_parses_degree_sign_format(self)`

nvme-cli 2.3 пишет `28°C` без пробела — градус должен распознаваться.

##### ### `def test_temp_cached_after_first_read(self)`

_без docstring_

##### ### `def test_temp_none_when_smart_log_fails(self)`

_без docstring_

##### ### `def test_temp_none_when_smart_log_unparseable(self)`

_без docstring_

##### ### `def test_sample_once_reads_link_and_temp_only(self)`

_без docstring_

##### ### `def test_source_status_and_none_values_when_sources_missing(self)`

_без docstring_

#### `class ParseFioLogsTests(unittest.TestCase)`

##### ### `def test_parses_bw_and_iops_logs_and_deletes_files(self)`

_без docstring_

##### ### `def test_sums_across_job_files_and_write_direction(self)`

_без docstring_

##### ### `def test_none_when_no_log_files(self)`

_без docstring_

##### ### `def test_skips_malformed_and_zero_rows(self)`

_без docstring_

##### ### `def test_iops_normalized_when_log_in_x1000_units(self)`

_без docstring_

##### ### `def test_merge_fio_logs_matches_samples_by_timestamp(self)`

_без docstring_

##### ### `def test_merge_returns_false_without_logs(self)`

_без docstring_

#### `class CollectStaticInfoTests(unittest.TestCase)`

##### ### `def test_numa_node_read_from_device_dir(self)`

_без docstring_

### Константы/переменные модуля

- `PROJECT_ROOT`

- `DISK`


## tests/test_exit_code.py

### Функции модуля

### `def _mk_disk(*statuses)`

Имитирует results[disk_idx] проекта: {test_id: {"status": ...}}.

### Классы

#### `class ExtractStatusesTests(unittest.TestCase)`

##### ### `def test_ignores_private_keys(self)`

_без docstring_

##### ### `def test_multiple_disks(self)`

_без docstring_

##### ### `def test_flat_list_dicts(self)`

_без docstring_

#### `class CountStatusesTests(unittest.TestCase)`

##### ### `def test_mixed(self)`

_без docstring_

##### ### `def test_empty(self)`

_без docstring_

#### `class DecideExitCodeTests(unittest.TestCase)`

##### ### `def test_all_pass(self)`

_без docstring_

##### ### `def test_all_fail(self)`

_без docstring_

##### ### `def test_partial_fail(self)`

_без docstring_

##### ### `def test_one_disk_fail_rest_pass(self)`

_без docstring_

##### ### `def test_single_pass(self)`

_без docstring_

##### ### `def test_single_fail(self)`

_без docstring_

##### ### `def test_empty(self)`

_без docstring_

##### ### `def test_case_insensitive(self)`

_без docstring_

#### `class SysExitTests(unittest.TestCase)`

##### ### `def test_exit_0(self)`

_без docstring_

##### ### `def test_exit_1(self)`

_без docstring_

##### ### `def test_exit_2(self)`

_без docstring_

### Константы/переменные модуля

- `PROJECT_ROOT`


## tests/test_fio_config.py

### Функции модуля

### `def _load_fio_test()`

_без docstring_

### Классы

#### `class ParseFioJobfileTests(unittest.TestCase)`

##### ### `def _parse(self, content)`

_без docstring_

##### ### `def test_parses_sections_in_order(self)`

_без docstring_

##### ### `def test_global_merged_into_each_section(self)`

_без docstring_

##### ### `def test_section_option_overrides_global(self)`

_без docstring_

##### ### `def test_global_after_sections_still_applies(self)`

_без docstring_

##### ### `def test_comments_and_blank_lines_ignored(self)`

_без docstring_

##### ### `def test_option_before_any_section_raises(self)`

_без docstring_

##### ### `def test_bare_boolean_option_treated_as_value_1(self)`

_без docstring_

##### ### `def test_bad_line_raises(self)`

_без docstring_

##### ### `def test_missing_file_raises(self)`

_без docstring_

##### ### `def test_only_global_section_raises(self)`

_без docstring_

#### `class ProjectFioConfigsTests(unittest.TestCase)`

Реальные конфиги проекта должны парситься и проходить валидацию.

##### ### `def test_all_interface_configs_parse(self)`

_без docstring_

### Константы/переменные модуля

- `PROJECT_ROOT`


## tests/test_fio_test.py

### Классы

#### `class RunDiskTestsTests(unittest.TestCase)`

##### ### `def test_tests_of_one_disk_run_in_order(self)`

_без docstring_

##### ### `def test_error_result_does_not_break_following_tests(self)`

_без docstring_

#### `class TestProgressConsoleTests(unittest.TestCase)`

Консольный прогресс тестов (строки «Готово ...») — только в режиме -l.

##### ### `def _run(self, diag_store)`

_без docstring_

##### ### `def test_logging_mode_prints_done_line_per_test(self)`

_без docstring_

##### ### `def test_non_logging_mode_prints_no_done_lines(self)`

_без docstring_

##### ### `def test_format_test_done_contains_metrics(self)`

_без docstring_

##### ### `def test_format_test_done_fail_status(self)`

_без docstring_

#### `class ParseFioResultTests(unittest.TestCase)`

##### ### `def test_deep_fields_parsed_from_fio_json(self)`

_без docstring_

##### ### `def test_cpu_usage_legacy_usage_key_fallback(self)`

_без docstring_

##### ### `def test_percentile_falls_back_to_lat_ns(self)`

_без docstring_

##### ### `def test_percentile_matched_by_numeric_key(self)`

_без docstring_

##### ### `def test_write_mode_selected_by_test_id(self)`

_без docstring_

##### ### `def test_p99_flagged_unreliable_when_far_above_avg(self)`

_без docstring_

##### ### `def test_p99_reliable_when_within_sane_range(self)`

_без docstring_

##### ### `def test_p99_not_flagged_when_avg_missing(self)`

_без docstring_

##### ### `def test_bad_json_returns_error(self)`

_без docstring_

#### `class ParseArgsLoggingTests(unittest.TestCase)`

##### ### `def test_logging_flag_parses(self)`

_без docstring_

##### ### `def test_combined_short_flags_with_l(self)`

_без docstring_

##### ### `def test_fast_short_flag_parses(self)`

_без docstring_

##### ### `def test_fast_long_flag_parses(self)`

_без docstring_

##### ### `def test_combined_short_flags_with_f(self)`

_без docstring_

#### `class ParseArgsBlockTests(unittest.TestCase)`

##### ### `def test_default_block_is_100(self)`

_без docstring_

##### ### `def test_block_short_flag_parses(self)`

_без docstring_

##### ### `def test_block_long_flag_parses(self)`

_без docstring_

##### ### `def test_block_invalid_value_exits(self)`

_без docstring_

##### ### `def test_block_negative_value_exits(self)`

_без docstring_

#### `class BlockGbTypeTests(unittest.TestCase)`

##### ### `def test_zero_is_allowed(self)`

_без docstring_

##### ### `def test_positive_integer(self)`

_без docstring_

##### ### `def test_non_numeric_raises(self)`

_без docstring_

##### ### `def test_float_raises(self)`

_без docstring_

##### ### `def test_negative_raises(self)`

_без docstring_

#### `class MainParallelModeTests(unittest.TestCase)`

Параллельный режим должен отправлять в пул по одной задаче на диск.

##### ### `def test_parallel_mode_submits_one_task_per_disk(self)`

_без docstring_

##### ### `def test_logging_mode_passes_diag_store_to_runner_and_report(self)`

_без docstring_

##### ### `def test_non_logging_mode_passes_none_diag_store(self)`

_без docstring_

#### `class MainBlockSizeTests(unittest.TestCase)`

-b/--block должен доходить до префилла и плана тестов как --size=NG.

##### ### `def _run_main(self, argv)`

_без docstring_

##### ### `def _plan_args(self, fake_runner)`

_без docstring_

##### ### `def test_default_block_100g_in_plan_and_prefill(self)`

_без docstring_

##### ### `def test_custom_block_in_plan_and_prefill(self)`

_без docstring_

##### ### `def test_block_zero_omits_size(self)`

_без docstring_

#### `class RunFioTestDiagStoreTests(unittest.TestCase)`

run_fio_test в диагностическом режиме заполняет diag_store сэмплами.

##### ### `def test_diag_store_filled_with_samples_and_summary(self)`

_без docstring_

##### ### `def test_raw_fio_json_saved_in_diag_mode(self)`

_без docstring_

##### ### `def test_raw_fio_json_not_saved_without_diag_store(self)`

_без docstring_

##### ### `def test_sampler_runs_and_diag_present_without_diag_store(self)`

_без docstring_

#### `class RunFioTestLogFlagsTests(unittest.TestCase)`

В диагностическом режиме fio пишет посекундные логи нагрузки.

##### ### `def _run(self, diag_store)`

_без docstring_

##### ### `def test_log_flags_added_in_diag_mode(self)`

_без docstring_

##### ### `def test_no_log_flags_and_no_merge_without_diag(self)`

_без docstring_

##### ### `def test_notes_for_missing_temp(self)`

_без docstring_

##### ### `def test_no_notes_when_all_sources_available(self)`

_без docstring_

#### `class MaxIodepthOverflowTests(unittest.TestCase)`

fio помечает переполненную корзину гистограммы глубины как ">=64".

##### ### `def test_max_iodepth_handles_overflow_bucket(self)`

_без docstring_

##### ### `def test_max_iodepth_ignores_keys_without_digits(self)`

_без docstring_

##### ### `def test_max_iodepth_empty(self)`

_без docstring_

##### ### `def test_parse_fio_result_with_overflow_bucket(self)`

_без docstring_

#### `class RunFioTestParseWrapTests(unittest.TestCase)`

Сбой разбора результата одного теста не должен ронять весь прогон.

##### ### `def test_parse_error_returns_error_dict(self)`

_без docstring_

#### `class RunDiskTestsReportQueueTests(unittest.TestCase)`

После каждого теста run_disk_tests уведомляет writer об обновлении отчёта.

##### ### `def test_puts_marker_after_each_test(self)`

_без docstring_

##### ### `def test_no_queue_no_markers(self)`

_без docstring_

#### `class SnapshotStateTests(unittest.TestCase)`

##### ### `def test_returns_copies_of_results(self)`

_без docstring_

##### ### `def test_preserves_none_diag_store(self)`

_без docstring_

##### ### `def test_merges_live_entries_into_diag_snapshot(self)`

_без docstring_

#### `class ReportWriterTests(unittest.TestCase)`

##### ### `def _start(self, has_live)`

_без docstring_

##### ### `def test_renders_on_notification_and_stops(self)`

_без docstring_

##### ### `def test_renders_on_live_tick_but_not_when_idle(self)`

_без docstring_

#### `class MainIncrementalReportTests(unittest.TestCase)`

С -l отчёт пишется до запуска тестов и ещё раз в конце прогона.

##### ### `def test_logging_mode_writes_initial_report_before_tests(self)`

_без docstring_

#### `class ParseDiskSelectionArgsTests(unittest.TestCase)`

-a/--add и -d/--delete: парсинг, интерактив и взаимное исключение.

##### ### `def test_add_parses_numbers(self)`

_без docstring_

##### ### `def test_add_long_form(self)`

_без docstring_

##### ### `def test_delete_parses_numbers(self)`

_без docstring_

##### ### `def test_add_range_expands(self)`

_без docstring_

##### ### `def test_add_mixed_numbers_and_ranges(self)`

_без docstring_

##### ### `def test_delete_range_expands(self)`

_без docstring_

##### ### `def test_add_descending_range(self)`

_без docstring_

##### ### `def test_invalid_token_exits(self)`

_без docstring_

##### ### `def test_bare_add_yields_empty_list(self)`

_без docstring_

##### ### `def test_defaults_are_none(self)`

_без docstring_

##### ### `def test_add_and_delete_are_mutually_exclusive(self)`

_без docstring_

#### `class ExpandTokenTests(unittest.TestCase)`

##### ### `def test_single_number(self)`

_без docstring_

##### ### `def test_ascending_range(self)`

_без docstring_

##### ### `def test_descending_range(self)`

_без docstring_

##### ### `def test_single_element_range(self)`

_без docstring_

##### ### `def test_invalid_token_raises(self)`

_без docstring_

##### ### `def test_empty_token_raises(self)`

_без docstring_

#### `class ParseDiskNumbersTests(unittest.TestCase)`

##### ### `def test_numbers_split_on_spaces(self)`

_без docstring_

##### ### `def test_single_number(self)`

_без docstring_

##### ### `def test_empty_string(self)`

_без docstring_

##### ### `def test_whitespace_only(self)`

_без docstring_

##### ### `def test_range_expands(self)`

_без docstring_

##### ### `def test_numbers_and_ranges_mix(self)`

_без docstring_

##### ### `def test_descending_range(self)`

_без docstring_

##### ### `def test_invalid_token_raises(self)`

_без docstring_

#### `class InputDiskNumbersTests(unittest.TestCase)`

##### ### `def test_reads_numbers_from_stdin(self)`

_без docstring_

##### ### `def test_reads_ranges_from_stdin(self)`

_без docstring_

##### ### `def test_empty_input_returns_empty_list(self)`

_без docstring_

##### ### `def test_invalid_input_reprompts(self)`

_без docstring_

##### ### `def test_eof_aborts(self)`

_без docstring_

#### `class ApplyDiskSelectionTests(unittest.TestCase)`

##### ### `def setUp(self)`

_без docstring_

##### ### `def _args(self, add, delete)`

_без docstring_

##### ### `def test_add_keeps_only_selected(self)`

_без docstring_

##### ### `def test_delete_removes_selected(self)`

_без docstring_

##### ### `def test_add_with_all_numbers_keeps_all(self)`

_без docstring_

##### ### `def test_delete_with_all_numbers_keeps_none(self)`

_без docstring_

##### ### `def test_no_flags_returns_unchanged(self)`

_без docstring_

##### ### `def test_empty_add_yields_no_disks(self)`

_без docstring_

##### ### `def test_empty_delete_keeps_all_disks(self)`

_без docstring_

##### ### `def test_out_of_range_number_exits(self)`

_без docstring_

##### ### `def test_out_of_range_message_single_disk(self)`

_без docstring_

##### ### `def test_out_of_range_message_multi_disk(self)`

_без docstring_

#### `class MainDiskSelectionTests(unittest.TestCase)`

Выбор дисков через -a/-d должен доходить до run_disk_tests.

##### ### `def _run_main(self, argv, disks)`

_без docstring_

##### ### `def _three_disks(self)`

_без docstring_

##### ### `def test_add_selects_subset_of_disks(self)`

_без docstring_

##### ### `def test_delete_excludes_disk(self)`

_без docstring_

##### ### `def test_add_range_selects_disks(self)`

_без docstring_

##### ### `def test_add_prompt_fills_numbers(self)`

_без docstring_

##### ### `def test_add_empty_prompt_exits_without_running(self)`

_без docstring_

##### ### `def test_delete_all_disks_exits_without_running(self)`

_без docstring_

#### `class TestModeDiskSelectionTests(unittest.TestCase)`

В тестовом режиме используются реальные диски из сканирования; -a/-d фильтруют их.

##### ### `def _scan(self)`

_без docstring_

##### ### `def _run(self, argv, scan)`

_без docstring_

##### ### `def test_add_filters_real_disks(self)`

_без docstring_

##### ### `def test_delete_filters_real_disks(self)`

_без docstring_

##### ### `def test_no_selection_keeps_all_real_disks(self)`

_без docstring_

##### ### `def test_empty_scan_falls_back_to_fake_disks(self)`

_без docstring_

##### ### `def test_fallback_prints_warning(self)`

_без docstring_

##### ### `def test_add_without_numbers_prompts_interactively(self)`

_без docstring_

##### ### `def test_delete_all_exits(self)`

_без docstring_

#### `class ElapsedParseTests(unittest.TestCase)`

##### ### `def test_elapsed_parsed_from_job(self)`

_без docstring_

##### ### `def test_elapsed_missing_defaults_zero(self)`

_без docstring_

#### `class BuildRunInfoTimingTests(unittest.TestCase)`

##### ### `def test_durations_appear_in_flags(self)`

_без docstring_

##### ### `def test_no_durations_no_extra_flags(self)`

_без docstring_

##### ### `def test_test_mode_marks_regime_and_skips_irrelevant_flags(self)`

_без docstring_

##### ### `def test_normal_mode_keeps_runtime_prefill_and_thresholds(self)`

_без docstring_

##### ### `def test_block_zero_rendered_as_full_disk(self)`

_без docstring_

#### `class BuildTestPlanTests(unittest.TestCase)`

build_test_plan: динамический расчёт bs/numjobs/iodepth по потолку шины.

##### ### `def _gen(self, gts)`

_без docstring_

##### ### `def _disk(self, interface, gts, width, rotational, max_sectors_kb, phys, link)`

_без docstring_

##### ### `def _plan_args(self, disk, test_id)`

_без docstring_

##### ### `def test_gen4_seq_read(self)`

_без docstring_

##### ### `def test_gen5_seq_read(self)`

_без docstring_

##### ### `def test_gen6_seq_read(self)`

_без docstring_

##### ### `def test_gen7_seq_read(self)`

_без docstring_

##### ### `def test_gen5_seq_write_same_bs_formula(self)`

_без docstring_

##### ### `def test_sata_ssd_seq_read(self)`

_без docstring_

##### ### `def test_sata_hdd_seq_read_uses_1m(self)`

_без docstring_

##### ### `def test_sas_seq_read(self)`

_без docstring_

##### ### `def test_max_sectors_kb_clamps_bs(self)`

_без docstring_

##### ### `def test_no_link_info_returns_unchanged(self)`

_без docstring_

##### ### `def test_gen5_rand_read_overridden(self)`

_без docstring_

##### ### `def test_gen4_rand_read_overridden(self)`

_без docstring_

##### ### `def test_sata_rand_overridden(self)`

_без docstring_

### Константы/переменные модуля

- `PROJECT_ROOT`

- `_spec`

- `fio_test`

- `DISK`

- `NVME_TEST_IDS`


## tests/test_prefill.py

### Классы

#### `class FormatDurationTests(unittest.TestCase)`

##### ### `def test_duration_seconds(self)`

_без docstring_

##### ### `def test_duration_minutes(self)`

_без docstring_

##### ### `def test_duration_hours(self)`

_без docstring_

##### ### `def test_duration_rounds(self)`

_без docstring_

#### `class FormatMiscTests(unittest.TestCase)`

##### ### `def test_bytes_units(self)`

_без docstring_

##### ### `def test_bw_zero(self)`

_без docstring_

##### ### `def test_bw_bytes(self)`

_без docstring_

##### ### `def test_bw_gibs(self)`

_без docstring_

#### `class PrefillConfigTests(unittest.TestCase)`

##### ### `def test_load_config_parses_key_value(self)`

_без docstring_

##### ### `def test_load_config_defaults_engine_when_missing(self)`

_без docstring_

##### ### `def test_load_config_appends_default_engine(self)`

_без docstring_

#### `class PrefillStatsTests(unittest.TestCase)`

##### ### `def test_extract_stats_from_write(self)`

_без docstring_

##### ### `def test_extract_stats_missing_write(self)`

_без docstring_

##### ### `def test_extract_stats_empty_jobs(self)`

_без docstring_

#### `class ResolveSizeBytesTests(unittest.TestCase)`

##### ### `def test_zero_means_full_disk(self)`

_без docstring_

##### ### `def test_block_in_gib(self)`

_без docstring_

##### ### `def test_small_disk_wins(self)`

_без docstring_

##### ### `def test_none_disk_becomes_zero(self)`

_без docstring_

##### ### `def test_zero_with_none_disk(self)`

_без docstring_

#### `class RunPrefillTests(unittest.TestCase)`

##### ### `def test_run_prefill_builds_cmd_from_config(self)`

_без docstring_

##### ### `def _capture_cmd(self, config_args, block_gb)`

_без docstring_

##### ### `def test_run_prefill_applies_default_block(self)`

_без docstring_

##### ### `def test_run_prefill_custom_block(self)`

_без docstring_

##### ### `def test_run_prefill_overrides_config_size(self)`

_без docstring_

##### ### `def test_run_prefill_block_zero_keeps_config_size(self)`

_без docstring_

##### ### `def test_run_prefill_block_zero_no_extra_size(self)`

_без docstring_

##### ### `def test_run_prefill_adds_numa_pinning(self)`

_без docstring_

##### ### `def test_run_prefill_without_numa_omits_cpus_allowed(self)`

_без docstring_

##### ### `def test_run_prefill_falls_back_to_psync_once(self)`

_без docstring_

##### ### `def test_run_prefill_no_infinite_fallback(self)`

_без docstring_

##### ### `def test_run_prefill_cancel_returns_none(self)`

_без docstring_

#### `class PrefillDisksTests(unittest.TestCase)`

##### ### `def test_prefill_disks_runs_all_disks(self)`

_без docstring_

##### ### `def test_prefill_disks_returns_phase_duration(self)`

_без docstring_

##### ### `def _progress_total(self, block_gb, disk_bytes)`

_без docstring_

##### ### `def test_progress_total_capped_by_block(self)`

_без docstring_

##### ### `def test_progress_total_full_disk_when_block_zero(self)`

_без docstring_

#### `class ExtractStatusTests(unittest.TestCase)`

##### ### `def test_pretty_multiline_status(self)`

_без docstring_

##### ### `def test_two_objects_concatenated(self)`

_без docstring_

##### ### `def test_partial_status_kept_for_next_chunk(self)`

_без docstring_

##### ### `def test_mixed_text_around_status(self)`

_без docstring_

##### ### `def test_braces_inside_strings_ignored(self)`

_без docstring_

##### ### `def test_no_braces_returns_empty(self)`

_без docstring_

#### `class FioStreamTests(unittest.TestCase)`

##### ### `def _make_proc(self)`

_без docstring_

##### ### `def test_feeds_progress_from_pretty_json_and_succeeds(self)`

_без docstring_

##### ### `def test_stall_when_statuses_show_zero_bytes(self)`

_без docstring_

##### ### `def test_stall_when_no_output_at_all(self)`

_без docstring_

##### ### `def test_cancel_returns_none(self)`

_без docstring_

### Константы/переменные модуля

- `PROJECT_ROOT`

- `DISK`

- `PRETTY_STATUS`


## tests/test_process.py

### Классы

#### `class KillProcessTreeTests(unittest.TestCase)`

##### ### `def test_kills_process_group_with_sigterm_by_default(self)`

_без docstring_

##### ### `def test_kills_with_custom_signal(self)`

_без docstring_

##### ### `def test_falls_back_to_proc_kill(self)`

_без docstring_

#### `class RunProcessTests(unittest.TestCase)`

##### ### `def test_returns_stdout_and_stderr(self)`

_без docstring_

##### ### `def test_cancel_kills_and_returns_none(self)`

_без docstring_

##### ### `def test_file_not_found_propagates(self)`

_без docstring_

##### ### `def test_run_error_kills_and_returns_none(self)`

_без docstring_

### Константы/переменные модуля

- `PROJECT_ROOT`


## tests/test_reporter.py

### Функции модуля

### `def make_diag_store(samples)`

_без docstring_

### `def make_results(include_diag)`

_без docstring_

### Классы

#### `class RenderSamplerTablesTests(unittest.TestCase)`

##### ### `def test_renders_per_second_rows(self)`

_без docstring_

##### ### `def test_unknown_disk_renders_nothing(self)`

_без docstring_

##### ### `def test_none_values_rendered_as_dash(self)`

_без docstring_

#### `class RenderSourceNotesTests(unittest.TestCase)`

##### ### `def test_notes_when_sources_missing(self)`

_без docstring_

##### ### `def test_no_notes_when_all_sources_available(self)`

_без docstring_

##### ### `def test_notes_from_diag_notes_are_rendered(self)`

_без docstring_

##### ### `def test_notes_deduplicated_across_tests(self)`

_без docstring_

##### ### `def test_note_when_p99_unreliable(self)`

_без docstring_

##### ### `def test_no_note_when_p99_reliable(self)`

_без docstring_

#### `class GenerateReportDiagStoreTests(unittest.TestCase)`

##### ### `def test_report_contains_sampler_tables_with_diag_store(self)`

_без docstring_

##### ### `def test_report_without_diag_store_has_no_diag_sections(self)`

_без docstring_

##### ### `def test_report_survives_wall_s_float_in_results(self)`

_без docstring_

##### ### `def test_report_survives_string_test_values(self)`

_без docstring_

#### `class GenerateReportRunInfoTests(unittest.TestCase)`

##### ### `def test_report_contains_run_info_command_and_flags(self)`

_без docstring_

##### ### `def test_report_without_run_info_has_no_params_section(self)`

_без docstring_

#### `class GenerateReportTestPlansTests(unittest.TestCase)`

##### ### `def test_report_contains_actual_test_params(self)`

_без docstring_

##### ### `def test_report_without_test_plans_has_no_config_section(self)`

_без docstring_

#### `class GenerateReportLatP99Tests(unittest.TestCase)`

##### ### `def test_lat_p99_column_shown_only_when_requested(self)`

_без docstring_

##### ### `def test_lat_p99_column_hidden_by_default(self)`

_без docstring_

##### ### `def test_unreliable_p99_rendered_as_dash_and_noted(self)`

_без docstring_

#### `class RenderSamplerTablesRampTests(unittest.TestCase)`

##### ### `def test_ramp_rows_without_load_skipped_when_fio_source(self)`

_без docstring_

##### ### `def test_empty_rows_kept_when_no_fio_load_source(self)`

_без docstring_

#### `class RenderSummaryTests(unittest.TestCase)`

##### ### `def test_summary_renders_status_and_metric_per_disk(self)`

_без docstring_

##### ### `def test_summary_error_test_shown_as_fail(self)`

_без docstring_

#### `class GenerateReportShowTmaxTests(unittest.TestCase)`

##### ### `def test_plain_report_has_tmax_column_but_no_monitoring_sections(self)`

_без docstring_

##### ### `def test_plain_report_tmax_dash_without_diag_data(self)`

_без docstring_

##### ### `def test_plain_report_without_tmax_flag_has_no_tmax_column(self)`

_без docstring_

### Константы/переменные модуля

- `PROJECT_ROOT`

- `DISK`

- `TEST_NAMES`

- `SAMPLE`


## tests/test_scanner.py

### Классы

#### `class LinkGenerationTests(unittest.TestCase)`

##### ### `def test_generation_mapping(self)`

_без docstring_

#### `class FindNvmeLinkDirTests(unittest.TestCase)`

Тесты поиска линк-файлов в sysfs (включая сценарий Intel VMD).

##### ### `def _patch_sys_root(self, sys_dir)`

Перенаправляет обращения к /sys/... на временный каталог.

##### ### `def test_found_via_class_nvme_when_block_device_path_is_dead_end(self)`

VMD: /sys/block/<name>/device ведёт в тупик без линк-файлов, но /sys/class/nvme/<nvmeN>/device указывает на реальную PCI-функцию.

##### ### `def test_walk_up_finds_link_files_in_parent(self)`

Линк-файлы лежат уровнем выше каталога, в который ведёт device.

##### ### `def test_no_link_files_returns_none(self)`

_без docstring_

#### `class DetectInterfaceTests(unittest.TestCase)`

##### ### `def test_nvme_detected_by_name_even_without_transport(self)`

_без docstring_

##### ### `def test_nvme_detected_by_name_with_non_standard_transport(self)`

_без docstring_

##### ### `def test_nvme_detected_by_name_with_standard_transport(self)`

_без docstring_

##### ### `def test_nvme_name_takes_priority_over_sata_transport(self)`

_без docstring_

##### ### `def test_sas_detected_from_transport(self)`

_без docstring_

##### ### `def test_sata_detected_from_transport(self)`

_без docstring_

##### ### `def test_unknown_transport_falls_back_to_sata(self)`

_без docstring_

#### `class ScanDisksTests(unittest.TestCase)`

##### ### `def _make_lsblk(self, blockdevices)`

_без docstring_

##### ### `def _fake_profile(self, disk_name, tran)`

_без docstring_

##### ### `def test_system_disk_excluded_and_interfaces_classified(self)`

_без docstring_

##### ### `def test_missing_transport_field_does_not_break_detection(self)`

_без docstring_

#### `class OccupiedDetectionTests(unittest.TestCase)`

Диски с данными (разделы/ФС/монтирование) исключаются из тестов.

##### ### `def _make_lsblk(self, blockdevices)`

_без docstring_

##### ### `def test_partition_table_is_occupied(self)`

_без docstring_

##### ### `def test_filesystem_is_occupied(self)`

_без docstring_

##### ### `def test_mounted_anywhere_is_occupied(self)`

_без docstring_

##### ### `def test_blank_disk_is_not_occupied(self)`

_без docstring_

##### ### `def test_scan_disks_three_way_split(self)`

_без docstring_

#### `class LinkBandwidthTests(unittest.TestCase)`

Теоретическая пропускная способность шины (без поправок на диск).

##### ### `def test_nvme_gen4_x4(self)`

_без docstring_

##### ### `def test_nvme_gen5_x4(self)`

_без docstring_

##### ### `def test_sas_12g(self)`

_без docstring_

##### ### `def test_sata_6g(self)`

_без docstring_

##### ### `def test_no_link_returns_none(self)`

_без docstring_

##### ### `def test_estimate_ceiling_uses_bandwidth_for_nvme(self)`

_без docstring_

#### `class NvmeMaxPayloadTests(unittest.TestCase)`

Чтение MaxPayload (размер TLP PCIe) NVMe-контроллера из sysfs.

##### ### `def test_read_mpss_ok(self)`

_без docstring_

##### ### `def test_read_mpss_missing(self)`

_без docstring_

##### ### `def test_read_mpss_bad_value(self)`

_без docstring_

##### ### `def test_read_device_and_port(self)`

_без docstring_

##### ### `def test_read_device_only_when_port_unavailable(self)`

_без docstring_

##### ### `def test_no_link_dir_returns_none(self)`

_без docstring_

##### ### `def test_read_upstream_mpss(self)`

_без docstring_

##### ### `def test_read_upstream_no_parent(self)`

_без docstring_

### Константы/переменные модуля

- `PROJECT_ROOT`

- `KNOWN_INTERFACES`


## tests/test_table_renderer.py

### Функции модуля

### `def render_table(renderable, width)`

_без docstring_

### `def load_entrypoint()`

_без docstring_

### Классы

#### `class TableRendererTests(unittest.TestCase)`

##### ### `def test_single_outer_table_with_simple_box_and_section_between_disks(self)`

_без docstring_

##### ### `def test_numeric_result_columns_are_centered_under_headers(self)`

_без docstring_

##### ### `def test_content_lines_have_three_columns(self)`

_без docstring_

##### ### `def test_single_header_row_with_number_disk_and_global_title(self)`

_без docstring_

##### ### `def test_number_in_own_column_without_disk_name_prefix(self)`

_без docstring_

##### ### `def test_sub_table_headers_repeat_for_every_disk(self)`

_без docstring_

##### ### `def test_long_test_names_fold_to_several_lines(self)`

_без docstring_

##### ### `def test_renderer_uses_every_configured_test_without_fixed_test_order(self)`

_без docstring_

##### ### `def test_passport_details_are_in_disk_column(self)`

_без docstring_

##### ### `def test_statuses_appear_once_per_test_per_disk(self)`

_без docstring_

##### ### `def test_entrypoint_exposes_flat_renderer(self)`

_без docstring_

##### ### `def test_only_status_values_receive_color_styles(self)`

_без docstring_

##### ### `def test_tmax_column_always_present_lat_p99_only_in_logging(self)`

_без docstring_

##### ### `def test_tmax_value_from_diag(self)`

_без docstring_

##### ### `def test_fake_test_mode_data_renders_without_errors(self)`

_без docstring_

#### `class DiskDetailsPcieTests(unittest.TestCase)`

Поколение PCIe, пропускная способность и downgrade в колонке 'Накопитель'.

##### ### `def _nvme_downgrade_disk(self)`

_без docstring_

##### ### `def test_nvme_downgrade_renders_gen_bandwidth_and_warning(self)`

_без docstring_

##### ### `def test_nvme_no_downgrade_no_warning(self)`

_без docstring_

##### ### `def test_nvme_downgrade_unknown_max(self)`

_без docstring_

##### ### `def test_nvme_maxpayload_limited(self)`

_без docstring_

##### ### `def test_nvme_maxpayload_ok(self)`

_без docstring_

##### ### `def test_sas_bandwidth_and_downgrade(self)`

_без docstring_

##### ### `def test_no_profile_keeps_legacy_lines(self)`

_без docstring_

### Константы/переменные модуля

- `PROJECT_ROOT`

- `DISKS`

- `RESULTS`

- `TEST_NAMES`


## tests/test_tuner.py

### Классы

#### `class GovernorPathTests(unittest.TestCase)`

##### ### `def test_governor_path_exists(self)`

_без docstring_

##### ### `def test_governor_path_missing(self)`

_без docstring_

#### `class AllGovernorPathsTests(unittest.TestCase)`

##### ### `def test_returns_sorted(self)`

_без docstring_

#### `class ReadApstTests(unittest.TestCase)`

##### ### `def test_enabled(self)`

_без docstring_

##### ### `def test_disabled(self)`

_без docstring_

##### ### `def test_result_high_bit_only_is_disabled(self)`

_без docstring_

##### ### `def test_command_failure_returns_none(self)`

_без docstring_

##### ### `def test_device_open_failure_returns_none(self)`

_без docstring_

#### `class ApstSupportedTests(unittest.TestCase)`

##### ### `def test_apsta_bit_set(self)`

_без docstring_

##### ### `def test_apsta_bit_clear(self)`

_без docstring_

##### ### `def test_identify_failure_returns_none(self)`

_без docstring_

#### `class ApplyGovernorTests(unittest.TestCase)`

##### ### `def _tuner(self, disks)`

_без docstring_

##### ### `def test_no_governor_paths_exits(self)`

_без docstring_

##### ### `def test_write_and_verify_ok(self)`

_без docstring_

##### ### `def test_verify_fails_exits(self)`

_без docstring_

##### ### `def test_write_oserror_exits(self)`

_без docstring_

##### ### `def test_multiple_cpus_all_verified(self)`

_без docstring_

#### `class ApplyApstTests(unittest.TestCase)`

##### ### `def _tuner(self, disks)`

_без docstring_

##### ### `def test_apst_not_supported_skipped_neutral(self)`

_без docstring_

##### ### `def test_ctrl_unavailable_recorded_as_failure(self)`

_без docstring_

##### ### `def test_apst_invalid_field_treated_as_unsupported(self)`

_без docstring_

##### ### `def test_apst_already_disabled_recorded(self)`

_без docstring_

##### ### `def test_apst_disable_ok(self)`

_без docstring_

##### ### `def test_apst_set_feature_fails_reports_error(self)`

_без docstring_

##### ### `def test_apst_set_feature_ok_but_still_enabled(self)`

_без docstring_

##### ### `def test_no_nvme_disks(self)`

_без docstring_

#### `class NumaCpusTests(unittest.TestCase)`

##### ### `def test_valid_cpulist(self)`

_без docstring_

##### ### `def test_no_numa_node(self)`

_без docstring_

##### ### `def test_missing_numa_key(self)`

_без docstring_

##### ### `def test_path_not_exists(self)`

_без docstring_

##### ### `def test_unknown_disk(self)`

_без docstring_

##### ### `def test_invalid_cpulist_chars(self)`

_без docstring_

#### `class NvmeTempsTests(unittest.TestCase)`

##### ### `def test_read_temp(self)`

_без docstring_

##### ### `def test_no_hwmon(self)`

_без docstring_

#### `class PrintSummaryTests(unittest.TestCase)`

##### ### `def test_empty_applied(self)`

_без docstring_

##### ### `def test_success_and_failure(self)`

_без docstring_

#### `class ReportTests(unittest.TestCase)`

##### ### `def test_returns_applied(self)`

_без docstring_

#### `class ApplyIntegrationTests(unittest.TestCase)`

##### ### `def test_apply_calls_both(self)`

_без docstring_

### Константы/переменные модуля

- `PROJECT_ROOT`

- `TARGET_NVME`

- `TARGET_SATA`


## utils/__init__.py


## utils/diagnostics.py

Модуль диагностики производительности дисков.

Во время fio-теста в отдельном потоке сэмплирует состояние системы:
    * состояние PCIe-линка (current_link_speed/current_link_width) — просадка
      поколения или ширины под нагрузкой;
    * температуру контроллера NVMe через `nvme smart-log` (nvme-cli) —
      перегрев/троттлинг;
    * реальную нагрузку на диск: посекундные логи fio
      (write_bw_log/write_iops_log) — IOPS/МБ/с. Скорость из логов вливается
      в сэмплы после завершения теста (merge_fio_logs).

Также собирает статичную информацию: NUMA-нода диска и привязка CPU.

### Функции модуля

### `def _nvme_dev_name(disk_name)`

_без docstring_

### `def collect_static_info(disk)`

Собирает статичную диагностику диска: NUMA-нода и привязка CPU.

### `def _log_files(prefix, kind)`

Все log-файлы `*_<kind>*.log` по префиксу.  С per_job_logs=1 fio пишет по файлу на job: `<prefix>_bw.0.log` и т.п.

### `def _parse_log_file(path)`

Читает один fio-лог в {ts_sec: {ddir: max_value}}.  Формат строки: <time_msec>, <value>, <ddir>[, ...]. Внутри одного файла дубликаты таймстампа (финальный flush неполного окна) схлопываются по max.

### `def parse_fio_logs(prefix)`

Парсит bw/iops-логи fio по префиксу в посекундную нагрузку.  Возвращает {ts_sec: {"read_mbs", "write_mbs", "iops"}} либо None, если файлы не найдены или не распарсились. Значения суммируются по всем job-файлам. Лог-файлы удаляются после чтения.

### Классы

#### `class DiagnosticSampler`

Сэмплирует линк и температуру в отдельном потоке.  Нагрузку (МБ/с, IOPS) сэмплер не считает: её отдаёт сам fio своими посекундными логами (write_bw_log/write_iops_log), которые после завершения теста вливаются в сэмплы через merge_fio_logs.

##### ### `def __init__(self, disk, interval)`

_без docstring_

##### ### `def _read_link(self)`

Возвращает (gts, width) текущего линка PCIe или (None, None).

##### ### `def _read_temp(self)`

Возвращает температуру диска в °C.  Для NVMe — через `nvme smart-log`, для SAS/SATA — через `smartctl -H`. Результат кэшируется на TEMP_CACHE_SEC секунд.

##### ### `def _nvme_smart_temp(self, dev)`

Запускает `nvme smart-log <dev>` и вытаскивает temperature.

##### ### `def _smartctl_temp(self, dev)`

Запускает `smartctl -H <dev>` и вытаскивает температуру для SAS/SATA.  Формат вывода smartctl различается по версиям: - `Temperature: 31 Celsius` - `Current Drive Temperature: 31 C` - `Temperature: 31°C`

##### ### `def merge_fio_logs(self, prefix)`

Вливает посекундную нагрузку из логов fio в сэмплы.  Лог-файлы fio — единственный источник нагрузки: скорость/IOPS fio пишет сам (write_bw_log/write_iops_log). Значения из логов матчатся по таймстампу (сэмплы и лог пишутся раз в секунду).  Возвращает True, если хотя бы один сэмпл получил данные из логов.

##### ### `def _sample_once(self)`

_без docstring_

##### ### `def run(self, stop_event)`

Поток-сэмплер: опрашивает источники до установки stop_event.

##### ### `def summary(self)`

Сводит собранные сэмплы в итоговый отчёт.

### Константы/переменные модуля

- `TEMP_CACHE_SEC`


## utils/disk_filter.py

Отбор и классификация блочных устройств для тестирования.

Сканирует вывод lsblk, фильтрует системные накопители (смонтированные
на системные пути) и классифицирует остальные на «занятые» (есть данные)
и «пустые/целевые» (тестируем только их). Профиль железа для каждого
диска берётся из utils.hw_profile.

### Функции модуля

### `def _is_system_mount(mp)`

Проверяет, является ли точка монтирования системной (критической для работы ОС).

### `def _is_system_device(device)`

Рекурсивно проверяет, является ли устройство или любой из его потомков системным. Проверяет как поле 'mountpoint', так и 'mountpoints' (для разных версий lsblk).

### `def _device_has_partitions(device)`

Рекурсивно ищет таблицу разделов (child с type == 'part').

### `def _device_has_filesystem(device)`

Рекурсивно ищет файловую систему (fstype задан).

### `def _device_is_mounted_anywhere(device)`

Рекурсивно проверяет, смонтирован ли диск/потомок в любой путь.

### `def _is_occupied_device(device)`

Диск «занят» (на нём есть данные) — его нельзя трогать по умолчанию: есть таблица разделов, ФС (fstype) или смонтирован в любой путь.

### `def _find_root_mount_name(node)`

Рекурсивно ищет имя устройства с корневой ФС (/). Возвращает имя или None.

### `def scan_disks(known_interfaces)`

Сканирует систему и возвращает три списка: (system_disks, occupied_disks, target_disks).  Системные диски   — хотя бы один потомок смонтирован на системный путь (/, /boot …). Занятые диски     — есть таблица разделов, ФС (fstype) или смонтированы в любой путь; тестированию НЕ подлежат (на них есть данные). Целевые диски     — абсолютно пустые (нет разделов/ФС, не смонтированы); единственные, которые скрипт реально тестирует.  Возвращает: (system_disks, occupied_disks, target_disks)


## utils/exit_code.py

### Функции модуля

### `def _normalize(status)`

_без docstring_

### `def extract_statuses(results)`

Извлекает статусы PASS/FAIL из структуры results fio-test.py.  results — список словарей по дискам: results[disk_idx] = { test_id: {"status": "PASS"/"FAIL", ...}, "_thresholds": {...},   # служебное, игнорируется "_wall_s": ...,         # служебное, игнорируется } Служебные ключи, начинающиеся с '_', пропускаются.  Также принимает плоский список элементов, у каждого из которых есть status (dict-ключ или атрибут объекта).

### `def count_statuses(results)`

Возвращает (fails, total) по извлечённым статусам PASS/FAIL.

### `def decide_exit_code(results)`

Решает итоговый код завершения по результатам тестов.  results — структура results fio-test.py (список словарей по дискам) либо плоский список статусов/элементов с полем status.  Возвращает: 0 — все диски и все тесты PASS (fails == 0); 1 — все тесты FAIL (fails == total); 2 — есть хотя бы один FAIL, но не все (0 < fails < total). Пустой набор результатов трактуется как PASS (код 0).

### `def sys_exit(results)`

Завершает процесс с кодом, вычисленным decide_exit_code(results).

### Константы/переменные модуля

- `_PRIVATE_PREFIX`


## utils/fio_config.py

Парсер FIO-конфигов (.fio) в плоские списки аргументов fio.

Поддерживает подмножество формата fio, которое генерирует этот проект:
- секции [имя];
- строки key=value;
- голые булевы опции (например, stonewall) — эквивалент key=1;
- полнострочные комментарии ; и # (пустые строки игнорируются);
- секция [global] применяется ко всем остальным секциям: её опции идут
  первыми, а собственные опции секции могут их переопределить.

Результат: {id_секции: ["--key=value", ...]} в порядке следования секций.
Запуск fio не требуется — файл разбирается чистым Python.

### Функции модуля

### `def parse_fio_jobfile(path)`

Читает .fio-файл и возвращает {id_секции: [аргументы fio]}.  Порядок секций сохраняется. Секция [global] в результат не попадает, а раскрывается в начале списка аргументов каждой секции.

### Классы

#### `class FioConfigError(ValueError)`

Ошибка разбора .fio-файла.

_без методов_

### Константы/переменные модуля

- `SECTION_RE`

- `OPTION_RE`

- `BARE_OPTION_RE`

- `GLOBAL_SECTION`


## utils/format.py

Утилиты форматирования чисел в человекочитаемый вид для консоли и отчётов.

### Функции модуля

### `def format_bytes(n)`

Форматирует байты в человекочитаемый вид (Б/КБ/МБ/ГБ/ТБ).

### `def format_duration(sec)`

Форматирует длительность в человекочитаемый вид (часы/минуты/секунды).

### `def format_bw(bps)`

Форматирует скорость (байт/с) в человекочитаемый вид (Б/с…ГБ/с).


## utils/thresholds.py

Пороги PASS/FAIL: загрузка, валидация и выбор для конкретного диска.

Никаких формул — два декларативных JSON-файла в configs/: base_thresholds.json (общие лояльные пороги по интерфейсу и поколению линка плюс секция hdd) и disk_thresholds.json (персональные пороги моделей, ключ — модель из lsblk в нормализованном виде). Приоритет выбора: персональная запись по модели (целиком) -> hdd -> интерфейс+поколение -> нижняя строка интерфейса, если поколение неизвестно.

### Функции модуля

### `def normalize_model(model)`

Нормализует строку модели: upper-case + схлопывание пробелов.

### `def load_base_thresholds(path)`

Читает общий файл порогов -> {интерфейс: {строка: {тест: {порог}}}}.

### `def load_disk_thresholds(path)`

Читает персональные пороги -> {модель: {тест: {порог}}}. Может быть пустым.

### `def validate_base_thresholds(base)`

Валидирует общий файл. Требования: каждый интерфейс (nvme/sas/sata, опционально hdd) содержит хотя бы одну строку; каждая строка — все четыре теста с корректными значениями ({min_bw_mb|min_iops}: число > 0).

### `def validate_disk_thresholds(personal)`

Валидирует персональный файл. Записи могут быть неполными (применяются целиком, недостающее — FAIL), но каждый указанный тест обязан быть известен и содержать корректный порог.

### `def resolve_thresholds(disk, base, personal)`

Итоговые пороги диска и источник каждого порога. См. описание в разделе fio-test.py.

### Константы/переменные модуля

- `KNOWN_TESTS`
- `BASE_THRESHOLDS_PATH`
- `DISK_THRESHOLDS_PATH`


## utils/hw_profile.py

Профилирование аппаратной части диска из sysfs.

Собирает физику линка (PCIe/SAS/SATA), параметры очереди блочного
устройства, MaxPayload PCIe и NUMA-узел; вычисляет теоретические
потолки шины (используются для подбора параметров тестов).

### Функции модуля

### `def _read_queue_file(path, default)`

Читает целое число из sysfs-файла, возвращает default при ошибке.

### `def _read_queue_info(disk_name)`

Читает информацию о блоках из /sys/block/<d>/queue/.

### `def _find_link_files(start_dir)`

Ищет current_link_speed/current_link_width, поднимаясь от start_dir вверх.  Возвращает (speed_file, width_file) или None. Лимит в 8 уровней защищает от бесконечного подъёма к корню ФС.

### `def find_nvme_link_dir(disk_name)`

Находит каталог с current_link_speed/current_link_width для NVMe диска.  Под Intel VMD /sys/block/<name>/device ведёт в виртуальный каталог nvme-subsystem без линк-файлов, поэтому первым пробуется реальная PCI-функция /sys/class/nvme/<nvmeN>/device.  Возвращает Path к каталогу с линк-файлами или None.

### `def _read_nvme_link(disk_name)`

Читает текущий и максимальный линк PCIe для NVMe.

### `def _read_sas_link(disk_name)`

Читает negotiated/max linkrate для SAS диска через sas_address.

### `def _read_sata_link(disk_name)`

Читает sata_spd_limit для SATA диска через ata_link.

### `def link_generation(speed_gts)`

Сопоставляет скорость линка (GT/s) с поколением PCIe.

### `def _detect_interface(disk_name, raw_tran)`

Определяет тип интерфейса диска.  Имя устройства (nvme*) имеет приоритет — на системах с Intel VMD lsblk может отдавать в поле tran нестандартные значения (например, "pcie"), при этом диск остаётся NVMe. Если tran известен (sas/sata), используем его, иначе считаем диск SATA.

### `def collect_hw_profile(disk_name, tran)`

Собирает полный профиль железа диска из sysfs.  Возвращает dict с ключами: - interface: "nvme"/"sas"/"sata" - logical_block_size, physical_block_size, minimum_io_size, optimal_io_size - max_hw_sectors_kb, max_sectors_kb: лимиты размера одного I/O (sysfs) - rotational: 0=SSD, 1=HDD - link: dict с информацией о линке (см. ниже) - ceiling_mbps: оценочный потолок скорости в МБ/с  link для NVMe: {"gen": int, "width": int, "speed_gts": float, "max_gen": int, "max_width": int, "max_speed_gts": float, "source": "sysfs"} link для SAS: {"negotiated_gbps": float, "maximum_gbps": float, "source": "sas_phy"} link для SATA: {"spd_limit_gbps": float, "hw_spd_limit_gbps": float, "source": "ata_link"}

### `def nvme_line_rate_mbps(gts, width)`

Базовая линейная скорость NVMe без учёта кодирования/TLP (МБ/с).  (GT/s × 1000/8) × width — основа для link_bandwidth_mbps; вынесена, чтобы не дублировать формулу.

### `def link_bandwidth_mbps(interface, link)`

Теоретическая пропускная способность шины в МБ/с (без поправок на диск).  Считается напрямую из физики линка (без таблицы поколений): NVMe: current_link_speed (GT/s) × width × кодирование; SAS:  negotiated_linkrate (Gbps) — 8b/10b; SATA: sata_spd_limit (Gbps) — 8b/10b. Возвращает None, если данные линка отсутствуют.

### `def estimate_ceiling_mbps(interface, link, rotational)`

Оценивает максимальную реальную скорость диска в МБ/с.  Для NVMe/SAS совпадает с пропускной способностью шины. Для SATA дополнительно ограничивается реальным потолком флеш/механики.

### `def _read_mpss(bdf)`

MaxPayload (байты) устройства из sysfs /sys/bus/pci/devices/<bdf>/mpss.

### `def _read_upstream_max_payload(bdf)`

Best-effort: MaxPayload upstream PCIe-моста (порта) из sysfs.  Позволяет сравнить MaxPayload устройства с лимитом порта. При любой ошибке возвращает None (признак «не удалось проверить»).

### `def read_nvme_max_payload(disk_name)`

Читает MaxPayload (макс. размер TLP PCIe) NVMe-контроллера.  MaxPayload — PCIe-уровень, относится к самому контроллеру диска (не к кабелю/протоколу SATA/SAS). Возвращает {'device': int_байты, 'port': int_байты|None}. При отсутствии данных в sysfs — None.

### `def _get_numa_node(disk_name)`

Определяет NUMA-узел, на котором находится диск.  Возвращает номер NUMA-узла или None.

### Константы/переменные модуля

- `_ENC_128B130B`

- `_ENC_8B10B`


## utils/nvme_admin.py

Прямые NVMe admin-команды через ioctl ядра (без nvme-cli).

Ядро принимает admin-команды из userspace через ioctl NVME_IOCTL_ADMIN_CMD
на символьном устройстве контроллера (/dev/nvme0) — структура
nvme_passthru_cmd из include/uapi/linux/nvme_ioctl.h. Требуется root
(CAP_SYS_ADMIN). Статусы ошибок NVMe ядро маппит в errno
(INVALID_FIELD -> EINVAL и т.п.).

Сейчас слой используется в tuner.py для отключения NVMe APST; позже тем же
механизмом можно заменить `nvme smart-log` в diagnostics.py (Get Log Page).
На Windows fcntl отсутствует — модуль импортируется, но admin_cmd()
возвращает ошибку платформы; тесты идут через мок _ioctl().

### Функции модуля

### `def ctrl_device(name_or_path)`

Путь к символьному устройству контроллера для диска.  '/dev/nvme0n1' и 'nvme0c0n1' -> '/dev/nvme0'; для не-NVMe имён None.

### `def _ioctl(fd, request, cmd)`

Обёртка над fcntl.ioctl (точка мока в тестах).

### `def admin_cmd(disk_path, opcode, cdw10, cdw11, nsid, out_buf, timeout_ms)`

Выполняет NVMe admin-команду на контроллере целевого диска.  out_buf — буфер data phase (identify, get-log-page); ядро пишет ответ прямо в него. Возвращает AdminResult(ok, result, errno, error).

### Классы

#### `class _PassthruCmd(ctypes.Structure)`

struct nvme_passthru_cmd из include/uapi/linux/nvme_ioctl.h (72 байта, размер проверяется assert при импорте).

#### `class AdminResult`

Итог выполнения admin-команды: ok / result (dword из CQE) / errno / error.

### Константы/переменные модуля

- `OPC_IDENTIFY = 0x06`, `OPC_GET_FEATURES = 0x0A`, `OPC_SET_FEATURES = 0x09`

- `FID_APST = 0x0C` (фича Autonomous Power State Transition)

- `IDENTIFY_DATA_LEN = 4096`

- `NVME_IOCTL_ADMIN_CMD` — вычисляется по формуле `_IOWR('N', 0x41, sizeof(nvme_passthru_cmd))` (x86_64/aarch64: 0xC0484E41)


## utils/prefill.py

Предварительное заполнение дисков (-f).

Фаза, при которой область дисков записывается данными до тестов. По умолчанию
заполняется блок в block_gb гигабайт (CLI-флаг -b/--block), при block_gb=0 —
весь объём диска (size=100% из configs/prefill.fio).
Движок и параметры записи задаются в configs/prefill.fio (key=value -> аргументы
fio). Если движок не записывает ни байта в течение STALL_SECONDS секунд, делается
один авто-fallback на psync. Прогресс берётся из живых JSON-статусов fio
(--status-interval=1), а не из sysfs: на сервере /sys/block/<name>/stat
не считается, поэтому это единственный надёжный источник прогресса.

### Функции модуля

### `def _load_prefill_config()`

Читает configs/prefill.fio в список аргументов fio (--key=value).  Лёгкий парсер без секций: пропускает комментарии (#, ;) и пустые строки. Если файла нет или в нём нет движка, подставляется дефолт (io_uring), чтобы prefill работал даже без конфига.

### `def _extract_prefill_stats(status)`

Из JSON-статуса fio возвращает (записано_байт, скорость_байт/с).  Прогресс строится по write.io_kbytes / write.bw_bytes из jobs[0]: sysfs-счётчики на сервере не считаются, поэтому это единственный источник.

### `def _kill_tree(proc)`

Завершает процесс вместе с группой (setsid), чтобы не осталось зомби.  Обёртка над utils.process.kill_process_tree с SIGKILL: при стагне/отмене fio должен умереть немедленно, без ожидания graceful-завершения.

### `def _extract_fio_statuses(text)`

Извлекает из потока текста полные JSON-объекты fio.  fio-3.28 печатает JSON-статусы многострочными (pretty), поэтому построчный парсинг невозможен: ищем первый '{' и балансируем фигурные скобки с учётом строк ("..." и экранирования \"). Возвращает (список_объектов, остаток). Незавершённый объект на конце остаётся в остатке и ждёт следующих данных.

### `def _run_fio_stream(cmd, cancel_event, on_progress)`

Запускает fio с живым чтением JSON-статусов (--status-interval=1).  Статусы fio многострочные (pretty) — парсятся по балансу скобок через _extract_fio_statuses по мере поступления и передаются в on_progress(io_bytes, bw_bytes). Читаются оба потока (stdout+stderr): JSON может прийти в любой, а заодно stderr-пайп не переполняется.  Стагн (движок не пишет) определяется двумя путями: * 0 записанных байт при живых статусах в течение STALL_SECONDS сек; * полное отсутствие вывода в течение STALL_SECONDS сек. В обоих случаях процесс убивается и возвращается "stall". Возвращает: True при успехе, "stall" при стагне, None при отмене/ошибке, False если fio не запустился.

### `def run_prefill(disk_info, cancel_event, tuner, on_progress, block_gb)`

Запускает предварительное заполнение диска по configs/prefill.fio.  Команда собирается из конфига + служебных аргументов fio (JSON-статусы, имя задачи). При block_gb > 0 область заполнения ограничивается --size={block_gb}G (конфиговый size= отбрасывается); при block_gb == 0 заполняется весь диск (size=100% из конфига). Если движок не пишет (стагн) или fio не запускается, делается один fallback на psync. Возвращает True при успехе, False при ошибке, None при отмене.

### `def _disk_total_bytes(name)`

Полный объём диска в байтах из /sys/block/<name>/size.

### `def _resolve_size_bytes(block_gb, disk_bytes)`

Размер заполняемой области в байтах.  block_gb == 0 → весь диск (disk_bytes); иначе — min(disk_bytes, N * 2**30).

### `def prefill_disks(disks, tuner, cancel_event, block_gb)`

Предварительно заполняет все диски параллельно.  Заполняются все переданные диски принудительно (флаг -f означает всегда полный префилл). Область заполнения — block_gb гигабайт на диск (при block_gb=0 — весь объём диска). Прогресс — Rich-бар на диск (проценты, объём, скорость, секундомер, ETA); данные приходят из живых статусов fio через коллбеки run_prefill. Возвращает длительность этапа в секундах.

### `def sys_exit(code)`

Отдельная функция-обёртка для sys.exit (удобно тестировать).

### Классы

#### `class _BytesColumn(ProgressColumn)`

Колонка прогрессбара: записано из общего объёма диска.

##### ### `def render(self, task)`

_без docstring_

### Константы/переменные модуля

- `console`

- `CONFIG_PATH`

- `DEFAULT_IOENGINE`

- `FALLBACK_IOENGINE`

- `STALL_SECONDS`


## utils/process.py

Утилиты управления дочерними процессами (запуск, завершение).

Единая точка для работы с процессами fio: Popen с группами (setsid) и
завершение всей группы, чтобы не оставались зомби после отмены/стагна.

### Функции модуля

### `def kill_process_tree(proc, sig)`

Завершает процесс вместе с его группой (setsid), чтобы не осталось зомби.  Сначала сигнал всей группе процессов, при неудаче — процессу напрямую.

### `def run_process(cmd, cancel_event)`

Запускает процесс в отдельной группе и ждёт завершения.  Возвращает (proc, stdout, stderr) либо None при отмене или ошибке запуска. FileNotFoundError пробрасывается наверх для точной диагностики.

### Константы/переменные модуля

- `SIGKILL`


## utils/reporter.py

Генерация MD-отчёта с результатами тестирования.

Создаёт Markdown-файл с таблицами, удобный для чтения и публикации.

### Функции модуля

### `def _strip_rich(text)`

Удаляет rich-разметку [tag]...[/tag] из строки.

### `def _render_source_notes(disk_results, test_names)`

Заметки о недоступных источниках сэмплера.  Основной источник заметок — res["diag"]["notes"] (собирает run_fio_test: отсутствие nvme-cli, нечитаемый линк и т.п.). Для старых отчётов/тестовых данных есть откат на res["diag"]["sources"].

### `def _render_sampler_tables(diag_store, disk_name, test_names)`

Строит посекундные таблицы сэмплера для диска (из diag_store).

### `def _render_summary(disks, results, test_names)`

Сводная таблица: статус и ключевая метрика по каждому тесту и диску.  Для последовательных тестов — скорость (МБ/с), для случайных — IOPS. Позволяет оценить все диски одним взглядом.

### `def _render_test_plans(test_plans)`

Секция фактических параметров тестов (вместо сырых .fio-шаблонов).

### `def generate_report(disks, results, test_names, output_path, diag_store, tuner_report, run_info, test_plans, show_lat_p99, show_tmax)`

Генерирует MD-файл с таблицей результатов.  Параметры: disks         — список словарей с данными дисков results       — список словарей с результатами тестов (по одному на диск) test_names    — порядок и отображаемые имена тестов (по умолчанию TEST_NAMES) output_path   — путь для выходного файла (по умолчанию fio_report_<timestamp>.md) diag_store    — диагностические данные {диск: {тест: {"samples", "summary"}}}; при наличии добавляет посекундные таблицы сэмплера tuner_report  — список применённых настроек тюнера (из SystemTuner.report()) run_info      — метаданные запуска {"command": str, "flags": [(label, value), ...]} test_plans    — фактические параметры тестов {диск: {интерфейс, ceiling_mbps, max_sectors_kb, target_iops, tests: {тест: {bs/iodepth/numjobs}}}}; при наличии добавляет секцию «Фактические параметры тестов» show_lat_p99  — добавлять колонку Lat P99 в таблицу результатов show_tmax     — добавлять колонку Tmax (°C) в таблицу результатов (в обычном режиме, без посекундного мониторинга)  Возвращает: Path к созданному файлу

### Константы/переменные модуля

- `TEST_NAMES`


## utils/table_renderer.py

Построение плоской консольной таблицы с результатами FIO.

Внешняя таблица — три колонки (№, Накопитель, результаты тестов)
с единой шапкой и тонкими линиями (box.ROUNDED). Строки дисков
разделяются горизонтальными линиями (add_section), результаты
собраны во вложенную таблицу с собственным заголовком, переносом
колонок и данными, отцентрированными под заголовками.

### Функции модуля

### `def _result_headers()`

Колонки вложенной таблицы с учётом текущего режима отображения.

### `def format_status(status)`

Возвращает статус с цветовой разметкой; неизвестные значения — как есть.

### `def _disk_link_lines(profile)`

Возвращает строки линка/поколения PCIe для паспорта накопителя.  Добавляет поколение интерфейса, теоретическую пропускную способность шины, MaxPayload (только NVMe — PCIe-уровень) и статус downgrade. Ключевое слово 'downgrade' пишется ВСЕГДА, далее через ':' — есть/нет/?. Для SAS/SATA MaxPayload неприменим (шина PCIe относится к HBA-контроллеру, а не к самому диску).

### `def _disk_details(disk)`

Возвращает строки паспорта накопителя.

### `def _fmt(value, spec)`

Форматирует число; строки (например, 'test') пропускает как есть.

### `def _test_rows(disk_results, test_names)`

Преобразует результаты тестов в строки вложенной таблицы.

### `def _results_cell(disk_results, test_names)`

Строит вложенную таблицу результатов с собственным заголовком.

### `def build_results_table(disks, results, test_names, show_lat_p99)`

Строит одну непрерывную таблицу для всех накопителей.

### Константы/переменные модуля

- `TITLE`

- `SHOW_LAT_P99`

- `BASE_RESULT_HEADERS`

- `RESULT_HEADERS`


## utils/tuner.py

Модуль настройки системы для максимальной производительности накопителей.

Применяет:
- CPU governor → performance (write + verify, критическая ошибка при неудаче);
- NVMe APST → отключён для целевых NVMe (best-effort).

APST управляется напрямую через ioctl ядра (utils/nvme_admin.py), без
зависимости от nvme-cli.

NUMA-привязка fio (--cpus_allowed) — через get_numa_cpus(), не через apply().

### Функции модуля

### `def _governor_path()`

Путь к scaling_governor для cpu0 или None.

### `def _all_governor_paths()`

Все scaling_governor файлы.

### `def _apst_supported(disk_path)`

Поддерживает ли контроллер APST (фича 0x0c).  Identify Controller через ioctl, байт 265 — APSTA, бит 0.  Возвращает:
    True  — APST реализован;
    False — не реализован (apsta == 0; типично для enterprise U.2/U.3/E3.S);
    None  — контроллер недоступен или ioctl завершился с ошибкой.

### `def _set_apst(disk_path, value)`

Устанавливает APST (Set Features 0x0c). value=0 — выключить.  Возвращает (ok, invalid_field, error):
        ok            — команда принята контроллером;
        invalid_field — контроллер ответил INVALID_FIELD (errno EINVAL):
                        фича не реализована;
        error         — человекочитаемый текст ошибки при неудаче.

### `def _read_apst(disk_path)`

Читает состояние APST для NVMe-диска.  Get Features 0x0c через ioctl: бит 0 значения фичи — признак включённого APST. Возвращает 'enabled'/'disabled'/None.

### Классы

#### `class SystemTuner`

Настройка системы для тестирования накопителей.

##### ### `def __init__(self, target_disks, system_disks)`

_без docstring_

##### ### `def apply(self)`

Применяет оптимизации: governor → performance, APST → off.  Governor — не критичная ошибка (тесты продолжаются, код завершения повышается до 2, см. tuner.governor_failed). APST — best-effort (ошибка только в отчёте).

##### ### `def print_summary(self)`

Выводит в консоль, что было применено.

##### ### `def report(self)`

Список применённых настроек для MD-отчёта.

##### ### `def get_numa_cpus(self, disk_name)`

CPU-маска NUMA-узла диска или None.

##### ### `def get_nvme_temps(self)`

Текущие температуры NVMe в °C: {имя_контроллера: temp}.

##### ### `def _apply_cpu_governor(self)`

_без docstring_

##### ### `def _apply_nvme_apst(self)`

Для каждого целевого NVMe: `_apst_supported` → при поддержке `_set_apst(0)`,
затем верификация чтением через `_read_apst`. INVALID_FIELD от контроллера
трактуется как «не поддерживается». Результат — записи в `self.applied`
(ключи param/after/success/target_disks/error).

### Константы/переменные модуля

- `console`

- `VALID_CPULIST_RE`

- `_APSTA_OFFSET` — байт 265 структуры Identify Controller: APSTA (Autonomous Power State Transition Attributes), бит 0 — контроллер поддерживает APST.



---

## Конфигурационные и служебные файлы (не .py)


### `configs/<interface>.fio` (nvme.fio, sas.fio, sata.fio)
FIO-конфиги по интерфейсам. Загружаются `parse_fio_jobfile()` в `INTERFACE_CONFIGS` (секции `[global]` + тесты seq_read/seq_write/rand_read/rand_write). Содержат ioengine, bs, iodepth, numjobs, direct=1, output-format=json.

### `configs/prefill.fio`
Конфиг предварительного заполнения дисков (префилл перед тестами).

### `configs/base_thresholds.json`
Общие («лояльные») пороги PASS/FAIL: секции nvme (gen3/gen4/gen5…), sas, sata, hdd; в каждой строке — все четыре теста через `min_bw_mb` или `min_iops`. Действуют для дисков без персональной записи; для NVMe строка выбирается по поколению PCIe-линка из sysfs с клампингом к доступным строкам, для остальных — первая строка секции. Загружаются в `BASE_THRESHOLDS`, выбор — `utils.thresholds.resolve_thresholds`.

### `configs/disk_thresholds.json`
Персональные пороги по моделям дисков (ключ — модель как в lsblk, нормализуется: upper-case + схлопывание пробелов). Запись применяется целиком; недостающие тесты = FAIL без порога. Имеет приоритет над общим файлом и секцией hdd. Загружаются в `PERSONAL_THRESHOLDS`; может быть пустым.

### `README.md`
Описание проекта для пользователя (руководство, примеры запуска).

### `.gitignore`
Исключения для VCS (виртуальное окружение, сырые логи, кэш).

### `tests/`
Юнит-тесты (`unittest`): по одному файлу на модуль `utils/*` плюс `test_fio_test.py` для сквозной логики `fio-test.py`. `fio-test.py` импортируется через `importlib.util.spec_from_file_location` из-за дефиса в имени.

### `reports/`
Сгенерированные MD-отчёты и подпапки (`raw/` — сырые JSON fio, `tech_specs/` — вспомогательные материалы).
