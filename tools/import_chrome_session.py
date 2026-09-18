#!/usr/bin/env python3
"""Import cookies from the user's real Chrome into the project browser profile.

Personal / local development helper. **Not part of the shipped MCP surface** and
not intended for distribution: it reads the local Chrome cookie store and the
macOS Keychain entry Chrome uses to encrypt it. That is the user's own data on
the user's own machine, but it is deliberately kept out of the product.

Why this exists
---------------
Chrome 136+ refuses to expose remote debugging on the *default* user-data-dir,
so the automation browser cannot simply attach to the Chrome profile you are
already signed in to. The alternative is to log in a second time inside the
automation browser.

This helper removes that second login: it decrypts the cookies you already
have in Chrome and injects them through Playwright, so Chrome re-encrypts them
with the *automation profile's* own key. That last detail matters -- copying
the Cookies SQLite file directly does **not** work, because Chrome 153 on macOS
prepends a random 16-byte IV to every cookie value and discards anything it
cannot decrypt. See `docs/` notes in the repo for the investigation.

Cookie format on macOS (Chrome 153, empirically verified)
---------------------------------------------------------
    v10 || header(16 bytes) || IV(16 bytes) || AES-128-CBC(key, IV, PKCS7(plaintext))

    key = PBKDF2-HMAC-SHA1(
        password = Keychain generic password, service "Chrome Safe Storage",
        salt     = b"saltysalt", iterations = 1003, dklen = 16,
    )

This is **not** the widely documented layout. The documented one is
`v10 || IV(16 spaces) || ciphertext`, which yields garbage here. Two earlier
layouts are also handled as fallbacks, because the exact scheme has changed
across Chrome versions and a hard failure would be worse than trying them in
order and checking that the result looks like text.

A useful consistency check: every `v10` body length is `32 + 16n`, because of
the 16-byte header plus a full 16-byte IV before any ciphertext block.

Usage
-----
    python tools/import_chrome_session.py --list
    python tools/import_chrome_session.py --domains linkedin.com
    python tools/import_chrome_session.py --domains linkedin.com --verify
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CHROME_ROOT = Path.home() / "Library/Application Support/Google/Chrome"
KEYCHAIN_SERVICE = "Chrome Safe Storage"
KEYCHAIN_ACCOUNT = "Chrome"
PBKDF2_SALT = b"saltysalt"
PBKDF2_ITERATIONS = 1003
IV_FALLBACK = b" " * 16
CHROME_EPOCH_OFFSET = 11_644_473_600  # seconds between 1601-01-01 and 1970-01-01

# Chrome's `samesite` column -> Playwright's expected string.
SAMESITE = {-1: "None", 0: "None", 1: "Lax", 2: "Strict"}

DEFAULT_DOMAINS = ["linkedin.com"]
# No default source profile: it is detected. Which Chrome profile holds the
# login is a property of the user's machine, so hardcoding "Profile 2" would
# only ever be correct on the machine that name was written on.
DEFAULT_TARGET = "data/browser-profile"


# --------------------------------------------------------------------------- #
# Chrome cookie decryption
# --------------------------------------------------------------------------- #
def keychain_password() -> str:
    """Read the Chrome Safe Storage password from the macOS Keychain."""
    proc = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "Could not read the Chrome Safe Storage key from the Keychain.\n"
            f"  {proc.stderr.strip()}\n"
            "Unlock the login keychain and try again, or just log in manually "
            "inside the automation browser instead."
        )
    return proc.stdout.strip()


def derive_key(password: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password.encode(), PBKDF2_SALT, PBKDF2_ITERATIONS, dklen=16)


def _aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """AES-128-CBC decrypt using the openssl CLI, so we need no extra dependency."""
    if len(ciphertext) % 16 != 0:
        raise ValueError("ciphertext is not block-aligned")
    with tempfile.NamedTemporaryFile(suffix=".bin") as fh:
        fh.write(ciphertext)
        fh.flush()
        proc = subprocess.run(
            [
                "openssl", "enc", "-d", "-aes-128-cbc",
                "-K", key.hex(),
                "-iv", iv.hex(),
                "-nopad",
                "-in", fh.name,
            ],
            capture_output=True,
        )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[:200])
    return proc.stdout


def _strip_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data.endswith(bytes([pad]) * pad):
        return data[:-pad]
    return data


def decrypt_cookie(key: bytes, encrypted_value: bytes) -> str:
    """Decrypt one Chrome cookie value on macOS.

    Tries the known layouts in order of likelihood and returns the first result
    that actually looks like a cookie value. A wrong layout still returns
    bytes -- they are just binary noise -- so "did it decrypt" has to be
    decided by inspecting the output, not by whether the call raised.
    """
    if not encrypted_value:
        return ""
    if encrypted_value[:3] != b"v10":
        raise ValueError(
            f"unsupported cookie encryption prefix {encrypted_value[:3]!r} "
            "(v20/app-bound cookies cannot be decrypted this way)"
        )
    body = encrypted_value[3:]

    # Chrome 153: header(16) || IV(16) || ciphertext
    if len(body) >= 48:
        try:
            plain = _strip_pkcs7(_aes_cbc_decrypt(key, body[16:32], body[32:]))
            if _looks_like_text(plain):
                return plain.decode("utf-8", errors="replace")
        except Exception:
            pass

        # Intermediate layout: IV(16) || ciphertext
        try:
            plain = _strip_pkcs7(_aes_cbc_decrypt(key, body[:16], body[16:]))
            if _looks_like_text(plain):
                return plain.decode("utf-8", errors="replace")
        except Exception:
            pass

    # Legacy layout: fixed all-space IV, whole body is ciphertext.
    plain = _strip_pkcs7(_aes_cbc_decrypt(key, IV_FALLBACK, body))
    return plain.decode("utf-8", errors="replace")


def _looks_like_text(data: bytes) -> bool:
    """True when `data` looks like a real (printable ASCII) cookie value."""
    if not data:
        return False
    # Cookie values are printable ASCII. Wrong-key output is binary noise, so a
    # single out-of-range byte is enough to say "that layout was wrong".
    return all(32 <= b <= 126 for b in data)


# --------------------------------------------------------------------------- #
# Reading the Chrome cookie store
# --------------------------------------------------------------------------- #
def chrome_profiles() -> list[str]:
    if not CHROME_ROOT.is_dir():
        return []
    names = []
    for entry in sorted(CHROME_ROOT.iterdir()):
        if entry.is_dir() and (entry / "Cookies").exists():
            names.append(entry.name)
    return names


def matching_cookie_count(profile: str, domains: list[str]) -> int:
    """How many cookies this profile holds for any of `domains`.

    Chrome keeps an exclusive lock on its cookie store while running, so the
    file is copied aside first -- the same dance `read_cookies` does.
    """
    db = CHROME_ROOT / profile / "Cookies"
    if not db.exists():
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "Cookies"
        try:
            shutil.copy2(db, tmp_db)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(db) + suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, Path(str(tmp_db) + suffix))
            con = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
            try:
                where = " OR ".join(["host_key LIKE ?"] * len(domains))
                params = [f"%{d}%" for d in domains]
                row = con.execute(
                    f"select count(*) from cookies where {where}", params
                ).fetchone()
                return int(row[0]) if row else 0
            finally:
                con.close()
        except Exception:
            return 0


def detect_profile(domains: list[str]) -> tuple[str | None, list[tuple[str, int]]]:
    """Pick the Chrome profile that actually holds this domain's cookies.

    Returns the winner plus the full ranking, so the choice can be shown rather
    than silently made -- if a user has two profiles with LinkedIn logins, they
    should be able to see that and override with `--chrome-profile`.
    """
    ranking = [(name, matching_cookie_count(name, domains)) for name in chrome_profiles()]
    ranking.sort(key=lambda item: -item[1])
    if not ranking or ranking[0][1] == 0:
        return None, ranking
    return ranking[0][0], ranking


def read_cookies(profile: str, domains: list[str], key: bytes) -> tuple[list[dict], list[str]]:
    """Return (playwright_cookies, skipped_notes) for the given domains."""
    db = CHROME_ROOT / profile / "Cookies"
    if not db.exists():
        raise FileNotFoundError(f"no cookie store at {db}")

    # Copy the DB first: Chrome holds a lock while running.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "Cookies"
        shutil.copy2(db, tmp_db)
        # WAL sidecars carry recent writes; copy them if present.
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db) + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, Path(str(tmp_db) + suffix))

        con = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
        try:
            # `partition_key` only exists on newer Chrome; tolerate its absence.
            cols = {r[1] for r in con.execute("PRAGMA table_info(cookies)")}
            has_partition = "partition_key" in cols
            rows = con.execute(
                f"""
                SELECT host_key, name, value, encrypted_value, path, expires_utc,
                       is_secure, is_httponly, samesite
                       {', partition_key' if has_partition else ", '' AS partition_key"}
                FROM cookies
                """
            ).fetchall()
        finally:
            con.close()

    matched = [r for r in rows if any(d in r[0] for d in domains)]

    now = time.time()
    cookies: list[dict] = []
    skipped: list[str] = []
    for host, name, value, enc, path, expires, secure, http_only, samesite, partition in matched:
        label = f"{host} {name}"

        # CHIPS / partitioned cookies need a partitionKey that CDP is strict
        # about. They are third-party ad cookies -- not needed for a session.
        if partition:
            skipped.append(f"{label}: partitioned cookie")
            continue
        if not name or not host:
            skipped.append(f"{label}: missing name or domain")
            continue

        if value:
            plain = value
        else:
            try:
                plain = decrypt_cookie(key, enc)
            except Exception as exc:  # noqa: BLE001 - report and continue
                skipped.append(f"{label}: {exc}")
                continue
        if not plain:
            skipped.append(f"{label}: empty after decrypt")
            continue
        # Guard against a wrong layout silently producing binary noise.
        if not _looks_like_text(plain.encode("utf-8", errors="replace")):
            skipped.append(f"{label}: decrypt produced non-text output")
            continue

        # Chrome epoch: microseconds since 1601-01-01. 0 means a session cookie.
        expiry = int(expires / 1_000_000 - CHROME_EPOCH_OFFSET) if expires else None
        if expiry is not None and expiry <= now:
            skipped.append(f"{label}: already expired")
            continue

        # CDP rejects SameSite=None unless the cookie is also Secure.
        same_site = SAMESITE.get(samesite, "Lax")
        if same_site == "None":
            secure = True

        cookie = {
            "name": name,
            "value": plain,
            "domain": host,
            "path": path or "/",
            "secure": bool(secure),
            "httpOnly": bool(http_only),
            "sameSite": same_site,
        }
        if expiry is not None:
            cookie["expires"] = expiry
        cookies.append(cookie)

    return cookies, skipped


# --------------------------------------------------------------------------- #
# Playwright injection
# --------------------------------------------------------------------------- #
async def inject(target_profile: Path, cookies: list[dict]) -> None:
    from playwright.async_api import async_playwright

    target_profile.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(target_profile),
            channel="chrome",
            headless=True,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        try:
            # Try in bulk first; fall back to one-by-one so a single bad cookie
            # cannot block the session cookies we actually need.
            try:
                await ctx.add_cookies(cookies)
            except Exception as exc:  # noqa: BLE001
                print(f"  bulk inject failed ({exc}); retrying individually")
                accepted = 0
                for cookie in cookies:
                    try:
                        await ctx.add_cookies([cookie])
                        accepted += 1
                    except Exception as inner:  # noqa: BLE001
                        print(f"    rejected {cookie['domain']} {cookie['name']}: {inner}")
                print(f"  accepted {accepted}/{len(cookies)} individually")
            stored = await ctx.cookies()
            print(f"  injected {len(cookies)} cookies; profile now holds {len(stored)}")
        finally:
            await ctx.close()


async def verify(target_profile: Path, url: str) -> bool:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(target_profile),
            channel="chrome",
            headless=True,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await asyncio.sleep(4)

            # The redirect is the authoritative signal. LinkedIn bounces any
            # unauthenticated request for /feed/ to /login/ or /authwall/.
            blocked = any(k in page.url for k in ("/login", "authwall", "/signup", "/checkpoint"))
            print(f"  landed on: {page.url}")

            html = await page.content()
            markers = {
                "global-nav": "global-nav" in html,
                "feed url kept": "/feed" in page.url,
                "no signin form": 'name="session_key"' not in html,
            }
            for name, present in markers.items():
                print(f"  {name}: {present}")

            ok = not blocked
            if ok:
                print("  result: LOGGED IN")
            else:
                print("  result: NOT logged in -- session cookies did not take effect")
            return ok
        finally:
            await ctx.close()


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list Chrome profiles that have a cookie store")
    ap.add_argument("--chrome-profile", help="source Chrome profile directory (default: auto-detect)")
    ap.add_argument("--target-profile", default=DEFAULT_TARGET, help="destination automation profile")
    ap.add_argument("--domains", default=",".join(DEFAULT_DOMAINS), help="comma-separated domain substrings")
    ap.add_argument("--verify", action="store_true", help="open the target profile and check login state")
    ap.add_argument("--verify-url", default="https://www.linkedin.com/feed/")
    args = ap.parse_args()

    domains = [d.strip() for d in args.domains.split(",") if d.strip()]

    if args.list:
        print(f"Chrome profiles holding cookies for: {', '.join(domains)}")
        _, ranking = detect_profile(domains)
        if not ranking:
            print(f"  (no cookie store found under {CHROME_ROOT})")
        for name, count in ranking:
            marker = "  <- would be used" if count and name == ranking[0][0] else ""
            print(f"  {name:20} {count:5} matching cookie(s){marker}")
        return 0

    source = args.chrome_profile
    if source is None:
        source, ranking = detect_profile(domains)
        if source is None:
            print(f"No Chrome profile holds cookies for {', '.join(domains)}.")
            print("Sign in to the site in Chrome first, then re-run.")
            if ranking:
                print("\nProfiles found (none matched):")
                for name, count in ranking:
                    print(f"  {name}")
            return 1
        if len(ranking) > 1 and ranking[1][1] > 0:
            print("More than one Chrome profile has cookies for these domains:")
            for name, count in ranking:
                print(f"  {name:20} {count:5} cookie(s)")
            print(f"  -> using '{source}' (most cookies). "
                  f"Override with --chrome-profile if that is wrong.")
        else:
            print(f"auto-detected Chrome profile: {source}")

    target = Path(args.target_profile)
    if not target.is_absolute():
        target = Path.cwd() / target

    print(f"source : {CHROME_ROOT / source}")
    print(f"target : {target}")
    print(f"domains: {', '.join(domains)}")

    print("\n[1/3] reading Keychain key")
    key = derive_key(keychain_password())
    print(f"  derived AES-128 key {key.hex()}")

    print(f"\n[2/3] decrypting cookies from '{source}'")
    cookies, skipped = read_cookies(source, domains, key)
    print(f"  usable: {len(cookies)}")
    for note in skipped[:10]:
        print(f"  skipped: {note}")
    if len(skipped) > 10:
        print(f"  skipped: ... and {len(skipped) - 10} more")
    if not cookies:
        print("\nNo cookies matched. Nothing to do.")
        return 1

    print(f"\n[3/3] injecting into {target.name}")
    asyncio.run(inject(target, cookies))

    if args.verify:
        print("\n[verify] opening the target profile")
        ok = asyncio.run(verify(target, args.verify_url))
        return 0 if ok else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
