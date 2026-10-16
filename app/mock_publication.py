"""Deterministic, process-local test providers. No HTTP or external credentials."""
import hashlib
from dataclasses import replace

from .publication_adapters import ArchiveReceipt, WitnessReceipt, PublicReadReceipt


class MockArchive:
    def __init__(self, clock=lambda: 0):
        self.clock = clock
        self.objects = {}
        self.pending = {}
        self.calls = []
        self.behavior = "success"

    async def store_once(self, batch_id, exact_batch_bytes, batch_hash, slot, stream):
        self.calls.append(("store_once", batch_id, exact_batch_bytes, batch_hash, slot, stream))
        if self.behavior == "failure":
            return ArchiveReceipt("mock-archive", status="ERROR")
        if self.behavior == "timeout":
            raise TimeoutError()
        if self.behavior == "delayed":
            pending = (exact_batch_bytes, batch_hash)
            if batch_id in self.pending and self.pending[batch_id] != pending:
                return ArchiveReceipt("mock-archive", status="CONFLICT")
            self.pending[batch_id] = pending
            return ArchiveReceipt("mock-archive", status="UNKNOWN")
        receipt = self._accept(batch_id, exact_batch_bytes, batch_hash)
        if self.behavior == "timeout_after_accept":
            raise TimeoutError()
        return receipt

    def _accept(self, batch_id, raw, digest):
        if batch_id in self.objects:
            old, receipt = self.objects[batch_id]
            if old != raw or receipt.stored_hash != digest:
                return replace(receipt, status="CONFLICT")
            return receipt
        if hashlib.sha256(raw).hexdigest() != digest:
            return ArchiveReceipt("mock-archive", status="CONFLICT")
        receipt = ArchiveReceipt("mock-archive", batch_id, "1", "mock://archive/"+batch_id,
                                 digest, self.clock(), "STORED")
        self.objects[batch_id] = (raw, receipt)
        return receipt

    async def check(self, batch_id, batch_hash):
        self.calls.append(("check", batch_id, batch_hash))
        if batch_id in self.objects:
            receipt = self.objects[batch_id][1]
            return receipt if receipt.stored_hash == batch_hash else replace(receipt, status="CONFLICT")
        return ArchiveReceipt("mock-archive", status="UNKNOWN" if batch_id in self.pending else "NOT_FOUND")

    def accept_pending(self):
        for batch_id, (raw, digest) in self.pending.items():
            self._accept(batch_id, raw, digest)
        self.pending.clear()


class MockWitness:
    def __init__(self, clock=lambda: 0):
        self.clock = clock
        self.records = {}
        self.pending = {}
        self.calls = []
        self.sequence = 0
        self.behavior = "success"

    async def register(self, project_id, official_stream_id, slot_utc, batch_hash, archive_reference):
        key = (project_id, official_stream_id, slot_utc)
        self.calls.append(("register", *key, batch_hash, archive_reference))
        if self.behavior == "failure":
            return WitnessReceipt("mock-witness", submitted_hash=batch_hash, status="ERROR")
        if self.behavior == "timeout":
            raise TimeoutError()
        if self.behavior == "delayed":
            pending = self.pending.setdefault(key, [])
            if (batch_hash, archive_reference) not in pending:
                pending.append((batch_hash, archive_reference))
            return WitnessReceipt("mock-witness", submitted_hash=batch_hash, status="UNKNOWN")
        receipt = self._accept(key, batch_hash)
        if self.behavior == "timeout_after_accept":
            raise TimeoutError()
        return receipt

    def _accept(self, key, digest):
        records = self.records.setdefault(key, [])
        for receipt in records:
            if receipt.submitted_hash == digest:
                return receipt
        self.sequence += 1
        receipt = WitnessReceipt("mock-witness", f"mock-record-{self.sequence}", self.sequence,
                                 self.clock(), digest, "CONFLICT" if records else "ACCEPTED",
                                 records[0].submitted_hash if records else None,
                                 records[0].sequence if records else None)
        records.append(receipt)
        return receipt

    async def check(self, project_id, official_stream_id, slot_utc, batch_hash):
        key = (project_id, official_stream_id, slot_utc)
        self.calls.append(("check", *key, batch_hash))
        records = self.records.get(key, [])
        if records:
            for receipt in records:
                if receipt.submitted_hash == batch_hash:
                    return receipt
            return replace(records[0], status="CONFLICT")
        return WitnessReceipt("mock-witness", submitted_hash=batch_hash,
                              status="UNKNOWN" if key in self.pending else "NOT_FOUND")

    def accept_pending(self):
        for key, submissions in self.pending.items():
            for digest, _ in submissions:
                self._accept(key, digest)
        self.pending.clear()


class MockPublicReader:
    def __init__(self, archive, clock=lambda: 0):
        self.archive, self.clock = archive, clock
        self.calls = []
        self.behavior = "success"

    async def read(self, public_reference):
        self.calls.append(public_reference)
        if self.behavior == "timeout":
            raise TimeoutError()
        found = next((raw for raw, receipt in self.archive.objects.values()
                      if receipt.public_reference == public_reference), None)
        if self.behavior == "failure" or found is None:
            return PublicReadReceipt("mock-reader", public_reference, status="ERROR")
        if self.behavior == "conflict":
            found = found+b"corrupted"
        return PublicReadReceipt("mock-reader", public_reference, found, self.clock(), "OK")
