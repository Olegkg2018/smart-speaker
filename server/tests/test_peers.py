"""Несколько устройств в одной сессии: слушает тот, кто слышит громче.

Колонка на кухне и телефон в комнате — два микрофона одного ассистента,
а не два ассистента. Здесь проверяется выбор источника: главное, чтобы
он не менялся посреди реплики, иначе распознавание получит склейку из
двух комнат и потеряет половину слов.
"""

import numpy as np

from app.peers import ROLE_SATELLITE, ROLE_SPEAKER, Peer, PeerSet


def _pcm(level: int, samples: int = 320) -> bytes:
    """Кадр заданной громкости."""
    return np.full(samples, level, dtype=np.int16).tobytes()


def _peer(name: str, role: str = ROLE_SPEAKER, screen: bool = True) -> Peer:
    return Peer(ws=None, device=name, role=role, has_screen=screen)


def test_single_device_is_always_the_source():
    peers = PeerSet()
    kitchen = _peer("kitchen")
    peers.add(kitchen)
    assert peers.choose_active() is kitchen


def test_louder_device_wins():
    peers = PeerSet()
    kitchen, phone = _peer("kitchen"), _peer("phone", ROLE_SATELLITE)
    peers.add(kitchen)
    peers.add(phone)

    kitchen.note_audio(_pcm(200))
    phone.note_audio(_pcm(3000))  # человек стоит у телефона

    assert peers.choose_active() is phone


def test_source_does_not_switch_mid_utterance():
    """Главное правило: выбор делается один раз, на начало реплики."""
    peers = PeerSet()
    kitchen, phone = _peer("kitchen"), _peer("phone", ROLE_SATELLITE)
    peers.add(kitchen)
    peers.add(phone)

    kitchen.note_audio(_pcm(3000))
    active = peers.choose_active()
    assert active is kitchen

    # Посреди реплики второй микрофон стал громче — источник не меняется.
    phone.note_audio(_pcm(9000))
    assert peers.active is kitchen
    assert peers.accepts_audio(kitchen)
    assert not peers.accepts_audio(phone), "звук другого микрофона брать нельзя"


def test_next_utterance_picks_again():
    peers = PeerSet()
    kitchen, phone = _peer("kitchen"), _peer("phone", ROLE_SATELLITE)
    peers.add(kitchen)
    peers.add(phone)

    kitchen.note_audio(_pcm(3000))
    assert peers.choose_active() is kitchen

    peers.release_active()  # реплика кончилась
    phone.note_audio(_pcm(9000))
    assert peers.choose_active() is phone, "новая реплика — новый выбор"


def test_nobody_is_heard_between_utterances():
    """Пока сервер не слушает, звук не берётся ни у кого."""
    peers = PeerSet()
    kitchen = _peer("kitchen")
    peers.add(kitchen)
    assert not peers.accepts_audio(kitchen)


def test_level_decays_so_a_quiet_room_does_not_win_forever():
    import app.peers as peers_mod

    peers = PeerSet()
    kitchen, phone = _peer("kitchen"), _peer("phone", ROLE_SATELLITE)
    peers.add(kitchen)
    peers.add(phone)

    original = peers_mod._LEVEL_DECAY_S
    peers_mod._LEVEL_DECAY_S = 0.01
    try:
        kitchen.note_audio(_pcm(9000))
        import time

        time.sleep(0.05)  # громкость успевает «остыть»
        phone.note_audio(_pcm(500))
        assert peers.choose_active() is phone
    finally:
        peers_mod._LEVEL_DECAY_S = original


def test_output_goes_only_where_there_is_a_speaker():
    peers = PeerSet()
    kitchen = _peer("kitchen", ROLE_SPEAKER)
    phone = _peer("phone", ROLE_SATELLITE, screen=True)
    peers.add(kitchen)
    peers.add(phone)

    assert peers.speakers() == [kitchen], "сателлит не должен ничего озвучивать"
    assert set(peers.screens()) == {kitchen, phone}, "экран есть у обоих"


def test_removing_active_device_frees_the_source():
    peers = PeerSet()
    kitchen, phone = _peer("kitchen"), _peer("phone", ROLE_SATELLITE)
    peers.add(kitchen)
    peers.add(phone)
    phone.note_audio(_pcm(5000))
    assert peers.choose_active() is phone

    peers.remove(phone)  # телефон унесли из комнаты
    assert peers.active is None
    assert peers.choose_active() is kitchen


def test_room_is_empty_only_when_all_left():
    peers = PeerSet()
    a, b = _peer("a"), _peer("b")
    peers.add(a)
    peers.add(b)
    peers.remove(a)
    assert not peers.empty
    peers.remove(b)
    assert peers.empty
