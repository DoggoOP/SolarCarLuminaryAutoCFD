from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from uuid import uuid4


@dataclass
class JobLogEntry:
    timestamp: float
    message: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp)),
            "message": self.message,
        }


@dataclass
class JobRecord:
    job_id: str
    title: str
    status: str = "pending"
    log: List[JobLogEntry] = field(default_factory=list)
    result: Optional[Dict[str, str]] = None
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.job_id,
            "title": self.title,
            "status": self.status,
            "log": [entry.as_dict() for entry in self.log],
            "result": self.result,
            "error": self.error,
        }


class JobStore:
    """Very small in-memory job tracker for long running CFD runs."""

    def __init__(self) -> None:
        self._jobs: Dict[str, JobRecord] = {}
        self._lock = threading.Lock()

    def create(self, title: str) -> str:
        job_id = uuid4().hex
        record = JobRecord(job_id=job_id, title=title)
        with self._lock:
            self._jobs[job_id] = record
        return job_id

    def append(self, job_id: str, message: str) -> None:
        entry = JobLogEntry(timestamp=time.time(), message=message)
        with self._lock:
            if job_id not in self._jobs:
                return
            self._jobs[job_id].log.append(entry)

    def set_status(
        self,
        job_id: str,
        status: str,
        *,
        result: Optional[Dict[str, str]] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if not record:
                return
            record.status = status
            if result is not None:
                record.result = result
            if error is not None:
                record.error = error

    def get(self, job_id: str) -> Optional[Dict[str, object]]:
        with self._lock:
            record = self._jobs.get(job_id)
            if not record:
                return None
            return record.as_dict()

    def list_jobs(self) -> List[Dict[str, object]]:
        with self._lock:
            return [record.as_dict() for record in self._jobs.values()]
