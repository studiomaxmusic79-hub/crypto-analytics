"""Provider-neutral receipts and resumable publication of already frozen bytes."""
import asyncio
import hashlib
from dataclasses import asdict, dataclass
from typing import Protocol


@dataclass(frozen=True)
class ArchiveReceipt:
    provider: str
    object_key: str | None = None
    version: str | None = None
    public_reference: str | None = None
    stored_hash: str | None = None
    stored_at: int | None = None
    status: str = "UNKNOWN"


@dataclass(frozen=True)
class WitnessReceipt:
    provider: str
    record_id: str | None = None
    sequence: int | None = None
    accepted_at: int | None = None
    submitted_hash: str | None = None
    status: str = "UNKNOWN"
    first_hash: str | None = None
    first_sequence: int | None = None


@dataclass(frozen=True)
class PublicReadReceipt:
    provider: str
    reference: str
    data: bytes | None = None
    received_at: int | None = None
    status: str = "UNKNOWN"


class PublicArchive(Protocol):
    async def store_once(self, batch_id: str, exact_batch_bytes: bytes,
                         batch_hash: str, slot: int, stream: str) -> ArchiveReceipt: ...
    async def check(self, batch_id: str, batch_hash: str) -> ArchiveReceipt: ...


class IndependentWitness(Protocol):
    async def register(self, project_id: str, official_stream_id: str, slot_utc: int,
                       batch_hash: str, archive_reference: str) -> WitnessReceipt: ...
    async def check(self, project_id: str, official_stream_id: str, slot_utc: int,
                    batch_hash: str) -> WitnessReceipt: ...


class PublicReader(Protocol):
    async def read(self, public_reference: str) -> PublicReadReceipt: ...


class _Stopped(Exception):
    pass


class _ClaimLost(Exception):
    pass


def publication_mode(value):
    if value not in ("disabled", "mock", "external"):
        raise ValueError("EXTERNAL_PUBLICATION_MODE must be disabled, mock or external")
    if value == "external":
        raise ValueError("External publication providers are not implemented")
    return value


class PublicationWorkflow:
    """Each I/O follows a committed claim/step marker; no network DB locks.

    UNKNOWN and abandoned IN_PROGRESS attempts reconcile before resubmission.
    The lease is only coordination; providers must themselves enforce idempotency.
    """
    def __init__(self, store, archive: PublicArchive, witness: IndependentWitness,
                 reader: PublicReader, clock, *, mode="mock"):
        self.store, self.archive, self.witness, self.reader = store, archive, witness, reader
        self.clock = clock
        self.mode = publication_mode(mode)

    async def run(self, slot):
        if self.mode == "disabled":
            return await asyncio.to_thread(self.store.publication_slot, slot)
        row = await asyncio.to_thread(self.store.claim_external_publication, slot, self.mode, self.clock())
        if row is None:
            return await asyncio.to_thread(self.store.publication_slot, slot)
        token = row["publication_claim_token"]

        async def save(**values):
            nonlocal row
            row = await asyncio.to_thread(self.store.external_publication_progress, slot,
                                          token, self.clock(), **values)
            if row is None:
                raise _ClaimLost()

        async def fail(status, reason, **evidence):
            await save(external_step_status=status, publication_error=reason, **evidence)
            raise _Stopped()

        try:
            raw = bytes(row["batch_bytes"])
            digest = row["batch_hash"]
            if hashlib.sha256(raw).hexdigest() != digest:
                await fail("HASH_MISMATCH", "FROZEN_BYTES_HASH_MISMATCH")
            if row["publication_status"] == "BATCH_FROZEN":
                await save(publication_status="EXTERNAL_SUBMISSION", external_step="ARCHIVE",
                           external_step_status="READY", publication_error=None)
            if not row["archive_receipt"]:
                uncertain = row["external_step_status"] in ("UNKNOWN", "IN_PROGRESS")
                await save(external_step="ARCHIVE", external_step_status="IN_PROGRESS")
                receipt = (await self.archive.check(row["batch_id"], digest) if uncertain else None)
                if receipt is None or receipt.status == "NOT_FOUND":
                    receipt = await self.archive.store_once(row["batch_id"], raw, digest,
                                                            slot, row["official_stream_id"])
                if receipt.status != "STORED":
                    evidence = {"archive_receipt": asdict(receipt)} if receipt.status == "CONFLICT" else {}
                    await fail(self._failure(receipt.status, uncertain), "ARCHIVE_" + receipt.status, **evidence)
                if (receipt.stored_hash != digest or not receipt.provider or not receipt.object_key
                    or not receipt.public_reference or not self._timestamp(receipt.stored_at)):
                    await fail("HASH_MISMATCH", "INVALID_ARCHIVE_RECEIPT", archive_receipt=asdict(receipt))
                await save(archive_receipt=asdict(receipt), external_object_key=receipt.object_key,
                           external_object_version=receipt.version, external_step="WITNESS",
                           external_step_status="READY", publication_error=None)
            if not row["witness_receipt"]:
                uncertain = row["external_step_status"] in ("UNKNOWN", "IN_PROGRESS")
                await save(external_step="WITNESS", external_step_status="IN_PROGRESS")
                args = (row["project_id"], row["official_stream_id"], slot, digest)
                receipt = await self.witness.check(*args) if uncertain else None
                if receipt is None or receipt.status == "NOT_FOUND":
                    receipt = await self.witness.register(*args, row["archive_receipt"]["public_reference"])
                if receipt.status != "ACCEPTED":
                    evidence = {"witness_receipt": asdict(receipt)} if receipt.status == "CONFLICT" else {}
                    await fail(self._failure(receipt.status, uncertain), "WITNESS_" + receipt.status, **evidence)
                if (receipt.submitted_hash != digest or not receipt.provider
                    or not (receipt.record_id or receipt.sequence)
                    or receipt.sequence is not None and (type(receipt.sequence) is not int or receipt.sequence < 1)
                    or not self._timestamp(receipt.accepted_at)):
                    await fail("HASH_MISMATCH", "INVALID_WITNESS_RECEIPT", witness_receipt=asdict(receipt))
                await save(witness_receipt=asdict(receipt), witness_id=receipt.record_id,
                           witness_sequence=str(receipt.sequence) if receipt.sequence is not None else None,
                           witness_accepted_at=receipt.accepted_at,
                           publication_status="WITNESS_ACCEPTED", external_step="PUBLIC_READ",
                           external_step_status="READY", publication_error=None)
            if row["publication_status"] == "WITNESS_ACCEPTED":
                await save(external_step="PUBLIC_READ", external_step_status="IN_PROGRESS")
                receipt = await self.reader.read(row["archive_receipt"]["public_reference"])
                if receipt.status != "OK":
                    await fail(self._failure(receipt.status), "PUBLIC_READ_"+receipt.status)
                if (not isinstance(receipt.data, bytes) or receipt.data != raw
                    or hashlib.sha256(receipt.data).hexdigest() != digest
                    or receipt.reference != row["archive_receipt"]["public_reference"]):
                    await fail("HASH_MISMATCH", "PUBLIC_READ_HASH_MISMATCH")
                if not receipt.provider or not self._timestamp(receipt.received_at):
                    await fail("FAILED", "INVALID_PUBLIC_READ_RECEIPT")
                saved = asdict(receipt)
                saved.pop("data")
                saved["read_hash"] = digest
                await save(public_read_receipt=saved, public_read_confirmed_at=receipt.received_at,
                           publication_status="PUBLIC_READ_CONFIRMED", external_step="FINALIZE",
                           external_step_status="READY", publication_error=None)
            if row["publication_status"] == "PUBLIC_READ_CONFIRMED":
                await asyncio.to_thread(self.store.complete_external_publication, slot, token, self.clock())
        except (_Stopped, _ClaimLost):
            pass
        except TimeoutError:
            await save(external_step_status="UNKNOWN", publication_error="PROVIDER_TIMEOUT_RECONCILIATION_REQUIRED")
        except Exception as exc:
            # Unexpected transport/adapter errors can occur after remote acceptance.
            await save(external_step_status="UNKNOWN", publication_error=f"{type(exc).__name__}: {exc}")
        finally:
            await asyncio.to_thread(self.store.release_external_publication, slot, token)
        return await asyncio.to_thread(self.store.publication_slot, slot)

    def _timestamp(self, value):
        return type(value) is int and 0 <= value <= self.clock()

    @staticmethod
    def _failure(status, uncertain=False):
        return "CONFLICT" if status == "CONFLICT" else "FAILED" if status == "ERROR" and not uncertain else "UNKNOWN"
