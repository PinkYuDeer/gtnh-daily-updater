import io
import json
import os
import tempfile
import unittest
import zipfile
from unittest import mock

import update_dev_client as client


def open_memory_zip(entries):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    data.seek(0)
    return zipfile.ZipFile(data, "r")


class PlayerPackageConfigTests(unittest.TestCase):
    def test_player_package_is_disabled_by_default_and_can_be_enabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg_path = os.path.join(temp_dir, "update_daily.cfg")
            with mock.patch.object(client, "UPDATE_CFG_PATH", cfg_path):
                self.assertEqual(client.load_exclude_mod_list(), set())
                self.assertFalse(client.load_player_package_enabled())

                with open(cfg_path, "w", encoding="utf-8") as cfg:
                    cfg.write("[player_package]\nenabled = true\n")
                self.assertTrue(client.load_player_package_enabled())


class PlayerPackageArchiveTests(unittest.TestCase):
    def test_outer_package_contains_exe_and_self_describing_payload(self):
        entries = {
            ".minecraft/mods/ExampleMod-2.0.jar": b"new mod",
            ".minecraft/config/example.cfg": b"general { B:enabled=true }\n",
            "libraries/org/example/lwjgl3ify/2.0/lwjgl3ify.jar": b"new library",
            "mmc-pack.json": json.dumps({
                "components": [
                    {"uid": "org.lwjgl3ify", "version": "2.0"},
                ],
            }).encode("utf-8"),
        }

        with open_memory_zip(entries) as inner, tempfile.TemporaryDirectory() as temp_dir:
            mod_prefix = client.detect_mod_prefix(inner)
            cfg_prefix = client.detect_config_prefix(inner)
            new_mods = {"ExampleMod-2.0.jar": ".minecraft/mods/ExampleMod-2.0.jar"}
            matches = [("update", "ExampleMod-1.0.jar", "ExampleMod-2.0.jar")]
            plan = client.build_player_package_plan(
                inner,
                matches,
                new_mods,
                cfg_prefix,
            )

            exe_path = os.path.join(temp_dir, "dummy.exe")
            with open(exe_path, "wb") as exe:
                exe.write(b"MZ test updater")

            with mock.patch.object(
                client,
                "build_player_updater_exe",
                return_value=exe_path,
            ):
                package_path, manifest = client.create_player_distribution_package(
                    "gtnh-daily-2026-08-13+1-mmcprism-new-java.zip",
                    inner,
                    plan,
                    output_dir=temp_dir,
                )

            self.assertEqual(len(manifest["mod_changes"]), 1)
            with zipfile.ZipFile(package_path, "r") as outer:
                names = outer.namelist()
                self.assertEqual(len(names), 2)
                self.assertIn(client.PLAYER_UPDATER_EXE_NAME, names)
                payload_name = next(
                    name for name in names
                    if name.startswith(client.PLAYER_PAYLOAD_PREFIX)
                )
                payload_data = outer.read(payload_name)

            with zipfile.ZipFile(io.BytesIO(payload_data), "r") as payload:
                self.assertEqual(
                    set(payload.namelist()),
                    {
                        "mods/ExampleMod-2.0.jar",
                        "config/example.cfg",
                        "libraries/org/example/lwjgl3ify/2.0/lwjgl3ify.jar",
                        "mmc-pack.json",
                        client.PLAYER_MANIFEST_NAME,
                    },
                )
                payload_manifest = json.loads(
                    payload.read(client.PLAYER_MANIFEST_NAME).decode("utf-8")
                )
                self.assertEqual(payload_manifest["type"], "gtnh-player-update")

            payload_path = os.path.join(temp_dir, payload_name)
            with open(payload_path, "wb") as payload_file:
                payload_file.write(payload_data)
            self.assertTrue(client.is_player_update_payload(payload_path))
            self.assertEqual(
                client.find_bundled_player_payload(temp_dir),
                payload_path,
            )


if __name__ == "__main__":
    unittest.main()
