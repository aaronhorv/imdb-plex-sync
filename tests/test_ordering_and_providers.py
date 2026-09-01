import unittest
from unittest.mock import patch

from app import check_streaming_availability, _provider_matches_service
from imdb_scraper import _normalize_imdb_list_url, parse_imdb_csv


class ImdbCsvOrderingTests(unittest.TestCase):
    def test_created_column_is_sorted_newest_first(self):
        csv_text = """Const,Title,Created
tt0000001,Old title,2025-01-01
tt0000002,Newest title,2026-08-31
tt0000003,Middle title,2026-01-15
"""

        items = parse_imdb_csv(csv_text)

        self.assertEqual(
            [item['imdb_id'] for item in items],
            ['tt0000002', 'tt0000003', 'tt0000001'],
        )

    def test_canonical_watchlist_url_gets_newest_first_sort(self):
        url = 'https://www.imdb.com/user/p.example/watchlist/'
        self.assertEqual(
            _normalize_imdb_list_url(url),
            f'{url}?sort=date_added%2Cdesc',
        )


class StreamingProviderMatchingTests(unittest.TestCase):
    def test_string_and_integer_provider_ids_match(self):
        provider = {'provider_id': 8, 'provider_name': 'Netflix'}
        self.assertTrue(_provider_matches_service(provider, {'id': '8', 'region': 'DE'}))

    def test_regional_prime_provider_matches_service_identity(self):
        provider = {'provider_id': 9, 'provider_name': 'Amazon Prime Video'}
        service = {'id': 119, 'key': 'prime', 'region': 'DE'}
        self.assertTrue(_provider_matches_service(provider, service))

    def test_tv_provider_variant_matches_service_name(self):
        provider = {'provider_id': 1796, 'provider_name': 'Netflix Standard with Ads'}
        service = {'id': 8, 'key': 'netflix', 'region': 'DE'}
        self.assertTrue(_provider_matches_service(provider, service))

    @patch('app.add_log')
    @patch('app.requests.get')
    def test_tmdb_failure_is_unknown_not_unavailable(self, mock_get, _mock_log):
        mock_get.side_effect = RuntimeError('temporary failure')

        available, providers = check_streaming_availability(
            123,
            'tv',
            'api-key',
            [{'id': 8, 'key': 'netflix', 'region': 'DE'}],
        )

        self.assertIsNone(available)
        self.assertEqual(providers, [])


if __name__ == '__main__':
    unittest.main()
