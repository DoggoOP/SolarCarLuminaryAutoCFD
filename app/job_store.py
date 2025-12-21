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
    created_at: float = field(default_factory=time.time)
    status: str = "pending"
    log: List[JobLogEntry] = field(default_factory=list)
    result: Optional[Dict[str, str]] = None
    error: Optional[str] = None
    completed_at: Optional[float] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.job_id,
            "title": self.title,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at)),
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
            # Track completion time for cleanup
            if status in ("completed", "failed"):
                record.completed_at = time.time()

    def get(self, job_id: str) -> Optional[Dict[str, object]]:
        with self._lock:
            record = self._jobs.get(job_id)
            if not record:
                return None
            return record.as_dict()

    def _cleanup_old_jobs(self) -> None:
        """Remove jobs that completed more than 1 day ago."""
        ONE_DAY = 24 * 60 * 60  # seconds
        now = time.time()

        jobs_to_remove = [
            job_id
            for job_id, record in self._jobs.items()
            if record.completed_at is not None
            and (now - record.completed_at) > ONE_DAY
        ]

        for job_id in jobs_to_remove:
            del self._jobs[job_id]

    def list_jobs(self) -> List[Dict[str, object]]:
        """Return all jobs sorted by creation time (newest first)."""
        with self._lock:
            # Clean up old completed jobs (1 day after completion)
            self._cleanup_old_jobs()

            sorted_jobs = sorted(
                self._jobs.values(),
                key=lambda job: job.created_at,
                reverse=True  # Newest first
            )
            return [record.as_dict() for record in sorted_jobs]
