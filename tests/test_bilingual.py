from pocketagent.core.bilingual import clean_reply, strip_ansi


def test_plain_english_is_untouched():
    text = "Hello.\n\n```py\nx = 1\n```"
    assert clean_reply(text) == text


def test_strips_ansi_codes():
    assert strip_ansi("\x1b[90mgray\x1b[39m \x1b]8;;http://x\x07link\x1b]8;;\x07") == "gray link"


def test_colored_translation_becomes_quote():
    text = "Cats sleep a lot.\n\x1b[90m猫睡得很多。\x1b[39m"
    assert clean_reply(text) == "Cats sleep a lot.\n> 猫睡得很多。"


def test_uncolored_cjk_line_after_english_becomes_quote():
    assert clean_reply("Run it first.\n首先运行它。") == "Run it first.\n> 首先运行它。"


def test_translated_list_item_quote_drops_indent():
    assert clean_reply("- Run `pip`\n  运行 `pip`") == "- Run `pip`\n> 运行 `pip`"


def test_heading_translation_is_split_out():
    assert clean_reply("## Setup / 设置") == "## Setup\n> 设置"


def test_all_chinese_reply_is_not_quoted():
    text = "你好！\n今天做什么？"
    assert clean_reply(text) == text


def test_code_fences_and_tables_are_left_alone():
    text = "Example:\n```\nprint('x')\n打印\n```\n| a |\n|---|\n| 中 |"
    assert clean_reply(text) == text


G, E = "\x1b[90m", "\x1b[39m"  # bbilingual's "gray" style


def test_colored_translation_full_of_code_and_urls_becomes_quote():
    # bbilingual colors only the plain pieces; code spans stay uncolored. Most
    # letters here are Latin, but the colors still mark it as the translation.
    text = (
        "The fix is committed as `0c9fa0d`, pushed to `buaazp:telegram-formatting-and-fixes`,"
        " and PR #1 is updated: https://github.com/bydsky/pocketagent/pull/1\n"
        f"{G}该修复已提交为{E}`0c9fa0d`{G}，推送到{E}`buaazp:telegram-formatting-and-fixes`"
        f"{G}，并且PR #1已更新：https://github.com/bydsky/pocketagent/pull/1{E}"
    )
    assert clean_reply(text).split("\n")[1] == (
        "> 该修复已提交为`0c9fa0d`，推送到`buaazp:telegram-formatting-and-fixes`"
        "，并且PR #1已更新：https://github.com/bydsky/pocketagent/pull/1"
    )


def test_colored_translation_of_bullet_quoting_english_title_becomes_quote():
    text = (
        '- **Commit:** "Keep each assistant message\'s bilingual translation in its own quote."'
        " It contains only the `engine.py` change and its test.\n"
        f"  **{G}Commit{E}**{G}：\"Keep each assistant message's bilingual translation in its own"
        f" quote。\"它只包含{E}`engine.py`{G}的改动及其测试。{E}"
    )
    assert clean_reply(text).split("\n")[1] == (
        "> **Commit**：\"Keep each assistant message's bilingual translation in its own"
        " quote。\"它只包含`engine.py`的改动及其测试。"
    )


def test_uncolored_translation_ignores_code_and_urls_when_counting():
    text = "Pushed `0c9fa0d` to `telegram-formatting-and-fixes`.\n已将`0c9fa0d`推送到`telegram-formatting-and-fixes`。"
    assert clean_reply(text) == (
        "Pushed `0c9fa0d` to `telegram-formatting-and-fixes`.\n> 已将`0c9fa0d`推送到`telegram-formatting-and-fixes`。"
    )


def test_translation_next_to_real_quote_stays_separate():
    text = "Done.\n完成。\n> [!NOTE]\n> hi"
    assert clean_reply(text) == "Done.\n> 完成。\n\n> [!NOTE]\n> hi"
