# Security evidence for the card-data claim

The claim: card data never leaves the store as a card number. Each swipe becomes a
one-way join ID with its context, checked before it touches the core network. This
page lists each control, where it is implemented, and the committed test or proof that
exercises it. It is evidence for this demo's behaviour, not a payment-industry
certification.

| Control | Where | Evidence |
|---|---|---|
| Card number replaced by a keyed one-way join ID (`jid1`, HMAC-SHA256) | `pipelines/pos-guard.yaml`, step `join_id_and_strip` | Fixture run checks every warehouse record's join ID against the central side's independent implementation; `tests/test_stores.py` pins it |
| Every card and person field dropped, and listed | same step, `stripped` | Fixture run compares each warehouse record exactly, with no extra keys; the shared replay schema forbids additional properties |
| Signature, schema, totals, range, age and injection checks on untrusted tills | step `scan_swipe` | Fixture run: tampered, malformed, injection, stale, unknown-register, totals and over-limit swipes are all quarantined with the expected reason |
| Final scan for card-number-shaped digits in any field | step `block_card_numbers` | Fixture run: a signed swipe with the card number typed into its note is quarantined, and its stored note has the digits masked |
| Quarantined records stay in the store | `QUARANTINE_FILE` output, never the uplink | Fixture run: none of the quarantine records reaches the warehouse and none contains a card number |
| The warehouse checks independently that no card number landed | `scripts/warehouse.py`, Luhn scan of every stored string | Fixture run: zero card numbers in every row; `tests/test_stores.py` covers digits inside identifiers |
| WAN leg is TLS 1.3 only | warehouse ingest listener | `tests/test_wan_security.py`: a TLS 1.2 client and a plaintext client are refused |
| WAN leg is authenticated both ways | client certificate required; node trusts only the group CA | `tests/test_wan_security.py`: no certificate, a certificate from another authority, and a client that does not trust the warehouse all fail; fixture run: a wrong-authority uplink delivers nothing and the records stay queued on disk |
| A store can deliver only its own records | certificate common name checked against each record's store | `tests/test_wan_security.py`: a valid certificate for one store sending another store's records is refused with 403 and stores nothing |
| No plaintext path in the job | `pipelines/pos-uplink.yaml` URL is literally `https://` | `tests/test_wan_security.py` reads the shipped job |
| Keys are not in the job or the environment | `POS_CONFIG_DIR` files, mode 0600 | Fixture run starts nodes with only the directory; with it missing, the job quarantines every swipe |
| Nothing lost across an outage or restart, nothing delivered twice | sqlite queue on the store's disk, warehouse deduplicates transaction IDs | Fixture run phases 2 and 3: the queue survives a node restart with the link cut, then drains once with zero duplicates |

The dated results are in `docs/proof/`.

## Limits

- The certificate authority here is created by `scripts/pki.py` for the demo. A real
  deployment issues store certificates from its own authority and rotates them.
- Card numbers in the fixtures are the card networks' published test numbers.
- The till's signing key and the join key are owned by the card business; the demo
  generates them locally and never commits them.
