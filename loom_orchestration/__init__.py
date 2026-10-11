"""Public helpers for submitting externally authored Loom orchestrations.

The package deliberately contains no Observer repository, Driver, or provider
implementation.  It only builds the public wire contracts and talks to the
Observer data plane.
"""

from .contracts import ClosureContract, ResourceRef, TaskClosure
from .client import LoomClient
from .package import OrchestratorPackage
from .polling import poll_submission
from .program import OrchestrationContext, ProgramSource
from .submission import SubmissionBuilder

__all__ = [
    "ClosureContract",
    "LoomClient",
    "OrchestrationContext",
    "OrchestratorPackage",
    "ProgramSource",
    "ResourceRef",
    "SubmissionBuilder",
    "TaskClosure",
    "poll_submission",
]
