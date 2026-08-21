"""Прямые NVMe admin-команды через ioctl ядра (без nvme-cli).

Ядро принимает admin-команды из userspace через ioctl NVME_IOCTL_ADMIN_CMD
на символьном устройстве контроллера (/dev/nvme0) — структура
nvme_passthru_cmd из include/uapi/linux/nvme_ioctl.h. Требуется root
(CAP_SYS_ADMIN). Статусы ошибок NVMe ядро маппит в errno
(INVALID_FIELD -> EINVAL и т.п.).

Сейчас слой используется в tuner.py для отключения NVMe APST; позже тем же
механизмом можно заменить `nvme smart-log` в diagnostics.py (Get Log Page).
"""

import ctypes
import os
import re
from dataclasses import dataclass
from typing import Optional

try:
    import fcntl
except ImportError:
    # Windows: ioctl-слой не используется, тесты идут через мок _ioctl().
    fcntl = None

# Opcodes из Admin Command Set (NVMe base spec)
OPC_IDENTIFY = 0x06
OPC_GET_FEATURES = 0x0A
OPC_SET_FEATURES = 0x09

FID_APST = 0x0C

IDENTIFY_DATA_LEN = 4096


class _PassthruCmd(ctypes.Structure):
    """struct nvme_passthru_cmd из include/uapi/linux/nvme_ioctl.h."""

    _fields_ = [
        ("opcode", ctypes.c_uint8),
        ("flags", ctypes.c_uint8),
        ("rsvd", ctypes.c_uint16),
        ("nsid", ctypes.c_uint32),
        ("cdw2", ctypes.c_uint32),
        ("cdw3", ctypes.c_uint32),
        ("metadata", ctypes.c_uint32),
        ("addr", ctypes.c_uint64),
        ("metadata_len", ctypes.c_uint32),
        ("data_len", ctypes.c_uint32),
        ("cdw10", ctypes.c_uint32),
        ("cdw11", ctypes.c_uint32),
        ("cdw12", ctypes.c_uint32),
        ("cdw13", ctypes.c_uint32),
        ("cdw14", ctypes.c_uint32),
        ("cdw15", ctypes.c_uint32),
        ("timeout_ms", ctypes.c_uint32),
        ("result", ctypes.c_uint32),
    ]


assert ctypes.sizeof(_PassthruCmd) == 72


def _ioc(direction: int, cmd_type: int, nr: int, size: int) -> int:
    """Код ioctl по формуле _IOC() из asm-generic/ioctl.h (x86_64/aarch64)."""
    return (direction << 30) | (size << 16) | (cmd_type << 8) | nr


_IOC_WRITE = 1
_IOC_READ = 2

NVME_IOCTL_ADMIN_CMD = _ioc(
    _IOC_WRITE | _IOC_READ, ord("N"), 0x41, ctypes.sizeof(_PassthruCmd)
)


@dataclass
class AdminResult:
    """Итог выполнения admin-команды."""

    ok: bool
    result: int            # dword из completion queue (result field)
    errno: Optional[int]   # errno при ошибке, иначе None
    error: str             # человекочитаемое описание ошибки


def ctrl_device(name_or_path: str) -> Optional[str]:
    """Путь к символьному устройству контроллера для диска.

    '/dev/nvme0n1' и 'nvme0c0n1' -> '/dev/nvme0'; для не-NVMe имён None.
    """
    m = re.search(r"nvme(\d+)", name_or_path)
    if not m:
        return None
    return "/dev/nvme{}".format(m.group(1))


def _ioctl(fd: int, request: int, cmd: "_PassthruCmd") -> int:
    """Обёртка над fcntl.ioctl (точка мока в тестах)."""
    return fcntl.ioctl(fd, request, cmd, True)


def admin_cmd(
    disk_path: str,
    opcode: int,
    cdw10: int = 0,
    cdw11: int = 0,
    nsid: int = 0,
    out_buf: Optional[bytearray] = None,
    timeout_ms: int = 5000,
) -> AdminResult:
    """Выполняет NVMe admin-команду на контроллере целевого диска.

    out_buf — буфер data phase (identify, get-log-page); ядро пишет ответ
    прямо в него.
    """
    dev = ctrl_device(disk_path)
    if dev is None:
        return AdminResult(
            False, 0, None,
            "не удалось определить контроллер NVMe для {!r}".format(disk_path),
        )
    if fcntl is None:
        return AdminResult(False, 0, None,
                           "ioctl недоступен на этой платформе")

    cmd = _PassthruCmd()
    cmd.opcode = opcode & 0xFF
    cmd.nsid = nsid & 0xFFFFFFFF
    cmd.cdw10 = cdw10 & 0xFFFFFFFF
    cmd.cdw11 = cdw11 & 0xFFFFFFFF
    cmd.timeout_ms = timeout_ms

    buf_view = None
    if out_buf is not None:
        buf_view = (ctypes.c_char * len(out_buf)).from_buffer(out_buf)
        cmd.addr = ctypes.addressof(buf_view)
        cmd.data_len = len(out_buf)

    try:
        fd = os.open(dev, os.O_RDWR)
    except OSError as exc:
        return AdminResult(False, 0, exc.errno,
                           "{}: {}".format(dev, exc.strerror or exc))

    try:
        ret = _ioctl(fd, NVME_IOCTL_ADMIN_CMD, cmd)
    except OSError as exc:
        return AdminResult(False, 0, exc.errno,
                           "{}: {}".format(dev, exc.strerror or exc))
    finally:
        os.close(fd)

    if ret != 0:
        return AdminResult(False, 0, None,
                           "{}: неожиданный код возврата ioctl {}".format(dev, ret))
    return AdminResult(True, cmd.result, None, "")
