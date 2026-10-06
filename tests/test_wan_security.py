#!/usr/bin/env -S uv run -s
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""The WAN leg to the warehouse must refuse every delivery that is not
encrypted, authenticated and the store's own.

These are negative tests against the real warehouse ingest server and the
shipped pos-uplink job: a plaintext request, a request with no client
certificate, one from a different authority, a TLS 1.2 client, and a valid
store delivering another store's records must all fail and store nothing.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import sys
import tempfile
import threading
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pki  # noqa: E402
import warehouse  # noqa: E402

RECORD = {"txn_id": "t-1", "join_id": "jid1_x", "total_cents": 1,
          "context": {"store_id": "s1", "register_id": "s1-r1"}}


class Rig:
    """A real ingest server on a free port with a throwaway CA."""

    def __init__(self, folder: Path):
        self.pki = folder / "pki"
        self.rogue = folder / "rogue"
        pki.init(self.pki, ["s1", "s2"])
        pki.init(self.rogue, ["s1"], ca_name="Not the retail group CA")
        self.wh = warehouse.Warehouse(folder / "wh.db", folder / "profiles.json")
        context = warehouse.tls_context(self.pki / "warehouse.pem", self.pki / "warehouse.key",
                                        self.pki / "ca.pem")
        self.server = warehouse.IngestServer(("127.0.0.1", 0), context, self.wh)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.wh.conn.close()

    def client(self, ca: Path, cert: Path | None, key: Path | None,
               maximum: ssl.TLSVersion | None = None) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(ca)
        if cert and key:
            ctx.load_cert_chain(cert, key)
        if maximum:
            ctx.maximum_version = maximum
        return ctx

    def post(self, ctx: ssl.SSLContext, records: list[dict]) -> int:
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=ctx, timeout=5)
        try:
            conn.request("POST", "/ingest", json.dumps(records),
                         {"Content-Type": "application/json"})
            return conn.getresponse().status
        finally:
            conn.close()

    def rows(self) -> int:
        return self.wh.conn.execute("SELECT count(*) FROM records").fetchone()[0]


def expect_failure(action) -> None:
    try:
        action()
    except (ssl.SSLError, OSError, http.client.HTTPException):
        return
    raise AssertionError("the delivery was accepted")


def test_valid_store_delivers_its_own_records() -> None:
    with tempfile.TemporaryDirectory() as folder:
        rig = Rig(Path(folder))
        try:
            ctx = rig.client(rig.pki / "ca.pem", rig.pki / "clients/s1.pem",
                             rig.pki / "clients/s1.key")
            assert rig.post(ctx, [RECORD]) == 200
            assert rig.rows() == 1
        finally:
            rig.close()


def test_plaintext_delivery_fails_and_stores_nothing() -> None:
    with tempfile.TemporaryDirectory() as folder:
        rig = Rig(Path(folder))
        try:
            def plaintext() -> None:
                conn = http.client.HTTPConnection("127.0.0.1", rig.port, timeout=5)
                conn.request("POST", "/ingest", json.dumps([RECORD]))
                conn.getresponse()
            expect_failure(plaintext)
            assert rig.rows() == 0
            for _ in range(50):  # the refusal is counted once the worker thread ends
                if rig.wh.refused_connections:
                    break
                socket.create_connection(("127.0.0.1", rig.port), timeout=1).close()
            assert rig.wh.refused_connections >= 1
        finally:
            rig.close()


def test_missing_client_certificate_fails() -> None:
    with tempfile.TemporaryDirectory() as folder:
        rig = Rig(Path(folder))
        try:
            ctx = rig.client(rig.pki / "ca.pem", None, None)
            expect_failure(lambda: rig.post(ctx, [RECORD]))
            assert rig.rows() == 0
        finally:
            rig.close()


def test_certificate_from_another_authority_fails() -> None:
    with tempfile.TemporaryDirectory() as folder:
        rig = Rig(Path(folder))
        try:
            ctx = rig.client(rig.pki / "ca.pem", rig.rogue / "clients/s1.pem",
                             rig.rogue / "clients/s1.key")
            expect_failure(lambda: rig.post(ctx, [RECORD]))
            assert rig.rows() == 0
        finally:
            rig.close()


def test_client_refuses_an_unknown_warehouse() -> None:
    with tempfile.TemporaryDirectory() as folder:
        rig = Rig(Path(folder))
        try:
            ctx = rig.client(rig.rogue / "ca.pem", rig.pki / "clients/s1.pem",
                             rig.pki / "clients/s1.key")
            expect_failure(lambda: rig.post(ctx, [RECORD]))
            assert rig.rows() == 0
        finally:
            rig.close()


def test_tls_below_1_3_fails() -> None:
    with tempfile.TemporaryDirectory() as folder:
        rig = Rig(Path(folder))
        try:
            ctx = rig.client(rig.pki / "ca.pem", rig.pki / "clients/s1.pem",
                             rig.pki / "clients/s1.key", maximum=ssl.TLSVersion.TLSv1_2)
            expect_failure(lambda: rig.post(ctx, [RECORD]))
            assert rig.rows() == 0
        finally:
            rig.close()


def test_a_store_cannot_deliver_another_stores_records() -> None:
    with tempfile.TemporaryDirectory() as folder:
        rig = Rig(Path(folder))
        try:
            ctx = rig.client(rig.pki / "ca.pem", rig.pki / "clients/s2.pem",
                             rig.pki / "clients/s2.key")
            assert rig.post(ctx, [RECORD]) == 403
            assert rig.rows() == 0
            assert rig.wh.forbidden_batches == 1
        finally:
            rig.close()


def test_uplink_job_requires_https_and_client_certificates() -> None:
    job = yaml.safe_load((ROOT / "pipelines" / "pos-uplink.yaml").read_text())
    output = job["config"]["output"]["retry"]["output"]["http_client"]
    assert output["url"].startswith("https://"), "the warehouse URL must be https"
    tls = output["tls"]
    assert tls["enabled"] is True
    assert tls["root_cas_file"] and tls["client_certs"][0]["cert_file"]
    assert tls["client_certs"][0]["key_file"]
    assert not tls.get("skip_cert_verify"), "certificate verification must stay on"
    text = (ROOT / "pipelines" / "pos-uplink.yaml").read_text()
    assert "http://" not in text.replace("http://${", ""), "no plaintext URL in the job"


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ok: {len(tests)} WAN security checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
