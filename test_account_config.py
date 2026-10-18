"""The accounts config and scripts/xiuxian_accounts.py: one config, generated jobs."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from miniapp_shared import accounts
from scripts import xiuxian_accounts as cli

ROOT = Path(__file__).resolve().parent
DEMO_REGISTRY = json.loads((ROOT / "config/active_flows.json").read_text(encoding="utf-8"))


class ExampleConfigTests(unittest.TestCase):
    def test_example_config_describes_the_demo_registry(self):
        for row in accounts.ACCOUNTS:
            registered = {entry["flow"] for entry in DEMO_REGISTRY["flows"]
                          if entry["account"] == row["id"] and entry.get("enabled", True)}
            configured = {cli.REGISTRY_FLOW.get(f, f) for f in row["features"]
                          if f in cli.TEMPLATE and f != "yuanshen"}  # 第二元神 has helper jobs only
            # recipe_notice is the operator's Hermes-only listener, not an offered feature.
            self.assertEqual(registered - {"recipe_notice"}, configured, row["id"])

    def test_each_template_account_renders_back_to_its_tracked_files_and_entries(self):
        demo = {row["id"]: row for row in accounts.ACCOUNTS}
        for feature, template in cli.TEMPLATE.items():
            files, flows, helpers = cli.render_account(demo[template], feature, DEMO_REGISTRY)
            self.assertTrue(files or feature == "qingyuanzi", feature)
            for name, text in files.items():
                self.assertEqual((ROOT / "scripts" / name).read_text(encoding="utf-8"), text, name)
            self.assertEqual([e for e in DEMO_REGISTRY["flows"] if e["account"] == template
                              and e["flow"] == cli.REGISTRY_FLOW.get(feature, feature) and e.get("enabled", True)],
                             flows, feature)
            prefix = f"ai.hermes.xiuxian.{template}."
            self.assertEqual([e for e in DEMO_REGISTRY["helpers"] if e["label"].startswith(prefix)
                              and cli._token(e["label"][len(prefix):]) in (feature, cli.REGISTRY_FLOW.get(feature))],
                             helpers, feature)


class ValidationTests(unittest.TestCase):
    def rows(self, *rows):
        return accounts.validate({"accounts": list(rows)})

    def test_rejects_bad_configs(self):
        good = {"id": "tg9300000001", "username": "my_name", "features": {"fishing": True}}
        self.assertEqual("tg9300000001", self.rows(good)[0]["credential_source"])
        for bad in ({**good, "id": "123456789"}, {**good, "username": "@x"},
                    {**good, "features": {"fihsing": True}},
                    {**good, "features": {"jiaohuan_fabao": True, "jiaohuan_gongfa": True}},
                    {**good, "session": "../elsewhere.session"}):
            with self.assertRaises(ValueError, msg=bad):
                self.rows(bad)
        with self.assertRaisesRegex(ValueError, "one account"):
            self.rows({**good, "features": {"yuanshen": True}},
                      {**good, "id": "tg9300000002", "features": {"yuanshen": True}})
        with self.assertRaisesRegex(ValueError, "partner"):
            self.rows({**good, "features": {"shuangxiu_wenyang": {"partner": "tg9300000099"}}})
        with self.assertRaisesRegex(ValueError, "game.group"):
            accounts.game_settings({"game": {**accounts.GAME_DEFAULTS, "group": "@bad"}})


class GeneratedDeploymentTests(unittest.TestCase):
    def test_new_accounts_render_and_install_without_demo_identities(self):
        config = {
            "game": {"group": "real_group", "chat_id": -1009300000042, "topic_id": 42,
                     "second_chat_id": -1009300000043},
            "accounts": [
                {"id": "tg9300000001", "username": "first_cultivator", "features": {
                    "shendubiguan": True, "yuanyingchuqiao": True, "jiyin_choice": {"reply": ".献上魂魄"},
                    "jiaohuan_gongfa": True, "shuangxiu_wenyang": {"partner": "tg9300000002"},
                    "yuanshen": True, "fishing": True, "tower": True}},
                {"id": "tg9300000002", "username": "second_cultivator", "credential_source": "tg9300000003",
                 "features": {"wendao": True, "jiaohuan_fabao": True, "jiyin_choice": True, "qingyuanzi": True,
                              "fumofabao_qingzhufengyunjian_shenlei": {"interval_dispatch_seconds": 7200}}},
            ]}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "accounts.json").write_text(json.dumps(config), encoding="utf-8")
            env = {**os.environ, "XIUXIAN_ACCOUNTS_FILE": str(out / "accounts.json")}

            def run(*args):
                return subprocess.run([sys.executable, "scripts/xiuxian_accounts.py", *args, "--out", tmp],
                                      cwd=ROOT, env=env, check=True, capture_output=True, text=True).stdout

            run("render")
            rendered = json.loads((out / "config/accounts/rendered.json").read_text())
            registry = json.loads((out / "config/accounts/active_flows.json").read_text())
            for name in rendered:
                text = (out / name).read_text(encoding="utf-8")
                if name.endswith("active_flows.json"):  # shared jobs keep the demo's schedule notes
                    text = json.dumps([{k: v for k, v in e.items() if k != "schedule"}
                                       for e in registry["flows"] + registry["helpers"]])
                # Template comments may still cite the demo history; no code line may.
                code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
                self.assertIsNone(re.search(r"tg91000000\d\d|demo_player_", code), name)
            flows = {(e["account"], e["flow"]) for e in registry["flows"]}
            self.assertIn(("tg9300000001", "jiaohuan_gongfa"), flows)
            self.assertIn(("tg9300000002", "qyz_miniapp"), flows)
            wendao = next(e for e in registry["flows"] if e["flow"] == "wendao")
            self.assertEqual(("tg9300000002", "second_cultivator", "tg9300000003"),
                             (wendao["account"], wendao["business_actor"], wendao["credential_source"]))
            helpers = {e["label"] for e in registry["helpers"]}
            self.assertIn("ai.hermes.xiuxian.tg9300000001.yuanshen-xiulian", helpers)
            self.assertIn("ai.hermes.xiuxian.miniapp-fishing.send", helpers)
            self.assertNotIn("ai.hermes.xiuxian.star-observatory.send", helpers)
            jiyin = (out / "scripts/send_jiyin_choice_tg9300000002.sh").read_text()
            self.assertIn("'tg9300000002' 'tg9300000003' 'second_cultivator' '.收敛气息'", jiyin)
            self.assertIn("'.献上魂魄'", (out / "scripts/send_jiyin_choice_tg9300000001.sh").read_text())
            wendao = (out / "scripts/send_wendao_tg9300000002.sh").read_text()
            self.assertIn("source ./config/accounts/tg9300000003.env\nexport XIU_ACCOUNT_SLUG='tg9300000002'", wendao)
            self.assertIn("XIU_GAME_TARGET='@real_group'", (out / "config/accounts/game.env").read_text())

            run("install", "--systemd", "--dry-run", "--dest", str(out / "units"))
            units = {path.name for path in (out / "units").iterdir()}
            self.assertIn("ai.hermes.xiuxian.tg9300000001.shendubiguan.send.timer", units)
            self.assertIn("ai.hermes.xiuxian.qingyuanzi-six.monitor.service", units)
            # The 7200-second sword flow runs from the deadline dispatcher, not its own job.
            self.assertNotIn("ai.hermes.xiuxian.tg9300000002.fumofabao_qingzhufengyunjian_shenlei.send.timer", units)
            service = (out / "units/ai.hermes.xiuxian.tg9300000001.shendubiguan.send.service").read_text()
            self.assertIn(f"cd {ROOT} &&", service)
            self.assertIn("Environment=XIU_SCHEDULER=systemd", service)

    def test_systemd_units_mirror_launchd_schedules(self):
        keep = cli.systemd_units("x.keep", {"ProgramArguments": ["/bin/bash", "-lc", "echo $HOME 100%"],
                                            "KeepAlive": True, "ThrottleInterval": 60})
        self.assertEqual({"x.keep.service"}, set(keep))
        self.assertIn('ExecStart="/bin/bash" "-lc" "echo $$HOME 100%%"', keep["x.keep.service"])
        self.assertIn("RestartSec=60", keep["x.keep.service"])
        timed = cli.systemd_units("x.timed", {"ProgramArguments": ["/bin/true"], "StartInterval": 300,
                                              "StartCalendarInterval": [{"Hour": 0, "Minute": 15},
                                                                        {"Weekday": 0, "Hour": 6, "Minute": 30}]})
        timer = timed["x.timed.timer"]
        for line in ("OnActiveSec=300", "OnUnitActiveSec=300", "OnCalendar=*-*-* 00:15:00 Asia/Shanghai",
                     "OnCalendar=Sun *-*-* 06:30:00 Asia/Shanghai"):
            self.assertIn(line, timer)
        self.assertIn("Type=oneshot", timed["x.timed.service"])


if __name__ == "__main__":
    unittest.main()
