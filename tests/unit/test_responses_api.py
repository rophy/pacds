from openai.types.responses import Response

from pacds.engine.responses_api import from_response, request_kwargs

TOOL = {
    "type": "function",
    "function": {"name": "read_file", "description": "Read a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}},
}


def response(output: list[dict], *, status: str = "completed", incomplete: str | None = None) -> Response:
    return Response.model_validate({
        "id": "resp_1", "object": "response", "created_at": 0, "model": "m", "status": status,
        "incomplete_details": {"reason": incomplete} if incomplete else None,
        "output": output, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                  "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
    })


def message(text: str) -> dict:
    return {"type": "message", "id": "m1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def function_call(name: str, arguments: str, call_id: str = "call_1") -> dict:
    return {"type": "function_call", "id": "fc1", "call_id": call_id, "name": name, "arguments": arguments, "status": "completed"}


def test_chat_messages_become_input_items():
    chat = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "print('hi')"},
        {"role": "assistant", "content": "done"},
    ]
    assert request_kwargs(messages=chat)["input"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "question"},
        {"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": '{"path": "a.py"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "print('hi')"},
        {"role": "assistant", "content": "done"},
    ]


def test_assistant_text_alongside_tool_calls_is_kept():
    chat = [{"role": "assistant", "content": "let me look", "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "list_files", "arguments": "{}"}},
    ]}]
    assert request_kwargs(messages=chat)["input"] == [
        {"role": "assistant", "content": "let me look"},
        {"type": "function_call", "call_id": "c", "name": "list_files", "arguments": "{}"},
    ]


def test_tools_are_flattened():
    assert request_kwargs(messages=[], tools=[TOOL])["tools"] == [
        {"type": "function", "name": "read_file", "description": "Read a file",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}, "strict": False},
    ]


def test_json_schema_response_format_becomes_text_format():
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    kwargs = request_kwargs(messages=[], response_format={"type": "json_schema", "json_schema": {"name": "evaluation", "schema": schema, "strict": True}})
    assert kwargs["text"] == {"format": {"type": "json_schema", "name": "evaluation", "schema": schema, "strict": True}}


def test_json_object_response_format_becomes_text_format():
    assert request_kwargs(messages=[], response_format={"type": "json_object"})["text"] == {"format": {"type": "json_object"}}


def test_requests_are_stateless():
    assert request_kwargs(messages=[])["store"] is False


def test_text_output_maps_to_stop():
    completion = from_response(response([message('{"answers": {}}')]))
    choice = completion.choices[0]
    assert (choice.finish_reason, choice.message.content, choice.message.tool_calls) == ("stop", '{"answers": {}}', None)
    assert (completion.usage.prompt_tokens, completion.usage.completion_tokens) == (10, 5)


def test_function_calls_map_to_tool_calls():
    completion = from_response(response([function_call("read_file", '{"path": "a.py"}'), function_call("list_files", "{}", "call_2")]))
    choice = completion.choices[0]
    assert choice.finish_reason == "tool_calls"
    assert [(c.id, c.function.name, c.function.arguments) for c in choice.message.tool_calls] == [
        ("call_1", "read_file", '{"path": "a.py"}'),
        ("call_2", "list_files", "{}"),
    ]


def test_reasoning_items_are_ignored():
    reasoning = {"type": "reasoning", "id": "r1", "summary": []}
    choice = from_response(response([reasoning, message("ok")])).choices[0]
    assert (choice.finish_reason, choice.message.content) == ("stop", "ok")


def test_incomplete_response_maps_to_length():
    choice = from_response(response([message('{"answ')], status="incomplete", incomplete="max_output_tokens")).choices[0]
    assert choice.finish_reason == "length"


def test_content_filter_maps_to_content_filter():
    choice = from_response(response([], status="incomplete", incomplete="content_filter")).choices[0]
    assert choice.finish_reason == "content_filter"


def test_cached_and_reasoning_tokens_are_kept():
    raw = response([message("hi")]).model_copy(deep=True)
    raw.usage.input_tokens_details.cached_tokens = 7
    raw.usage.output_tokens_details.reasoning_tokens = 3
    usage = from_response(raw).usage
    assert usage.prompt_tokens_details.cached_tokens == 7 and usage.completion_tokens_details.reasoning_tokens == 3
