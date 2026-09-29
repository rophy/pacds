from pacds.engine.claude_code import Result, Turn
from pacds.engine.replay import Recordings
from pacds.engine.trace import SESSION_PHASE, Trace, messages_at


def _result(output=None) -> Result:
    return Result(session_id="s-1", subtype="success", is_error=False, structured_output=output if output is not None else {"a": 1},
                  turns=[Turn("Reading.", [{"id": "t1", "name": "read_file", "arguments": '{"path": "x"}'}], {"input": 10, "output": 2, "cached": 0}, "m"),
                         Turn("", [], {"input": 12, "output": 5, "cached": 10}, "m")],
                  tool_results={"t1": "body"}, usage={"input": 22, "output": 7, "cached": 10}, cost_usd=0.01, num_turns=3, text="", latency_ms=900)


def test_add_session_writes_turns_and_session():
    trace = Trace(request_id="r")
    trace.add_session(_result(), request_sha256="h", label="investigate")
    assert [c["phase"] for c in trace.calls] == [SESSION_PHASE, SESSION_PHASE]
    assert all(c["request_sha256"] is None for c in trace.calls)
    assert trace.calls[0]["response"]["tool_calls"] == [{"id": "t1", "name": "read_file", "arguments": '{"path": "x"}'}]
    assert messages_at(trace.calls, 2)[-1] == {"role": "tool", "tool_call_id": "t1", "content": "body"}
    session = trace.info["sessions"][0]
    assert session["request_sha256"] == "h" and session["cost_usd"] == 0.01 and session["replayed"] is False
    assert trace.usage()["input"] == 22 and trace.tools == []


def test_replayed_session_rebuilds_tools():
    trace = Trace()
    trace.add_session(_result(), request_sha256="h", label="investigate", replayed=True, tools_from_result=True)
    assert all(c["replayed"] for c in trace.calls)
    assert trace.tools[0]["name"] == "read_file" and trace.tools[0]["result"] == "body"
    assert trace.usage()["replayed_calls"] == 2


def test_recordings_serve_sessions_once():
    trace = Trace()
    trace.add_session(_result(), request_sha256="h", label="investigate")
    recordings = Recordings([trace.to_dict()])
    assert recordings.take_session("h") == _result()
    assert recordings.take_session("h") is None
    assert recordings.take_session("other") is None


def test_sessions_without_answer_are_not_recorded():
    trace = Trace()
    failed = _result()
    failed.structured_output = None
    trace.add_session(failed, request_sha256="h", label="investigate")
    assert Recordings([trace.to_dict()]).take_session("h") is None
