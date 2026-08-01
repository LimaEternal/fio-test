# Flat Results Hypertable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить отдельные и вложенные таблицы дисков одной плоской таблицей с повторяемыми строками названий колонок.

**Architecture:** `utils/table_renderer.py` строит одну внешнюю Rich `Table` без стандартной шапки. Каждый диск добавляется одной многострочной строкой: первая визуальная строка каждой ячейки содержит название колонки, последующие строки — паспорт и произвольное количество тестов.

**Tech Stack:** Python, Rich, стандартный `unittest`.

## Global Constraints

- Во всём выводе должна быть одна внешняя рамка и ни одной вложенной таблицы.
- Общая плашка или заголовок отсутствует.
- Названия всех девяти колонок повторяются перед каждым диском.
- Между дисками нет пустых строк и горизонтальных секционных линий.
- Количество результатов тестов определяется входным словарём, а не фиксированной четвёркой.
- Цвет используется только для `done` и `undone`.
- Сканирование, запуск FIO и Markdown-отчёт не изменяются.

---

### Task 1: Плоский динамический рендерер

**Files:**
- Modify: `utils/table_renderer.py`
- Modify: `tests/test_table_renderer.py`
- Modify: `fio-test.py`

**Interfaces:**
- Consumes: `build_results_table(disks, results, test_names)`.
- Produces: одна Rich `Table`; порядок тестов берётся из ключей `test_names`, присутствующих в результатах, с сохранением порядка отображаемых имён.

- [ ] **Step 1: Заменить ожидания тестов на поведение плоской таблицы**

Добавить проверки текстового рендера: одна верхняя и нижняя граница, `Профиль теста` встречается по одному разу на диск, между последним результатом первого диска и шапкой второго нет пустой строки или рамки. Добавить пятый тест в fixture и проверить его присутствие, чтобы рендер не зависел от четырёх фиксированных ID.

- [ ] **Step 2: Запустить тесты и подтвердить ожидаемое падение**

Run: `python -m unittest tests.test_table_renderer -v`

Expected: FAIL — текущий рендер создаёт отдельную верхнюю и нижнюю рамку для каждого диска и вложенную таблицу результатов.

- [ ] **Step 3: Реализовать одну таблицу из многострочных ячеек**

Удалить `build_test_results_table`, `build_disk_table`, `Group` и параметр `gap`. Добавить функции построения простых и статусных многострочных `Text`; создать одну `Table(show_header=False, box=ROUNDED, expand=True)`. На каждый диск добавлять одну строку из девяти ячеек, где первая строка — название колонки, затем данные.

- [ ] **Step 4: Обновить вызов точки входа**

```python
table = build_results_table(disks, results, TEST_NAMES)
```

- [ ] **Step 5: Запустить полный набор тестов**

Run: `python -m unittest discover -s tests -v`

Expected: все тесты `OK`.

- [ ] **Step 6: Проверить синтаксис и фактический рендер**

Run: `python -m compileall -q fio-test.py configs utils tests`

Expected: код возврата `0`. Затем отрендерить fixture из трёх дисков при ширине 150 символов и визуально подтвердить отсутствие вложенных рамок, горизонтальных секций и промежутков.

- [ ] **Step 7: Закоммитить, слить в main и отправить**

```bash
git add fio-test.py utils/table_renderer.py tests/test_table_renderer.py docs/superpowers/plans/2026-08-01-results-table-rendering.md
git commit -m "fix: сделать таблицу результатов плоской"
git merge --ff-only codex/flat-results-table
git push origin main
```
