"""
Предварительное заполнение дисков (-f).

Фаза, при которой весь объём дисков записывается данными до тестов.
Движок и параметры записи задаются в configs/prefill.fio (key=value -> аргументы
fio). Если движок не записывает ни байта в течение STALL_SECONDS секунд, делается
один авто-fallback на psync. Прогресс берётся из живых JSON-статусов fio
(--status-interval=1), а не из sysfs: на сервере /sys/block/<name>/stat
не считается, поэтому это единственный надёжный источник прогресса.
"""

import concurrent.futures
import json
import os
import select
import subprocess
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.text import Text

from utils.format import format_bw, format_bytes, format_duration
from utils.process import SIGKILL, kill_process_tree

console = Console(color_system=None, highlight=False)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "configs" / "prefill.fio"
DEFAULT_IOENGINE = "io_uring"
FALLBACK_IOENGINE = "psync"
STALL_SECONDS = 8


def _load_prefill_config():
    """Читает configs/prefill.fio в список аргументов fio (--key=value).

    Лёгкий парсер без секций: пропускает комментарии (#, ;) и пустые строки.
    Если файла нет или в нём нет движка, подставляется дефолт (io_uring),
    чтобы prefill работал даже без конфига.
    """
    lines = []
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        lines.append(f"--{key}={value}")
    if not any(arg.startswith("--ioengine=") for arg in lines):
        lines.append(f"--ioengine={DEFAULT_IOENGINE}")
    return lines


def _extract_prefill_stats(status: dict):
    """Из JSON-статуса fio возвращает (записано_байт, скорость_байт/с).

    Прогресс строится по write.io_kbytes / write.bw_bytes из jobs[0]:
    sysfs-счётчики на сервере не считаются, поэтому это единственный источник.
    """
    try:
        job = status["jobs"][0]
        write = job.get("write") or {}
    except (KeyError, IndexError, TypeError):
        return 0, 0
    io_kbytes = write.get("io_kbytes") or 0
    bw_bytes = write.get("bw_bytes") or 0
    return int(io_kbytes) * 1024, int(bw_bytes)


def _kill_tree(proc) -> None:
    """Завершает процесс вместе с группой (setsid), чтобы не осталось зомби.

    Обёртка над utils.process.kill_process_tree с SIGKILL: при стагне/отмене
    fio должен умереть немедленно, без ожидания graceful-завершения.
    """
    kill_process_tree(proc, sig=SIGKILL)


def _extract_fio_statuses(text):
    """Извлекает из потока текста полные JSON-объекты fio.

    fio-3.28 печатает JSON-статусы многострочными (pretty), поэтому построчный
    парсинг невозможен: ищем первый '{' и балансируем фигурные скобки с учётом
    строк ("..." и экранирования \\"). Возвращает (список_объектов, остаток).
    Незавершённый объект на конце остаётся в остатке и ждёт следующих данных.
    """
    statuses = []
    while True:
        start = text.find("{")
        if start == -1:
            break
        depth = 0
        in_str = False
        escaped = False
        end = -1
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            break
        chunk = text[start:end + 1]
        text = text[end + 1:]
        try:
            statuses.append(json.loads(chunk))
        except ValueError:
            continue
    return statuses, text


def _run_fio_stream(cmd, cancel_event=None, on_progress=None):
    """Запускает fio с живым чтением JSON-статусов (--status-interval=1).

    Статусы fio многострочные (pretty) — парсятся по балансу скобок через
    _extract_fio_statuses по мере поступления и передаются в
    on_progress(io_bytes, bw_bytes). Читаются оба потока (stdout+stderr):
    JSON может прийти в любой, а заодно stderr-пайп не переполняется.

    Стагн (движок не пишет) определяется двумя путями:
      * 0 записанных байт при живых статусах в течение STALL_SECONDS сек;
      * полное отсутствие вывода в течение STALL_SECONDS сек.
    В обоих случаях процесс убивается и возвращается "stall".
    Возвращает: True при успехе, "stall" при стагне, None при отмене/ошибке,
    False если fio не запустился.
    """
    try:
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
        if hasattr(os, "setsid"):
            kwargs["preexec_fn"] = os.setsid
        proc = subprocess.Popen(cmd, **kwargs)
    except (OSError, FileNotFoundError):
        return False

    buf = ""
    stdout_eof = False
    stall_deadline = None
    max_io_bytes = 0
    last_output = time.monotonic()
    exit_code = None
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _kill_tree(proc)
                return None
            now = time.monotonic()
            if stall_deadline is not None and now >= stall_deadline:
                _kill_tree(proc)
                return "stall"
            if max_io_bytes == 0 and now - last_output >= STALL_SECONDS:
                _kill_tree(proc)
                return "stall"

            ready, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.5)
            for stream in ready:
                try:
                    chunk = os.read(stream.fileno(), 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    if stream is proc.stdout:
                        stdout_eof = True
                    continue
                last_output = time.monotonic()
                buf += chunk.decode("utf-8", "replace")

            statuses, buf = _extract_fio_statuses(buf)
            for status in statuses:
                io_bytes, bw_bytes = _extract_prefill_stats(status)
                if io_bytes > max_io_bytes:
                    max_io_bytes = io_bytes
                    stall_deadline = None
                elif io_bytes == 0 and stall_deadline is None:
                    stall_deadline = time.monotonic() + STALL_SECONDS
                if on_progress is not None:
                    on_progress(io_bytes, bw_bytes)

            if stdout_eof:
                if not buf.strip() and proc.poll() is not None:
                    exit_code = proc.wait()
                    break
            elif proc.poll() is not None:
                exit_code = proc.wait()
                break
    except Exception:
        _kill_tree(proc)
        return None

    if exit_code is None:
        return None
    return exit_code == 0


def run_prefill(disk_info, cancel_event=None, tuner=None, on_progress=None):
    """Запускает предварительное заполнение диска по configs/prefill.fio.

    Команда собирается из конфига + служебных аргументов fio (JSON-статусы,
    имя задачи). Если движок не пишет (стагн) или fio не запускается, делается
    один fallback на psync. Возвращает True при успехе, False при ошибке,
    None при отмене.
    """
    disk_path = disk_info["path"]
    config_args = _load_prefill_config()

    def build(engine_override=None):
        cmd = [
            "fio",
            "--name=prefill",
            "--filename", disk_path,
            "--output-format=json",
            "--status-interval=1",
        ]
        for arg in config_args:
            if engine_override and arg.startswith("--ioengine="):
                continue
            cmd.append(arg)
        if engine_override:
            cmd.append(f"--ioengine={engine_override}")
        if tuner:
            numa_cpus = tuner.get_numa_cpus(disk_info["name"])
            if numa_cpus:
                cmd.extend(["--cpus_allowed", numa_cpus])
        return cmd

    def run_once(cmd):
        result = _run_fio_stream(cmd, cancel_event, on_progress)
        if result is None:
            return None
        if result is False:
            return False
        if result == "stall":
            return "stall"
        return True

    outcome = run_once(build())
    if outcome == "stall" or outcome is False:
        console.print(
            f"[yellow]Движок из {CONFIG_PATH.name} не пишет на "
            f"/dev/{disk_info['name']} — переключаюсь на "
            f"{FALLBACK_IOENGINE}.[/yellow]"
        )
        outcome = run_once(build(FALLBACK_IOENGINE))
        if outcome == "stall":
            return False
    return outcome


def _disk_total_bytes(name: str):
    """Полный объём диска в байтах из /sys/block/<name>/size."""
    try:
        sectors = int(
            Path(f"/sys/block/{name}/size").read_text(encoding="utf-8").strip()
        )
        return sectors * 512
    except Exception:
        return None


class _BytesColumn(ProgressColumn):
    """Колонка прогрессбара: записано из общего объёма диска."""

    def render(self, task):
        return Text(
            f"({format_bytes(task.completed)} из {format_bytes(task.total)})"
        )


def prefill_disks(disks, tuner=None, cancel_event=None):
    """Принудительно предварительно заполняет все диски параллельно.

    Заполняются все переданные диски принудительно (флаг -f означает всегда
    полный префилл). Прогресс — Rich-бар на диск (проценты, объём, скорость,
    секундомер, ETA); данные приходят из живых статусов fio через коллбеки
    run_prefill.
    Возвращает длительность этапа в секундах.
    """
    phase_start = time.monotonic()

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(disks))
    future_map = {}
    starts = {}
    finished = []
    update_lock = threading.Lock()
    try:
        with Progress(
            TextColumn("[bold]{task.description}[/bold]"),
            BarColumn(),
            TextColumn("{task.percentage:>3.0f}%"),
            _BytesColumn(),
            TextColumn("{task.fields[speed]}"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            task_ids = {}
            for d in disks:
                name = d["name"]
                total = _disk_total_bytes(name) or 1
                task_ids[name] = progress.add_task(
                    f"Заполнение /dev/{name}", total=total, completed=0, speed=""
                )
                starts[name] = time.monotonic()

                def on_progress(io_bytes, bw_bytes, _name=name, _total=total):
                    try:
                        with update_lock:
                            progress.update(
                                task_ids[_name],
                                completed=min(_total, io_bytes),
                                speed=format_bw(bw_bytes),
                            )
                    except Exception:
                        pass

                future_map[
                    pool.submit(
                        run_prefill,
                        d,
                        cancel_event=cancel_event,
                        tuner=tuner,
                        on_progress=on_progress,
                    )
                ] = d

            pending = set(future_map)
            while pending:
                for fut in list(pending):
                    if not fut.done():
                        continue
                    pending.discard(fut)
                    d = future_map[fut]
                    name = d["name"]
                    dur = time.monotonic() - starts[name]
                    try:
                        ok = fut.result()
                    except Exception as exc:
                        finished.append((d, False, dur, str(exc)))
                    else:
                        finished.append((d, ok, dur))
                if pending:
                    time.sleep(0.2)
    except KeyboardInterrupt:
        if cancel_event:
            cancel_event.set()
        console.print("\n[bold yellow]Прервано пользователем.[/bold yellow]")
        sys_exit(130)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    phase_dur = time.monotonic() - phase_start
    for d, ok, dur, *err in finished:
        if ok:
            console.print(
                f"  [green]Готово[/green] /dev/{d['name']} "
                f"(за {format_duration(dur)})"
            )
        else:
            msg = f"  [red]Ошибка[/red] /dev/{d['name']}"
            if err and err[0]:
                msg += f": {err[0]}"
            console.print(msg)
    console.print(f"[bold]Предзаполнение заняло {format_duration(phase_dur)}[/bold]")
    return phase_dur


def sys_exit(code):
    """Отдельная функция-обёртка для sys.exit (удобно тестировать)."""
    import sys

    sys.exit(code)
