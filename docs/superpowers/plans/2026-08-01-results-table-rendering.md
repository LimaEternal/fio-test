# Results Table Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Вывести каждый накопитель отдельной таблицей с собственной шапкой, без промежутков между таблицами, и вынести представление в `utils`.

**Architecture:** Новый модуль `utils/table_renderer.py` получает уже подготовленные диски, результаты и отображаемые имена тестов. Он строит паспорт, вложенную таблицу тестов, блок одного диска и итоговую группу Rich; `fio-test.py` только вызывает публичную функцию.

**Tech Stack:** Python 3.8+, Rich, стандартный `unittest`.

## Global Constraints

- Каждый диск имеет собственную шапку `№ | Накопитель | Результаты тестирования накопителя (FIO)`.
- Блоки дисков выводятся подряд без пустой строки; промежуток задаётся одним параметром.
- Цвет применяется только к `done` и `undone`.
- Форматы значений, порядок тестов и Markdown-отчёт не меняются.

---

### Task 1: Модуль отрисовки

**Files:**
- Create: `utils/table_renderer.py`
- Create: `tests/test_table_renderer.py`

**Interfaces:**
- Consumes: `disks: list[dict]`, `results: list[dict]`, `test_names: dict[str, str]`.
- Produces: `build_results_table(disks, results, test_names, gap=0)` — Rich renderable с отдельным блоком на диск.

- [ ] **Step 1: Написать падающие тесты публичного рендера и статусов**

```python
from rich.console import Console
from utils.table_renderer import build_results_table, format_status

def render(renderable):
    console = Console(file=StringIO(), width=140, color_system=None)
    console.print(renderable)
    return console.file.getvalue()

def test_each_disk_has_own_header_and_blocks_have_no_blank_line():
    output = render(build_results_table(DISKS, RESULTS, TEST_NAMES))
    assert output.count("Результаты тестирования накопителя (FIO)") == 2
    lines = output.splitlines()
    first_bottom = next(i for i, line in enumerate(lines) if i > 0 and line.startswith("╰"))
    assert lines[first_bottom + 1].startswith("╭")

def test_only_status_values_are_styled():
    assert format_status("done").style == "bold green"
    assert format_status("undone").style == "bold red"
```

- [ ] **Step 2: Запустить тесты и подтвердить ожидаемое падение**

Run: `python -m unittest tests.test_table_renderer -v`

Expected: `ModuleNotFoundError: No module named 'utils.table_renderer'`.

- [ ] **Step 3: Реализовать минимальный модуль**

Создать функции `format_status`, `build_disk_info`, `build_test_results_table`, `build_disk_table` и `build_results_table`. Для каждой внешней таблицы использовать `box=ROUNDED`, `header_style=None`; для вложенной — рамку с видимой шапкой. Итог собирать через `Group`, а при `gap > 0` вставлять `Text("\n" * gap)` между блоками.

- [ ] **Step 4: Запустить тесты до зелёного результата**

Run: `python -m unittest tests.test_table_renderer -v`

Expected: все тесты `OK`.

- [ ] **Step 5: Проверить модуль статически**

Run: `python -m py_compile utils/table_renderer.py tests/test_table_renderer.py`

Expected: код возврата `0`, вывода нет.

### Task 2: Подключение к точке входа

**Files:**
- Modify: `fio-test.py:20-45,390-465`
- Modify: `tests/test_table_renderer.py`

**Interfaces:**
- Consumes: `utils.table_renderer.build_results_table(disks, results, TEST_NAMES, gap=0)`.
- Produces: прежний вызов `console.print(table)` без локальной логики построения таблиц.

- [ ] **Step 1: Добавить падающий тест отсутствия дублирующей реализации**

```python
def test_entrypoint_uses_shared_renderer():
    source = Path("fio-test.py").read_text(encoding="utf-8")
    assert "from utils.table_renderer import build_results_table" in source
    assert "def build_results_table(" not in source
```

- [ ] **Step 2: Запустить тест и подтвердить ожидаемое падение**

Run: `python -m unittest tests.test_table_renderer.TableRendererTests.test_entrypoint_uses_shared_renderer -v`

Expected: FAIL, потому что функция пока определена в `fio-test.py`.

- [ ] **Step 3: Подключить модуль и удалить локальную отрисовку**

Добавить импорт `from utils.table_renderer import build_results_table`, удалить импорт `Table`, локальные `_status_style` и `build_results_table`, а вызов заменить на:

```python
table = build_results_table(disks, results, TEST_NAMES, gap=0)
```

- [ ] **Step 4: Запустить весь набор тестов**

Run: `python -m unittest discover -s tests -v`

Expected: все тесты `OK`.

- [ ] **Step 5: Проверить синтаксис всего проекта**

Run: `python -m compileall -q fio-test.py configs utils tests`

Expected: код возврата `0`, вывода нет.

- [ ] **Step 6: Просмотреть текстовый рендер двух дисков**

Run: `python -m unittest tests.test_table_renderer.TableRendererTests.test_each_disk_has_own_header_and_blocks_have_no_blank_line -v`

Expected: `OK`; обе шапки присутствуют, нижняя рамка первого блока непосредственно соседствует с верхней рамкой второго.

