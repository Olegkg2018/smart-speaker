"""Индекс домашней фонотеки по настроению, жанру и поводу.

Поиск по имени файла (`_search_local` в `player.py`) понимает только то, что
написано в названии — «Queen Don't Stop Me Now», но не «весёлую музыку» и не
«шум дождя», если файл называется иначе. Разбирать сам звук на плате нечем
(об этом уже есть заметка про Whisper: RTF 1.78 даже для распознавания речи),
а вот классифицировать по названию и тегам ID3 — по силам текстовой модели,
и это разовая пакетная работа, а не разговор в реальном времени: копейки за
всю фонотеку сразу.

Индекс лежит вне контейнера — в `data/`, рядом со списками и памятью, —
чтобы его было видно и можно было стереть руками, если он ошибся.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_AUDIO_SUFFIXES = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac"}

# Сколько треков classифицировать одним запросом к модели: партия держит
# запрос компактным, а таких партий за ночь обычно одна-две даже для большой
# библиотеки.
_BATCH_SIZE = 60

_INDEX_FILENAME = "index.json"


@dataclass
class TrackTags:
    title: str
    artist: str
    # Жанр, настроение и повод одной строкой через пробел, по-русски, в
    # нижнем регистре — так же ищем, как и по имени файла: совпадением слов.
    tags: str
    mtime: float


def index_file_path(index_dir: Path) -> Path:
    return index_dir / _INDEX_FILENAME


def _scan_files(music_dir: Path) -> list[Path]:
    if not music_dir.is_dir():
        return []
    return [p for p in music_dir.rglob("*") if p.suffix.lower() in _AUDIO_SUFFIXES]


def _read_id3(path: Path) -> tuple[str, str, str]:
    """Название, исполнитель, жанр из тегов — по силам, а не выдумано.

    Повреждённый или отсутствующий тег — обычное дело в домашней фонотеке,
    падать из-за этого нельзя: остаётся то, что можно взять из имени файла.
    """
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        return "", "", ""
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        return "", "", ""
    if audio is None or audio.tags is None:
        return "", "", ""
    tags = audio.tags
    title = (tags.get("title", [""]) or [""])[0]
    artist = (tags.get("artist", [""]) or [""])[0]
    genre = (tags.get("genre", [""]) or [""])[0]
    return title, artist, genre


def _load(index_path: Path) -> dict[str, TrackTags]:
    if not index_path.exists():
        return {}
    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("не удалось прочитать индекс фонотеки: %s", exc)
        return {}
    return {relpath: TrackTags(**item) for relpath, item in raw.get("tracks", {}).items()}


def _save(index_path: Path, tracks: dict[str, TrackTags]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"tracks": {relpath: asdict(t) for relpath, t in tracks.items()}}
    index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def _classify_batch(
    api_key: str, model: str, entries: list[tuple[str, str, str]]
) -> dict[int, str]:
    """Один запрос к модели на партию треков. entries: (файл, исполнитель, жанр)."""
    listed = "\n".join(
        f"{i}. файл «{name}»" + (f", исполнитель «{artist}»" if artist else "")
        + (f", жанр по тегу «{genre}»" if genre else "")
        for i, (name, artist, genre) in enumerate(entries)
    )
    prompt = (
        "Ниже список музыкальных файлов домашней колонки. Для каждого номера "
        "определи короткий набор тегов на русском, через пробел, в нижнем "
        "регистре: жанр (диско, рок, поп, реп и т.п.), настроение (весёлая, "
        "грустная, спокойная, энергичная и т.п.) и повод, если он очевиден "
        "(для вечеринки, для спорта, для сна, колыбельная, шум дождя, шум "
        "природы, будильник). Если это не песня, а фоновый звук вроде шума "
        "дождя или белого шума — обязательно укажи это тегом. Не выдумывай "
        "то, что не понять по названию: тег может быть и один.\n\n"
        f"{listed}\n\n"
        'Ответь только JSON-массивом объектов {"i": номер, "tags": "теги"}, '
        "без пояснений."
    )
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
    response.raise_for_status()
    text = response.json()["choices"][0]["message"]["content"]
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        log.warning("модель вернула не-JSON при разметке фонотеки")
        return {}
    result: dict[int, str] = {}
    for item in parsed:
        try:
            result[int(item["i"])] = str(item.get("tags", "")).strip().lower()
        except (KeyError, TypeError, ValueError):
            continue
    return result


async def refresh(music_dir: Path, index_dir: Path, api_key: str, model: str) -> int:
    """Обновляет индекс: классифицирует только новые или изменённые файлы.

    Возвращает число размеченных треков — 0 без единого запроса к модели,
    если фонотека не менялась со вчерашней ночи.
    """
    index_path = index_file_path(index_dir)
    existing = _load(index_path)
    files = _scan_files(music_dir)
    seen: set[str] = set()
    # relpath, title, artist, genre, mtime
    to_classify: list[tuple[str, str, str, str, float]] = []

    for path in files:
        relpath = str(path.relative_to(music_dir))
        seen.add(relpath)
        mtime = path.stat().st_mtime
        prior = existing.get(relpath)
        if prior is not None and prior.mtime == mtime:
            continue
        id3_title, artist, genre = _read_id3(path)
        title = id3_title or path.stem
        to_classify.append((relpath, title, artist, genre, mtime))

    # Удалённые с диска файлы не должны засорять поиск.
    for relpath in list(existing):
        if relpath not in seen:
            del existing[relpath]

    if not to_classify:
        _save(index_path, existing)
        return 0

    if not api_key:
        log.warning(
            "в фонотеке %d новых треков, но OPENAI_API_KEY не задан — "
            "разметка по настроению и жанру недоступна",
            len(to_classify),
        )
        _save(index_path, existing)
        return 0

    classified = 0
    for start in range(0, len(to_classify), _BATCH_SIZE):
        batch = to_classify[start : start + _BATCH_SIZE]
        try:
            tags_by_index = await _classify_batch(
                api_key, model, [(t, a, g) for _, t, a, g, _mtime in batch]
            )
        except Exception:
            log.exception("не удалось разметить партию треков (%d шт.)", len(batch))
            continue
        for i, (relpath, title, artist, _genre, mtime) in enumerate(batch):
            tags = tags_by_index.get(i, "")
            existing[relpath] = TrackTags(title=title, artist=artist, tags=tags, mtime=mtime)
            classified += 1

    _save(index_path, existing)
    log.info("фонотека размечена: %d новых/изменённых треков", classified)
    return classified


def search(music_dir: Path, index_dir: Path, query: str) -> tuple[str, str] | None:
    """Ищет трек по тегам настроения/жанра/повода — когда имя файла не совпало."""
    tracks = _load(index_file_path(index_dir))
    if not tracks:
        return None

    # Теги пишет модель в одной грамматической форме («весёлая»), а просят
    # обычно в другой («весёлую музыку») — точное совпадение слов такое не
    # ловит. Сравниваем по первым буквам: для русских слов этого достаточно,
    # чтобы пережить падеж, не путая при этом разные по смыслу слова.
    #
    # На четырёх буквах поймали реальную путаницу: запрос «Adele» находил
    # совсем другой трек только потому, что среди фитов в его метаданных
    # был другой артист — «Adela Jens». «adel» — общий стем для «Adele» и
    # «Adela», хотя это разные имена. Пять букв всё ещё переживают русские
    # падежные окончания («весёлая»/«весёлую» совпадают и на пяти), но уже
    # различают эту пару.
    def _stem(word: str) -> str:
        return word[:5]

    words = [w for w in query.lower().split() if len(w) > 2]
    if not words:
        return None
    query_stems = {_stem(w) for w in words}
    need = 1 if len(words) == 1 else max(1, len(words) // 2)

    best: tuple[int, str, str] | None = None  # score, relpath, title
    for relpath, t in tracks.items():
        haystack_words = f"{t.title} {t.artist} {t.tags}".lower().split()
        haystack_stems = {_stem(w) for w in haystack_words if len(w) > 2}
        score = len(query_stems & haystack_stems)
        if score >= need and (best is None or score > best[0]):
            best = (score, relpath, t.title or relpath)

    if best is None:
        return None
    _, relpath, title = best
    full_path = music_dir / relpath
    if not full_path.exists():
        return None
    return str(full_path), title
