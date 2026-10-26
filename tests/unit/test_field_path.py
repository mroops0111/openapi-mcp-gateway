import pydantic
import pytest

from openapi_mcp_gateway.field_path import FieldPath
from openapi_mcp_gateway.openapi import ToolOverride


class TestParse:
    """``FieldPath.parse`` reads exactly the syntax ``str`` writes, and nothing else."""

    @pytest.mark.parametrize('text', ['photo', 'signature.image', 'attachments[*].content', '[*].data', 'rows[*][*]'])
    def test_valid_path_round_trips(self, text):
        assert str(FieldPath.parse(text)) == text

    @pytest.mark.parametrize('text', ['', '.photo', 'a..b', 'a.', 'items[0].data', 'items[].data', 'a[*]b'])
    def test_malformed_path_is_rejected(self, text):
        with pytest.raises(ValueError, match='Invalid field path'):
            FieldPath.parse(text)

    def test_building_matches_parsing(self):
        """A path built step by step is the one its text parses to."""
        built = FieldPath().child('attachments').each_item().child('content')
        assert built == FieldPath.parse('attachments[*].content')


class TestFind:
    """``FieldPath.find`` reaches every matching value and reports where each one is."""

    def test_wildcard_reaches_every_item_with_its_index(self):
        payload = {'attachments': [{'content': 'a'}, {'content': 'b'}]}

        matches = list(FieldPath.parse('attachments[*].content').find(payload))

        assert [(match.path, match.value) for match in matches] == [
            ('attachments[0].content', 'a'),
            ('attachments[1].content', 'b'),
        ]

    def test_replace_writes_back_into_the_payload(self):
        payload = {'signature': {'image': 'old'}}

        (match,) = FieldPath.parse('signature.image').find(payload)
        match.replace('new')

        assert payload == {'signature': {'image': 'new'}}

    @pytest.mark.parametrize('payload', [{}, {'attachments': 'not a list'}, {'attachments': [{'other': 1}]}, None])
    def test_a_shape_that_does_not_match_yields_nothing(self, payload):
        assert list(FieldPath.parse('attachments[*].content').find(payload)) == []


def test_tool_override_rejects_a_malformed_media_path():
    """A typo in a ``media`` path fails when the config loads, rather than silently matching nothing."""
    with pytest.raises(pydantic.ValidationError, match='Invalid field path'):
        ToolOverride.model_validate({'media': {'items[0].data': {'encoding': 'base64'}}})
