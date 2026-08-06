"""
Генерация MD-отчёта с результатами тестирования.

Создаёт Markdown-файл с таблицами, удобный для чтения и публикации.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

from utils.table_renderer import _fmt


def _strip_rich(text: str) -> str:
    """Удаляет rich-разметку [tag]...[/tag] из строки."""
    return re.sub(r"\[.*?\]", "", text)


def _fmt_duration(sec) -> str:
    """Форматирует длительность в человекочитаемый вид (часы/минуты/секунды)."""
    sec = int(round(float(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} ч {m:02d} мин"
    if m:
        return f"{m} мин {s:02d} с"
    return f"{s} с"


TEST_NAMES = {
    "seq_read": "1. Послед. Чтение",
    "seq_write": "2. Послед. Запись",
    "rand_read": "3. Случ. Чтение 4k",
    "rand_write": "4. Случ. Запись 4k",
}


def _render_source_notes(disk_results: dict, test_names: dict) -> List[str]:
    """Заметки о недоступных источниках сэмплера.

    Основной источник заметок — res["diag"]["notes"] (собирает run_fio_test:
    отсутствие nvme-cli, нечитаемый линк и т.п.). Для старых отчётов/тестовых
    данных есть откат на res["diag"]["sources"].
    """
    seen = set()
    out: List[str] = []
    for test_id in test_names:
        res = disk_results.get(test_id) or {}
        diag = res.get("diag") or {}

        for note in diag.get("notes") or []:
            if note not in seen:
                seen.add(note)
                out.append(f"> {note}")

        sources = diag.get("sources")
        if sources and not diag.get("notes"):
            notes = []
            if not sources.get("link"):
                notes.append("линк PCIe не удалось прочитать")
            if not sources.get("temp"):
                notes.append("температура недоступна (нет hwmon)")
            for note in notes:
                if note not in seen:
                    seen.add(note)
                    out.append(f"> {note}")
    if out:
        out.append("")
    return out


def _render_sampler_tables(diag_store: Optional[dict], disk_name: str, test_names: dict) -> List[str]:
    """Строит посекундные таблицы сэмплера для диска (из diag_store)."""
    lines: List[str] = []
    store = (diag_store or {}).get(disk_name)
    if not store:
        return lines

    for test_id, test_name in test_names.items():
        entry = store.get(test_id)
        if not entry:
            continue
        samples = entry.get("samples") or []
        if not samples:
            continue

        lines.append(f"**Сэмплы линка/температуры/нагрузки — {test_name}**")
        lines.append("")
        lines.append("| Сек | Линк | t°C | Чтение МБ/с | Запись МБ/с | IOPS |")
        lines.append("|-----|------|-----|-------------|-------------|------|")
        for i, s in enumerate(samples, 1):
            link = "—"
            if s.get("gts") is not None:
                link = f"{s['gts']:g} GT/s x{s.get('width') or '?'}"
            temp = _fmt(s.get("temp"), ".1f") if s.get("temp") is not None else "—"
            read_mbs = _fmt(s.get("read_mbs"), ".1f") if s.get("read_mbs") is not None else "—"
            write_mbs = _fmt(s.get("write_mbs"), ".1f") if s.get("write_mbs") is not None else "—"
            iops = _fmt(s.get("iops"), ",.0f") if s.get("iops") is not None else "—"
            lines.append(
                f"| {i} | {link} | {temp} | {read_mbs} | {write_mbs} | {iops} |"
            )
        lines.append("")

    return lines


def generate_report(
    disks: List[dict],
    results: List[dict],
    test_names: Optional[dict] = None,
    output_path: Optional[Union[str, Path]] = None,
    diag_store: Optional[dict] = None,
    tuner_report: Optional[List[dict]] = None,
    run_info: Optional[dict] = None,
    fio_configs: Optional[dict] = None,
    show_lat_p99: bool = False,
) -> Path:
    """
    Генерирует MD-файл с таблицей результатов.

    Параметры:
        disks         — список словарей с данными дисков
        results       — список словарей с результатами тестов (по одному на диск)
        test_names    — порядок и отображаемые имена тестов (по умолчанию TEST_NAMES)
        output_path   — путь для выходного файла (по умолчанию fio_report_<timestamp>.md)
        diag_store    — диагностические данные {диск: {тест: {"samples", "summary"}}};
                        при наличии добавляет посекундные таблицы сэмплера
        tuner_report  — список применённых настроек тюнера (из SystemTuner.report())
        run_info      — метаданные запуска {"command": str, "flags": [(label, value), ...]}
        fio_configs   — {интерфейс: сырое содержимое .fio-файла} для секции конфигов
        show_lat_p99  — добавлять колонку Lat P99 в таблицу результатов

    Возвращает:
        Path к созданному файлу
    """
    if test_names is None:
        test_names = TEST_NAMES
    try:
        if output_path is None:
            reports_dir = Path("reports")
            reports_dir.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            output_path = reports_dir / f"fio_report_{timestamp}.md"
        else:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)

        lines = []

        lines.append("# Результаты тестирования накопителей (FIO)")
        lines.append("")
        lines.append(f"> Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")

        if run_info:
            lines.append("## Параметры запуска")
            lines.append("")
            command = run_info.get("command")
            if command:
                lines.append("Команда:")
                lines.append("")
                lines.append("```bash")
                lines.append(command)
                lines.append("```")
                lines.append("")
            flags = run_info.get("flags") or []
            if flags:
                lines.append("| Параметр | Значение |")
                lines.append("|----------|----------|")
                for label, value in flags:
                    lines.append(f"| {label} | {value} |")
                lines.append("")

        has_diag = (
            any(d.get("diag_static") for d in disks)
            or any(
                res.get("diag")
                for disk_results in results
                for res in disk_results.values()
            )
        )

        if tuner_report:
            lines.append("## Системные настройки")
            lines.append("")
            lines.append("| Параметр | Было | Стало | Статус |")
            lines.append("|----------|------|-------|--------|")
            for item in tuner_report:
                if "target_disks" in item and "success" not in item:
                    if item.get("skipped_reason"):
                        status = f"пропущено ({item['skipped_reason']})"
                    else:
                        status = "будет применено"
                    param = f"{item['param']} ({item['target_disks']})"
                else:
                    status = "✓" if item.get("success") else f"✗ {item.get('error', '')}"
                    param = item["param"]
                lines.append(
                    f"| {param} | {item['before']} | {item['after']} | {status} |"
                )
            lines.append("")

        lines.append("## Обнаруженные диски")
        lines.append("")
        if has_diag:
            lines.append("| Диск | Модель | Серийный номер | Интерфейс | Объём | NUMA | CPU-аффинити |")
            lines.append("|------|--------|----------------|-----------|-------|------|--------------|")
        else:
            lines.append("| Диск | Модель | Серийный номер | Интерфейс | Объём |")
            lines.append("|------|--------|----------------|-----------|-------|")

        for d in disks:
            pcie_str = ""
            if d.get("pcie_info") and d["pcie_info"].get("gen"):
                pcie_str = f" (PCIe Gen{d['pcie_info']['gen']} x{d['pcie_info'].get('width', '?')})"

            if has_diag:
                static = d.get("diag_static") or {}
                numa = static.get("numa_node") or "—"
                affinity = static.get("cpu_affinity") or "—"
                lines.append(
                    f"| /dev/{d['name']} | {d['model']} | {d['serial']} "
                    f"| {d['tran'].upper()}{pcie_str} | {d['size']} | {numa} | {affinity} |"
                )
            else:
                lines.append(
                    f"| /dev/{d['name']} | {d['model']} | {d['serial']} "
                    f"| {d['tran'].upper()}{pcie_str} | {d['size']} |"
                )

        lines.append("")
        if fio_configs:
            lines.append("## Конфигурация тестов (fio)")
            lines.append("")
            for name, content in fio_configs.items():
                lines.append(f"Использован файл: `configs/{name}.fio`")
                lines.append("")
                lines.append("```ini")
                lines.extend(content.splitlines())
                lines.append("```")
                lines.append("")

        lines.append("## Результаты тестирования")
        lines.append("")

        for idx, (disk, disk_results) in enumerate(zip(disks, results), 1):
            header = f"### {idx}. /dev/{disk['name']} ({disk['model']})"
            wall = disk_results.get("_wall_s") if disk_results else None
            if wall:
                header += f" — тесты: {_fmt_duration(wall)}"
            lines.append(header)
            lines.append("")
            if show_lat_p99:
                lines.append(
                    "| Профиль теста | Блок | IOPS | Скорость (МБ/с) | Lat Avg (мс) | Lat P99 (мс) | Статус |"
                )
                lines.append(
                    "|---------------|------|------|-----------------|--------------|--------------|--------|"
                )
            else:
                lines.append(
                    "| Профиль теста | Блок | IOPS | Скорость (МБ/с) | Lat Avg (мс) | Статус |"
                )
                lines.append(
                    "|---------------|------|------|-----------------|--------------|--------|"
                )

            for test_id, test_name in test_names.items():
                res = disk_results.get(test_id, {})

                if "error" in res:
                    cells = [test_name, res.get("bs", "—"), "—", "—", "—"]
                    if show_lat_p99:
                        cells.append("—")
                    cells.append("FAIL")
                else:
                    cells = [
                        test_name,
                        res.get("bs", "4k"),
                        _fmt(res.get("iops", 0), ",.0f"),
                        _fmt(res.get("bw_mb", 0), ".1f"),
                        _fmt(res.get("lat_avg", 0), ".2f"),
                    ]
                    if show_lat_p99:
                        cells.append(_fmt(res.get("lat_p99", 0), ".2f"))
                    cells.append(res.get("status", "FAIL"))
                lines.append("| " + " | ".join(cells) + " |")

            lines.append("")

            if has_diag:
                lines.append(f"**Мониторинг — {disk['name']}**")
                lines.append("")
                lines.append(
                    "| Тест | CPU user/sys (%) | Линк min | Tmax (°C) | "
                    "clat p99/p99.9 (мс) | slat (мс) | iodepth | Объём (ГБ) |"
                )
                lines.append(
                    "|------|------------------|----------|-----------|"
                    "--------------------|-----------|---------|-------------|"
                )
                for test_id, test_name in test_names.items():
                    res = disk_results.get(test_id, {})
                    if "error" in res:
                        continue
                    diag = res.get("diag") or {}

                    cpu = f"{res.get('cpu_user', '—')} / {res.get('cpu_sys', '—')}"

                    link = "—"
                    if diag.get("link_gts_min") is not None:
                        width = diag.get("link_width_min") or "?"
                        link = f"{diag['link_gts_min']:g} GT/s x{width}"

                    tmax = _fmt(diag["temp_max_c"], ".1f") if diag.get("temp_max_c") is not None else "—"

                    p99 = res.get("clat_p99_ms", "—")
                    p999 = res.get("clat_p99_9_ms", "—")
                    clat = f"{p99} / {p999}" if p99 != "—" else "—"

                    iod = res.get("iodepth", "—")
                    io_kb = res.get("io_kb")
                    gb = _fmt(io_kb / 1e6, ".1f") if io_kb else "—"

                    lines.append(
                        f"| {test_name} | {cpu} | {link} | {tmax} | "
                        f"{clat} | {res.get('slat_avg_ms', '—')} | {iod} | {gb} |"
                    )
                lines.append("")
                lines.extend(_render_source_notes(disk_results, test_names))
                lines.extend(_render_sampler_tables(diag_store, disk["name"], test_names))

        lines.append("---")
        lines.append("*Отчёт сгенерирован автоматически*")

        output_path.write_text("\n".join(lines), encoding="utf-8")

        return output_path
    except PermissionError:
        raise RuntimeError(f"Отказано в доступе при попытке записи отчёта по пути '{output_path}'.")
    except Exception as e:
        raise RuntimeError(f"Ошибка при создании файла отчёта '{output_path}': {e}")
