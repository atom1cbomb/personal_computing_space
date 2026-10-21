"""Public helpers for submitting externally authored Loom orchestrations.

The package deliberately contains no Observer repository, Driver, or provider
implementation.  It only builds the public wire contracts and talks to the
Observer data plane.
"""

from .contracts import ClosureContract, ResourceRef, TaskClosure
from .client import LoomClient
from .service_package import ServicePackage
from .grip import GripError, Names
from .package import OrchestratorPackage
from .polling import poll_submission
from .program import OrchestrationContext, ProgramSource
from .submission import SubmissionBuilder
from .metadata import ResourceEffect, validate_action_descriptor
from .mcue import McueDefinition

__all__ = [
    "ClosureContract",
    "LoomClient",
    "McueDefinition",
    "ServicePackage",
    "GripError",
    "Names",
    "OrchestrationContext",
    "OrchestratorPackage",
    "ProgramSource",
    "ResourceRef",
    "ResourceEffect",
    "SubmissionBuilder",
    "TaskClosure",
    "poll_submission",
    "validate_action_descriptor",
]
