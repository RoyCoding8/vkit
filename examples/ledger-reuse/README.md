# Reuse one model through two APIs

The [fixtures](fixtures.json) interpret the same owned resource ledger in two applications. They contain command data and API namespaces. They add no model or proof copy.

| Interpretation | Meaning of a reservation | Demonstrated behavior |
| --- | --- | --- |
| Quota admission | A grant consumes units from a shared pool of ten. | Over-capacity admission rejects; an incorrect owner cannot release a grant; release restores capacity. |
| Worker-slot reservations | A command reserves one of two available slot units. | A third reservation rejects; owner checks protect release; a released unit can be reserved again. |

The qualification script renders each API from the single canonical candidate fixture, binds its six names to the installed package, and performs fresh model, candidate and expected-client verification. It then compiles and runs the concrete command data against that captured candidate. Formal receipts and runtime observations are separate results.

The source contract proves ledger accounting and the six bound operations. Application code must still establish that its quota units, caller identities and real workers correspond to these inputs. These fixtures establish neither per-tenant quota policy nor exclusive access to a physical worker. No application service or scheduler is added.

The same qualification also compares five interleaved warm-image direct procedures with durable VKit jobs. Both perform the same version checks and complete proof phases. The retained report records wall time, proof-phase time and orchestration overhead separately. Image acquisition is excluded.

The package pins both the official archive and a compact digest of its complete expanded-file inventory. Every execution hashes actual archive/tool bytes; it does not decompress the archive again. Maintainers regenerate the inventory pin with `python scripts/check_verus_ledger.py --pin-extracted --archive PATH_TO_PINNED_ARCHIVE`. This command first verifies the archive's existing official hash. CI independently checks that derivation during qualification. Changing this pin changes the package identity and requires acceptance again.
