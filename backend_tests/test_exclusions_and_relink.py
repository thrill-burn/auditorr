import unittest
from unittest.mock import patch

from arr import arr_search, fetch_arr_media_index, normalize_arr_connections, test_arr_connections
from audit import _compute_tracker_file_stats, _not_imported_paths, process_health_metrics
from exclusions import compile_exclusions, is_excluded
from media_server_exclusions import expand_exclusion_patterns


class ExclusionRuleTests(unittest.TestCase):
    def test_path_prefix_rules_match_container_paths(self):
        patterns = [
            "**/.plexmatch",
            "torrents/games/**",
            "torrents/lidarr/**",
            "torrents/music/**",
            "torrents/books/**",
            "media/music/**",
            "data/torrents/games/**",
            "data/torrents/lidarr/**",
            "data/torrents/music/**",
            "data/torrents/books/**",
        ]

        self.assertTrue(is_excluded(
            "/data/media/Movies/Example/.plexmatch",
            "Movies/Example/.plexmatch",
            ".plexmatch",
            patterns,
        ))
        self.assertTrue(is_excluded(
            "/data/torrents/games/Game.iso",
            "games/Game.iso",
            "Game.iso",
            patterns,
        ))
        self.assertTrue(is_excluded(
            "/data/torrents/music/Album/Track.flac",
            "music/Album/Track.flac",
            "Track.flac",
            patterns,
        ))
        self.assertTrue(is_excluded(
            "/data/media/music/Album/Track.flac",
            "music/Album/Track.flac",
            "Track.flac",
            patterns,
        ))
        self.assertTrue(is_excluded(
            "/mnt/user/data/torrents/books/Book.epub",
            "books/Book.epub",
            "Book.epub",
            patterns,
        ))
        self.assertTrue(is_excluded(
            "/data/torrents/books/Book.epub",
            "books/Book.epub",
            "Book.epub",
            ["/mnt/user/data/torrents/books"],
        ))

    def test_plain_directory_and_legacy_glob_rules_still_work(self):
        patterns = ["Featurettes", "*.srt", "ext:.nfo"]

        self.assertTrue(is_excluded(
            "/data/media/Movie/Featurettes/Behind The Scenes.mkv",
            "Movie/Featurettes/Behind The Scenes.mkv",
            "Behind The Scenes.mkv",
            patterns,
        ))
        self.assertTrue(is_excluded(
            "/data/media/Movie/Movie.en.srt",
            "Movie/Movie.en.srt",
            "Movie.en.srt",
            patterns,
        ))
        self.assertTrue(is_excluded(
            "/data/torrents/Movie/Movie.nfo",
            "Movie/Movie.nfo",
            "Movie.nfo",
            patterns,
        ))

    def test_excluded_torrent_folders_do_not_count_as_not_imported(self):
        torrent_files = [
            {
                "path": "movies/Movie.mkv",
                "size": 1000,
                "status": "Seeding",
                "imported": False,
                "excluded": False,
                "trackers": ["tracker.example"],
            },
            {
                "path": "books/Book.epub",
                "size": 2000,
                "status": "Seeding",
                "imported": False,
                "excluded": True,
                "trackers": ["tracker.example"],
            },
        ]
        cfg = {"OR_RATIO": 0.01, "NI_RATIO": 0.01, "DUP_RATIO": 0.01}

        with patch("audit.db_load_history", return_value={"hourly_stats": [], "daily_stats": []}):
            dashboard = process_health_metrics([], torrent_files, cfg, update_history=False)
        tracker_stats = _compute_tracker_file_stats(torrent_files)

        self.assertEqual(dashboard["current"]["details"]["not_imported_count"], 1)
        self.assertEqual(dashboard["current"]["details"]["not_imported_size"], 1000)
        self.assertEqual(_not_imported_paths(torrent_files), ["movies/Movie.mkv"])
        self.assertEqual(tracker_stats["tracker.example"]["not_imported_count"], 1)
        self.assertEqual(tracker_stats["tracker.example"]["not_imported_size"], 1000)

    def test_media_server_presets_add_common_sidecar_exclusions(self):
        patterns = expand_exclusion_patterns({
            "EXCLUSION_PATTERNS": [],
            "MEDIA_SERVER_EXCLUSION_PRESETS": ["plex", "jellyfin", "emby", "kodi", "ums"],
        })

        for full_path, rel_path, filename in [
            ("/data/media/TV/Show/.plexmatch", "TV/Show/.plexmatch", ".plexmatch"),
            ("/data/media/Movies/Movie (2024)/movie.nfo", "Movies/Movie (2024)/movie.nfo", "movie.nfo"),
            ("/data/media/Movies/Movie (2024)/poster.jpg", "Movies/Movie (2024)/poster.jpg", "poster.jpg"),
            ("/data/media/Movies/Movie (2024)/fanart-1.png", "Movies/Movie (2024)/fanart-1.png", "fanart-1.png"),
            ("/data/media/Movies/Movie (2024)/Movie (2024)-poster.jpg", "Movies/Movie (2024)/Movie (2024)-poster.jpg", "Movie (2024)-poster.jpg"),
            ("/data/media/Movies/Movie (2024)/Movie (2024)-fanart-1.jpg", "Movies/Movie (2024)/Movie (2024)-fanart-1.jpg", "Movie (2024)-fanart-1.jpg"),
            ("/data/media/TV/Show/Season 01/season.nfo", "TV/Show/Season 01/season.nfo", "season.nfo"),
            ("/data/media/Music/Album/albumart.jpg", "Music/Album/albumart.jpg", "albumart.jpg"),
        ]:
            self.assertTrue(is_excluded(full_path, rel_path, filename, patterns), filename)
        self.assertFalse(is_excluded(
            "/data/media/Movies/Movie (2024)/movie.mkv",
            "Movies/Movie (2024)/movie.mkv",
            "movie.mkv",
            patterns,
        ))


class CompiledExclusionMatcherTests(unittest.TestCase):
    def test_compiled_matcher_agrees_with_is_excluded_across_all_pattern_types(self):
        """compile_exclusions().match() must produce identical results to is_excluded()."""
        custom_patterns = [
            # ext: rules
            "ext:.nfo",
            "ext:srt",
            # name: rules
            "name:@eaDir",
            "name:.@__thumb",
            # contains: rules
            "contains:sample",
            # barewords
            "Featurettes",
            "Extras",
            "BehindTheScenes",
            # subtree glob patterns
            "torrents/games/**",
            "torrents/books/**",
            "data/torrents/music/**",
            "media/music/**",
            # Docker path: plain path → subtree prefix with /data/ variant handling
            "/mnt/user/data/torrents/books",
            # Path glob (** prefix)
            "**/.plexmatch",
            # Legacy filename glob
            "*.srt",
            # Pure ext glob (should route to ext bucket, not fnmatch)
            "*.nfo",
        ]
        cfg = {
            "EXCLUSION_PATTERNS": custom_patterns,
            "MEDIA_SERVER_EXCLUSION_PRESETS": ["plex", "jellyfin", "emby", "kodi", "ums"],
        }
        patterns = expand_exclusion_patterns(cfg)
        compiled = compile_exclusions(patterns)

        # Corpus: (full_path, rel_path, filename)
        corpus = [
            # NFO / sidecar files — should be excluded
            ("/data/media/Movies/Movie (2024)/movie.nfo", "Movies/Movie (2024)/movie.nfo", "movie.nfo"),
            ("/data/media/TV/Show/Season 01/season.nfo", "TV/Show/Season 01/season.nfo", "season.nfo"),
            ("/data/media/TV/Show/tvshow.nfo", "TV/Show/tvshow.nfo", "tvshow.nfo"),
            ("/data/media/Movies/Movie/artist.nfo", "Movies/Movie/artist.nfo", "artist.nfo"),
            ("/data/media/Movies/Movie/Movie.en.srt", "Movies/Movie/Movie.en.srt", "Movie.en.srt"),
            # Artwork exact names — should be excluded
            ("/data/media/Movies/Movie (2024)/poster.jpg", "Movies/Movie (2024)/poster.jpg", "poster.jpg"),
            ("/data/media/Movies/Movie (2024)/folder.jpg", "Movies/Movie (2024)/folder.jpg", "folder.jpg"),
            ("/data/media/Movies/Movie (2024)/cover.jpg", "Movies/Movie (2024)/cover.jpg", "cover.jpg"),
            ("/data/media/Movies/Movie (2024)/background.jpg", "Movies/Movie (2024)/background.jpg", "background.jpg"),
            ("/data/media/Music/Album/albumart.jpg", "Music/Album/albumart.jpg", "albumart.jpg"),
            # Artwork globs (*-name.ext) — should be excluded
            ("/data/media/Movies/Movie (2024)/Movie (2024)-poster.jpg", "Movies/Movie (2024)/Movie (2024)-poster.jpg", "Movie (2024)-poster.jpg"),
            ("/data/media/Movies/Movie (2024)/Movie (2024)-fanart-1.jpg", "Movies/Movie (2024)/Movie (2024)-fanart-1.jpg", "Movie (2024)-fanart-1.jpg"),
            # Artwork globs (name*.ext) — should be excluded
            ("/data/media/Movies/Movie/fanart-1.png", "Movies/Movie/fanart-1.png", "fanart-1.png"),
            ("/data/media/Movies/Movie/backdrop-wide.jpg", "Movies/Movie/backdrop-wide.jpg", "backdrop-wide.jpg"),
            # .plexmatch — path glob **/.plexmatch
            ("/data/media/TV/Show/.plexmatch", "TV/Show/.plexmatch", ".plexmatch"),
            ("/data/media/Movies/Movie (2024)/.plexmatch", "Movies/Movie (2024)/.plexmatch", ".plexmatch"),
            # Segment barewords — should be excluded
            ("/data/media/Movie/Featurettes/Behind The Scenes.mkv", "Movie/Featurettes/Behind The Scenes.mkv", "Behind The Scenes.mkv"),
            ("/data/media/Movie/Extras/Deleted.mkv", "Movie/Extras/Deleted.mkv", "Deleted.mkv"),
            # contains: sample — should be excluded
            ("/data/media/Movies/Movie/sample.mkv", "Movies/Movie/sample.mkv", "sample.mkv"),
            ("/data/torrents/movies/Movie/sample-clip.mp4", "movies/Movie/sample-clip.mp4", "sample-clip.mp4"),
            # name: segments — should be excluded
            ("/data/media/TV/@eaDir/thumb.jpg", "TV/@eaDir/thumb.jpg", "thumb.jpg"),
            # Subtree globs — should be excluded
            ("/data/torrents/games/Game.iso", "games/Game.iso", "Game.iso"),
            ("/data/torrents/books/Book.epub", "books/Book.epub", "Book.epub"),
            # Docker /data/ variant — plain path rule
            ("/mnt/user/data/torrents/books/Book.epub", "books/Book.epub", "Book.epub"),
            # ext: rule (srt)
            ("/data/media/Movies/Movie/Movie.en.srt", "Movies/Movie/Movie.en.srt", "Movie.en.srt"),
            # Media files — must NOT be excluded
            ("/data/media/Movies/Movie (2024)/Movie (2024).mkv", "Movies/Movie (2024)/Movie (2024).mkv", "Movie (2024).mkv"),
            ("/data/media/Movies/Movie (2024)/Movie (2024).mp4", "Movies/Movie (2024)/Movie (2024).mp4", "Movie (2024).mp4"),
            ("/data/media/TV/Show/Season 01/S01E01.mkv", "TV/Show/Season 01/S01E01.mkv", "S01E01.mkv"),
            ("/data/torrents/movies/The.Matrix.1999.1080p.BluRay-GRP/The.Matrix.1999.1080p.BluRay-GRP.mkv",
             "movies/The.Matrix.1999.1080p.BluRay-GRP/The.Matrix.1999.1080p.BluRay-GRP.mkv",
             "The.Matrix.1999.1080p.BluRay-GRP.mkv"),
            ("/data/media/TV/Show/Season 02/S02E03.mp4", "TV/Show/Season 02/S02E03.mp4", "S02E03.mp4"),
            ("/data/torrents/movies/Sci-Fi.Classic.1999.REMUX/Sci-Fi.Classic.1999.REMUX.mkv",
             "movies/Sci-Fi.Classic.1999.REMUX/Sci-Fi.Classic.1999.REMUX.mkv",
             "Sci-Fi.Classic.1999.REMUX.mkv"),
        ]

        for full_path, rel_path, filename in corpus:
            expected = is_excluded(full_path, rel_path, filename, patterns)
            actual = compiled.match(full_path, rel_path, filename)
            self.assertEqual(
                expected, actual,
                f"compiled.match() disagreed with is_excluded() for {filename!r} ({full_path!r}): "
                f"expected {expected}, got {actual}",
            )

        # Media-server presets alone must never exclude .mkv or .mp4 files.
        # Use a preset-only compiled matcher (no user custom patterns) so that
        # bareword rules like "Featurettes" don't confound the assertion.
        preset_only = compile_exclusions(expand_exclusion_patterns({
            "EXCLUSION_PATTERNS": [],
            "MEDIA_SERVER_EXCLUSION_PRESETS": ["plex", "jellyfin", "emby", "kodi", "ums"],
        }))
        for full_path, rel_path, filename in corpus:
            if filename.endswith((".mkv", ".mp4")):
                self.assertFalse(
                    preset_only.match(full_path, rel_path, filename),
                    f"Media-server preset should not exclude {filename!r}",
                )


class ArrConnectionTests(unittest.TestCase):
    def test_legacy_arr_config_normalizes_to_connection_list(self):
        cfg = {
            "SONARR_URL": "http://sonarr.local:8989",
            "SONARR_API_KEY": "sonarr-key",
            "RADARR_URL": "http://radarr.local:7878",
            "RADARR_API_KEY": "radarr-key",
        }

        self.assertEqual(
            [(c["id"], c["service"], c["base_url"]) for c in normalize_arr_connections(cfg)],
            [
                ("sonarr-default", "sonarr", "http://sonarr.local:8989"),
                ("radarr-default", "radarr", "http://radarr.local:7878"),
            ],
        )

    def test_multiple_arr_connections_are_preserved(self):
        cfg = {
            "ARR_CONNECTIONS": [
                {
                    "id": "radarr-main",
                    "service": "radarr",
                    "name": "Main Radarr",
                    "base_url": "http://radarr-main.local:7878/",
                    "api_key": "one",
                },
                {
                    "id": "radarr-4k",
                    "service": "radarr",
                    "name": "4K Radarr",
                    "url": "http://radarr-4k.local:7878/",
                    "api_key": "two",
                },
                {
                    "id": "sonarr-main",
                    "service": "sonarr",
                    "name": "Main Sonarr",
                    "base_url": "http://sonarr-main.local:8989/",
                    "api_key": "three",
                },
            ],
        }

        self.assertEqual(
            [(c["id"], c["service"], c["base_url"]) for c in normalize_arr_connections(cfg)],
            [
                ("radarr-main", "radarr", "http://radarr-main.local:7878"),
                ("radarr-4k", "radarr", "http://radarr-4k.local:7878"),
                ("sonarr-main", "sonarr", "http://sonarr-main.local:8989"),
            ],
        )

    def test_arr_search_uses_matching_multi_instance(self):
        cfg = {
            "ARR_CONNECTIONS": [
                {
                    "id": "radarr-main",
                    "service": "radarr",
                    "name": "Main Radarr",
                    "base_url": "http://radarr-main.local:7878",
                    "api_key": "one",
                },
                {
                    "id": "radarr-4k",
                    "service": "radarr",
                    "name": "4K Radarr",
                    "base_url": "http://radarr-4k.local:7878",
                    "api_key": "two",
                },
            ],
        }

        def fake_get(base_url, _api_key, _path):
            if "radarr-main" in base_url:
                return [{"title": "Wrong Movie", "cleanTitle": "wrongmovie", "titleSlug": "wrong-movie"}]
            return [{"title": "The Matrix", "cleanTitle": "thematrix", "titleSlug": "the-matrix"}]

        with patch("arr._arr_get", side_effect=fake_get):
            result = arr_search(cfg, "radarr", "The.Matrix.1999.1080p.mkv")

        self.assertEqual(result["connection_id"], "radarr-4k")
        self.assertEqual(result["url"], "http://radarr-4k.local:7878/movie/the-matrix")

    def test_arr_connection_test_reports_api_status_and_managed_paths(self):
        cfg = {
            "ARR_CONNECTIONS": [
                {
                    "id": "radarr-main",
                    "service": "radarr",
                    "name": "Main Radarr",
                    "base_url": "http://radarr-main.local:7878",
                    "api_key": "one",
                },
                {
                    "id": "sonarr-main",
                    "service": "sonarr",
                    "name": "Main Sonarr",
                    "base_url": "http://sonarr-main.local:8989",
                    "api_key": "two",
                },
            ],
        }

        def fake_get(base_url, _api_key, path):
            if "radarr-main" in base_url and path == "/api/v3/movie":
                return [{
                    "id": 10,
                    "title": "The Matrix",
                    "year": 1999,
                    "movieFile": {
                        "id": 11,
                        "path": "/data/media/Movies/Sci-Fi Classic (1999)/Feature.mkv",
                    },
                }]
            if "sonarr-main" in base_url and path == "/api/v3/series":
                return [{"id": 20, "title": "Example Show", "year": 2024}]
            if "sonarr-main" in base_url and path == "/api/v3/episodefile?seriesId=20":
                return [{
                    "id": 21,
                    "path": "/data/media/TV/Example Show/Season 01/Episode One.mkv",
                    "episodeIds": [100],
                }]
            return []

        with patch("arr._test_arr_connection", return_value=(True, None)), \
                patch("arr._arr_get", side_effect=fake_get):
            result = test_arr_connections(cfg)

        self.assertTrue(result["ok"])
        self.assertEqual(result["connection_count"], 2)
        self.assertEqual(
            [(c["id"], c["ok"], c["managed_file_count"]) for c in result["connections"]],
            [("radarr-main", True, 1), ("sonarr-main", True, 1)],
        )
        self.assertEqual(result["connections"][0]["sample_paths"], [
            "/data/media/Movies/Sci-Fi Classic (1999)/Feature.mkv"
        ])

    def test_arr_media_index_preserves_connection_ids_and_maps_instance_paths(self):
        cfg = {
            "ARR_CONNECTIONS": [
                {
                    "id": "radarr-main",
                    "service": "radarr",
                    "base_url": "http://radarr-main.local:7878",
                    "api_key": "one",
                    "media_path": "/movies",
                    "local_media_path": "/data/media/movies",
                },
                {
                    "id": "radarr-4k",
                    "service": "radarr",
                    "base_url": "http://radarr-4k.local:7878",
                    "api_key": "two",
                    "media_path": "/movies-4k",
                    "local_media_path": "/data/media/movies-4k",
                },
                {
                    "id": "sonarr-main",
                    "service": "sonarr",
                    "base_url": "http://sonarr-main.local:8989",
                    "api_key": "three",
                    "media_path": "/tv",
                    "local_media_path": "/data/media/tv",
                },
                {
                    "id": "sonarr-anime",
                    "service": "sonarr",
                    "base_url": "http://sonarr-anime.local:8989",
                    "api_key": "four",
                    "media_path": "/anime",
                    "local_media_path": "/data/media/anime",
                },
            ],
        }

        def fake_get(base_url, _api_key, path):
            if path == "/api/v3/movie":
                title = "The Matrix" if "radarr-main" in base_url else "The Matrix 4K"
                root = "/movies" if "radarr-main" in base_url else "/movies-4k"
                return [{
                    "id": 1,
                    "title": title,
                    "year": 1999,
                    "movieFile": {"id": 2, "path": f"{root}/{title}/Feature.mkv"},
                }]
            if path == "/api/v3/series":
                if "sonarr-main" in base_url:
                    return [{"id": 10, "title": "Example Show", "year": 2024}]
                return [{"id": 20, "title": "Example Anime", "year": 2024}]
            if path == "/api/v3/episodefile?seriesId=10":
                return [{"id": 11, "path": "/tv/Example Show/S01E01.mkv"}]
            if path == "/api/v3/episodefile?seriesId=20":
                return [{"id": 21, "path": "/anime/Example Anime/S01E01.mkv"}]
            return []

        with patch("arr._arr_get", side_effect=fake_get):
            media = fetch_arr_media_index(cfg)

        by_id = {item["connection_id"]: item for item in media}
        self.assertEqual(set(by_id), {"radarr-main", "radarr-4k", "sonarr-main", "sonarr-anime"})
        self.assertEqual(by_id["radarr-main"]["path"], "/data/media/movies/The Matrix/Feature.mkv")
        self.assertEqual(by_id["radarr-4k"]["path"], "/data/media/movies-4k/The Matrix 4K/Feature.mkv")
        self.assertEqual(by_id["sonarr-main"]["path"], "/data/media/tv/Example Show/S01E01.mkv")
        self.assertEqual(by_id["sonarr-anime"]["path"], "/data/media/anime/Example Anime/S01E01.mkv")
        self.assertEqual(by_id["sonarr-anime"]["arr_path"], "/anime/Example Anime/S01E01.mkv")


if __name__ == "__main__":
    unittest.main()
