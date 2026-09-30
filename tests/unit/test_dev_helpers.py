from pacds_eval.s3 import dev_credentials


def test_s3_credentials_default_to_the_dev_stack(monkeypatch):
    monkeypatch.delenv("PACDS_S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("PACDS_S3_SECRET_KEY", raising=False)
    assert dev_credentials() == ("pacds-dev", "pacds-dev-secret")


def test_s3_credentials_come_from_the_environment_when_set(monkeypatch):
    monkeypatch.setenv("PACDS_S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("PACDS_S3_SECRET_KEY", "sk")
    assert dev_credentials() == ("ak", "sk")
