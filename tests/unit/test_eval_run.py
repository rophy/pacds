import json

from tests.eval_run import errors, record


def test_record_keeps_commit_model_and_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "m1")
    info = record(tmp_path, ["--replay", "--set hard"])
    saved = json.loads((tmp_path / "run.json").read_text())
    assert saved == info and saved["llm"]["model"] == "m1" and saved["args"] == ["--replay", "--set hard"]
    assert len(saved["commit"]) == 40


def test_errors_collects_failed_requests_with_their_ids(tmp_path):
    (tmp_path / "replay-1.json").write_text(json.dumps({"results": [
        {"case_id": "c1", "error": None, "request_id": "r0"},
        {"case_id": "c2", "error": "504 budget", "request_id": "r1"},
    ]}))
    (tmp_path / "support-1.json").write_text(json.dumps({"results": [
        {"case_id": "c3", "error": None, "pacds_requests": [
            {"request_id": "r2", "reply": {"answers": {}}},
            {"request_id": "r3", "reply": {"error": {"status": 500, "code": "engine_error", "message": "the engine failed"}}},
        ]},
        {"case_id": "c4", "error": "APITimeoutError()", "pacds_requests": []},
    ]}))
    (tmp_path / "run.json").write_text("{}")
    found = errors(tmp_path)
    assert [(f["case_id"], f["request_id"], f["code"]) for f in found] == [
        ("c2", "r1", None), ("c3", "r3", "engine_error"), ("c4", None, "agent_failed")]
    assert json.loads((tmp_path / "errors.json").read_text()) == found


def test_repeats_number_each_case_in_order():
    from tests.eval_run import repeats
    from tests.replay.harness import Case

    a, b = (Case(id=i, truth="A", repo="u", ref="r", report="x") for i in ("a", "b"))
    assert [(case.id, n) for case, n in repeats([a, b] * 2)] == [("a", 1), ("b", 1), ("a", 2), ("b", 2)]


def test_client_traces_are_written_per_case_and_repeat_without_signatures(tmp_path):
    from pacds.engine.trace import Trace
    from tests.eval_run import write_trace

    trace = Trace(case_id="c1")
    trace.info["url"] = "https://s3/x?X-Amz-Signature=abc123&X-Amz-Date=d"
    write_trace(tmp_path / "support-1", "c1", 2, trace)
    text = (tmp_path / "support-1" / "c1-2.json").read_text()
    assert json.loads(text)["case_id"] == "c1" and "abc123" not in text


def test_manifest_hashes_every_prompt_and_keeps_the_pacds_config(tmp_path):
    from tests.eval_run import manifest

    (tmp_path / "run.json").write_text(json.dumps({"commit": "c"}))
    (tmp_path / "pacds-config.json").write_text(json.dumps({"llm": {"model": "m", "api_key": "<redacted>"}}))
    info = manifest(tmp_path, tmp_path / "pacds-config.json")
    assert info["commit"] == "c" and info["pacds_config"]["llm"]["model"] == "m"
    assert {"pacds.system", "pacds.final", "pacds.tools", "baseline.system", "skill.tech-support", "skill.pacds"} <= set(info["prompts"])
    assert all(len(value) == 64 for value in info["prompts"].values())


def test_finish_lists_evaluations_cases_and_traces(tmp_path):
    from tests.eval_run import finish

    (tmp_path / "run.json").write_text("{}")
    cases = [{"id": "c1", "truth": "D", "set": "hard", "tier": "certain"}]
    (tmp_path / "support-1.json").write_text(json.dumps({"variant": "full", "repeat": 3, "cases": cases, "results": [{}, {}, {}]}))
    (tmp_path / "replay-1.json").write_text(json.dumps({"baseline": True, "repeat": 1, "cases": cases, "results": [{}]}))
    for name in ("support-1/c1-1.json", "support-1/c1-2.json", "pacds/r1.json"):
        (tmp_path / "traces" / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "traces" / name).write_text("{}")
    info = finish(tmp_path)
    assert [(e["file"], e["variant"], e["repeat"], e["traces"]) for e in info["evaluations"]] == [
        ("replay-1.json", "baseline", 1, 0), ("support-1.json", "full", 3, 2)]
    assert info["cases"] == cases and info["pacds_traces"] == 1


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload_file(self, path, bucket, key):
        self.objects[f"{bucket}/{key}"] = open(path, "rb").read()

    def download_file(self, bucket, key, path):
        open(path, "wb").write(self.objects[f"{bucket}/{key}"])


def test_archive_round_trip(tmp_path, monkeypatch):
    from tests import eval_run

    s3 = FakeS3()
    monkeypatch.setattr(eval_run, "_archive", lambda: (s3, "bucket", "pacds/eval-runs/"))
    run = tmp_path / "runs" / "20260928T120000Z"
    (run / "traces" / "pacds").mkdir(parents=True)
    (run / "run.json").write_text('{"commit": "c"}')
    (run / "traces" / "pacds" / "r1.json").write_text('{"calls": []}')
    assert eval_run.archive(run) == "s3://bucket/pacds/eval-runs/20260928T120000Z.tar.gz"
    assert s3.objects["bucket/pacds/eval-runs/20260928T120000Z.run.json"] == b'{"commit": "c"}'
    fetched = eval_run.fetch("20260928T120000Z", tmp_path / "fetched")
    assert (fetched / "traces" / "pacds" / "r1.json").read_text() == '{"calls": []}'


def test_archive_needs_its_own_credentials_setting(monkeypatch):
    import pytest

    from tests import eval_run

    monkeypatch.delenv(eval_run.ARCHIVE_ENV, raising=False)
    with pytest.raises(SystemExit):
        eval_run._archive()


class ListingS3(FakeS3):
    def get_paginator(self, name):
        s3 = self

        class Paginator:
            def paginate(self, Bucket, Prefix, Delimiter=None):
                keys = [k.removeprefix(f"{Bucket}/") for k in s3.objects if k.startswith(f"{Bucket}/{Prefix}")]
                if Delimiter:
                    top = {k for k in keys if "/" not in k.removeprefix(Prefix)}
                    folders = {Prefix + k.removeprefix(Prefix).split("/")[0] + "/" for k in keys if k not in top}
                    return [{"Contents": [{"Key": k, "Size": 5} for k in sorted(top)], "CommonPrefixes": [{"Prefix": f} for f in sorted(folders)]}]
                return [{"Contents": [{"Key": k, "Size": 5} for k in sorted(keys)]}]
        return Paginator()

    def download_file(self, bucket, key, path):
        if f"{bucket}/{key}" not in self.objects:
            raise RuntimeError("404")
        super().download_file(bucket, key, path)


def test_sync_uploads_only_new_or_changed_files_and_fetch_recovers_a_partial_run(tmp_path, monkeypatch):
    import time

    from tests import eval_run

    s3 = ListingS3()
    monkeypatch.setattr(eval_run, "_archive", lambda: (s3, "bucket", "runs/"))
    run = tmp_path / "20260928T150000Z"
    (run / "traces" / "pacds").mkdir(parents=True)
    (run / "run.json").write_text("{}")
    (run / "traces" / "pacds" / "r1.json").write_text("{}")
    assert eval_run.sync(run) == 2 and eval_run.sync(run) == 0
    time.sleep(0.01)
    (run / "traces" / "pacds" / "r2.json").write_text('{"x": 1}')
    assert eval_run.sync(run) == 1
    assert "bucket/runs/20260928T150000Z/traces/pacds/r2.json" in s3.objects
    assert eval_run.list_archived() == ["20260928T150000Z"]
    fetched = eval_run.fetch("20260928T150000Z", tmp_path / "fetched")
    assert (fetched / "traces" / "pacds" / "r2.json").read_text() == '{"x": 1}' and not (fetched / ".synced.json").exists()


def test_collect_traces_copies_only_this_runs_requests(tmp_path):
    from tests.eval_run import collect_traces

    run, source = tmp_path / "run", tmp_path / "source"
    run.mkdir(); source.mkdir()
    (run / "support-1.json").write_text(json.dumps({"results": [{"case_id": "c", "pacds_requests": [{"request_id": "r1"}, {"request_id": None}]}]}))
    (run / "replay-1.json").write_text(json.dumps({"results": [{"case_id": "c", "request_id": "r2"}]}))
    for name in ("r1", "r2", "other"):
        (source / f"{name}.json").write_text("{}")
    assert collect_traces(run, source) == 2
    assert sorted(p.stem for p in (run / "traces" / "pacds").iterdir()) == ["r1", "r2"]


def test_llm_extra_body_is_a_json_object():
    import pytest

    from tests.eval_run import llm_extra_body

    assert llm_extra_body({}) == {}
    assert llm_extra_body({"LLM_EXTRA_BODY": '{"chat_template_kwargs": {"enable_thinking": true}}'}) == {"chat_template_kwargs": {"enable_thinking": True}}
    with pytest.raises(ValueError):
        llm_extra_body({"LLM_EXTRA_BODY": "[1]"})


def test_archive_endpoint_reaches_the_s3_client(monkeypatch):
    import boto3

    from tests import eval_run

    seen = {}
    monkeypatch.setattr(boto3, "client", lambda *a, **k: seen.update(k) or object())
    monkeypatch.setenv("PACDS_EVAL_ARCHIVE_S3_URI", "s3://runs/pacds/")
    monkeypatch.setenv("PACDS_EVAL_ARCHIVE_ENDPOINT", "https://minio.corp.example")
    _, bucket, prefix = eval_run._archive()
    assert seen["endpoint_url"] == "https://minio.corp.example" and (bucket, prefix) == ("runs", "pacds/")
