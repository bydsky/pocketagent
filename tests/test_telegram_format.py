from pocketagent.platforms.telegram_format import markdown_to_telegram_html as to_html


def test_escapes_html_special_characters():
    assert to_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_inline_styles():
    assert to_html("**b** *i* _j_ ~~s~~ `c`") == "<b>b</b> <i>i</i> <i>j</i> <s>s</s> <code>c</code>"


def test_snake_case_and_math_are_not_italicized():
    assert to_html("use snake_case_names and 2*3*4") == "use snake_case_names and 2*3*4"


def test_code_span_contents_are_not_formatted():
    assert to_html("`**x** <y>`") == "<code>**x** &lt;y&gt;</code>"


def test_link():
    assert to_html("see [the docs](https://x.y/a_b)") == 'see <a href="https://x.y/a_b">the docs</a>'


def test_fenced_code_block_with_language():
    out = to_html("```python\nif a < b:\n    **x**\n```")
    assert out == '<pre><code class="language-python">if a &lt; b:\n    **x**</code></pre>'


def test_unclosed_fence_is_still_closed():
    assert to_html("```\nx") == "<pre><code>x</code></pre>"


def test_heading_bullets_tasks_rule():
    out = to_html("# Title\n- one\n  - two\n- [ ] todo\n- [x] done\n---")
    assert out == "<b>Title</b>\n• one\n  • two\n☐ todo\n☑ done\n──────────"


def test_quote_and_github_alert():
    assert to_html("> one\n> two") == "<blockquote>one\ntwo</blockquote>"
    assert to_html("> [!NOTE]\n> hi") == "<blockquote><b>Note:</b>\nhi</blockquote>"


def test_table_becomes_aligned_pre_block():
    out = to_html("| a | **bb** |\n|---|---|\n| ccc | 中 |")
    assert out == "<pre>a   │ bb\n────┼───\nccc │ 中</pre>"
