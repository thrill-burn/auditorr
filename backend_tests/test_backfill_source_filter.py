"""Backfill's Source chips filter on the quality name, not the raw arr enum.

`source` on a release row is a serialized C# enum, and Sonarr and Radarr do not
use the same one. Filtering against it directly meant three of the five chips
silently returned nothing:

    chip      Radarr `source`   Sonarr `source`
    remux     (from quality_name)  (from quality_name)
    bluray    bluray            bluray
    webdl     webdl             web            <- broken on Sonarr
    webrip    webrip            webRip         <- broken on Sonarr
    hdtv      tv                television     <- broken on both

The failure was silent — the candidate came back `not_found`, which is
indistinguishable from "no release exists" — so a user narrowing to WEB-DL on a
TV library got a page of dashes and concluded their indexers were down.

`quality_name` is the display string both services agree on, and
`arr.parse_quality_name` already maps it onto the chips' own vocabulary. This is
the only thing that ever read the raw enum.
"""
import unittest

import app


def _rel(quality_name, source, title='Rel', size=1):
    """A release row as `fetch_release_matrix` builds it."""
    return {'title': title, 'size': size, 'indexer': 'ix', 'quality_name': quality_name,
            'source': source, 'resolution': 1080, 'hdr': '',
            'custom_format_score': 0, 'quality_weight': 0, 'seeders': 1}


def _sources(rows, chips):
    out = app._apply_release_filters(rows, [], [], source_filter=chips)
    return sorted(r['quality_name'] for r in out)


class SourceChipTests(unittest.TestCase):

    def test_webdl_matches_both_services(self):
        rows = [_rel('WEBDL-1080p', 'webdl', title='radarr-spelling'),
                _rel('WEBDL-1080p', 'web',   title='sonarr-spelling', size=2)]
        self.assertEqual(len(app._apply_release_filters(
            rows, [], [], source_filter=['webdl'])), 2)

    def test_webrip_matches_sonarrs_camelcase(self):
        rows = [_rel('WEBRip-1080p', 'webrip', title='radarr-spelling'),
                _rel('WEBRip-1080p', 'webRip', title='sonarr-spelling', size=2)]
        self.assertEqual(len(app._apply_release_filters(
            rows, [], [], source_filter=['webrip'])), 2)

    def test_hdtv_matches_at_all(self):
        """Neither service spelled this 'hdtv', so the chip returned nothing on
        both — the one chip that was broken everywhere."""
        rows = [_rel('HDTV-720p', 'tv',         title='radarr-spelling'),
                _rel('HDTV-1080p', 'television', title='sonarr-spelling', size=2)]
        self.assertEqual(len(app._apply_release_filters(
            rows, [], [], source_filter=['hdtv'])), 2)

    def test_remux_and_bluray_stay_independent(self):
        """The deleted special case, still holding: Sonarr reports source
        'bluray' for a Remux too, so the two chips have to be told apart by the
        quality name. parse_quality_name matches 'remux' ahead of 'bluray',
        which is what makes the special case redundant rather than missing."""
        rows = [_rel('Bluray-1080p',       'bluray'),
                _rel('Bluray-1080p Remux', 'bluray', size=2),
                _rel('Remux-2160p',        'bluray', size=3)]
        self.assertEqual(_sources(rows, ['bluray']), ['Bluray-1080p'])
        self.assertEqual(_sources(rows, ['remux']),
                         ['Bluray-1080p Remux', 'Remux-2160p'])

    def test_chips_still_exclude_what_they_should(self):
        rows = [_rel('WEBDL-1080p', 'web'),
                _rel('Bluray-1080p', 'bluray', size=2),
                _rel('HDTV-720p', 'television', size=3)]
        self.assertEqual(_sources(rows, ['webdl']), ['WEBDL-1080p'])
        self.assertEqual(_sources(rows, ['bluray', 'hdtv']),
                         ['Bluray-1080p', 'HDTV-720p'])

    def test_no_filter_keeps_everything(self):
        rows = [_rel('WEBDL-1080p', 'web'), _rel('Bluray-1080p', 'bluray', size=2)]
        self.assertEqual(len(app._apply_release_filters(rows, [], [])), 2)

    def test_an_unparseable_quality_name_is_filtered_out_not_crashed_on(self):
        rows = [_rel('Unknown', ''), _rel('', None, size=2)]
        self.assertEqual(app._apply_release_filters(
            rows, [], [], source_filter=['webdl']), [])


if __name__ == '__main__':
    unittest.main()
