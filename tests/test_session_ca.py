import datetime as dt
import fcntl
import os
import stat
import tempfile
import threading
import time
from pathlib import Path
from unittest import TestCase

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import (
    PolicyBuilder,
    Store,
    VerificationError,
)

from project_sandbox import session_ca

NOW = dt.datetime(2026, 11, 4, 12, 0, tzinfo=dt.UTC)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _load(ca: session_ca.SessionCA):
    cert = x509.load_pem_x509_certificate(ca.cert_path.read_bytes())
    key = serialization.load_pem_private_key(ca.key_path.read_bytes(), password=None)
    return cert, key


def _leaf(ca_cert, ca_key, host: str):
    """A server certificate for ``host`` signed by the session CA, as Squid mints."""
    key = ec.generate_private_key(ec.SECP256R1())
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - dt.timedelta(minutes=1))
        .not_valid_after(NOW + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )


def _verify(ca_cert, leaf, host: str) -> None:
    verifier = (
        PolicyBuilder()
        .store(Store([ca_cert]))
        .time(NOW)
        .build_server_verifier(x509.DNSName(host))
    )
    verifier.verify(leaf, [])


class GenerateTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "s1"
        self.dir.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_extensions_for_copilot_interception(self) -> None:
        ca = session_ca.generate(
            self.dir, ["api.github.com"], name="project-sandbox egress docker", now=NOW
        )
        cert, key = _load(ca)
        self.assertTrue(ca.interception)
        self.assertEqual(ca.name_constraints, ("github.com",))

        basic = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        self.assertTrue(basic.critical)
        self.assertTrue(basic.value.ca)
        self.assertEqual(basic.value.path_length, 0)

        usage = cert.extensions.get_extension_for_class(x509.KeyUsage)
        self.assertTrue(usage.critical)
        self.assertTrue(usage.value.key_cert_sign)
        self.assertTrue(usage.value.crl_sign)
        self.assertFalse(usage.value.digital_signature)

        constraints = cert.extensions.get_extension_for_class(x509.NameConstraints)
        self.assertTrue(constraints.critical)
        self.assertEqual(
            constraints.value.permitted_subtrees, [x509.DNSName("github.com")]
        )
        self.assertIsNone(constraints.value.excluded_subtrees)

        self.assertIsInstance(key, ec.EllipticCurvePrivateKey)
        self.assertEqual(key.curve.name, "secp256r1")
        self.assertEqual(
            key.public_key().public_numbers(), cert.public_key().public_numbers()
        )
        cert.verify_directly_issued_by(cert)

    def test_name_constraint_is_enforced_by_a_verifier(self) -> None:
        ca = session_ca.generate(
            self.dir, ["api.github.com"], name="project-sandbox egress docker", now=NOW
        )
        cert, key = _load(ca)
        # Squid mimics GitHub's certificate names (*.github.com, github.com).
        _verify(cert, _leaf(cert, key, "api.github.com"), "api.github.com")
        _verify(cert, _leaf(cert, key, "github.com"), "github.com")
        for host in ("evil.example", "api.github.com.evil.example", "notgithub.com"):
            with self.subTest(host=host), self.assertRaises(VerificationError):
                _verify(cert, _leaf(cert, key, host), host)

    def test_no_interception_constrains_to_invalid(self) -> None:
        ca = session_ca.generate(
            self.dir, [], name="project-sandbox egress docker", now=NOW
        )
        cert, key = _load(ca)
        self.assertFalse(ca.interception)
        self.assertEqual(ca.name_constraints, ("invalid",))
        constraints = cert.extensions.get_extension_for_class(x509.NameConstraints)
        self.assertEqual(
            constraints.value.permitted_subtrees, [x509.DNSName("invalid")]
        )
        with self.assertRaises(VerificationError):
            _verify(cert, _leaf(cert, key, "api.github.com"), "api.github.com")

    def test_validity_is_bounded(self) -> None:
        ca = session_ca.generate(
            self.dir,
            [],
            name="project-sandbox egress docker",
            lifetime=dt.timedelta(days=90),
            now=NOW,
        )
        cert, _ = _load(ca)
        # The cap is 30 days (design.md Decision 7).
        self.assertEqual(session_ca.MAX_LIFETIME, dt.timedelta(days=30))
        self.assertEqual(cert.not_valid_before_utc, NOW - session_ca.BACKDATE)
        self.assertEqual(cert.not_valid_after_utc, NOW + session_ca.MAX_LIFETIME)

        other = Path(self._tmp.name) / "s2"
        other.mkdir()
        short = session_ca.generate(
            other,
            [],
            name="project-sandbox egress docker",
            lifetime=dt.timedelta(hours=2),
            now=NOW,
        )
        self.assertEqual(short.not_after, NOW + dt.timedelta(hours=2))

    def test_invalid_lifetime(self) -> None:
        with self.assertRaises(session_ca.SessionCAError):
            session_ca.generate(
                self.dir,
                [],
                name="project-sandbox egress docker",
                lifetime=dt.timedelta(0),
            )

    def test_file_modes(self) -> None:
        old = os.umask(0)
        try:
            ca = session_ca.generate(
                self.dir, [], name="project-sandbox egress docker", now=NOW
            )
        finally:
            os.umask(old)
        self.assertEqual(_mode(ca.key_path), 0o600)
        self.assertEqual(_mode(ca.cert_path), 0o644)
        self.assertEqual(_mode(self.dir), 0o700)

    def test_refuses_to_overwrite(self) -> None:
        session_ca.generate(self.dir, [], name="project-sandbox egress docker", now=NOW)
        with self.assertRaises(session_ca.SessionCAError):
            session_ca.generate(
                self.dir, [], name="project-sandbox egress docker", now=NOW
            )

    def test_refuses_symlinked_key_path(self) -> None:
        target = Path(self._tmp.name) / "elsewhere"
        (self.dir / session_ca.KEY_NAME).symlink_to(target)
        with self.assertRaises((session_ca.SessionCAError, OSError)):
            session_ca.generate(
                self.dir, [], name="project-sandbox egress docker", now=NOW
            )
        self.assertFalse(target.exists())


class SidecarCATests(TestCase):
    """global-egress-sidecar-macos task 3.1: name, lifetime and reuse."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_named_after_the_runtime_with_default_lifetime(self) -> None:
        ca = session_ca.generate(
            self.dir, [], name=session_ca.common_name("apple-container"), now=NOW
        )
        cert, _ = _load(ca)
        cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
        self.assertEqual(cn[0].value, "project-sandbox egress apple-container")
        self.assertEqual(ca.not_after, NOW + dt.timedelta(days=7))

    def test_load_round_trips(self) -> None:
        made = session_ca.generate(self.dir, ["api.github.com"], name="x", now=NOW)
        loaded = session_ca.load(self.dir)
        assert loaded is not None
        self.assertEqual(loaded.name_constraints, ("github.com",))
        self.assertEqual(loaded.not_after, made.not_after)
        self.assertTrue(loaded.interception)
        self.assertIsNone(session_ca.load(self.dir / "absent"))
        (self.dir / session_ca.KEY_NAME).unlink()
        self.assertIsNone(session_ca.load(self.dir))  # a CA without its key

    def test_reuse_needs_two_days_and_matching_constraints(self) -> None:
        ca = session_ca.generate(self.dir, [], name="x", now=NOW)  # 7 days
        self.assertTrue(session_ca.reusable(ca, [], now=NOW + dt.timedelta(days=2)))
        self.assertTrue(session_ca.reusable(ca, [], now=NOW + dt.timedelta(days=5)))
        # Less than 2 days left, and expired.
        self.assertFalse(
            session_ca.reusable(ca, [], now=NOW + dt.timedelta(days=5, hours=1))
        )
        self.assertFalse(session_ca.reusable(ca, [], now=NOW + dt.timedelta(days=8)))
        # Enabling Copilot changes the constraints from 'invalid' to github.com.
        self.assertFalse(session_ca.reusable(ca, ["api.github.com"], now=NOW))
        self.assertFalse(session_ca.reusable(None, [], now=NOW))

    def test_remove_deletes_key_and_certificate(self) -> None:
        session_ca.generate(self.dir, [], name="x", now=NOW)
        session_ca.remove(self.dir)
        self.assertFalse((self.dir / session_ca.KEY_NAME).exists())
        self.assertFalse((self.dir / session_ca.CERT_NAME).exists())
        session_ca.remove(self.dir)  # idempotent


class SessionDirectoryTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "egress"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_normal_exit_removes_directory_and_key(self) -> None:
        with session_ca.session_directory("sid1", root=self.root) as directory:
            self.assertEqual(_mode(directory), 0o700)
            self.assertTrue(session_ca.is_live(directory))
            ca = session_ca.generate(directory, ["api.github.com"], name="x")
            self.assertTrue(ca.key_path.exists())
        self.assertFalse(directory.exists())
        self.assertFalse(session_ca.is_live(directory))

    def test_exception_path_removes_directory(self) -> None:
        with self.assertRaises(RuntimeError):
            with session_ca.session_directory("sid2", root=self.root) as directory:
                session_ca.generate(directory, [], name="x")
                raise RuntimeError("sandbox start failed")
        self.assertFalse(directory.exists())

    def test_existing_directory_is_refused(self) -> None:
        (self.root / "sid3").mkdir(parents=True)
        with self.assertRaises(session_ca.SessionCAError):
            with session_ca.session_directory("sid3", root=self.root):
                pass
        self.assertTrue((self.root / "sid3").exists())  # not ours to delete

    def test_invalid_session_ids(self) -> None:
        for sid in ("", ".", "..", "a/b"):
            with self.subTest(sid=sid), self.assertRaises(session_ca.SessionCAError):
                with session_ca.session_directory(sid, root=self.root):
                    pass

    def test_default_root_is_next_to_session_records(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            old = os.environ.get("XDG_STATE_HOME")
            os.environ["XDG_STATE_HOME"] = state
            try:
                self.assertEqual(
                    session_ca.egress_root(), Path(state) / "project-sandbox" / "egress"
                )
            finally:
                if old is None:
                    os.environ.pop("XDG_STATE_HOME")
                else:
                    os.environ["XDG_STATE_HOME"] = old


class AcquireExclusiveTests(TestCase):
    """Regression: a liveness probe holding the lock for an instant beat its owner."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "x.lock"
        self.path.touch()

    def _fd(self) -> int:
        fd = os.open(self.path, os.O_RDWR)
        self.addCleanup(os.close, fd)
        return fd

    def test_waits_out_a_brief_holder(self) -> None:
        probe = self._fd()
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        timer = threading.Timer(0.05, fcntl.flock, (probe, fcntl.LOCK_UN))
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertTrue(session_ca.acquire_exclusive(self._fd(), tolerance=2.0))

    def test_fails_while_an_owner_keeps_it(self) -> None:
        fcntl.flock(self._fd(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = time.monotonic()
        self.assertFalse(session_ca.acquire_exclusive(self._fd(), tolerance=0.1))
        self.assertLess(time.monotonic() - start, 1.0)

    def test_concurrent_probes_never_make_the_owner_fail(self) -> None:
        stop = threading.Event()

        def probe() -> None:
            while not stop.is_set():
                session_ca.lock_held(self.path)

        thread = threading.Thread(target=probe)
        thread.start()
        try:
            for _ in range(50):
                fd = os.open(self.path, os.O_RDWR)
                try:
                    self.assertTrue(session_ca.acquire_exclusive(fd))
                finally:
                    os.close(fd)
        finally:
            stop.set()
            thread.join(5)


class RemoveStaleTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "egress"
        self.root.mkdir()
        self.old = time.time() - 3600

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stale_dir(self, name: str, *, lock: bool = True) -> Path:
        directory = self.root / name
        directory.mkdir(mode=0o700)
        (directory / session_ca.KEY_NAME).write_text("key")
        if lock:
            (directory / session_ca.LOCK_NAME).write_text("")
        os.utime(directory, (self.old, self.old))
        return directory

    def test_removes_unlocked_old_directories(self) -> None:
        crashed = self._stale_dir("crashed")
        no_lock_file = self._stale_dir("nolock", lock=False)
        removed = session_ca.remove_stale(self.root)
        self.assertEqual(sorted(removed), sorted([crashed, no_lock_file]))
        self.assertFalse(crashed.exists())
        self.assertFalse(no_lock_file.exists())

    def test_keeps_live_session(self) -> None:
        with session_ca.session_directory("live", root=self.root) as directory:
            os.utime(directory, (self.old, self.old))
            self.assertEqual(session_ca.remove_stale(self.root), [])
            self.assertTrue(directory.exists())

    def test_keeps_young_directory(self) -> None:
        young = self.root / "young"
        young.mkdir()
        self.assertEqual(session_ca.remove_stale(self.root), [])
        self.assertTrue(young.exists())

    def test_ignores_symlinks_and_files(self) -> None:
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (outside / "keep").write_text("x")
        link = self.root / "link"
        link.symlink_to(outside)
        (self.root / "file").write_text("x")
        self.assertEqual(session_ca.remove_stale(self.root, grace_seconds=0), [])
        self.assertTrue((outside / "keep").exists())

    def test_missing_root(self) -> None:
        self.assertEqual(session_ca.remove_stale(self.root / "absent"), [])
