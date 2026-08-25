"""`xa help` explains the model; --help lists the flags.

These tests guard the properties that make it worth having: that every topic
renders, that the overview points only at topics that exist, and that an unknown
topic says what it should have been.
"""

import pytest

from xa.help import TOPICS, render


def test_overview_renders():
    out = render()
    assert "external amygdala" in out
    assert "Monitors emit facts" not in out          # that is the `model` topic


@pytest.mark.parametrize("topic", sorted(TOPICS))
def test_every_topic_renders(topic):
    out = render(topic)
    assert len(out) > 200
    # No unsubstituted placeholders. Checked by name rather than by brace,
    # because the monitor example legitimately contains a dict literal.
    assert "{h_" not in out
    assert "{title}" not in out


def test_overview_only_advertises_topics_that_exist():
    """A help page that points at a page that does not exist is worse than none."""
    import re

    advertised = set(re.findall(r"xa help (\w+)", render()))
    assert advertised
    assert advertised <= set(TOPICS)


def test_unknown_topic_lists_the_real_ones():
    out = render("nonsense")
    assert "no help topic" in out
    assert "model" in out


def test_topics_do_not_leak_format_syntax():
    """The monitor example contains a dict literal, which format() will eat."""
    assert '{"log": excerpt}' in render("monitors")
