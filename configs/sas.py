"""
FIO-конфиги для SAS накопителей.

SAS — серверный интерфейс с хорошей поддержкой очередей.
Случайный доступ тестируется в 2 потока (QD32 x 2 = QD64).
Последовательные тесты используют bs=64k.
"""

TESTS = [
    {
        "id": "seq_read",
        "name": "1. Послед. Чтение",
        "args": ["--rw=read", "--bs=64k", "--iodepth=32", "--numjobs=1",
                 "--runtime=30", "--time_based", "--fsync=1"],
    },
    {
        "id": "seq_write",
        "name": "2. Послед. Запись",
        "args": ["--rw=write", "--bs=64k", "--iodepth=32", "--numjobs=1",
                 "--runtime=30", "--time_based", "--fsync=1"],
    },
    {
        "id": "rand_read",
        "name": "3. Случ. Чтение 4k",
        "args": ["--rw=randread", "--bs=4k", "--iodepth=32", "--numjobs=2",
                 "--runtime=30", "--time_based", "--fsync=1"],
    },
    {
        "id": "rand_write",
        "name": "4. Случ. Запись 4k",
        "args": ["--rw=randwrite", "--bs=4k", "--iodepth=32", "--numjobs=2",
                 "--runtime=30", "--time_based", "--fsync=1"],
    },
]

DESCRIPTION = (
    "SAS — серверный интерфейс. "
    "Случайный доступ: 2 потока (QD64) — сбалансированный профиль. "
    "Последовательный: bs=64k."
)

THRESHOLDS = {
    "seq_read":  {"min_bw_mb": 800},
    "seq_write": {"min_bw_mb": 600},
    "rand_read": {"min_iops": 30000},
    "rand_write": {"min_iops": 25000},
}
