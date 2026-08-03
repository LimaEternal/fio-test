TESTS = [
    {
        "id": "seq_read",
        "name": "1. Послед. Чтение",
        "args": ["--rw=read", "--bs=128k", "--iodepth=128", "--numjobs=1",
                 "--runtime=30", "--time_based", "--direct=1", "--ioengine=libaio"],
    },
    {
        "id": "seq_write",
        "name": "2. Послед. Запись",
        "args": ["--rw=write", "--bs=128k", "--iodepth=128", "--numjobs=1",
                 "--runtime=30", "--time_based", "--direct=1", "--ioengine=libaio"],
    },
    {
        "id": "rand_read",
        "name": "3. Случ. Чтение 4k",
        "args": ["--rw=randread", "--bs=4k", "--iodepth=32", "--numjobs=8",
                 "--runtime=30", "--time_based", "--direct=1", "--ioengine=libaio"],
    },
    {
        "id": "rand_write",
        "name": "4. Случ. Запись 4k",
        "args": ["--rw=randwrite", "--bs=4k", "--iodepth=32", "--numjobs=8",
                 "--runtime=30", "--time_based", "--direct=1", "--ioengine=libaio"],
    },
]