"""The library map's title detail (2026-11-10): which series or movie a map tile
is, found by folder from the cached title list, and its poster, fetched from the
arr through auditorr.

The tile is a folder, and a folder's name isn't the arr's title (B15), so the
match is by folder like `arr_search`. A miss with an instance down is "couldn't
ask", never "not there" (R1). The poster rides the API address with the key, and
an address the listing gives is used only as a path on that host.
"""
import io
import unittest
import urllib.error
from unittest.mock import patch

import app
import arr


def _conn(cid='sonarr-main', service='sonarr', **kw):
    c = {'id': cid, 'service': service, 'name': kw.pop('name', 'Sonarr'),
         'base_url': kw.pop('base_url', 'http://sonarr:8989'), 'external_url': '',
         'api_key': 'KEY', 'remote_path': '', 'media_path': kw.pop('media_path', '/tv'),
         'local_media_path': kw.pop('local_media_path', '/data/media/television')}
    c.update(kw)
    return c


def _row(folder, **kw):
    r = {'service': 'sonarr', 'connection_id': 'sonarr-main', 'arr_id': 7, 'title': 'PLUR1BUS',
         'title_slug': 'plur1bus', 'year': 2025, 'has_file': True, 'alt_titles': [],
         'folder': folder, 'status': 'continuing', 'network': 'Apple TV+', 'season_count': 1,
         'runtime': 50, 'poster': '/MediaCover/7/poster.jpg?lastWrite=1'}
    r.update(kw)
    return r


CFG = {'MEDIA_PATH': '/data/media'}


class SnapshotRowsTests(unittest.TestCase):
    """The rows carry each item's folder as auditorr sees it, and what the strip says."""

    def setUp(self):
        arr._arr_titles_cache['snapshot'] = None

    def tearDown(self):
        arr._arr_titles_cache['snapshot'] = None

    def test_a_series_row_has_its_mapped_folder_and_detail(self):
        series = [{'id': 7, 'title': 'PLUR1BUS', 'titleSlug': 'plur1bus', 'year': 2025,
                   'path': '/tv/Pluribus', 'status': 'continuing', 'network': 'Apple TV+',
                   'runtime': 50, 'statistics': {'episodeFileCount': 9, 'seasonCount': 1},
                   'images': [{'coverType': 'banner', 'url': '/MediaCover/7/banner.jpg'},
                              {'coverType': 'poster', 'url': '/MediaCover/7/poster.jpg?lastWrite=1'}]}]
        with patch.object(arr, 'normalize_arr_connections', return_value=[_conn()]), \
             patch.object(arr, '_arr_get', return_value=series):
            rows, errors = arr.fetch_arr_all_titles_result(CFG)
        self.assertEqual(errors, [])
        r = rows[0]
        self.assertEqual(r['folder'], '/data/media/television/Pluribus')
        self.assertEqual((r['network'], r['season_count'], r['status']), ('Apple TV+', 1, 'continuing'))
        self.assertEqual(r['poster'], '/MediaCover/7/poster.jpg?lastWrite=1')

    def test_a_movie_row_carries_its_studio(self):
        movies = [{'id': 3, 'title': 'Dune', 'titleSlug': 'dune', 'year': 2021, 'hasFile': True,
                   'path': '/movies/Dune (2021)', 'status': 'released', 'studio': 'Legendary',
                   'runtime': 155, 'images': []}]
        conn = _conn('radarr-main', 'radarr', name='Radarr', media_path='/movies',
                     local_media_path='/data/media/movies')
        with patch.object(arr, 'normalize_arr_connections', return_value=[conn]), \
             patch.object(arr, '_arr_get', return_value=movies):
            rows, _ = arr.fetch_arr_all_titles_result(CFG)
        self.assertEqual(rows[0]['folder'], '/data/media/movies/Dune (2021)')
        self.assertEqual(rows[0]['studio'], 'Legendary')
        self.assertEqual(rows[0]['poster'], '')


class FolderMatchTests(unittest.TestCase):
    def _find(self, rel, rows, errors=()):
        with patch.object(arr, 'fetch_arr_all_titles_result', return_value=(rows, list(errors))):
            return arr.arr_title_for_folder(CFG, rel)

    def test_the_items_own_folder_wins_whatever_the_name(self):
        row, _ = self._find('television/Pluribus', [_row('/data/media/television/Pluribus')])
        self.assertEqual(row['title'], 'PLUR1BUS')

    def test_the_deepest_folder_above_it(self):
        rows = [_row('/data/media/television', arr_id=1), _row('/data/media/television/Show', arr_id=2)]
        row, _ = self._find('television/Show/Season 1', rows)
        self.assertEqual(row['arr_id'], 2)

    def test_the_only_one_below_it(self):
        row, _ = self._find('movies/A', [_row('/data/media/movies/A/Avatar (2009)', arr_id=4)])
        self.assertEqual(row['arr_id'], 4)

    def test_a_folder_of_several_is_none(self):
        rows = [_row('/data/media/movies/A/Avatar (2009)', arr_id=4), _row('/data/media/movies/A/Alien (1979)', arr_id=5)]
        row, _ = self._find('movies/A', rows)
        self.assertIsNone(row)

    def test_a_sibling_with_a_longer_name_is_not_a_match(self):
        row, _ = self._find('television/Show', [_row('/data/media/television/Show 2')])
        self.assertIsNone(row)

    def test_a_decomposed_name_matches_its_composed_folder(self):
        row, _ = self._find('movies/Amélie (2001)', [_row('/data/media/movies/Amélie (2001)')])
        self.assertIsNotNone(row)

    def test_errors_come_back_with_a_miss(self):
        err = [{'connection_id': 'r', 'name': 'Radarr', 'service': 'radarr', 'partial': False, 'message': 'x'}]
        row, errors = self._find('movies/Nope', [], err)
        self.assertIsNone(row)
        self.assertEqual(errors, err)


class _Resp:
    def __init__(self, data, ctype):
        self._data, self.headers = data, {'Content-Type': ctype}

    def read(self, n=-1):
        return self._data if n < 0 else self._data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class PosterFetchTests(unittest.TestCase):
    def _fetch(self, row, answer):
        asked = []

        def urlopen(req, timeout=None):
            asked.append((req.full_url, req.headers.get('X-api-key')))
            return answer(req.full_url)
        with patch.object(arr, 'normalize_arr_connections', return_value=[_conn(base_url='http://sonarr:8989/sonarr')]), \
             patch.object(arr.urllib.request, 'urlopen', side_effect=urlopen):
            return arr.fetch_arr_poster(CFG, row), asked

    def _missing(self, url):
        raise urllib.error.HTTPError(url, 404, 'Not Found', {}, io.BytesIO())

    def test_the_media_cover_route_first_with_the_key(self):
        got, asked = self._fetch(_row('/x'), lambda u: _Resp(b'JPEG', 'image/jpeg'))
        self.assertEqual(got, (b'JPEG', 'image/jpeg'))
        self.assertEqual(asked, [('http://sonarr:8989/sonarr/api/v3/mediacover/7/poster-250.jpg', 'KEY')])

    def test_then_the_listed_address_on_the_same_host_resized(self):
        def answer(url):
            if '/api/v3/' in url:
                self._missing(url)
            return _Resp(b'JPEG', 'image/jpeg')
        got, asked = self._fetch(_row('/x', poster='/sonarr/MediaCover/7/poster.jpg?lastWrite=1'), answer)
        self.assertEqual(got, (b'JPEG', 'image/jpeg'))
        self.assertEqual(asked[1][0], 'http://sonarr:8989/sonarr/MediaCover/7/poster-250.jpg?lastWrite=1')

    def test_a_listed_address_with_a_host_of_its_own_is_never_asked(self):
        got, asked = self._fetch(_row('/x', poster='http://elsewhere.example/MediaCover/7/poster.jpg'), self._missing)
        self.assertIsNone(got)
        self.assertTrue(all(u.startswith('http://sonarr:8989/') for u, _ in asked))
        self.assertEqual(len(asked), 1)

    def test_a_page_that_is_not_an_image_is_none(self):
        got, _ = self._fetch(_row('/x', poster=''), lambda u: _Resp(b'<html>', 'text/html'))
        self.assertIsNone(got)

    def test_an_oversized_answer_is_none(self):
        got, _ = self._fetch(_row('/x', poster=''), lambda u: _Resp(b'x' * (arr._POSTER_MAX_BYTES + 1), 'image/jpeg'))
        self.assertIsNone(got)


class RouteTests(unittest.TestCase):
    def _get(self, path, rows=(), errors=(), conns=(_conn(),), poster=None):
        with patch.object(app, 'db_load_config', return_value=CFG), \
             patch.object(app, 'normalize_arr_connections', return_value=list(conns)), \
             patch.object(arr, 'fetch_arr_all_titles_result', return_value=(list(rows), list(errors))), \
             patch.object(app, 'fetch_arr_poster', return_value=poster):
            return app.app.test_client().get(path)

    def test_found(self):
        resp = self._get('/api/library_map/title?folder=television/Pluribus', [_row('/data/media/television/Pluribus')])
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((body['found'], body['service'], body['title'], body['network'], body['poster']),
                         (True, 'sonarr', 'PLUR1BUS', 'Apple TV+', True))

    def test_not_there_when_every_instance_answered(self):
        self.assertEqual(self._get('/api/library_map/title?folder=movies/Nope').get_json(), {'found': False})

    def test_could_not_ask_is_not_not_there(self):
        err = [{'connection_id': 'r', 'name': 'Radarr', 'service': 'radarr', 'partial': False, 'message': 'x'}]
        body = self._get('/api/library_map/title?folder=movies/Nope', errors=err).get_json()
        self.assertEqual(body, {'found': None, 'unreachable': ['Radarr']})

    def test_no_arr_configured(self):
        body = self._get('/api/library_map/title?folder=movies/Nope', conns=()).get_json()
        self.assertEqual(body, {'found': False, 'configured': False})

    def test_a_folder_is_required(self):
        self.assertEqual(self._get('/api/library_map/title').status_code, 400)

    def test_the_poster_is_streamed_and_cached_by_the_browser(self):
        resp = self._get('/api/library_map/poster?folder=television/Pluribus',
                         [_row('/data/media/television/Pluribus')], poster=(b'JPEG', 'image/jpeg'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b'JPEG')
        self.assertEqual(resp.mimetype, 'image/jpeg')
        self.assertIn('max-age=86400', resp.headers['Cache-Control'])

    def test_no_poster_is_a_404(self):
        resp = self._get('/api/library_map/poster?folder=movies/Nope')
        self.assertEqual(resp.status_code, 404)


if __name__ == '__main__':
    unittest.main()
