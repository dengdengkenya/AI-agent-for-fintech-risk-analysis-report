"""Settings validation shared by the CLI, config files and tools."""

import unittest

from loan_analyst.settings import (DatabaseConfig, ModelConfig, Scenario, SettingsError, load_config, local_url,
                                   project_path)
from loan_analyst import PROJECT_ROOT
from loan_analyst.cli import build_parser
from tests.fixtures import temporary_directory


class SettingsTests(unittest.TestCase):
    def test_model_default_is_consistent_across_entry_points_and_example_config(self):
        expected = "qwen3:4b-instruct-2507-q4_K_M"
        self.assertEqual(ModelConfig().model, expected)
        parser = build_parser()
        for argv in (["report"], ["ask", "Question?"], ["chat"]):
            with self.subTest(argv=argv):
                self.assertEqual(parser.parse_args(argv).ollama_model, expected)
        self.assertEqual(load_config(PROJECT_ROOT / "loan_analyst.example.toml")["model"]["model"], expected)
        self.assertEqual(parser.parse_args(["report", "--ollama-model", "custom-local"]).ollama_model,
                         "custom-local")

    def test_cutoffs_are_sorted_unique_and_include_baseline(self):
        self.assertEqual(Scenario(cutoffs="740,600,740", baseline=680).cutoffs, (600, 680, 740))
        self.assertEqual(Scenario(cutoffs=[700, 600], baseline=650).cutoffs, (600, 650, 700))
        changed = Scenario(cutoffs=(600, 700), baseline=700).with_overrides(baseline=650)
        self.assertEqual(changed.cutoffs, (600, 650, 700))

    def test_invalid_scenarios_are_rejected(self):
        invalid = [
            {"cutoffs": "299,700"}, {"cutoffs": "600,"}, {"cutoffs": []}, {"cutoffs": [True]}, {"cutoffs": [700.0]},
            {"baseline": 851}, {"baseline": True}, {"cutoffs": list(range(600, 631))},
            {"start_month": "2016-13"}, {"start_month": "2016-1"}, {"start_month": "2016-01\n"},
            {"end_month": 2016}, {"start_month": "2017-01", "end_month": "2016-12"},
            {"min_outcomes": 0}, {"min_outcomes": True}, {"max_loss_rate": float("nan")},
            {"max_loss_rate": float("inf")}, {"max_loss_rate": 1.01}, {"max_loss_rate": -.1}, {"max_loss_rate": True},
            {"rejected_loss_rate": .1}, {"assume_comparable_scores": True}, {"take_up_rate": .5},
            {"assume_comparable_scores": "true", "rejected_loss_rate": .1},
            {"assume_comparable_scores": True, "rejected_loss_rate": -1},
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(SettingsError):
                Scenario(**values)
        Scenario(assume_comparable_scores=True, rejected_loss_rate=.15, take_up_rate=.25)

    def test_database_settings_are_validated_and_exclude_credentials(self):
        for values in ({"port": 0}, {"port": True}, {"query_timeout": 0}, {"host": ""},
                       {"accepted_table": "same", "rejected_table": "SAME"}, {"database": "bad\x00name"},
                       {"accepted_table": "x" * 65}):
            with self.subTest(values=values), self.assertRaises(SettingsError):
                DatabaseConfig(**values)
        self.assertEqual(DatabaseConfig(host="secret-host", user="secret-user").sources(),
                         {"database": "demo", "accepted_table": "accepted", "rejected_table": "rejected"})

    def test_only_loopback_model_endpoints_are_accepted(self):
        for url in ("http://localhost:11434", "http://127.0.0.1:11434/", "http://[::1]:11434"):
            self.assertEqual(local_url(url), url.rstrip("/"))
        for url in ("https://ollama.com", "http://example.com", "http://localhost.evil", "file:///tmp",
                    "http://user:secret@localhost:11434", "http://localhost:11434/api",
                    "http://localhost:11434?target=evil", "http://localhost:99999", "http://192.168.1.2", None):
            with self.subTest(url=url), self.assertRaises(ValueError):
                local_url(url)
        with self.assertRaises(SettingsError):
            ModelConfig(max_tool_rounds=11)
        self.assertEqual(ModelConfig(max_tool_rounds=0).max_tool_rounds, 0)

    def test_config_files_reject_passwords_and_unknown_settings(self):
        folder = temporary_directory(self, "config-test-")
        good = folder / "good.toml"
        good.write_text('[database]\nhost = "db.local"\n[scenario]\ncutoffs = [640, 700]\nmax_loss_rate = 0.08\n'
                        '[report]\nsections = ["summary", "cutoffs"]\n', encoding="utf-8")
        self.assertEqual(load_config(good)["scenario"]["cutoffs"], [640, 700])
        for text in ('[database]\npassword = "x"\n', '[database]\nmysql_password = "x"\n', '[database]\nport_x = 1\n',
                     '[secrets]\nkey = 1\n', "not toml ["):
            path = folder / "bad.toml"
            path.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(SettingsError):
                load_config(path)

    def test_paths_resolve_from_the_project_root(self):
        self.assertEqual(project_path("outputs/x"), PROJECT_ROOT / "outputs" / "x")
        self.assertEqual(project_path(PROJECT_ROOT / "y"), PROJECT_ROOT / "y")


if __name__ == "__main__":
    unittest.main()
