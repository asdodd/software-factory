import pytest

from factory.core.conditional import ConditionalError, render

CAPS = {"subagents": False, "internet": True}


def test_if_else_and_negation():
    text = "a\n<!-- factory:if subagents -->\nS\n<!-- factory:else -->\nN\n<!-- factory:end -->\nb\n" \
           "<!-- factory:if not internet -->\nOFF\n<!-- factory:end -->\nc\n"
    assert render(text, CAPS) == "a\nN\nb\nc\n"
    assert render(text, {"subagents": True, "internet": False}) == "a\nS\nb\nOFF\nc\n"


@pytest.mark.parametrize("bad", [
    "<!-- factory:if subagents -->\nx\n",
    "<!-- factory:end -->\n",
    "<!-- factory:else -->\n",
    "<!-- factory:if subagents -->\n<!-- factory:if internet -->\n<!-- factory:end -->\n<!-- factory:end -->\n",
    "<!-- factory:if telepathy -->\nx\n<!-- factory:end -->\n",
])
def test_malformed_blocks_are_errors(bad):
    with pytest.raises(ConditionalError):
        render(bad, CAPS, "test.md")


def test_text_without_markers_is_unchanged():
    assert render("plain <!-- a normal comment --> text\n", CAPS) == "plain <!-- a normal comment --> text\n"
