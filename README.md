# Free IPTV playlist (UA + світ, без RU)

Автооновлюваний плейлист: кожні 6 год GitHub Actions збирає канали з відкритих джерел,
відсіює російські, перевіряє, що стрім реально віддає відео-сегменти, і комітить результат.

## Посилання для плеєра
Заміни `USER/REPO`:

| Що | URL |
|---|---|
| **Усе** (зверху UA + озвучка, нижче 🌍 зарубіжне) | `https://raw.githubusercontent.com/USER/REPO/main/playlist.m3u` |
| **Тільки UA + UA/RU озвучка** | `https://raw.githubusercontent.com/USER/REPO/main/playlist_ua.m3u` |
| Фільми UA / RU озвучка / зарубіжні | `.../playlists/movies_ua.m3u`, `movies_ru.m3u`, `movies_foreign.m3u` |
| Серіали UA / RU озвучка / зарубіжні | `.../playlists/series_ua.m3u`, `series_ru.m3u`, `series_foreign.m3u` |
| UA нестабільні (кіно/серіали) | `.../playlists/ua_unstable_movies.m3u`, `ua_unstable_series.m3u` |
| FAST фільми / серіали | `.../playlists/fast_movies.m3u`, `fast_series.m3u` |

Статистика останньої збірки — `report.md`.

## Запуск
1. Створи **публічний** репозиторій (для публічних Actions безкоштовні), залий ці файли.
2. Settings → Actions → General → Workflow permissions → **Read and write**.
3. Actions → `build-playlist` → **Run workflow** (перший прогін ~10–30 хв).
4. Далі сам за cron. Зміни в `config.yaml` / `build.py` запускають збірку автоматично.

Локально: `pip install -r requirements.txt && python build.py --no-check` (швидкий тест фільтрів).

## Як це працює
1. **Джерела:** iptv-org (API: канали, фіди з мовами, лого, blocklist) + Free-TV/IPTV + FAST (Plex, Samsung TV Plus, Pluto, Tubi, Roku, Rakuten UK, LG Channels, Xumo, DistroTV; лише кіно/серіали/дитячі/док, окремі групи `🌎 FAST ...`).
2. **Фільтри:** країни RU/BY, URL на російських CDN, назви за regex, nsfw, закриті, гео-блок, YouTube, DASH/DRM.
   Російська мова дозволена тільки у фільмах/серіалах/дитячих/документальних (озвучка) і для каналів UA.
3. **Дедуп:** один канал → до 3 кандидатів-стрімів (пріоритет джерела, 24/7, якість).
4. **Health-check:** плейлист → варіант → реальний сегмент; DRM відсіюється.
   Канал, який колись працював, живе ще `grace_fails` прогонів (щоб список не "мигав").
5. **Групи:** блок UA (🇺🇦 Україна, 🎬/📺/🧸/🎥 `| UA` і `| RU озвучка`, нестабільні UA) іде першим; нижче 🌍 зарубіжне (`| EN`, `| Інші мови`, новини, спорт…) і 🌎 FAST.
6. **Запобіжник:** якщо живих < `min_alive`, старий плейлист не перезаписується.

## Тюнінг
- Щось російське пролізло → `filters.id_blocklist` (tvg-id з плейлиста) або `name_block_patterns`.
- Більше/менше країн → `selection.countries`.
- Свої групи/порядок → `groups`.
- Новий m3u-джерело → `m3u_sources` (tvg-id автоматично матчиться з базою iptv-org).

## Обмеження
- Раннер GitHub у США/ЄС: гео-блок для України він не бачить (і навпаки).
- Українських каналів у відкритому доступі обмежено; фільми — лінійні кіноканали, не VOD-бібліотека.
