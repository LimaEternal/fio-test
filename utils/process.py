"""Утилиты управления дочерними процессами (запуск, завершение).

Единая точка для работы с процессами fio: Popen с группами (setsid) и
завершение всей группы, чтобы не оставались зомби после отмены/стагна.
"""

import os
import signal
import subprocess

# SIGKILL отсутствует на Windows — используем числовой код 9.
SIGKILL = getattr(signal, "SIGKILL", 9)


def kill_process_tree(proc, sig=signal.SIGTERM) -> None:
    """Завершает процесс вместе с его группой (setsid), чтобы не осталось зомби.

    Сначала сигнал всей группе процессов, при неудаче — процессу напрямую.
    """
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_process(cmd, cancel_event=None):
    """Запускает процесс в отдельной группе и ждёт завершения.

    Возвращает (proc, stdout, stderr) либо None при отмене или ошибке запуска.
    FileNotFoundError пробрасывается наверх для точной диагностики.
    """
    proc = None
    try:
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
        if hasattr(os, "setsid"):
            kwargs["preexec_fn"] = os.setsid
        proc = subprocess.Popen(cmd, **kwargs)
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                kill_process_tree(proc)
                proc.wait()
                return None
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                continue
        stdout, stderr = proc.communicate()
        return proc, stdout, stderr
    except FileNotFoundError:
        raise
    except Exception:
        if proc and proc.poll() is None:
            kill_process_tree(proc)
        return None
