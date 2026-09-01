import tempfile
import unittest
from unittest.mock import patch

from app import ImdbAccessBlockedError, check_streaming_availability, get_imdb_watchlist, _provider_matches_service
from imdb_scraper import _normalize_imdb_list_url, _seed_imdb_profile_cookies, parse_imdb_csv


class FakeBrowserContext:
    def __init__(self):
        self.cookie_jar = []
        self.add_calls = 0

    def cookies(self, _url):
        return self.cookie_jar

    def add_cookies(self, cookies):
        self.cookie_jar = cookies
        self.add_calls += 1


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


class ImdbPersistentProfileTests(unittest.TestCase):
    def test_cookie_seeds_profile_only_once_when_unchanged(self):
        context = FakeBrowserContext()
        cookie = 'session-id=123; at-main=authenticated'

        with tempfile.TemporaryDirectory() as profile_dir:
            self.assertTrue(_seed_imdb_profile_cookies(context, profile_dir, cookie))
            self.assertFalse(_seed_imdb_profile_cookies(context, profile_dir, cookie))

        self.assertEqual(context.add_calls, 1)

    def test_changed_cookie_refreshes_profile(self):
        context = FakeBrowserContext()

        with tempfile.TemporaryDirectory() as profile_dir:
            _seed_imdb_profile_cookies(
                context,
                profile_dir,
                'session-id=123; at-main=first',
            )
            self.assertTrue(
                _seed_imdb_profile_cookies(
                    context,
                    profile_dir,
                    'session-id=456; at-main=second',
                )
            )

        self.assertEqual(context.add_calls, 2)

    def test_profile_can_run_without_configured_cookie(self):
        context = FakeBrowserContext()

        with tempfile.TemporaryDirectory() as profile_dir:
            self.assertFalse(_seed_imdb_profile_cookies(context, profile_dir, ''))

        self.assertEqual(context.add_calls, 0)


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


class ImdbCacheSafetyTests(unittest.TestCase):
    @patch('app.add_log')
    @patch('app.load_imdb_items_cache')
    @patch('app.get_imdb_export_data')
    @patch('app.scrape_imdb_watchlist')
    @patch('app.load_config')
    def test_stale_cache_is_not_returned_as_fresh_data(
        self,
        mock_config,
        mock_scrape,
        mock_fallback,
        mock_cache,
        _mock_log,
    ):
        mock_config.return_value = {'imdbCookie': 'cookie'}
        mock_scrape.side_effect = ImdbAccessBlockedError('blocked')
        mock_fallback.return_value = []
        mock_cache.return_value = [{'imdb_id': 'tt0000001', 'title': 'Cached'}]

        items = get_imdb_watchlist('https://www.imdb.com/user/ur123/watchlist/')

        self.assertEqual(items, [])
        mock_cache.assert_called_once()


if __name__ == '__main__':
    unittest.main()
