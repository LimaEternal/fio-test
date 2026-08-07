import signal
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.process import SIGKILL, kill_process_tree, run_process


class KillProcessTreeTests(unittest.TestCase):
    def test_kills_process_group_with_sigterm_by_default(self):
        proc = mock.Mock(pid=42)
        with mock.patch("utils.process.os.killpg", create=True) as killpg, \
             mock.patch("utils.process.os.getpgid", create=True, return_value=7):
            kill_process_tree(proc)
        killpg.assert_called_once_with(7, signal.SIGTERM)

    def test_kills_with_custom_signal(self):
        proc = mock.Mock(pid=42)
        with mock.patch("utils.process.os.killpg", create=True) as killpg, \
             mock.patch("utils.process.os.getpgid", create=True, return_value=7):
            kill_process_tree(proc, sig=SIGKILL)
        killpg.assert_called_once_with(7, SIGKILL)

    def test_falls_back_to_proc_kill(self):
        proc = mock.Mock(pid=42)
        with mock.patch("utils.process.os.killpg", create=True, side_effect=OSError), \
             mock.patch("utils.process.os.getpgid", create=True, return_value=7):
            kill_process_tree(proc)
        proc.kill.assert_called_once()


class RunProcessTests(unittest.TestCase):
    def test_returns_stdout_and_stderr(self):
        proc = mock.Mock()
        proc.poll.side_effect = [None, 0]
        proc.communicate.return_value = (b"out", b"err")
        with mock.patch("utils.process.subprocess.Popen", return_value=proc):
            result = run_process(["fio", "--x"])
        self.assertEqual(result, (proc, b"out", b"err"))

    def test_cancel_kills_and_returns_none(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        cancel = mock.Mock()
        cancel.is_set.side_effect = [False, True]
        with mock.patch("utils.process.subprocess.Popen", return_value=proc), \
             mock.patch("utils.process.kill_process_tree") as kill:
            result = run_process(["fio", "--x"], cancel_event=cancel)
        self.assertIsNone(result)
        kill.assert_called_once_with(proc)
        proc.wait.assert_called()

    def test_file_not_found_propagates(self):
        with mock.patch("utils.process.subprocess.Popen",
                        side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):
                run_process(["fio"])

    def test_run_error_kills_and_returns_none(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.wait.side_effect = RuntimeError("boom")
        with mock.patch("utils.process.subprocess.Popen", return_value=proc), \
             mock.patch("utils.process.kill_process_tree") as kill:
            result = run_process(["fio", "--x"])
        self.assertIsNone(result)
        kill.assert_called_once_with(proc)


if __name__ == "__main__":
    unittest.main()
