from pocketagent.core.textsplit import split_message


def test_short_message_not_split():
    assert split_message("hello", 100) == ["hello"]


def test_empty_message():
    assert split_message("", 100) == []


def test_splits_on_length():
    content = "\n".join(f"line {i}" for i in range(50))
    chunks = split_message(content, 60)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 60 + len("```")  # allow for fence-close padding


def test_reassembled_chunks_preserve_all_lines():
    content = "\n".join(f"line {i}" for i in range(50))
    chunks = split_message(content, 60)
    rejoined = "\n".join(chunks)
    for i in range(50):
        assert f"line {i}" in rejoined


def test_keeps_open_fence_closed_and_reopened_across_split():
    lines = ["intro", "```python"] + [f"x{i} = {i}" for i in range(30)] + ["```", "outro"]
    content = "\n".join(lines)
    chunks = split_message(content, 80)
    assert len(chunks) > 1
    # every chunk that contains an opening fence must also contain a closing fence
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0
