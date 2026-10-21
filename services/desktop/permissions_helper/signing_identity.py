"""The certificate the permissions helper is signed with.

An ad-hoc signature names the code hash, so every rebuild is a new code identity
and macOS asks for the desktop grants again. This module owns one self-signed
certificate with a fixed name, held in a keychain of its own, so the helper's
designated requirement names the certificate instead and survives every rebuild.

Everything here runs through a caller-supplied `run` (lifecycle's bounded
`run_tool`), so a provisioning or signing step that fails aborts the build
instead of falling back to an ad-hoc signature.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from collections.abc import Callable
from pathlib import Path

import base.paths
from services.desktop.permissions_helper import hardened_runtime

IDENTITY_NAME = "Meiva Local Signing"
IDENTITY_ORG = "Meiva"
VALID_DAYS = 3650
KEYCHAIN_NAME = "signing.keychain-db"
PASSWORD_NAME = "signing.keychain-pass"
README_NAME = "SIGNING-README.md"

# meiva:note
# Absolute paths: the system tools, never a Homebrew `openssl` whose PKCS#12
# archives `security import` rejects (it writes OpenSSL 3's AES defaults).
SECURITY = "/usr/bin/security"
OPENSSL = "/usr/bin/openssl"
CODESIGN = "/usr/bin/codesign"

# What `codesign --entitlements` needs on top of the identity; the hardened
# runtime belongs to `hardened_runtime` so one module owns that decision.
_SIGN_ARGV_HEAD = ("--force", "--sign", IDENTITY_NAME)

_CERT_CONFIG = f"""\
[ req ]
distinguished_name = dn
x509_extensions = v3
prompt = no
[ dn ]
CN = {IDENTITY_NAME}
O = {IDENTITY_ORG}
[ v3 ]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = codeSigning
"""

PROVISION_COMMAND = "python3 scripts/host_ops/tcc/helper-signing.py provision"

Runner = Callable[[list[str]], object]


class SigningIdentityError(RuntimeError):
    """The signing identity is absent or unusable; the build must not continue."""


def directory() -> Path:
    return base.paths.permissions_helper_app_dir()


def keychain_path() -> Path:
    return directory() / KEYCHAIN_NAME


def password_path() -> Path:
    return directory() / PASSWORD_NAME


def readme_path() -> Path:
    return directory() / README_NAME


# meiva:note
# The identity's only build-visible property: the name every artifact is signed
# with. It is folded into the source content hash, so changing the signing policy
# produces a new artifact instead of an in-place re-sign. The certificate itself
# is not hashed -- reading it would need `security` in the content hash, which
# must stay pure -- so a regenerated certificate shows up as a changed designated
# requirement in the build state instead.
def policy() -> str:
    return f"{IDENTITY_NAME}|{IDENTITY_ORG}|{VALID_DAYS}"


def sign_arguments(
    app: Path, *, entitlements: Path, identifier: str
) -> list[str]:
    """The codesign invocation that signs `app` with the fixed identity."""
    return [
        CODESIGN,
        *_SIGN_ARGV_HEAD,
        "--keychain",
        str(keychain_path()),
        *hardened_runtime.SIGNING_OPTIONS,
        "--timestamp=none",
        "--entitlements",
        str(entitlements),
        "--identifier",
        identifier,
        str(app),
    ]


def ensure(run: Runner, *, create: bool) -> None:
    """Guarantee the identity can sign; `create` provisions one when none exists.

    `create=False` is the honest case for a host whose installed helper was
    already signed with a certificate: a missing keychain there means the
    identity was destroyed, and quietly minting a replacement would invalidate
    every grant without saying so.
    """
    if not keychain_path().exists():
        if not create:
            raise SigningIdentityError(
                f"the permissions helper was signed with a certificate but {keychain_path()} "
                f"is gone -- every desktop grant was keyed to it. Restore that keychain, or "
                f"provision a new identity with `{PROVISION_COMMAND}` and grant the helper again"
            )
        provision(run)
    require(run)


def provision(run: Runner) -> None:
    """Create the keychain, certificate and password file, then record them."""
    directory().mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(32)
    _write_secret(password_path(), password)
    keychain = keychain_path()
    run([SECURITY, "create-keychain", "-p", password, str(keychain)])
    run([SECURITY, "set-keychain-settings", str(keychain)])
    run([SECURITY, "unlock-keychain", "-p", password, str(keychain)])
    with tempfile.TemporaryDirectory(dir=directory()) as scratch:
        archive = _certificate_archive(Path(scratch), run)
        run(
            [
                SECURITY,
                "import",
                str(archive),
                "-k",
                str(keychain),
                "-P",
                password,
                "-T",
                CODESIGN,
                "-T",
                SECURITY,
            ]
        )
    # Without a partition list the imported key's ACL admits only the importing
    # process: `codesign` would block on a keychain prompt (or fail headless).
    run(
        [
            SECURITY,
            "set-key-partition-list",
            "-S",
            "apple-tool:,apple:",
            "-s",
            "-k",
            password,
            str(keychain),
        ]
    )
    _add_to_search_list(run)
    require(run)
    readme_path().write_text(_readme_text())


def require(run: Runner) -> None:
    """Prove the identity is present, unlocked and reachable by `codesign`."""
    keychain = keychain_path()
    if not keychain.exists():
        raise SigningIdentityError(
            f"no signing identity at {keychain}; provision one with `{PROVISION_COMMAND}`"
        )
    if not password_path().exists():
        raise SigningIdentityError(
            f"the keychain password record {password_path()} is missing, so {keychain} cannot "
            f"be unlocked; it was written when the identity was provisioned"
        )
    password = password_path().read_text().strip()
    run([SECURITY, "unlock-keychain", "-p", password, str(keychain)])
    listed = _identities(run)
    if identity_line(listed) is None:
        raise SigningIdentityError(
            f"{keychain} holds no {IDENTITY_NAME!r} code-signing identity; "
            f"`security find-identity -p codesigning` reports: {listed.strip() or '(nothing)'}"
        )
    _add_to_search_list(run)
    rivals = _rival_keychains(run)
    if rivals:
        raise SigningIdentityError(
            f"{IDENTITY_NAME!r} also exists in {', '.join(rivals)}, and `codesign` refuses an "
            f"ambiguous identity. Remove those keychains from the user search list: "
            f"`security list-keychains -d user -s <every keychain but those>`"
        )


def identity_line(find_identity_output: str) -> str | None:
    """The `find-identity` row naming this certificate, or None."""
    for line in find_identity_output.splitlines():
        if f'"{IDENTITY_NAME}"' in line:
            return line.strip()
    return None


def search_list(list_keychains_output: str) -> list[str]:
    return [line.strip().strip('"') for line in list_keychains_output.splitlines() if line.strip()]


# meiva:note
# Other keychains in the user search list that hold a certificate with our name.
#
# `codesign --sign <name> --keychain <keychain>` still resolves the name across
# the whole search list and fails with "ambiguous" when two keychains match, so
# a leftover test keychain breaks every build until it is removed.
def _rival_keychains(run: Runner) -> list[str]:
    ours = keychain_path().resolve()
    rivals: list[str] = []
    for entry in search_list(_text(run([SECURITY, "list-keychains", "-d", "user"]))):
        path = Path(entry)
        if path.resolve() == ours or not path.exists():
            continue
        if identity_line(_identities(run, keychain=path)) is not None:
            rivals.append(entry)
    return rivals


def _identities(run: Runner, *, keychain: Path | None = None) -> str:
    return _text(
        run([SECURITY, "find-identity", "-p", "codesigning", str(keychain or keychain_path())])
    )


# meiva:note
# Keep the keychain in the user's search list without ever replacing the list.
#
# `codesign --keychain` restricts the search to that keychain but cannot add it:
# an identity outside the search list is "no identity found". Every write here
# passes the whole current list plus ours, because `security list-keychains -s`
# replaces it wholesale -- the way an unrelated signing line drops ours.
#
# Entries are compared by resolved path: the same keychain is `/tmp/...` in a
# home path and `/private/tmp/...` from `security`, and comparing the strings
# would append a duplicate on every build.
def _add_to_search_list(run: Runner) -> None:
    current = search_list(_text(run([SECURITY, "list-keychains", "-d", "user"])))
    listed: dict[str, str] = {}
    for entry in current:
        listed.setdefault(str(Path(entry).resolve()), entry)
    keychain = keychain_path()
    wanted = list(listed.values())
    if str(keychain.resolve()) not in listed:
        wanted.append(str(keychain))
    if wanted != current:
        run([SECURITY, "list-keychains", "-d", "user", "-s", *wanted])


def _certificate_archive(scratch: Path, run: Runner) -> Path:
    config = scratch / "cert.cnf"
    config.write_text(_CERT_CONFIG)
    key = scratch / "identity.key"
    certificate = scratch / "identity.crt"
    archive = scratch / "identity.p12"
    run(
        [
            OPENSSL,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            str(VALID_DAYS),
            "-config",
            str(config),
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ]
    )
    run(
        [
            OPENSSL,
            "pkcs12",
            "-export",
            "-out",
            str(archive),
            "-inkey",
            str(key),
            "-in",
            str(certificate),
            "-passout",
            f"file:{password_path()}",
        ]
    )
    return archive


def _text(result: object) -> str:
    return getattr(result, "stdout", b"").decode(errors="replace")


def _write_secret(path: Path, secret: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(secret + "\n")
    path.chmod(0o600)


def _readme_text() -> str:
    password = password_path()
    keychain = keychain_path()
    return f"""\
# Meiva permissions helper -- signing identity

The helper at `MeivaPermissionsHelper.app` is signed with one self-signed
certificate, `{IDENTITY_NAME}` (organisation `{IDENTITY_ORG}`), stored in
`{keychain}`. Its password is in `{password}` (owner-only). Nothing else uses
either file.

Why: TCC keys a grant on the designated requirement. Under an ad-hoc signature
that requirement is the code hash, so every rebuild is a new identity and the
grants silently stop matching. With a certificate the requirement is
`identifier "com.meiva.permissions-helper" and certificate root = H"..."`, which
does not change when the helper is rebuilt.

## How it was created

    {OPENSSL} req -x509 -newkey rsa:2048 -sha256 -nodes -days {VALID_DAYS} \\
        -config cert.cnf -keyout identity.key -out identity.crt
    {SECURITY} create-keychain -p "$PASSWORD" {KEYCHAIN_NAME}
    {SECURITY} set-keychain-settings {KEYCHAIN_NAME}
    {SECURITY} unlock-keychain -p "$PASSWORD" {KEYCHAIN_NAME}
    {SECURITY} import identity.p12 -k {KEYCHAIN_NAME} -P "$PASSWORD" \\
        -T {CODESIGN} -T {SECURITY}
    {SECURITY} set-key-partition-list -S apple-tool:,apple: -s -k "$PASSWORD" {KEYCHAIN_NAME}
    {SECURITY} list-keychains -d user -s <existing keychains> {keychain}

`cert.cnf` carried `basicConstraints = critical,CA:false`,
`keyUsage = critical,digitalSignature` and `extendedKeyUsage = codeSigning`; the
`subject.CN` is `{IDENTITY_NAME}` and the organisation is `{IDENTITY_ORG}`. The
private key, certificate and PKCS#12 archive were created in a temporary
directory beside the keychain and deleted after the import.

## A deployment that predates this identity undoes it

`meiva start` rebuilds the helper whenever the content hash it records differs
from the installed one, and that hash now includes the signing policy. A host
whose `$MEIVA_HOME/source` predates this change computes a different hash, reads
the installed helper as stale, and signs it ad-hoc again on its next start --
which silently invalidates the grants the operator just granted. Update that
checkout to a release that contains this identity *before* switching, and never
switch a host whose deployment still runs older code.

## What breaks if it is lost

The certificate *is* the helper's identity. If this keychain is deleted, the
designated requirement changes, and every grant -- Screen Recording,
Accessibility, AppleEvents, App Management -- must be given again by hand on
every Mac that shares this identity. The build refuses to sign rather than
quietly minting a replacement, so the loss is loud.

To move the identity to another Mac, copy `{KEYCHAIN_NAME}` and
`{PASSWORD_NAME}` into that Mac's `~/.meiva/helper/`, then run
`python3 scripts/host_ops/tcc/helper-signing.py status` there (a machine without
the keychain can `import` a PKCS#12 archive exported here). The same certificate
means the same designated requirement everywhere, so one set of System Settings
clicks covers every host.

Only one keychain per machine may hold `{IDENTITY_NAME}`: `codesign` resolves the
name across the whole user search list and refuses an ambiguous identity, so a
stale test keychain with the same name breaks every build until it is removed
from `security list-keychains -d user`.
"""
