import unittest
from jobagent.sources.base import _validate_safe_url


class TestSSRFGuard(unittest.TestCase):
    def test_ssrf_url_validation(self):
        # 正常合规的 HTTP / HTTPS 请求
        _validate_safe_url("https://www.zhipin.com/job/123")
        _validate_safe_url("http://api.example.com/v1/jobs")

        # 尝试利用 file:// 协议进行本地文件任意读取
        with self.assertRaises(ValueError) as exc:
            _validate_safe_url("file:///etc/passwd")
        self.assertIn("只允许 http/https 协议请求", str(exc.exception))

        # 尝试利用 ftp:// 等其他非常规协议
        with self.assertRaises(ValueError) as exc2:
            _validate_safe_url("ftp://internal-server/data")
        self.assertIn("只允许 http/https 协议请求", str(exc2.exception))

        # 缺少主机名
        with self.assertRaises(ValueError) as exc3:
            _validate_safe_url("https://")
        self.assertIn("缺少有效主机名", str(exc3.exception))


if __name__ == "__main__":
    unittest.main()
