"""
log.py — Structured JSON logging setup (same format as the other services).

Call configure() once at startup.  After that, use standard logging calls
anywhere in the codebase — every record is emitted as a single JSON line:

    {"timestamp": "2026-03-30T12:00:00.000Z", "level": "INFO",
     "logger": "sentinel.collector", "message": "..."}

The 'extra' dict on any log call is merged into the JSON object, making it
easy to attach structured context (vm_uuid, node_uuid, dimension, etc.).
"""

import logging
import sys

from pythonjsonlogger import jsonlogger


class _JsonFormatter(jsonlogger.JsonFormatter):
    def add_fields(self, log_record: dict, record: logging.LogRecord, message_dict: dict) -> None:
        super().add_fields(log_record, record, message_dict)
        # normalise field names to a consistent schema
        log_record['timestamp'] = log_record.pop('asctime', None) or self.formatTime(record)
        log_record['level']     = record.levelname
        log_record['logger']    = record.name
        # remove redundant fields added by the base formatter
        log_record.pop('levelname', None)
        log_record.pop('name',      None)


def configure(level: str = 'INFO') -> None:
    """
    Configure the root logger to emit JSON to stdout.
    Call once, before any other logging.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter(
        fmt='%(asctime)s %(levelname)s %(name)s %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S',
    ))

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.addHandler(handler)

    # quiet down noisy third-party loggers
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('influxdb_client').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
