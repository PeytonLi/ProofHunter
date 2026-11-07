"""ProofHunter: evidence-backed verification of GitHub pull requests.

Shared, frozen setup surface (G0):

* ``proofhunter.contracts`` - versioned DTOs, enums and the canonical serializer.
* ``proofhunter.ports`` - typed Protocols, adapter failures and required implementations.
* ``proofhunter.config`` - environment settings with offline-safe defaults.
"""

__all__ = ["SCHEMA_VERSION", "__version__"]

__version__ = "0.1.0"
SCHEMA_VERSION = "1.0"
