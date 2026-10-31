from copy import deepcopy
import unittest

from paperbot.models import Topic


USER_TOPIC = {
    'id': 'llm',
    'name': '1',
    'description': '我想了解cv，nlp，llm infra领域的最新进展',
    'categories': ['cs. AI'],
    'keywords': ['cv', 'nlp', 'llm infra'],
    'threshold': 70,
    'retrieval_mode': 'broad',
}


class TopicConfigTests(unittest.TestCase):
    def config(self, **changes):
        value = deepcopy(USER_TOPIC)
        value['categories'] = ['cs.AI']
        value.update(changes)
        return value

    def assert_config_error(self, value, field, secret=''):
        with self.assertRaises(ValueError) as caught:
            Topic.from_dict(value)
        self.assertEqual(type(caught.exception).__name__, 'ConfigError')
        self.assertEqual(caught.exception.field, field)
        self.assertIn('例如', str(caught.exception))
        if secret:
            self.assertNotIn(secret, str(caught.exception))

    def test_screenshot_configuration_normalizes_spaces_in_arxiv_category(self):
        topic = Topic.from_dict(deepcopy(USER_TOPIC))
        self.assertEqual(topic.categories, ('cs.AI',))
        self.assertEqual(topic.keywords, ('cv', 'nlp', 'llm infra'))
        self.assertEqual(topic.name, '1')
        self.assertEqual(topic.description, USER_TOPIC['description'])
        self.assertEqual(topic.threshold, 70)
        self.assertEqual(topic.retrieval_mode, 'broad')
        self.assertEqual(USER_TOPIC['categories'], ['cs. AI'])

    def test_category_normalization_deduplicates_without_changing_valid_codes(self):
        topic = Topic.from_dict(self.config(categories=[' cs . AI ', 'cs.AI', 'cs . *', 'hep-th', 'math. OC']))
        self.assertEqual(topic.categories, ('cs.AI', 'cs.*', 'hep-th', 'math.OC'))
        canonical = Topic.from_dict(self.config(categories=['cs.AI', 'cs.*', 'hep-th', 'math.OC']))
        self.assertEqual(topic.fingerprint, canonical.fingerprint)

    def test_categories_reject_query_syntax_and_malformed_codes_with_specific_field(self):
        for category in ('cs.AI OR cat:stat.ML', 'cat:cs.AI', 'cs..AI', 'cs.', 'cs. A I', '*', 'cs.AI)'):
            with self.subTest(category=category):
                self.assert_config_error(self.config(categories=[category]), 'categories')

    def test_non_object_topic_is_actionable(self):
        for value in (None, True, 1, 'token_must_not_echo', [], ['cs.AI']):
            with self.subTest(value_type=type(value).__name__):
                self.assert_config_error(value, 'topics', 'token_must_not_echo')

    def test_required_fields_have_individual_errors_without_implicit_string_conversion(self):
        for field in ('id', 'name', 'description'):
            for value in (None, True, 1, [], {}, '', '   '):
                with self.subTest(field=field, value_type=type(value).__name__):
                    self.assert_config_error(self.config(**{field: value}), field)
            missing = self.config()
            del missing[field]
            self.assert_config_error(missing, field)
        self.assert_config_error(self.config(id='invalid ID'), 'id')

    def test_list_fields_reject_invalid_types_and_nonstring_elements(self):
        for field in ('categories', 'keywords', 'exclude_keywords'):
            for value in (None, True, 1, {}, [None], [True], [1], [[]]):
                with self.subTest(field=field, value_type=type(value).__name__):
                    self.assert_config_error(self.config(**{field: value}), field)

    def test_legacy_comma_separated_fields_are_still_supported(self):
        topic = Topic.from_dict(self.config(categories=' cs. AI, stat.ML ',
                                           keywords='cv, nlp, cv', exclude_keywords='robotics, biology'))
        self.assertEqual(topic.categories, ('cs.AI', 'stat.ML'))
        self.assertEqual(topic.keywords, ('cv', 'nlp'))
        self.assertEqual(topic.exclude_keywords, ('robotics', 'biology'))

    def test_empty_categories_require_at_least_one_keyword(self):
        topic = Topic.from_dict(self.config(categories=[], keywords=['vision']))
        self.assertEqual(topic.categories, ())
        self.assertEqual(topic.keywords, ('vision',))
        self.assert_config_error(self.config(categories=[' '], keywords=[]), 'categories')

    def test_keyword_quotes_and_backslashes_are_rejected_without_echo(self):
        for keyword in ('private-token"query', 'private-token\\query'):
            self.assert_config_error(self.config(keywords=[keyword]), 'keywords', 'private-token')

    def test_threshold_retrieval_mode_and_enabled_are_type_checked(self):
        for value in (True, None, '70', 70.0, -1, 101, []):
            self.assert_config_error(self.config(threshold=value), 'threshold')
        for value in (True, None, 1, [], 'unknown'):
            self.assert_config_error(self.config(retrieval_mode=value), 'retrieval_mode')
        for value in (None, 0, 1, 'false', [], {}):
            self.assert_config_error(self.config(enabled=value), 'enabled')
        self.assertFalse(Topic.from_dict(self.config(enabled=False)).enabled)
        self.assertTrue(Topic.from_dict(self.config(enabled=True)).enabled)

    def test_errors_never_echo_raw_secret_values(self):
        secret = 'private-api-key-that-must-not-appear'
        cases = {
            'id': secret + '/bad', 'name': {'token': secret}, 'description': [secret],
            'categories': [secret + ':bad'], 'keywords': [secret + '"'],
            'exclude_keywords': {'token': secret}, 'threshold': secret,
            'retrieval_mode': secret, 'enabled': secret,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                self.assert_config_error(self.config(**{field: value}), field, secret)


if __name__ == '__main__':
    unittest.main()
