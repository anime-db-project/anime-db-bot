"""Source-native URL templates for all namespaces in the mapping database."""
from __future__ import annotations

from string import Formatter
from urllib.parse import quote, urlsplit

DEFAULT_MAPPING_LINKS = {
    'allcinema': 'https://www.allcinema.net/cinema/{id}',
    'amazon_prime_video_asin': 'https://www.amazon.com/dp/{id}',
    'anidb': 'https://anidb.net/anime/{id}',
    'anikore': 'https://www.anikore.jp/anime/{id}/',
    'anilist': 'https://anilist.co/anime/{id}',
    'animecharacters': 'https://www.animecharactersdatabase.com/source.php?id={id}',
    'animeclick': 'https://www.animeclick.it/anime/{id}',
    'animecountdown': 'https://animecountdown.com/{id}',
    'animefillerlist': 'https://www.animefillerlist.com/shows/{id}',
    'animemorial': '',
    'animenfo': '',
    'animeplanet': 'https://www.anime-planet.com/anime/{id}',
    'animetaizen': '',
    'animethemes': 'https://api.animethemes.moe/anime?filter[id]={id}',
    'anisearch': 'https://www.anisearch.com/anime/{id}',
    'anison': '',
    'ann': 'https://www.animenewsnetwork.com/encyclopedia/anime.php?id={id}',
    'annict': 'https://annict.com/works/{id}',
    'bahamut': '',
    'bangumi': 'https://bgm.tv/subject/{id}',
    'bilibili': 'https://www.bilibili.com/{path}',
    'crunchyroll': 'https://www.crunchyroll.com/series/{id}',
    'disneyplus_movie': '',
    'disneyplus_series': '',
    'douban': 'https://movie.douban.com/subject/{id}/',
    'funimation': '',
    'hidive': '',
    'hikka': 'https://hikka.io/anime/{id}',
    'hulu_movie': '',
    'hulu_series': '',
    'imdb': 'https://www.imdb.com/title/{id}/',
    'kaize': 'https://kaize.io/anime/{id}',
    'kaize_id': '',
    'kitsu': 'https://kitsu.app/anime/{id}',
    'lain': 'https://lain.gr.jp/{path}',
    'letterboxd_lid': 'https://boxd.it/{id}',
    'letterboxd_slug': 'https://letterboxd.com/film/{id}/',
    'letterboxd_uid': '',
    'livechart': 'https://www.livechart.me/anime/{id}',
    'mal': 'https://myanimelist.net/anime/{id}',
    'marumeganel': '',
    'mediaarts': '',
    'nanime': '',
    'nautiljon': 'https://www.nautiljon.com/animes/{path}.html',
    'nautiljon_id': '',
    'netflix': 'https://www.netflix.com/title/{id}',
    'niconico': 'https://ch.nicovideo.jp/{id}',
    'notify': 'https://notify.moe/anime/{id}',
    'otakotaku': 'https://otakotaku.com/anime/view/{id}',
    'primevideo_title': 'https://www.primevideo.com/detail/{id}',
    'shikimori': 'https://shikimori.one/animes/{id}',
    'shoboi': 'https://cal.syoboi.jp/tid/{id}',
    'silveryasha': 'https://db.silveryasha.id/anime/{id}',
    'simkl': 'https://simkl.com/anime/{id}/',
    'tmdb_movie': 'https://www.themoviedb.org/movie/{id}',
    'tmdb_tv': 'https://www.themoviedb.org/tv/{id}',
    'trakt': '',
    'tv_animation_museum': '',
    'tvdb': 'https://thetvdb.com/?tab=series&id={id}',
    'tvdb_movie': 'https://thetvdb.com/?tab=movie&id={id}',
    'wikidata': 'https://www.wikidata.org/wiki/{id}',
}


def mapping_link_templates(values):
    result = dict(DEFAULT_MAPPING_LINKS)
    prefix = "TELEGRAM_ID_URL_"
    for key, value in values.items():
        if key.startswith(prefix):
            name = key.removeprefix(prefix).lower()
            template = str(value or "").strip()
            if template:
                validate_mapping_link(template)
            result[name] = template
    return result


def validate_mapping_link(template):
    parts = list(Formatter().parse(template))
    fields = [field for _, field, _, _ in parts if field is not None]
    if not fields or any(field not in {"id", "path"} or spec or conversion for _, field, spec, conversion in parts if field is not None):
        raise ValueError("Mapping URL must contain {id} or {path} only")
    if "{" in urlsplit(template).netloc:
        raise ValueError("Mapping URL host must be fixed")
    parsed = urlsplit(template.replace("{id}", "123").replace("{path}", "123"))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or "{" in parsed.netloc:
        raise ValueError("Mapping URL must use a fixed HTTPS host")


def mapping_id_url(provider, value, templates):
    key = provider.lower().replace(" ", "_")
    if key == "amdb" or not value:
        return None
    template = templates.get(key)
    if not template:
        return None
    validate_mapping_link(template)
    if "{path}" in template and any(part in {".", ".."} for part in value.split("/")):
        return None
    return template.replace("{id}", quote(value, safe="")).replace("{path}", quote(value, safe="/+"))
