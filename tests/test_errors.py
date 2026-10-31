import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from paperbot.errors import ConfigError, SafeError, diagnose, external_error, public_error


SECRET = "sk-private-provider-secret"


class DiagnosticTests(unittest.TestCase):
    def test_unknown_exception_never_exposes_message_or_response(self):
        error = ValueError(f"request https://example.test/?key={SECRET}; body={SECRET}")
        error.response = SimpleNamespace(text=SECRET, status_code=401)
        report = diagnose(error, "模型筛选")
        self.assertEqual(report["type"], "ValueError")
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn("example.test", public_error(report))
        self.assertIn("[Paper Bot]", public_error(report))
        self.assertIn(report["id"], public_error(report))

    def test_status_is_preserved_without_reading_body(self):
        for attribute, value in [("status_code", 401), ("status", 403),
                                 ("response", SimpleNamespace(status_code=404)),
                                 ("status", 429), ("status", 503)]:
            with self.subTest(attribute=attribute, value=value):
                error = RuntimeError(SECRET)
                setattr(error, attribute, value)
                safe = external_error(error, "模型")
                report = diagnose(safe, "模型筛选")
                expected = value.status_code if attribute == "response" else value
                self.assertIn(str(expected), report["message"])
                self.assertTrue(report["hint"])
                self.assertNotIn(SECRET, json.dumps(report))

    def test_provider_response_body_is_never_inspected(self):
        class Response:
            status_code = 401

            @property
            def text(self):
                raise AssertionError("Response body must not be read")

        error = ValueError(SECRET)
        error.response = Response()
        report = diagnose(external_error(error, "模型"), "模型筛选")
        self.assertIn("HTTP 401", report["message"])
        self.assertNotIn(SECRET, json.dumps(report))

    def test_external_timeout_connection_and_unknown_have_actionable_hints(self):
        for error, needle in [(TimeoutError(SECRET), "超时"),
                              (ConnectionError(SECRET), "连接"),
                              (ValueError(SECRET), "失败")]:
            report = diagnose(external_error(error, "QQ"), "消息投递")
            self.assertIn(needle, report["message"])
            self.assertNotIn(SECRET, json.dumps(report))
            self.assertTrue(report["hint"])

    def test_safe_configuration_field_and_message_are_visible(self):
        report = diagnose(ConfigError("阈值须为 0–100 的整数", field="topics[2].threshold"),
                          "配置校验", context="主题索引 2")
        rendered = public_error(report)
        self.assertIn("topics[2].threshold", rendered)
        self.assertIn("0–100", rendered)
        self.assertIn("配置校验", rendered)
        self.assertEqual(report["context"], "主题索引 2")

    def test_trace_only_contains_project_locations_without_source(self):
        project_file = Path(__file__).resolve().parents[1] / "paperbot" / "service.py"
        inner = compile("def foreign_secret_function():\n raise ValueError('" + SECRET + "')\nforeign_secret_function()",
                        "/private/provider-secret-file.py", "exec")
        wrapper = compile("def run():\n exec(inner)\nrun()", str(project_file), "exec")
        try:
            exec(wrapper, {"inner": inner})
        except Exception as error:
            report = diagnose(error, "模型筛选")
        encoded = json.dumps(report)
        self.assertIn("service.py:run:2", report["trace"])
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn("foreign_secret_function", encoded)
        self.assertNotIn("provider-secret-file", encoded)
        self.assertNotIn("raise ValueError", encoded)

    def test_malicious_exception_type_and_metadata_cannot_pollute_diagnostics(self):
        error_type = type("Secret\n" + SECRET, (Exception,), {})
        error = error_type(SECRET)
        error.status_code = SECRET
        error.response = SimpleNamespace(status_code=SECRET, text=SECRET)
        report = diagnose(error, "模型筛选")
        self.assertEqual(report["type"], "Exception")
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn(SECRET, str(external_error(error, "模型")))

    def test_reports_are_bounded_and_ids_are_distinct(self):
        first = diagnose(SafeError("安全说明" * 5000), "抓取", context="页码 1")
        second = diagnose(ValueError(SECRET), "抓取")
        self.assertNotEqual(first["id"], second["id"])
        self.assertLessEqual(len(first["message"]), 400)
        self.assertLessEqual(len(public_error(first)), 1200)


if __name__ == "__main__":
    unittest.main()
