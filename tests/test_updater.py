import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATE_PS1 = ROOT / "update.ps1"
MANIFEST = ROOT / "release_manifest.json"

PROGRAM_FILES = [
    "INSTALL.bat",
    "install.ps1",
    "UPDATE.bat",
    "update.ps1",
    "release_manifest.json",
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "requirements.txt",
    "run_python.bat",
    "start_free.bat",
    "telegram_collector_free.py",
    "Telegram_Digest.exe",
    "Telegram_Digest.exe.sha256",
]

OLD_RELEASE_FILES = [
    "INSTALL.bat",
    "install.ps1",
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "requirements.txt",
    "run_python.bat",
    "start_free.bat",
    "telegram_collector_free.py",
    "Telegram_Digest.exe",
    "Telegram_Digest.exe.sha256",
]


class UpdaterContractTests(unittest.TestCase):
    def test_manifest_whitelist_excludes_user_state(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest["ProgramFiles"], PROGRAM_FILES)
        self.assertEqual(manifest["ObsoleteFiles"], [])

        forbidden = [
            "credentials.bin",
            "telegram_session.session",
            "selected_channels.json",
            "settings_free.json",
            "news.db",
            "Дайджесты",
            "logs",
            ".venv",
        ]
        for name in forbidden:
            self.assertNotIn(name, manifest["ProgramFiles"])

    def test_builder_and_ci_share_release_manifest(self):
        builder = (ROOT / "scripts" / "build_release.ps1").read_text(encoding="utf-8-sig")
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("ConvertFrom-Json", builder)
        self.assertIn("release_manifest.json", builder)
        self.assertIn("release_manifest.json", workflow)
        self.assertNotIn("$copied = @(", builder)

    def test_updater_contract_is_file_only_and_preserving(self):
        updater = UPDATE_PS1.read_text(encoding="utf-8-sig")
        for required in [
            "Unofficial TelegramNewsAI.lnk",
            "AllowDowngrade",
            "Get-StateSnapshot",
            "Get-Sha256",
            "ConvertFrom-Json",
            "install.ps1",
            "--self-test",
            "ObsoleteFiles",
            "telegram_session.session",
            "credentials.bin",
            "news.db-*",
        ]:
            self.assertIn(required, updater)

        for forbidden in [
            "TelegramClient(",
            "api_hash",
            "phone_code",
            "send_code_request",
            "sign_in(",
        ]:
            self.assertNotIn(forbidden, updater)


@unittest.skipUnless(os.name == "nt", "Windows-only updater integration")
class UpdaterWindowsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not cls.powershell:
            raise unittest.SkipTest("Windows PowerShell not found")

        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        candidates = [
            windir / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe",
            windir / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe",
        ]
        cls.csc = next((path for path in candidates if path.exists()), None)
        if not cls.csc:
            raise unittest.SkipTest("C# compiler not found")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "new-release"
        self.target = self.base / "installed-app"
        self.source.mkdir()
        self.target.mkdir()
        self.user_files = {
            "credentials.bin": b"credential-secret-old",
            "credentials.unreadable-test.bin": b"unreadable-old",
            "telegram_session.session": b"session-old",
            "telegram_session.session-journal": b"journal-old",
            "selected_channels.json": b'{"channels":["a","b"]}',
            "settings_free.json": b'{"period":24}',
            "news.db": b"sqlite-dummy-old",
            "news.db-wal": b"wal-old",
            "news.db-shm": b"shm-old",
        }

    def tearDown(self):
        self.temp.cleanup()

    def _build_launcher(self, destination):
        source = destination.with_suffix(".cs")
        source.write_text(
            textwrap.dedent(
                """
                using System;
                internal static class Program {
                    public static int Main(string[] args) {
                        return (args.Length == 1 && args[0] == "--self-test") ? 0 : 0;
                    }
                }
                """
            ),
            encoding="utf-8",
        )
        subprocess.run(
            [
                str(self.csc),
                "/nologo",
                "/target:exe",
                "/out:" + str(destination),
                str(source),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        source.unlink()

    def _write_release(self, version="9.9.9 Testing", fail_install=False):
        shutil.copy2(UPDATE_PS1, self.source / "update.ps1")
        shutil.copy2(ROOT / "UPDATE.bat", self.source / "UPDATE.bat")
        shutil.copy2(MANIFEST, self.source / "release_manifest.json")

        for name in PROGRAM_FILES:
            path = self.source / name
            if path.exists():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if name == "telegram_collector_free.py":
                path.write_text(
                    f'APP_VERSION = "{version}"\nprint("stub collector")\n',
                    encoding="utf-8",
                )
            elif name == "install.ps1":
                if fail_install:
                    path.write_text(
                        "param([switch]$NoShortcut)\nthrow 'forced updater regression failure'\n",
                        encoding="utf-8",
                    )
                else:
                    path.write_text(
                        "param([switch]$NoShortcut)\nWrite-Host 'stub dependency check ok'\n",
                        encoding="utf-8",
                    )
            elif name == "Telegram_Digest.exe":
                self._build_launcher(path)
            elif name == "Telegram_Digest.exe.sha256":
                pass
            else:
                path.write_text("new:" + name + "\n", encoding="utf-8")

        launcher = self.source / "Telegram_Digest.exe"
        digest = hashlib.sha256(launcher.read_bytes()).hexdigest().upper()
        (self.source / "Telegram_Digest.exe.sha256").write_text(
            digest + "  Telegram_Digest.exe\n",
            encoding="utf-8",
        )

        # Эти файлы намеренно лежат рядом с release, но не входят в whitelist.
        (self.source / "credentials.bin").write_bytes(b"evil-new-credentials")
        (self.source / "news.db").write_bytes(b"evil-new-db")
        (self.source / "not-in-whitelist.tmp").write_text("must-not-copy", encoding="utf-8")

    def _write_target(self, version="9.9.8 Testing"):
        for name in OLD_RELEASE_FILES:
            path = self.target / name
            if name == "telegram_collector_free.py":
                path.write_text(
                    f'APP_VERSION = "{version}"\nprint("old collector")\n',
                    encoding="utf-8",
                )
            elif name == "Telegram_Digest.exe":
                self._build_launcher(path)
            else:
                path.write_text("old:" + name + "\n", encoding="utf-8")

        for name, content in self.user_files.items():
            (self.target / name).write_bytes(content)

        (self.target / "Дайджесты").mkdir()
        (self.target / "Дайджесты" / "old.json").write_bytes(b"digest-old")
        (self.target / "logs").mkdir()
        (self.target / "logs" / "old.log").write_bytes(b"log-old")
        (self.target / "Резервные_копии").mkdir()
        (self.target / "Резервные_копии" / "backup.bin").write_bytes(b"backup-old")
        (self.target / ".venv").mkdir()
        (self.target / ".venv" / "keep.txt").write_bytes(b"venv-old")
        (self.target / "my-private-note.txt").write_bytes(b"unknown-user-file")

    def _run_update(self, *extra):
        return subprocess.run(
            [
                self.powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.source / "update.ps1"),
                "-TargetPath",
                str(self.target),
                *extra,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    def test_update_preserves_user_state_and_ignores_extra_source_files(self):
        self._write_release()
        self._write_target()
        before = {name: hashlib.sha256((self.target / name).read_bytes()).hexdigest()
                  for name in self.user_files}

        result = self._run_update()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('APP_VERSION = "9.9.9 Testing"', (self.target / "telegram_collector_free.py").read_text(encoding="utf-8"))

        for name, digest in before.items():
            self.assertEqual(
                hashlib.sha256((self.target / name).read_bytes()).hexdigest(),
                digest,
                name,
            )

        self.assertEqual((self.target / "Дайджесты" / "old.json").read_bytes(), b"digest-old")
        self.assertEqual((self.target / "logs" / "old.log").read_bytes(), b"log-old")
        self.assertEqual((self.target / "Резервные_копии" / "backup.bin").read_bytes(), b"backup-old")
        self.assertEqual((self.target / ".venv" / "keep.txt").read_bytes(), b"venv-old")
        self.assertEqual((self.target / "my-private-note.txt").read_bytes(), b"unknown-user-file")
        self.assertFalse((self.target / "not-in-whitelist.tmp").exists())
        self.assertNotEqual((self.target / "credentials.bin").read_bytes(), b"evil-new-credentials")
        self.assertTrue((self.target / "UPDATE.bat").exists())
        self.assertTrue((self.target / "update.ps1").exists())
        self.assertTrue((self.target / "release_manifest.json").exists())

    def test_rollback_restores_old_program_files(self):
        self._write_release(fail_install=True)
        self._write_target()
        before = {
            name: (self.target / name).read_bytes()
            for name in OLD_RELEASE_FILES
        }

        result = self._run_update()
        self.assertNotEqual(result.returncode, 0, result.stdout)

        for name, content in before.items():
            self.assertEqual((self.target / name).read_bytes(), content, name)

        self.assertFalse((self.target / "UPDATE.bat").exists())
        self.assertFalse((self.target / "update.ps1").exists())
        self.assertFalse((self.target / "release_manifest.json").exists())
        for name, content in self.user_files.items():
            self.assertEqual((self.target / name).read_bytes(), content, name)

    def test_downgrade_is_blocked_by_default(self):
        self._write_release(version="9.9.9 Testing")
        self._write_target(version="9.9.10 Testing")
        collector_before = (self.target / "telegram_collector_free.py").read_bytes()

        result = self._run_update()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("более старой версии", result.stdout)
        self.assertEqual(
            (self.target / "telegram_collector_free.py").read_bytes(),
            collector_before,
        )


if __name__ == "__main__":
    unittest.main()
