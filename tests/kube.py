"""kubectl for tests: the kind-pacds context from the host, the pod's service account in-cluster."""

import os

NAMESPACE = "pacds"


def kubectl(*args: str) -> list[str]:
    # PACDS_KUBE_CONTEXT="" (set in the in-cluster test pod) means: use the pod's service account.
    context = os.environ.get("PACDS_KUBE_CONTEXT", "kind-pacds")
    return ["kubectl", *(["--context", context] if context else []), "-n", NAMESPACE, *args]
