import io
import unittest
import zipfile

import update_dev_client as client
import update_dev_server as server


def open_memory_zip(entries):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zf:
        for name in entries:
            zf.writestr(name, b"test")
    data.seek(0)
    return zipfile.ZipFile(data, "r")


class ArchiveLayoutTests(unittest.TestCase):
    def test_server_root_sections_win_over_nested_sections(self):
        with open_memory_zip([
            "journeymap/config/waypoints.cfg",
            "nested/mods/NotThePack.jar",
            "config/server.cfg",
            "mods/ActualMod.jar",
        ]) as zf:
            self.assertEqual(server.detect_mod_prefix(zf), "mods/")
            self.assertEqual(server.detect_config_prefix(zf), "config/")

    def test_client_detects_wrapped_minecraft_sections(self):
        with open_memory_zip([
            "GTNH/.minecraft/config/client.cfg",
            "GTNH/.minecraft/mods/ActualMod.jar",
        ]) as zf:
            self.assertEqual(
                client.detect_mod_prefix(zf),
                "GTNH/.minecraft/mods/",
            )
            self.assertEqual(
                client.detect_config_prefix(zf),
                "GTNH/.minecraft/config/",
            )


if __name__ == "__main__":
    unittest.main()
