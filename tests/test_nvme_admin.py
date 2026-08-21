import errno
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils import nvme_admin


class CtrlDeviceTests(unittest.TestCase):
    def test_namespace_path(self):
        self.assertEqual(nvme_admin.ctrl_device("/dev/nvme0n1"), "/dev/nvme0")

    def test_private_namespace_name(self):
        self.assertEqual(nvme_admin.ctrl_device("nvme0c0n1"), "/dev/nvme0")

    def test_controller_name(self):
        self.assertEqual(nvme_admin.ctrl_device("/dev/nvme3"), "/dev/nvme3")

    def test_non_nvme_returns_none(self):
        self.assertIsNone(nvme_admin.ctrl_device("/dev/sda"))


class PassthruStructTests(unittest.TestCase):
    def test_size_matches_uapi_header(self):
        self.assertEqual(nvme_admin.ctypes.sizeof(nvme_admin._PassthruCmd), 72)

    def test_ioctl_number_x86_64_encoding(self):
        self.assertEqual(nvme_admin.NVME_IOCTL_ADMIN_CMD, 0xC0484E41)


class AdminCmdTests(unittest.TestCase):
    """Тесты полного пути admin_cmd; fcntl подменяется, т.к. на Windows None."""

    def test_success_packs_fields_and_returns_result(self):
        captured = {}

        def fake_ioctl(fd, request, cmd):
            captured["fd"] = fd
            captured["request"] = request
            captured["cmd"] = cmd
            cmd.result = 0x21
            return 0

        with mock.patch("utils.nvme_admin.fcntl", mock.Mock()), \
             mock.patch("utils.nvme_admin.os.open", return_value=7), \
             mock.patch("utils.nvme_admin.os.close") as mock_close, \
             mock.patch("utils.nvme_admin._ioctl", side_effect=fake_ioctl):
            res = nvme_admin.admin_cmd(
                "/dev/nvme0n1",
                nvme_admin.OPC_GET_FEATURES,
                cdw10=nvme_admin.FID_APST,
            )

        self.assertTrue(res.ok)
        self.assertEqual(res.result, 0x21)
        self.assertIsNone(res.errno)
        self.assertEqual(res.error, "")
        self.assertEqual(captured["request"], nvme_admin.NVME_IOCTL_ADMIN_CMD)
        self.assertEqual(captured["fd"], 7)
        cmd = captured["cmd"]
        self.assertEqual(cmd.opcode, nvme_admin.OPC_GET_FEATURES)
        self.assertEqual(cmd.cdw10, nvme_admin.FID_APST)
        self.assertEqual(cmd.cdw11, 0)
        self.assertEqual(cmd.nsid, 0)
        self.assertEqual(cmd.data_len, 0)
        self.assertEqual(cmd.addr, 0)
        self.assertEqual(cmd.timeout_ms, 5000)
        mock_close.assert_called_once_with(7)

    def test_open_failure_reports_errno(self):
        err = OSError(errno.ENOENT, "No such file or directory")
        with mock.patch("utils.nvme_admin.fcntl", mock.Mock()), \
             mock.patch("utils.nvme_admin.os.open", side_effect=err), \
             mock.patch("utils.nvme_admin._ioctl") as mock_ioctl:
            res = nvme_admin.admin_cmd("/dev/nvme9n1", nvme_admin.OPC_IDENTIFY)
        self.assertFalse(res.ok)
        self.assertEqual(res.errno, errno.ENOENT)
        self.assertIn("No such file or directory", res.error)
        self.assertIn("/dev/nvme9", res.error)
        mock_ioctl.assert_not_called()

    def test_ioctl_einval_propagates_errno(self):
        err = OSError(errno.EINVAL, "Invalid argument")
        with mock.patch("utils.nvme_admin.fcntl", mock.Mock()), \
             mock.patch("utils.nvme_admin.os.open", return_value=3), \
             mock.patch("utils.nvme_admin.os.close"), \
             mock.patch("utils.nvme_admin._ioctl", side_effect=err):
            res = nvme_admin.admin_cmd(
                "/dev/nvme0n1",
                nvme_admin.OPC_SET_FEATURES,
                cdw10=nvme_admin.FID_APST,
                cdw11=0,
            )
        self.assertFalse(res.ok)
        self.assertEqual(res.errno, errno.EINVAL)
        self.assertIn("Invalid argument", res.error)

    def test_nonzero_retval_is_error(self):
        with mock.patch("utils.nvme_admin.fcntl", mock.Mock()), \
             mock.patch("utils.nvme_admin.os.open", return_value=3), \
             mock.patch("utils.nvme_admin.os.close"), \
             mock.patch("utils.nvme_admin._ioctl", return_value=-5):
            res = nvme_admin.admin_cmd("/dev/nvme0n1", nvme_admin.OPC_IDENTIFY)
        self.assertFalse(res.ok)
        self.assertIsNone(res.errno)
        self.assertIn("ioctl", res.error)

    def test_out_buf_bound_to_kernel_write(self):
        data = bytearray(4096)

        def fake_ioctl(fd, request, cmd):
            self.assertEqual(cmd.data_len, len(data))
            self.assertNotEqual(cmd.addr, 0)
            view = (nvme_admin.ctypes.c_char * len(data)).from_address(cmd.addr)
            view[265] = b"\x01"
            return 0

        with mock.patch("utils.nvme_admin.fcntl", mock.Mock()), \
             mock.patch("utils.nvme_admin.os.open", return_value=3), \
             mock.patch("utils.nvme_admin.os.close"), \
             mock.patch("utils.nvme_admin._ioctl", side_effect=fake_ioctl):
            res = nvme_admin.admin_cmd(
                "/dev/nvme0n1",
                nvme_admin.OPC_IDENTIFY,
                cdw10=1,
                out_buf=data,
            )
        self.assertTrue(res.ok)
        self.assertEqual(data[265], 1)

    def test_unknown_device_short_circuit(self):
        with mock.patch("utils.nvme_admin.os.open") as mock_open:
            res = nvme_admin.admin_cmd("/dev/sda", nvme_admin.OPC_IDENTIFY)
        self.assertFalse(res.ok)
        self.assertIsNone(res.errno)
        self.assertIn("sda", res.error)
        mock_open.assert_not_called()


class RealDeviceSmokeTest(unittest.TestCase):
    @unittest.skipUnless(os.path.exists("/dev/nvme0"),
                         "требуется Linux с /dev/nvme0")
    def test_identify_controller(self):
        buf = bytearray(nvme_admin.IDENTIFY_DATA_LEN)
        res = nvme_admin.admin_cmd(
            "/dev/nvme0", nvme_admin.OPC_IDENTIFY, cdw10=1, out_buf=buf,
        )
        if not res.ok and res.errno == errno.EACCES:
            self.skipTest("нужны root-права")
        self.assertTrue(res.ok, res.error)
        vid = buf[0] | (buf[1] << 8)
        self.assertGreaterEqual(vid, 0)


if __name__ == "__main__":
    unittest.main()
