"""
Генерация MD-отчёта с результатами тестирования.

Создаёт Markdown-файл с таблицами, удобный для чтения и публикации.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

from utils.format import format_duration
from utils.table_renderer import _fmt


def _strip_rich(text: str) -> str:
    """Удаляет rich-разметку [tag]...[/tag] из строки."""
    return re.sub(r"\[.*?\]", "", text)


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

        if res.get("lat_p99_unreliable"):
            note = (f"перцентили задержек по {test_names.get(test_id, test_id)} "
                    "недостоверны (clat p99 на порядки выше среднего — мусор от fio), "
                    "в таблицах показано '—'; сырой JSON в reports/raw/")
            if note not in seen:
                seen.add(note)
                out.append(f"> {note}")

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

        # Когда нагрузка приходит из fio-логов, строки без неё — это ramp-период
        # (fio ещё не пишет лог) и секунды до старта теста. Их пропускаем, чтобы
        # таблица начиналась с реальной нагрузки. Если логов нет вовсе
        # (load_source не "fio"), показываем все строки: там только линк/температура.
        load_from_fio = (entry.get("summary") or {}).get("load_source") == "fio"

        lines.append(f"**Сэмплы линка/температуры/нагрузки — {test_name}**")
        lines.append("")
        lines.append("| Сек | Линк | t°C | Чтение МБ/с | Запись МБ/с | IOPS |")
        lines.append("|-----|------|-----|-------------|-------------|------|")
        for i, s in enumerate(samples, 1):
            if load_from_fio and s.get("read_mbs") is None and s.get("write_mbs") is None:
                continue
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


def _render_summary(disks: List[dict], results: List[dict], test_names: dict) -> List[str]:
    """Сводная таблица: статус и ключевая метрика по каждому тесту и диску.

    Для последовательных тестов — скорость (МБ/с), для случайных — IOPS.
    Позволяет оценить все диски одним взглядом.
    """
    lines = ["## Сводка", ""]

    def metric(test_id, res):
        if test_id.startswith("seq_"):
            bw = res.get("bw_mb")
            if not isinstance(bw, (int, float)) or not bw:
                return ""
            return f"{_fmt(bw, '.0f')} МБ/с"
        if test_id.startswith("rand_"):
            iops = res.get("iops")
            if not isinstance(iops, (int, float)) or not iops:
                return ""
            if iops >= 1_000_000:
                return f"{_fmt(iops / 1e6, ',.2f')}M IOPS"
            return f"{_fmt(iops / 1e3, ',.0f')}k IOPS"
        return ""

    header = ["Диск", "Модель"] + [test_names.get(t, t) for t in test_names]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))

    for disk, disk_results in zip(disks, results):
        row = [f"/dev/{disk['name']}", disk["model"]]
        for test_id in test_names:
            res = (disk_results or {}).get(test_id, {})
            if "error" in res:
                row.append("FAIL")
                continue
            status = res.get("status", "FAIL")
            m = metric(test_id, res)
            row.append(f"{status} {m}" if m else status)
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")
    return lines


def _render_test_plans(test_plans: dict) -> List[str]:
    """Секция фактических параметров тестов (вместо сырых .fio-шаблонов)."""
    lines = ["## Фактические параметры тестов", ""]
    lines.append(
        "> Глобальные опции (ioengine, direct, runtime) задаются шаблонами "
        "`configs/<interface>.fio`; ниже — параметры, с которыми реально "
        "прошли тесты."
    )
    lines.append("")
    for disk_name, info in test_plans.items():
        header = f"**/dev/{disk_name}**"
        details = []
        ceiling = info.get("ceiling_mbps")
        if ceiling:
            details.append(f"потолок шины {ceiling:.0f} МБ/с")
        max_sectors = info.get("max_sectors_kb")
        if max_sectors:
            details.append(f"лимит I/O ядра {max_sectors}k")
        if info.get("target_iops"):
            details.append(f"target {info['target_iops']} IOPS/поток")
        if details:
            header += " — " + ", ".join(details)
        lines.append(header)
        lines.append("")
        lines.append("| Тест | Блок | iodepth | numjobs |")
        lines.append("|------|------|---------|---------|")
        tests = info.get("tests") or {}
        if not tests:
            lines.append("| — | — | — | — |")
        else:
            for test_id, params in tests.items():
                lines.append(
                    f"| {TEST_NAMES.get(test_id, test_id)} | "
                    f"{params.get('bs', '—')} | {params.get('iodepth', '—')} | "
                    f"{params.get('numjobs', '—')} |"
                )
        lines.append("")

        # Пороги PASS/FAIL (динамические из sysfs для seq + конфиг для rand).
        thr = info.get("thresholds") or {}
        src = info.get("threshold_source") or {}
        if thr:
            lines.append("**Пороги PASS/FAIL:**")
            lines.append("")
            lines.append("| Тест | Порог | Источник |")
            lines.append("|------|--------|----------|")
            for test_id in tests:
                t = thr.get(test_id)
                if not t:
                    continue
                if "min_bw_mb" in t:
                    pct = f"{t['min_bw_mb']:.0f} МБ/с"
                elif "min_iops" in t:
                    pct = f"{t['min_iops']:,} IOPS"
                else:
                    pct = "—"
                lines.append(
                    f"| {TEST_NAMES.get(test_id, test_id)} | {pct} | "
                    f"{src.get(test_id, '—')} |"
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
    test_plans: Optional[dict] = None,
    show_lat_p99: bool = False,
    show_tmax: bool = False,
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
        test_plans    — фактические параметры тестов {диск: {интерфейс, ceiling_mbps,
                        max_sectors_kb, target_iops, tests: {тест: {bs/iodepth/numjobs}}}};
                        при наличии добавляет секцию «Фактические параметры тестов»
        show_lat_p99  — добавлять колонку Lat P99 в таблицу результатов
        show_tmax     — добавлять колонку Tmax (°C) в таблицу результатов
                        (в обычном режиме, без посекундного мониторинга)

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

        # Мониторинг-секции (расширенная таблица дисков, «Мониторинг»,
        # посекундные сэмплы, заметки) показываются только в подробном режиме
        # (-l), когда diag_store передаётся; в обычном режиме res["diag"] тоже
        # заполняется (для колонки Tmax), но посекундных данных в отчёте нет.
        has_diag = diag_store is not None and (
            any(d.get("diag_static") for d in disks)
            or any(
                res.get("diag")
                for disk_results in results
                for res in disk_results.values()
                if isinstance(res, dict)
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
            lines.append("| Диск | Модель | Серийный номер | Интерфейс | Объём | Link | Блок (физ/лог) | Потолок (МБ/с) | NUMA | CPU-аффинити |")
            lines.append("|------|--------|----------------|-----------|-------|------|----------------|----------------|------|--------------|")
        else:
            lines.append("| Диск | Модель | Серийный номер | Интерфейс | Объём |")
            lines.append("|------|--------|----------------|-----------|-------|")

        for d in disks:
            profile = d.get("profile") or {}
            interface = profile.get("interface", d.get("tran", "N/A").upper())
            link = profile.get("link")
            phys_block = profile.get("physical_block_size", "—")
            log_block = profile.get("logical_block_size", "—")
            ceiling = profile.get("ceiling_mbps")

            link_str = ""
            if interface == "nvme" and link:
                gen = link.get("gen")
                width = link.get("width")
                max_gen = link.get("max_gen")
                if gen and width:
                    link_str = f"PCIe {gen} x{width}"
                    if max_gen:
                        link_str += f" (max: {max_gen})"
            elif interface == "sas" and link:
                neg = link.get("negotiated_gbps")
                max_l = link.get("maximum_gbps")
                if neg:
                    link_str = f"{neg} Gbps"
                    if max_l:
                        link_str += f" / {max_l} Gbps"
            elif interface == "sata" and link:
                spd = link.get("spd_limit_gbps")
                hw_spd = link.get("hw_spd_limit_gbps")
                if spd:
                    link_str = f"{spd} Gbps"
                    if hw_spd and hw_spd != spd:
                        link_str += f" (hw: {hw_spd})"

            block_str = f"{phys_block}/{log_block}" if phys_block != "—" and log_block != "—" else "—"
            ceiling_str = f"{ceiling:.0f}" if ceiling else "—"

            pcie_str = ""
            if d.get("pcie_info") and d["pcie_info"].get("gen"):
                pcie_str = f" (PCIe Gen{d['pcie_info']['gen']} x{d['pcie_info'].get('width', '?')})"

            if has_diag:
                static = d.get("diag_static") or {}
                numa = static.get("numa_node") or "—"
                affinity = static.get("cpu_affinity") or "—"
                lines.append(
                    f"| /dev/{d['name']} | {d['model']} | {d['serial']} "
                    f"| {interface}{pcie_str} | {d['size']} | {link_str or '—'} | {block_str} | {ceiling_str} | {numa} | {affinity} |"
                )
            else:
                lines.append(
                    f"| /dev/{d['name']} | {d['model']} | {d['serial']} "
                    f"| {interface}{pcie_str} | {d['size']} |"
                )

        lines.append("")
        if test_plans:
            lines.extend(_render_test_plans(test_plans))

        lines.append("## Результаты тестирования")
        lines.append("")

        for idx, (disk, disk_results) in enumerate(zip(disks, results), 1):
            header = f"### {idx}. /dev/{disk['name']} ({disk['model']})"
            wall = disk_results.get("_wall_s") if disk_results else None
            if wall:
                header += f" — тесты: {format_duration(wall)}"
            lines.append(header)
            lines.append("")
            if show_lat_p99:
                lines.append(
                    "| Профиль теста | Блок | IOPS | Скорость (МБ/с) | Lat Avg (мс) | Lat P99 (мс) | Статус |"
                )
                lines.append(
                    "|---------------|------|------|-----------------|--------------|--------------|--------|"
                )
            elif show_tmax:
                lines.append(
                    "| Профиль теста | Блок | IOPS | Скорость (МБ/с) | Lat Avg (мс) | Tmax (°C) | Статус |"
                )
                lines.append(
                    "|---------------|------|------|-----------------|--------------|-----------|--------|"
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
                    if show_lat_p99 or show_tmax:
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
                        if res.get("lat_p99_unreliable"):
                            cells.append("—")
                        else:
                            cells.append(_fmt(res.get("lat_p99", 0), ".2f"))
                    elif show_tmax:
                        diag = res.get("diag") or {}
                        if diag.get("temp_max_c") is not None:
                            cells.append(_fmt(diag["temp_max_c"], ".1f"))
                        else:
                            cells.append("—")
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

                    if res.get("lat_p99_unreliable"):
                        clat = "—"
                    else:
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

        lines.extend(_render_summary(disks, results, test_names))

        lines.append("---")
        lines.append("*Отчёт сгенерирован автоматически*")

        output_path.write_text("\n".join(lines), encoding="utf-8")

        return output_path
    except PermissionError:
        raise RuntimeError(f"Отказано в доступе при попытке записи отчёта по пути '{output_path}'.")
    except Exception as e:
        raise RuntimeError(f"Ошибка при создании файла отчёта '{output_path}': {e}")
