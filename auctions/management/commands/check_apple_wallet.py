"""Diagnose the Apple Wallet signing setup end to end.

Answers "is it a file-permission problem or a code problem?" in one run:
checks each configured file for existence/readability (with the exact chown to
run), parses the certs, verifies the WWDR intermediate actually issued the Pass
Type ID cert, and test-signs a manifest. Run inside the container:

    docker exec -it django python3 manage.py check_apple_wallet
"""

import os

from django.conf import settings
from django.core.management.base import BaseCommand

from auctions import apple_wallet


class Command(BaseCommand):
    help = "Check Apple Wallet certificate files, permissions, chain validity, and signing."

    def _fail(self, msg):
        self.stdout.write(self.style.ERROR(f"  ✗ {msg}"))
        self._failed = True

    def _ok(self, msg):
        self.stdout.write(self.style.SUCCESS(f"  ✓ {msg}"))

    def _check_file(self, label, rel_path):
        path = settings.BASE_DIR / rel_path
        self.stdout.write(f"{label}: {path}")
        if not path.exists():
            self._fail(f"missing — paths are relative to the repo root ({settings.BASE_DIR})")
            return None
        stat = path.stat()
        if not os.access(path, os.R_OK):
            self._fail(
                f"not readable by uid={os.getuid()} (file is uid={stat.st_uid} gid={stat.st_gid}, "
                f"mode={oct(stat.st_mode & 0o777)}). Fix on the host from the project root: "
                f"sudo chown {os.getuid()}:{os.getgid()} {rel_path}"
            )
            return None
        self._ok(f"exists and is readable ({stat.st_size} bytes)")
        return path

    def handle(self, *args, **options):
        self._failed = False
        required = {
            "APPLE_WALLET_CERT_FILE": settings.APPLE_WALLET_CERT_FILE,
            "APPLE_WALLET_WWDR_FILE": settings.APPLE_WALLET_WWDR_FILE,
            "APPLE_WALLET_PASS_TYPE_IDENTIFIER": settings.APPLE_WALLET_PASS_TYPE_IDENTIFIER,
            "APPLE_WALLET_TEAM_IDENTIFIER": settings.APPLE_WALLET_TEAM_IDENTIFIER,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            self.stdout.write(self.style.ERROR(f"Not configured — set in .env: {', '.join(missing)}"))
            return

        self._check_file("Pass Type ID cert (.p12)", settings.APPLE_WALLET_CERT_FILE)
        self._check_file("WWDR intermediate", settings.APPLE_WALLET_WWDR_FILE)
        if self._failed:
            self.stdout.write(self.style.ERROR("\nFix the file problems above, then re-run."))
            return

        # Parse + chain-verify. _load_signing_certs raises ValueError with an
        # operator-actionable message for every known failure mode (bad password,
        # incomplete .p12, PEM/DER problems, WWDR/signer chain mismatch).
        apple_wallet._load_signing_certs.cache_clear()
        try:
            _key, signer_cert, wwdr_cert = apple_wallet._load_signing_certs()
        except ValueError as exc:
            self._fail(str(exc))
            return
        except Exception as exc:
            self._fail(f"could not load certificates: {exc}")
            self.stdout.write("  (A bad APPLE_WALLET_CERT_PASSWORD commonly fails here.)")
            return
        self.stdout.write("Certificates:")
        self._ok(
            f"signer:  {signer_cert.subject.rfc4514_string()} (expires {signer_cert.not_valid_after_utc:%Y-%m-%d})"
        )
        self._ok(f"WWDR:    {wwdr_cert.subject.rfc4514_string()} (expires {wwdr_cert.not_valid_after_utc:%Y-%m-%d})")
        self._ok("chain:   WWDR issued the signer cert (verified)")

        try:
            signature = apple_wallet._sign_manifest(b'{"test": "manifest"}')
        except Exception as exc:
            self._fail(f"test signing failed: {exc}")
            return
        self._ok(f"signing: produced a {len(signature)}-byte PKCS#7 detached signature")
        self.stdout.write(self.style.SUCCESS("\nApple Wallet signing setup looks good."))
