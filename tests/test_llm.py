import json

from backend.llm import Sentences, _partial_text


def test_partial_text_decodes_what_has_arrived():
    assert _partial_text('{"text": "Hi, I') == "Hi, I"
    assert _partial_text('{"text": "He said \\"go') == 'He said "go'
    assert _partial_text('{"text": "Done.\\') == "Done."  # escape still arriving
    assert _partial_text('{"text": "Done."}') == "Done."
    assert _partial_text('{"te') == ""


def test_sentences_split_on_whole_sentences_only():
    out = []
    s = Sentences(out.append)
    for chunk in ["I can move you to Northwind 232 at 9:40", " PM. The fare difference is $84.", "00. Shall I go ahead?"]:
        s.feed(chunk)
    s.flush()
    assert out == ["I can move you to Northwind 232 at 9:40 PM.", "The fare difference is $84.00.", "Shall I go ahead?"]


def test_plain_text_answers_stream_and_spaces_are_normalized():
    import asyncio

    from backend.llm import _read_stream

    class Stream:
        async def aiter_lines(self):
            for chunk in ["\n\n", "I can move you to Northwind 232 at 9:40 PM. ", "Shall I go ahead?"]:
                yield "data: " + json.dumps({"choices": [{"delta": {"content": chunk}}]})
            yield "data: [DONE]"

    heard, spoken = [], {}
    msg = asyncio.run(_read_stream(Stream(), spoken, heard.append))
    assert "".join(heard) == msg["content"] == "I can move you to Northwind 232 at 9:40 PM. Shall I go ahead?"
    assert spoken
