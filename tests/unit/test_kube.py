from tests.kube import NAMESPACE, kubectl
from tests.s3 import dev_credentials


def test_host_runs_target_the_kind_context(monkeypatch):
    monkeypatch.delenv("PACDS_KUBE_CONTEXT", raising=False)
    assert kubectl("get", "pods") == ["kubectl", "--context", "kind-pacds", "-n", NAMESPACE, "get", "pods"]


def test_in_cluster_runs_use_the_pod_service_account(monkeypatch):
    monkeypatch.setenv("PACDS_KUBE_CONTEXT", "")
    assert kubectl("get", "pods") == ["kubectl", "-n", NAMESPACE, "get", "pods"]


def test_everything_lives_in_the_pacds_namespace():
    assert NAMESPACE == "pacds"


def test_s3_credentials_come_from_the_environment_when_set(monkeypatch):
    monkeypatch.setenv("PACDS_S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("PACDS_S3_SECRET_KEY", "sk")
    assert dev_credentials() == ("ak", "sk")
