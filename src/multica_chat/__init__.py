"""Multi-agent chat platform service.

Implements the backend core for MARS-20: durable request records, request and
supervisor-task state machines, three execution strategies (linear / parallel /
supervisor), invoke-gate security, budgets and audit.
"""

from .config import ServiceConfig
from .service import MultiTriggerService

__all__ = ["ServiceConfig", "MultiTriggerService"]
