"""Offline verifier regressions; execute only with the guarded test harness."""
import copy
import unittest
from unittest.mock import Mock, patch

from capture import BASE, Client, LookupFailure, epoch, usable_html


FIELDS = ['timestamp', 'original', 'statuscode', 'mimetype', 'digest']
URL = BASE + '/collections/all?page=2&sort=title'
TS = '20261103120000'
NOW = epoch('20261103120100')
GOOD = '<html><title>Somerville Watch Co.</title><body>' + 'Watch collection ' * 60 + '</body></html>'


class CDXSchemaCandidateTests(unittest.TestCase):
    def client(self, data=None):
        client = Client()
        row = [TS, URL, '200', 'text/html', 'digest']
        client.json = Mock(return_value=(200, {}, [FIELDS, row] if data is None else data))
        client.request = Mock(return_value=(200, {'Content-Type': 'text/html'}, GOOD,
            'https://web.archive.org/web/' + TS + 'id_/' + URL))
        return client

    def verify(self, client):
        with patch('capture.time.time', return_value=NOW):
            return client.verify(URL, epoch('20261103000000'), until=epoch('20261103235959'))

    def assert_lookup_failure(self, data):
        client = self.client(data)
        with self.assertRaises(LookupFailure):
            self.verify(client)
        client.request.assert_not_called()

    def test_malformed_outer_schema(self):
        for data in (None, {}, '', 0, [None], [{}], [['timestamp']], [[*FIELDS, 'extra']],
                     [[*FIELDS[:-1], 'timestamp']], [[*FIELDS[:-1], []]]):
            with self.subTest(data=data):
                # None is normally the helper's default sentinel.
                client = self.client()
                client.json.return_value = (200, {}, data)
                with self.assertRaises(LookupFailure):
                    self.verify(client)
                client.request.assert_not_called()

    def test_malformed_row_containers(self):
        for row in (None, False, 3, 'abcde', {}, [], [TS], [TS, URL, '200', 'text/html', 'digest', 'extra']):
            with self.subTest(row=row):
                self.assert_lookup_failure([FIELDS, row])

    def test_all_columns_reject_invalid_types_and_controls(self):
        for index in range(len(FIELDS)):
            for value in (None, False, 7, [], {}, '', 'bad\nvalue', 'bad\x00value'):
                with self.subTest(column=FIELDS[index], value=value):
                    row = [TS, URL, '200', 'text/html', 'digest']
                    row[index] = value
                    self.assert_lookup_failure([FIELDS, row])

    def test_timestamp_must_be_exact_and_calendar_valid(self):
        for ts in ('2026110312000', '202611031200000', '20260230120000', '20261303120000',
                   '20261103240000', '20261103120060', '00001103120000', '２０２６１１０３１２００００'):
            with self.subTest(ts=ts):
                self.assert_lookup_failure([FIELDS, [ts, URL, '200', 'text/html', 'digest']])

    def test_malformed_original_urls_fail_typed(self):
        for url in ('https://[broken', 'https://', '/relative', 'javascript:alert(1)',
                    'https://user:password@somervillewatchco.com/', BASE + ':broken/',
                    BASE + ':1234/', BASE + '/has space'):
            with self.subTest(url=url):
                self.assert_lookup_failure([FIELDS, [TS, url, '200', 'text/html', 'digest']])

    def test_malformed_status_code_fails_typed(self):
        for code in ('ok', '0200', '99', '600', '200 '):
            with self.subTest(code=code):
                self.assert_lookup_failure([FIELDS, [TS, URL, code, 'text/html', 'digest']])

    def test_malformed_mime_fails_typed(self):
        for mime in ('html', 'text/html ', 'text/', '/html', 'text/html; charset=utf-8'):
            with self.subTest(mime=mime):
                self.assert_lookup_failure([FIELDS, [TS, URL, '200', mime, 'digest']])

    def test_malformed_row_not_hidden_by_valid_candidate(self):
        valid = [TS, URL, '200', 'text/html', 'digest']
        for rows in ([None, valid], [valid, None]):
            with self.subTest(rows=rows):
                self.assert_lookup_failure([FIELDS, *rows])

    def test_malformed_non_html_row_is_not_absence(self):
        self.assert_lookup_failure([FIELDS, [None, URL, '404', 'image/png', '-']])

    def test_response_not_mutated(self):
        data = [FIELDS, [TS, URL, '200', 'text/html', 'digest']]
        original = copy.deepcopy(data)
        self.assertIsNotNone(self.verify(self.client(data)))
        self.assertEqual(data, original)

    def test_empty_response_and_header_only_are_absence(self):
        for data in ([], [FIELDS]):
            with self.subTest(data=data):
                client = self.client(data)
                self.assertIsNone(self.verify(client))
                client.request.assert_not_called()

    def test_valid_nonmatching_rows_are_not_success(self):
        for row in ([TS, URL, '404', 'text/html', 'digest'],
                    [TS, URL, '-', '-', '-'],
                    [TS, URL, '200', 'image/png', 'digest'],
                    [TS, BASE + '/other', '200', 'text/html', 'digest'],
                    ['20261104120000', URL, '200', 'text/html', 'digest']):
            with self.subTest(row=row):
                client = self.client([FIELDS, row])
                self.assertIsNone(self.verify(client))
                client.request.assert_not_called()

    def test_column_order_can_change_without_ambiguity(self):
        data = [list(reversed(FIELDS)), list(reversed([TS, URL, '200', 'text/html', 'digest']))]
        self.assertEqual(self.verify(self.client(data))['timestamp'], TS)

    def test_verification_upper_clock_bound_is_frozen(self):
        client = self.client([FIELDS, ['20261103120300', URL, '200', 'text/html', 'digest']])
        with patch('capture.time.time', side_effect=[NOW, NOW + 600]):
            self.assertIsNone(client.verify(URL, epoch('20261103000000')))
        client.request.assert_not_called()

    def test_exact_time_historical_reconciliation_remains_inclusive(self):
        client = self.client()
        with patch('capture.time.time', return_value=NOW):
            result = client.verify(URL, epoch(TS), until=epoch(TS))
        self.assertEqual(result['timestamp'], TS)
        self.assertIn('from=' + TS, client.json.call_args.args[0])
        self.assertIn('to=' + TS, client.json.call_args.args[0])


class CanonicalParserCandidateTests(unittest.TestCase):
    def test_active_wrong_canonical_variants_rejected(self):
        for tag in ('<link rel=canonical href=/other>', '<LINK HREF=/other REL=CANONICAL>',
                    '<link href="/other" data-x="quoted > value" rel="alternate CANONICAL">',
                    "<link href='/other' rel='canonical alternate'>", '<link rel=canonical href=//evil.example/>',
                    '<link rel=canonical href="https://[broken">', '<link rel=canonical>',
                    '<link rel=canonical href>', '<link rel=canonical href="">',
                    '<link rel=canonical href="   ">', '<link rel=canonical href="?page=1&amp;sort=title">',
                    '<link rel=canonical href="https://user@somervillewatchco.com/collections/all?page=2&sort=title">',
                    '<link rel=canonical href="https://somervillewatchco.com:444/collections/all?page=2&sort=title">'):
            with self.subTest(tag=tag):
                self.assertFalse(usable_html(GOOD + tag, URL))

    def test_matching_relative_query_entities_and_rel_tokens(self):
        for tag in ('<link rel=canonical href="?page=2&amp;sort=title">',
                    '<link href="/collections/all?sort=title&#38;page=2" rel="alternate CANONICAL">',
                    '<LINK HREF="all?page=2&sort=title" REL=canonical>',
                    '<link rel=canonical href="http://www.somervillewatchco.com/collections/all?page=2&sort=title">',
                    '<link rel="alternate\tcanonical\nstylesheet" href="?page=2&sort=title">'):
            with self.subTest(tag=tag):
                self.assertTrue(usable_html(GOOD + tag, URL))

    def test_inert_canonical_links_are_ignored(self):
        wrong = '<link rel=canonical href=/other>'
        for markup in ('<script>' + wrong + '</script>', '<style>' + wrong + '</style>',
                       '<template>' + wrong + '</template>',
                       '<template><template>' + wrong + '</template></template>',
                       '<!--' + wrong + '-->'):
            with self.subTest(markup=markup):
                self.assertTrue(usable_html(GOOD + markup, URL))
                self.assertFalse(usable_html(GOOD + markup + wrong, URL))

    def test_duplicate_attributes_and_conflicting_links_are_rejected(self):
        for tag in ('<link rel=canonical href="?page=2&sort=title" href=/other>',
                    '<link rel=alternate rel=canonical href="?page=2&sort=title">',
                    '<link rel=canonical href="?page=2&sort=title"><link rel=canonical href=/other>'):
            with self.subTest(tag=tag):
                self.assertFalse(usable_html(GOOD + tag, URL))

    def test_first_active_base_controls_relative_canonical(self):
        self.assertFalse(usable_html(GOOD + '<base href="https://evil.example/">'
            '<link rel=canonical href="/collections/all?page=2&sort=title">', URL))
        self.assertTrue(usable_html(GOOD + '<template><base href="https://evil.example/"></template>'
            '<link rel=canonical href="?page=2&sort=title">', URL))
        self.assertTrue(usable_html(GOOD + '<base href="' + BASE + '/collections/">'
            '<base href="https://evil.example/"><link rel=canonical href="all?page=2&sort=title">', URL))

    def test_no_canonical_or_noncanonical_link_remains_usable(self):
        self.assertTrue(usable_html(GOOD, URL))
        self.assertTrue(usable_html(GOOD + '<link rel=stylesheet href=/other>', URL))
