"""共享证据层：一次性取证 → 广播给所有下游角色。"""

from agent.evidence.bundle import EvidenceBundle, MemoryHit, findings_from
from agent.evidence.collector import EvidenceCollector, collect_evidence
from agent.evidence.store import EvidenceStore, code_fingerprint
from agent.evidence.verifier import (
    Corroboration,
    VerificationResult,
    VerifierAgent,
    collaboration_text,
)

__all__ = [
    "EvidenceBundle",
    "MemoryHit",
    "findings_from",
    "EvidenceCollector",
    "collect_evidence",
    "EvidenceStore",
    "code_fingerprint",
    "Corroboration",
    "VerificationResult",
    "VerifierAgent",
    "collaboration_text",
]
