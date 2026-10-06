#!/usr/bin/env -S uv run -s
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""The group's certificate authority for the WAN leg to the warehouse.

Every store's pos-uplink job proves who it is with a client certificate whose
common name is the store ID; the warehouse proves itself with a server
certificate. Both sides trust only this CA. Keys never leave the directory
they are written to, which is owner-only.

    uv run -s scripts/pki.py init .secrets/pki s1 s2 s3 s4
    uv run -s scripts/pki.py rogue .secrets/rogue s1   # wrong CA, for tests

Layout of the directory it writes:

    ca.pem  ca.key            the authority (the key stays at the warehouse)
    warehouse.pem  warehouse.key
    clients/<store>.pem  clients/<store>.key
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

WAREHOUSE_NAMES = ("IP:127.0.0.1", "DNS:warehouse.dmz.internal")
DAYS = 825


def run(*args: str, cwd: Path | None = None) -> None:
    done = subprocess.run(["openssl", *args], cwd=cwd, capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"openssl {args[0]} failed: {done.stderr.strip()[:300]}")


def private(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    return path


def make_ca(directory: Path, name: str) -> None:
    key, cert = private(directory / "ca.key"), directory / "ca.pem"
    run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key))
    with tempfile.TemporaryDirectory() as scratch:
        # A config file rather than -addext: it works on LibreSSL as well.
        conf = Path(scratch) / "ca.cnf"
        conf.write_text("[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=ext\n"
                        f"[dn]\nO=Retail group\nCN={name}\n"
                        "[ext]\nbasicConstraints=critical,CA:TRUE,pathlen:0\n"
                        "keyUsage=critical,keyCertSign,cRLSign\n")
        run("req", "-x509", "-new", "-key", str(key), "-sha256", "-days", str(DAYS),
            "-config", str(conf), "-out", str(cert))
    os.chmod(key, 0o600)


def issue(directory: Path, stem: Path, subject: str, usage: str, san: str | None) -> None:
    """One leaf certificate signed by the CA in `directory`."""
    key, cert = private(stem.with_suffix(".key")), stem.with_suffix(".pem")
    run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key))
    with tempfile.TemporaryDirectory() as scratch:
        csr = Path(scratch) / "leaf.csr"
        ext = Path(scratch) / "leaf.ext"
        lines = ["basicConstraints=critical,CA:FALSE",
                 "keyUsage=critical,digitalSignature",
                 f"extendedKeyUsage={usage}"]
        if san:
            lines.append(f"subjectAltName={san}")
        ext.write_text("\n".join(lines) + "\n")
        run("req", "-new", "-key", str(key), "-subj", subject, "-out", str(csr))
        run("x509", "-req", "-in", str(csr), "-CA", str(directory / "ca.pem"),
            "-CAkey", str(directory / "ca.key"), "-CAcreateserial", "-sha256",
            "-days", str(DAYS), "-extfile", str(ext), "-out", str(cert))
    os.chmod(key, 0o600)


def init(directory: Path, stores: list[str], ca_name: str = "Retail group POS CA") -> None:
    """Creates the CA, the warehouse certificate and one certificate per store."""
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    if not (directory / "ca.pem").exists():
        make_ca(directory, ca_name)
    if not (directory / "warehouse.pem").exists():
        issue(directory, directory / "warehouse", "/O=Retail group/CN=warehouse",
              "serverAuth", ",".join(WAREHOUSE_NAMES))
    for store in stores:
        if not (directory / "clients" / f"{store}.pem").exists():
            issue(directory, directory / "clients" / store, f"/O=Retail group/CN={store}",
                  "clientAuth", None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("init", help="create the CA, warehouse and store certificates")
    a.add_argument("directory", type=Path)
    a.add_argument("stores", nargs="+")
    r = sub.add_parser("rogue", help="a store certificate from a different CA")
    r.add_argument("directory", type=Path)
    r.add_argument("stores", nargs="+")
    args = ap.parse_args()
    if args.cmd == "init":
        init(args.directory, args.stores)
    else:
        init(args.directory, args.stores, ca_name="Not the retail group CA")
    print(f"wrote {args.directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
