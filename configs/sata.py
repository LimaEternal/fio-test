"""
FIO-конфиги для SATA накопителей.

SATA ограничен протоколом AHCI: максимум QD32, многопоточность вредит.
Случайный доступ строго в 1 поток.
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
        "args": ["--rw=randread", "--bs=4k", "--iodepth=32", "--numjobs=1",
                 "--runtime=30", "--time_based", "--fsync=1"],
    },
    {
        "id": "rand_write",
        "name": "4. Случ. Запись 4k",
        "args": ["--rw=randwrite", "--bs=4k", "--iodepth=32", "--numjobs=1",
                 "--runtime=30", "--time_based", "--fsync=1"],
    },
]

DESCRIPTION = (
    "SATA — потребительский интерфейс (AHCI). "
    "Случайный доступ: строго 1 поток (QD32) — многопоточность вредит. "
    "Последовательный: bs=64k."
)

THRESHOLDS = {
    "seq_read":  {"min_bw_mb": 400},
    "seq_write": {"min_bw_mb": 300},
    "rand_read": {"min_iops": 10000},
    "rand_write": {"min_iops": 8000},
}
