from app.notify import TelegramNotifier


def _notifier(**kw) -> TelegramNotifier:
    n = TelegramNotifier(kw.get("token", "тест"), kw.get("chat", "1"), kw.get("device", ""))
    n.sent = []

    async def fake_send(text):
        n.sent.append(text)

    n._send = fake_send
    return n


def test_disabled_without_token():
    assert TelegramNotifier("", "1").enabled is False
    assert TelegramNotifier("токен", "").enabled is False
    assert TelegramNotifier("токен", "1").enabled is True


async def test_question_waits_for_answer():
    """Два сообщения на реплику превращают переписку в свалку."""
    n = _notifier()
    await n.on_turn("user", "какая погода")
    assert n.sent == [], "вопрос ушёл раньше ответа"

    await n.on_turn("assistant", "Ясно, двадцать градусов.")
    assert len(n.sent) == 1
    assert "какая погода" in n.sent[0] and "двадцать градусов" in n.sent[0]


async def test_unanswered_question_is_not_lost():
    n = _notifier()
    await n.on_turn("user", "первый вопрос")
    await n.on_turn("user", "второй вопрос")
    # Первый остался без ответа, но пропасть не должен.
    assert len(n.sent) == 1 and "первый вопрос" in n.sent[0]
    n._cancel_flush()


async def test_answer_without_question_still_sent():
    """Колонка говорит сама — сработавший таймер или будильник."""
    n = _notifier()
    await n.on_turn("assistant", "Таймер сработал.")
    assert len(n.sent) == 1 and "Таймер" in n.sent[0]


async def test_empty_text_ignored():
    n = _notifier()
    await n.on_turn("user", "   ")
    await n.on_turn("assistant", "")
    assert n.sent == []


async def test_close_flushes_pending_question():
    n = _notifier()
    await n.on_turn("user", "вопрос перед обрывом")
    await n.close()
    assert len(n.sent) == 1 and "вопрос перед обрывом" in n.sent[0]


async def test_nothing_sent_when_disabled():
    n = TelegramNotifier("", "")
    # Не должно падать и не должно ничего слать.
    await n.on_turn("user", "привет")
    await n.close()
